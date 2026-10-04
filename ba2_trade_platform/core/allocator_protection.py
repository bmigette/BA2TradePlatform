"""Pure helpers of the allocator's per-symbol TP/SL protection (no IO, no DB, no broker).

Everything that DECIDES something about a protection lives here so it can be tested without
a broker: validation, tick rounding, slicing the position across take-profit targets,
classifying what the broker says about one placed OCO, and deriving the status the page shows.
The IO is ``allocator_protection_service`` (DB + orchestration) and the broker calls are on
``TastyTradeAccount``. Design: ``docs/plans/2026-10-04-allocator-tp-sl-design.md``.

LIVE-ONLY, in-tree: nothing under ``packages/`` may import this (keeps the feature GA-neutral).
"""
from dataclasses import dataclass, field
from datetime import date as Date, datetime as DateTime
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from math import floor
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .allocator_protection_models import (
    SLICE_ALARM_STATES, SLICE_CANCELLED_BY_US, SLICE_CANCELLING, SLICE_FILLED_SL,
    SLICE_FILLED_TP, SLICE_LIVE, SLICE_LOST_CANCELLED, SLICE_LOST_EXPIRED,
    SLICE_LOST_REJECTED, SLICE_PLACING, SLICE_RESTING_STATES, SLICE_UNKNOWN,
)

#: The GTC lifetime ASSUMED when the broker returns no ``gtc_date``. NOT verified against
#: TastyTrade documentation (nothing local states it); it only ever produces an early WARNING.
ASSUMED_GTC_LIFETIME_DAYS = 90
#: Days before the stored GTC end date at which the expiry warning fires.
GTC_EXPIRY_WARN_DAYS = 7
#: A PLACING row older than this is an alarm: the broker call never reported back.
PLACING_STALE_SECONDS = 60
#: A ``pending_replace`` older than this is an alarm: the re-placement is stuck.
PENDING_REPLACE_STALE_SECONDS = 15 * 60

_FRACTION_TOLERANCE = 1e-6

# --- statuses shown on the page ----------------------------------------------------------
STATUS_OFF = "OFF"
STATUS_NO_POSITION = "NO_POSITION"
STATUS_PROTECTED = "PROTECTED"
STATUS_PARTIAL = "PARTIAL"
STATUS_UNPROTECTED = "UNPROTECTED"
STATUS_REPLACING = "REPLACING"
STATUS_HELD = "HELD"

#: Alarm / alert codes (persisted on the protection row and written to the activity log).
CODE_PLACEMENT_REFUSED = "PLACEMENT_REFUSED"
CODE_CANCEL_UNCONFIRMED = "CANCEL_UNCONFIRMED"
CODE_LOST_EXPIRED = "LOST_EXPIRED"
CODE_LOST_CANCELLED = "LOST_CANCELLED"
CODE_LOST_REJECTED = "LOST_REJECTED"
CODE_UNKNOWN_STATE = "UNKNOWN_STATE"
CODE_REPLACE_FAILED = "REPLACE_FAILED"
CODE_QUANTITY_MISMATCH = "QUANTITY_MISMATCH"
CODE_RECONCILE_FETCH_FAILED = "RECONCILE_FETCH_FAILED"
CODE_GTC_EXPIRING = "GTC_EXPIRING"
CODE_HELD_FILL = "HELD_FILL"


# =========================================================================================
# targets, validation, rounding, slicing
# =========================================================================================

@dataclass(frozen=True)
class TpTarget:
    """One take-profit target: a limit price and the share of the position it covers."""
    price: float
    fraction: float

    def to_dict(self) -> Dict[str, float]:
        return {"price": float(self.price), "fraction": float(self.fraction)}


@dataclass(frozen=True)
class SlicePlan:
    """One OCO to place: ``quantity`` whole shares, take-profit at ``tp_price``, stop at ``sl_price``."""
    slice_index: int
    target_index: int
    quantity: int
    tp_price: float
    sl_price: float


def targets_from_dicts(raw: Optional[Iterable[Dict[str, Any]]]) -> List[TpTarget]:
    """``[{"price","fraction"}]`` (the stored shape) -> ``TpTarget`` list. Raises on a bad row."""
    out: List[TpTarget] = []
    for item in raw or []:
        out.append(TpTarget(price=float(item["price"]), fraction=float(item["fraction"])))
    return out


def whole_shares(position_quantity: Optional[float]) -> int:
    """The whole shares of a position that can carry a priced order. Floors, never rounds up.

    TastyTrade refuses a fractional quantity on every limit/stop order, so only
    ``floor(quantity)`` can be protected; the remainder is reported, not hidden.
    Raises ``ValueError`` for ``None`` -- an unknown position is not a flat one.
    """
    if position_quantity is None:
        raise ValueError("position quantity is unknown")
    # The tiny epsilon keeps 2.9999999999 (a float artefact of 3.0) from flooring to 2.
    return max(0, int(floor(float(position_quantity) + 1e-9)))


def fractional_remainder(position_quantity: float) -> float:
    """Shares above the whole-share part (0.4 for 10.4)."""
    return max(0.0, float(position_quantity) - whole_shares(position_quantity))


def default_tick(price: float) -> Decimal:
    """US-equity fallback tick: one cent from $1, a hundredth of a cent below."""
    return Decimal("0.01") if Decimal(str(price)) >= 1 else Decimal("0.0001")


def tick_for_price(price: float, tick_sizes: Optional[Sequence[Any]] = None) -> Decimal:
    """The tick that applies at ``price``.

    ``tick_sizes`` follows the broker's ``Equity.tick_sizes``: each entry has ``value`` and an
    optional ``threshold``; an entry applies to prices BELOW its threshold, an entry without a
    threshold applies otherwise. An empty/absent list falls back to ``default_tick`` -- a
    documented US-equity rule, not a guess at a broker fact: the dry run still rejects an
    off-tick price loudly.
    """
    sizes = list(tick_sizes or [])
    if not sizes:
        return default_tick(price)
    p = Decimal(str(price))
    bounded = sorted((s for s in sizes if getattr(s, "threshold", None) is not None),
                     key=lambda s: Decimal(str(s.threshold)))
    for entry in bounded:
        if p < Decimal(str(entry.threshold)):
            return Decimal(str(entry.value))
    for entry in sizes:
        if getattr(entry, "threshold", None) is None:
            return Decimal(str(entry.value))
    return default_tick(price)


def round_price_to_tick(price: float, tick: Decimal, mode: str) -> float:
    """Snap ``price`` onto the tick grid. ``mode``: ``"nearest"`` (a TP limit) or ``"down"`` (an
    SL trigger -- never closer to the market than the operator asked)."""
    if mode not in ("nearest", "down"):
        raise ValueError(f"unknown rounding mode {mode!r}")
    p = Decimal(str(price))
    steps = (p / tick).to_integral_value(rounding=ROUND_HALF_UP if mode == "nearest" else ROUND_DOWN)
    return float(steps * tick)


def validate_protection(*, sl_price: Optional[float], targets: Sequence[TpTarget],
                        last_price: Optional[float], position_quantity: Optional[float],
                        tick_sizes: Optional[Sequence[Any]] = None) -> List[str]:
    """Blocking validation messages for a protection request; an empty list means valid. Pure.

    No defaults for live data: an unknown price or position is itself an error, never "assume
    OK". Rules: SL > 0 and strictly BELOW the current price; every TP strictly ABOVE it; at
    least one target; TP prices distinct AFTER tick rounding; each fraction in (0, 1]; fractions
    sum to 1; the whole-share quantity must be at least the number of targets that can be
    placed (at least one share).
    """
    errors: List[str] = []
    if last_price is None or last_price <= 0:
        errors.append("The current price is unknown, so the prices cannot be checked. "
                      "Refresh and try again.")
    if position_quantity is None:
        errors.append("The position is unknown, so the shares to protect cannot be sized. "
                      "Refresh and try again.")
    if sl_price is None or sl_price <= 0:
        errors.append("The stop-loss price must be greater than 0.")
    if not targets:
        errors.append("Add at least one take-profit target.")

    if last_price is not None and last_price > 0:
        if sl_price is not None and sl_price > 0 and sl_price >= last_price:
            errors.append(f"The stop-loss ({sl_price:g}) must be BELOW the current price "
                          f"({last_price:g}); a stop at or above the market would trigger "
                          f"immediately.")
        for number, target in enumerate(targets, start=1):
            if target.price <= 0:
                errors.append(f"Take-profit {number}: the price must be greater than 0.")
            elif target.price <= last_price:
                errors.append(f"Take-profit {number} ({target.price:g}) must be ABOVE the "
                              f"current price ({last_price:g}); a limit sell at or below the "
                              f"market would fill immediately.")

    for number, target in enumerate(targets, start=1):
        if not (0 < target.fraction <= 1 + _FRACTION_TOLERANCE):
            errors.append(f"Take-profit {number}: the share of the position must be above 0% "
                          f"and at most 100%.")
    if targets:
        total = sum(t.fraction for t in targets)
        if abs(total - 1.0) > _FRACTION_TOLERANCE:
            errors.append(f"The take-profit shares must total 100% (they total "
                          f"{total * 100:.2f}%).")

    if last_price is not None and last_price > 0 and targets:
        tick = tick_for_price(last_price, tick_sizes)
        rounded = [round_price_to_tick(t.price, tick, "nearest") for t in targets if t.price > 0]
        if len(set(rounded)) != len(rounded):
            errors.append("Two take-profit targets land on the same price once rounded to the "
                          "tick size; make the prices distinct.")
        if (sl_price is not None and sl_price > 0
                and any(r <= round_price_to_tick(sl_price, tick, "down") for r in rounded)):
            errors.append("A take-profit rounds to or below the stop-loss; widen the range.")

    if position_quantity is not None:
        n = whole_shares(position_quantity)
        if n < 1:
            errors.append(f"The position ({position_quantity:g} shares) has no whole share to "
                          f"protect: TastyTrade does not accept fractional quantities on limit "
                          f"or stop orders.")
    return errors


def split_quantity(shares: int, fractions: Sequence[float]) -> List[int]:
    """Split ``shares`` whole shares across ``fractions`` (sum 1) by largest remainder.

    The result sums to ``shares`` exactly and has one entry per fraction, in order; an entry can
    be 0 when there are fewer shares than targets. Pure and deterministic (ties go to the
    earlier index).
    """
    if shares < 0:
        raise ValueError("shares must be >= 0")
    if not fractions:
        raise ValueError("at least one fraction is required")
    total = float(sum(fractions))
    if total <= 0:
        raise ValueError("fractions must sum to a positive number")
    exact = [shares * f / total for f in fractions]
    base = [int(floor(x + 1e-12)) for x in exact]
    leftover = shares - sum(base)
    order = sorted(range(len(fractions)), key=lambda i: (-(exact[i] - base[i]), i))
    for i in order[:leftover]:
        base[i] += 1
    return base


def plan_slices(*, shares: int, targets: Sequence[TpTarget], sl_price: float,
                last_price: float, tick_sizes: Optional[Sequence[Any]] = None
                ) -> Tuple[List[SlicePlan], List[str]]:
    """The OCOs to place for ``shares`` whole shares, plus notes about what could not be placed.

    Targets that receive 0 shares (fewer shares than targets) are DROPPED and their share is
    folded into the nearest placeable target by re-splitting over the survivors, so the slices
    always sum to ``shares``. Each dropped target is named in the notes (the dialog shows
    them). Prices are snapped to the tick grid: TP to nearest, SL down.
    """
    if shares < 1:
        return [], ["no whole share to protect"]
    notes: List[str] = []
    ordered = sorted(enumerate(targets), key=lambda it: it[1].price)
    fractions = [t.fraction for _, t in ordered]
    quantities = split_quantity(shares, fractions)
    kept = [(idx, t) for (idx, t), q in zip(ordered, quantities) if q > 0]
    dropped = [(idx, t) for (idx, t), q in zip(ordered, quantities) if q == 0]
    if dropped:
        for idx, t in dropped:
            notes.append(f"Take-profit target {idx + 1} ({t.price:g}) gets 0 shares of {shares} "
                         f"and is not placed; its share goes to the other targets.")
        quantities = split_quantity(shares, [t.fraction for _, t in kept])
    else:
        quantities = [q for q in quantities if q > 0]
    tick = tick_for_price(last_price, tick_sizes)
    sl = round_price_to_tick(sl_price, tick, "down")
    plans = [SlicePlan(slice_index=n, target_index=idx, quantity=q,
                       tp_price=round_price_to_tick(t.price, tick, "nearest"), sl_price=sl)
             for n, ((idx, t), q) in enumerate(zip(kept, quantities))]
    return plans, notes


def remaining_targets(targets: Sequence[TpTarget],
                      consumed_indexes: Iterable[int]) -> List[TpTarget]:
    """The targets still to be served after some were consumed by a fill, fractions
    renormalised to sum 1. Raises ``ValueError`` when none are left."""
    gone = set(consumed_indexes)
    left = [t for i, t in enumerate(targets) if i not in gone]
    if not left:
        raise ValueError("every take-profit target has been consumed")
    total = sum(t.fraction for t in left)
    return [TpTarget(price=t.price, fraction=t.fraction / total) for t in left]


def preview_orders(*, shares: int, position_quantity: float, targets: Sequence[TpTarget],
                   sl_price: float, last_price: float,
                   tick_sizes: Optional[Sequence[Any]] = None) -> List[str]:
    """Human sentences for the dialog's preview, one per order plus notes. Pure."""
    plans, notes = plan_slices(shares=shares, targets=targets, sl_price=sl_price,
                               last_price=last_price, tick_sizes=tick_sizes)
    lines = [f"OCO {p.slice_index + 1}: sell {p.quantity} sh -- limit {p.tp_price:g} "
             f"OR stop {p.sl_price:g} (GTC)" for p in plans]
    lines.extend(notes)
    rest = fractional_remainder(position_quantity)
    if rest > 1e-9:
        lines.append(f"{rest:g} fractional share(s) cannot carry a limit or stop on "
                     f"TastyTrade and stay UNPROTECTED.")
    return lines


# =========================================================================================
# classifying what the broker says about one placed OCO
# =========================================================================================

_LIVE_STATUSES = frozenset({"Received", "Live", "Contingent", "Routed", "In Flight",
                            "Replace Requested"})
_CANCEL_REQUESTED = "Cancel Requested"
_CANCELLED_STATUSES = frozenset({"Cancelled", "Removed", "Partially Removed"})
_TERMINAL_STATUSES = _CANCELLED_STATUSES | {"Filled", "Expired", "Rejected"}

KIND_TP = "TP"
KIND_SL = "SL"


@dataclass
class SliceObservation:
    """What the broker says about one OCO, reduced to what the protection layer acts on.

    ``remaining_live_qty`` is the number of shares of THIS slice still reserved by a resting
    order (a partially filled member keeps its remainder live); the position's covered
    quantity is the sum of it over the slices.
    """
    state: str
    kind: Optional[str] = None
    filled_qty: float = 0.0
    fill_price: Optional[float] = None
    remaining_live_qty: float = 0.0
    detail: str = ""
    tp_order_id: Optional[int] = None
    sl_order_id: Optional[int] = None
    gtc_date: Optional[Date] = None


def _status_text(order: Any) -> str:
    raw = getattr(order, "status", None)
    return str(getattr(raw, "value", raw) or "")


def _type_text(order: Any) -> str:
    raw = getattr(order, "order_type", None)
    return str(getattr(raw, "value", raw) or "")


def _member_kind(order: Any) -> Optional[str]:
    kind = _type_text(order)
    if kind in ("Limit", "Marketable Limit"):
        return KIND_TP
    if kind in ("Stop", "Stop Limit"):
        return KIND_SL
    return None


def _member_fills(order: Any) -> Tuple[float, Optional[float]]:
    """(filled quantity, quantity-weighted average price) over a member's legs."""
    qty = 0.0
    notional = 0.0
    for leg in getattr(order, "legs", None) or []:
        for fill in getattr(leg, "fills", None) or []:
            q = float(fill.quantity)
            qty += q
            notional += q * float(fill.fill_price)
    return qty, (notional / qty if qty > 0 else None)


def classify_complex_order(placed: Any, *, slice_quantity: float,
                           we_requested_cancel: bool) -> SliceObservation:
    """Reduce a broker ``PlacedComplexOrder`` (duck-typed) to a ``SliceObservation``. Pure.

    A complex order has NO top-level status, so the state is derived from its member orders.
    The rules, in priority order (section 5.3 of the design):

    1. any fill on any member -> ``FILLED_TP`` / ``FILLED_SL`` by the member's type (a partial
       fill counts: ANY protective fill holds the symbol);
    2. any member ``Cancel Requested`` -> ``CANCELLING``;
    3. every member in a resting status -> ``LIVE``;
    4. any ``Expired`` -> ``LOST_EXPIRED``; any ``Rejected`` -> ``LOST_REJECTED``; all
       cancelled/removed -> ``CANCELLED_BY_US`` when we asked, else ``LOST_CANCELLED``;
    5. anything else (a mix, an unmapped status, no members) -> ``UNKNOWN``. Never read as LIVE.
    """
    members = list(getattr(placed, "orders", None) or [])
    tp_id = next((getattr(o, "id", None) for o in members if _member_kind(o) == KIND_TP), None)
    sl_id = next((getattr(o, "id", None) for o in members if _member_kind(o) == KIND_SL), None)
    gtc = next((getattr(o, "gtc_date", None) for o in members
                if getattr(o, "gtc_date", None) is not None), None)
    base = dict(tp_order_id=tp_id, sl_order_id=sl_id, gtc_date=gtc)
    if not members:
        return SliceObservation(state=SLICE_UNKNOWN, detail="the broker returned no member orders",
                                **base)

    statuses = [_status_text(o) for o in members]
    best = None
    for order in members:
        qty, price = _member_fills(order)
        if (qty > 0 or _status_text(order) == "Filled") and (best is None or qty > best[1]):
            best = (order, qty, price)
    if best is not None:
        order, qty, price = best
        kind = _member_kind(order)
        if kind is None:
            return SliceObservation(state=SLICE_UNKNOWN, filled_qty=qty, fill_price=price,
                                    detail=f"a member filled but its type {_type_text(order)!r} "
                                           f"is neither a limit nor a stop", **base)
        member_live = _status_text(order) in _LIVE_STATUSES or _status_text(order) == _CANCEL_REQUESTED
        remaining = max(0.0, float(slice_quantity) - qty) if member_live else 0.0
        return SliceObservation(
            state=SLICE_FILLED_TP if kind == KIND_TP else SLICE_FILLED_SL, kind=kind,
            filled_qty=qty, fill_price=price, remaining_live_qty=remaining,
            detail=("partially filled, remainder still resting" if remaining > 0
                    else "filled"), **base)

    if any(s == _CANCEL_REQUESTED for s in statuses):
        return SliceObservation(state=SLICE_CANCELLING, remaining_live_qty=float(slice_quantity),
                                detail="cancel requested, not yet confirmed", **base)
    if all(s in _LIVE_STATUSES for s in statuses):
        return SliceObservation(state=SLICE_LIVE, remaining_live_qty=float(slice_quantity),
                                detail=", ".join(statuses), **base)
    if "Expired" in statuses:
        return SliceObservation(state=SLICE_LOST_EXPIRED,
                                detail=f"expired at the broker ({', '.join(statuses)})", **base)
    if "Rejected" in statuses:
        return SliceObservation(state=SLICE_LOST_REJECTED,
                                detail=f"rejected by the broker ({', '.join(statuses)})", **base)
    if all(s in _CANCELLED_STATUSES for s in statuses):
        return SliceObservation(
            state=SLICE_CANCELLED_BY_US if we_requested_cancel else SLICE_LOST_CANCELLED,
            detail=("cancelled as requested" if we_requested_cancel else
                    f"cancelled at the broker and NOT by this platform ({', '.join(statuses)})"),
            **base)
    return SliceObservation(
        state=SLICE_UNKNOWN,
        detail=f"cannot read the member statuses {statuses!r} as live, filled or cancelled",
        **base)


def is_complex_order_terminal(placed: Any) -> bool:
    """True when every member of a broker complex order is in a final status."""
    members = list(getattr(placed, "orders", None) or [])
    return bool(members) and all(_status_text(o) in _TERMINAL_STATUSES for o in members)


# =========================================================================================
# status shown on the page
# =========================================================================================

@dataclass
class ProtectionStatus:
    """What the page shows for one symbol: a status code, a colour, a label, a tooltip."""
    code: str
    label: str
    color: str            # a Quasar colour name
    tooltip: str
    alarm: bool = False
    covered_quantity: float = 0.0
    whole_shares: int = 0


_COLORS = {STATUS_OFF: "grey", STATUS_NO_POSITION: "grey", STATUS_PROTECTED: "positive",
           STATUS_PARTIAL: "warning", STATUS_REPLACING: "warning",
           STATUS_UNPROTECTED: "negative", STATUS_HELD: "info"}


def _as_utc_naive(value: Optional[DateTime]) -> Optional[DateTime]:
    if value is None:
        return None
    return value.replace(tzinfo=None) if value.tzinfo is not None else value


def protection_status(*, enabled: bool, held_at: Optional[DateTime], held_reason: Optional[str],
                      pending_replace: bool, pending_replace_since: Optional[DateTime],
                      slice_states: Sequence[Tuple[str, float]], position_quantity: Optional[float],
                      alert_message: Optional[str] = None,
                      now: Optional[DateTime] = None) -> ProtectionStatus:
    """The page status of one symbol. Pure; see section 3 of the design.

    ``slice_states`` is ``[(state, remaining_live_qty)]`` over this protection's slices (ALL of
    them: history rows are ignored here, alarm rows are what make it UNPROTECTED).
    ``position_quantity`` ``None`` (position unreadable) yields UNPROTECTED with that reason on
    an enabled protection: unknown is never "fine".
    """
    now = _as_utc_naive(now) or DateTime.utcnow()
    live_qty = sum(q for state, q in slice_states if state in SLICE_RESTING_STATES)
    alarms = [state for state, _ in slice_states if state in SLICE_ALARM_STATES]
    held = held_at is not None

    if not enabled and not held:
        return ProtectionStatus(STATUS_OFF, "Set TP/SL", _COLORS[STATUS_OFF],
                                "TP/SL protection is off for this symbol.")

    n_whole = None if position_quantity is None else whole_shares(position_quantity)
    if held:
        when = held_at.strftime("%Y-%m-%d")
        tip = (f"Held after TP/SL fill on {when}: excluded from allocator rebalancing "
               f"(no buys, no sells) until you re-enable it. {held_reason or ''}").strip()
        if live_qty > 0:
            tip += f" {live_qty:g} share(s) are still covered by resting orders."
        if alarms:
            tip += f" ALERT: {len(alarms)} protective order(s) were lost ({', '.join(sorted(set(alarms)))})."
        return ProtectionStatus(STATUS_HELD, f"Held after TP/SL fill on {when}",
                                _COLORS[STATUS_HELD], tip, alarm=bool(alarms),
                                covered_quantity=live_qty, whole_shares=n_whole or 0)

    if position_quantity is None:
        return ProtectionStatus(STATUS_UNPROTECTED, "Unprotected", _COLORS[STATUS_UNPROTECTED],
                                "The position could not be read, so protection cannot be "
                                "confirmed.", alarm=True)
    if pending_replace:
        since = _as_utc_naive(pending_replace_since)
        stale = since is not None and (now - since).total_seconds() > PENDING_REPLACE_STALE_SECONDS
        if stale:
            return ProtectionStatus(
                STATUS_UNPROTECTED, "Unprotected", _COLORS[STATUS_UNPROTECTED],
                "The protective orders were cancelled for a rebalance and the replacement "
                "has not been placed. " + (alert_message or ""), alarm=True,
                whole_shares=n_whole)
        return ProtectionStatus(STATUS_REPLACING, "Re-placing", _COLORS[STATUS_REPLACING],
                                "Protective orders are being re-placed after a trade.",
                                covered_quantity=live_qty, whole_shares=n_whole)
    if n_whole == 0 and not alarms:
        return ProtectionStatus(STATUS_NO_POSITION, "Armed, no position", _COLORS[STATUS_NO_POSITION],
                                "Enabled, but there is no whole share to protect yet.",
                                whole_shares=0)
    if alarms:
        return ProtectionStatus(
            STATUS_UNPROTECTED, "Unprotected", _COLORS[STATUS_UNPROTECTED],
            f"A protective order was lost ({', '.join(sorted(set(alarms)))}). "
            f"{alert_message or ''}".strip(), alarm=True, covered_quantity=live_qty,
            whole_shares=n_whole)
    if live_qty <= 0:
        return ProtectionStatus(STATUS_UNPROTECTED, "Unprotected", _COLORS[STATUS_UNPROTECTED],
                                "Enabled, but no protective order is live. "
                                + (alert_message or ""), alarm=True, whole_shares=n_whole)
    if abs(live_qty - n_whole) < 1e-9:
        rest = fractional_remainder(position_quantity)
        note = (f" {rest:g} fractional share(s) cannot carry a stop on TastyTrade and are "
                f"unprotected." if rest > 1e-9 else "")
        return ProtectionStatus(STATUS_PROTECTED, "Protected", _COLORS[STATUS_PROTECTED],
                                f"{live_qty:g} of {position_quantity:g} shares are covered by "
                                f"resting TP/SL orders.{note}", covered_quantity=live_qty,
                                whole_shares=n_whole)
    return ProtectionStatus(
        STATUS_PARTIAL, "Partly protected", _COLORS[STATUS_PARTIAL],
        f"Only {live_qty:g} of {n_whole} whole shares are covered. "
        f"{alert_message or 'Re-place protection to resize it.'}", alarm=True,
        covered_quantity=live_qty, whole_shares=n_whole)


def gtc_expiry_due(gtc_date: Optional[DateTime], *, today: Optional[Date] = None) -> bool:
    """True when ``gtc_date`` is within ``GTC_EXPIRY_WARN_DAYS`` of ``today`` (or past)."""
    if gtc_date is None:
        return False
    day = gtc_date.date() if isinstance(gtc_date, DateTime) else gtc_date
    today = today or DateTime.utcnow().date()
    return (day - today).days <= GTC_EXPIRY_WARN_DAYS


# =========================================================================================
# broker-call result types (shared by the account adapter and the service)
# =========================================================================================

class ProtectionRefused(Exception):
    """The broker (or a pre-check) refused to place a protective order. Never swallowed: the
    service turns it into a loud alert and an UNPROTECTED status."""


@dataclass
class ProtectiveOcoResult:
    """What ``place_protective_oco`` hands back after the broker accepted the OCO."""
    complex_order_id: int
    tp_order_id: Optional[int]
    sl_order_id: Optional[int]
    status: str
    gtc_date: Optional[Date]
    tp_price: float
    sl_price: float
    quantity: int


@dataclass
class CancelOutcome:
    """Result of ``cancel_complex_order``. ``confirmed`` False means the orders may STILL BE
    LIVE at the broker; ``filled`` True means a member filled before the cancel landed."""
    confirmed: bool
    filled: bool
    final: Any = None
    detail: str = ""
