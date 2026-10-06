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
    ORDER_KIND_OCO, ORDER_KIND_STOP, SLICE_ALARM_STATES, SLICE_CANCELLED_BY_US, SLICE_CANCELLING, SLICE_FILLED_SL,
    SLICE_FILLED_TP, SLICE_LIVE, SLICE_LOST_CANCELLED, SLICE_LOST_EXPIRED,
    SLICE_LOST_REJECTED, SLICE_PLACING, SLICE_RESTING_STATES, SLICE_UNKNOWN,
)

#: A broker that returns NO ``gtc_date`` is never renewed and never warned about (operator, 2026-10-05):
#: the date is stored as NULL, not guessed.
#: Days before the stored GTC end date at which the expiry warning fires.
GTC_EXPIRY_WARN_DAYS = 7
#: A slice whose BROKER-REPORTED gtc_date is this close is cancelled (confirmed) and re-placed with the
#: same prices by the account refresh; one symbol per refresh cycle.
GTC_RENEW_DAYS = 7
#: After a protective fill the broker's position read may lag; automatic growth/resize/disarm wait this long.
FILL_SETTLE_SECONDS = 300
#: Consecutive failed AUTOMATIC placements after which the background paths stop retrying (loud alert).
AUTO_FAILURE_LIMIT = 3
#: An UNKNOWN slice (a placement whose outcome we do not know) is never concluded 'never placed'
#: before it is this old: the broker may simply not have listed it yet (round-2 review N2).
UNKNOWN_MIN_AGE_SECONDS = 300
#: Minimum gap between two automatic cancel/re-place actions on one symbol.
AUTO_ACTION_MIN_INTERVAL_SECONDS = 600
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
#: Switched off / deleted, but a cancel was never confirmed: orders may still rest at the broker.
STATUS_CANCEL_UNCONFIRMED = "CANCEL_UNCONFIRMED"

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
CODE_FILL = "FILL"
CODE_REPLACE_STALE = "REPLACE_STALE"
CODE_AUTO_STOPPED = "AUTO_STOPPED"
#: A platform sale is filled but the broker position read has not caught up yet: the re-placement waits.
CODE_SALE_SETTLING = "SALE_SETTLING"
#: Alert codes that are WARNINGS: a failure-class alert is never overwritten by one of these.
WARNING_CODES = frozenset({CODE_GTC_EXPIRING, CODE_RECONCILE_FETCH_FAILED, CODE_SALE_SETTLING})


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
    """One order to place: ``quantity`` whole shares.

    ``kind`` OCO: take-profit limit at ``tp_price`` OR stop at ``sl_price`` (``target_index`` is
    the take-profit target it serves). ``kind`` STOP: a plain GTC stop-only sell at ``sl_price``
    for the part of the position no take-profit covers (the "runner"); ``tp_price`` is None and
    ``target_index`` is -1.
    """
    slice_index: int
    target_index: int
    quantity: int
    tp_price: Optional[float]
    sl_price: float
    kind: str = ORDER_KIND_OCO


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
    OK". Rules: SL > 0 and strictly BELOW the current price; every TP strictly ABOVE it; TP
    prices distinct AFTER tick rounding; each fraction in (0, 1] and the fractions sum to AT MOST
    1 (the part no target covers is a stop-only runner, so ZERO targets is valid: a stop for the
    whole position); at least one whole share.
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
        if total > 1.0 + _FRACTION_TOLERANCE:
            errors.append(f"The take-profit shares must not exceed 100% (they total "
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


def runner_fraction(targets: Sequence[TpTarget]) -> float:
    """The share of the position no take-profit covers (the stop-only runner), 0..1."""
    return max(0.0, 1.0 - sum(t.fraction for t in targets))


def drop_reached_targets(targets: Sequence[TpTarget], last_price: float,
                         tick_sizes: Optional[Sequence[Any]] = None
                         ) -> Tuple[List[TpTarget], List[TpTarget]]:
    """Split ``targets`` into ``(usable, reached)``: a target AT or BELOW the current price (after tick
    rounding) is already reached -- typically the one that just filled -- and a limit sell there would
    fill at once or be refused. Pure. The fraction of a dropped target is not carved out of anything:
    ``plan_slices`` sizes the stop-only remainder as ``1 - sum(usable fractions)``, so it folds into
    the stop-only order and the stop ALWAYS covers the whole position.
    """
    tick = tick_for_price(last_price, tick_sizes)
    usable, reached = [], []
    for t in targets:
        (reached if round_price_to_tick(t.price, tick, "nearest") <= last_price else usable).append(t)
    return usable, reached


@dataclass(frozen=True)
class EffectiveTargets:
    """The targets a re-placement should actually place (see ``effective_targets``).

    ``usable`` are scaled to the shares that are LEFT; ``usable_index`` are their positions in the
    ORIGINAL list; ``filled`` and ``reached`` are original positions too.
    """
    usable: List[TpTarget]
    usable_index: List[int]
    filled: List[int]
    reached: List[int]


def effective_targets(raw: Optional[Iterable[Dict[str, Any]]], last_price: float,
                      tick_sizes: Optional[Sequence[Any]] = None) -> EffectiveTargets:
    """What to place now from the stored targets (each ``{price, fraction[, filled]}``). Pure.

    1. A target recorded as FILLED is never placed again (it was already taken); one PARTLY taken
       (``"taken"``: the filled share of its slice) keeps the part that is left.
    2. The rest are spread over the shares that are left: ``f / (1 - sum(filled f))``, so TP2 of
       50% after TP1's 50% fill covers ALL the remaining shares, not half of them.
    3. Only then are targets at or below the price dropped (reached but not filled). Their share is
       NOT redistributed: ``plan_slices`` sizes the stop-only remainder as ``1 - sum(usable)``, so it
       folds into the stop and the stop always covers the whole position.
    """
    items = list(raw or [])

    ratios = [target_taken(it) for it in items]
    filled = [i for i, r in enumerate(ratios) if r >= 1.0 - 1e-9]
    taken = sum(float(it["fraction"]) * r for it, r in zip(items, ratios))
    left = [i for i in range(len(items)) if i not in filled]
    if not left or taken >= 1.0 - 1e-9:
        return EffectiveTargets([], [], filled, [])
    scale = 1.0 / (1.0 - taken)
    scaled = [TpTarget(price=float(items[i]["price"]),
                       fraction=float(items[i]["fraction"]) * (1.0 - ratios[i]) * scale) for i in left]
    usable, reached = drop_reached_targets(scaled, last_price, tick_sizes)
    reached_keys = {(t.price, t.fraction) for t in reached}
    usable_index = [i for i, t in zip(left, scaled) if (t.price, t.fraction) not in reached_keys]
    reached_index = [i for i, t in zip(left, scaled) if (t.price, t.fraction) in reached_keys]
    return EffectiveTargets(usable, usable_index, filled, reached_index)


#: What the broker's refusal says when the stop's buying-power reservation is not affordable.
MARGIN_FAILED_MARK = "margin_check_failed"


def is_margin_refusal(text: Optional[str]) -> bool:
    """True when a broker message is the buying-power refusal of a stop (A11)."""
    return MARGIN_FAILED_MARK in (text or "")


@dataclass(frozen=True)
class ProtectiveDryRun:
    """The broker's verdict on ONE protective order, from a dry run (nothing is placed).

    ``bp_change`` is the SIGNED buying-power change (negative = reserves buying power), ``None`` when the
    broker refused before computing one. ``message`` is the broker's own words (kept verbatim).
    """
    ok: bool
    bp_change: Optional[float] = None
    message: str = ""
    margin_failed: bool = False


def no_acceptable_stop_sentence(available: Optional[float] = None, distance_pct: float = 3.0) -> str:
    """What the dialog says when no stop at or below the suggestion cap is accepted. Never a near-market stop."""
    detail = f" (available ${available:,.2f})" if available is not None else ""
    return (f"No acceptable stop for this symbol/account: TastyTrade refuses even a stop {distance_pct:g}% below "
            f"the market{detail}. Free buying power or reduce the position.")


def margin_sentence(*, needed: Optional[float] = None, available: Optional[float] = None,
                    suggested: Optional[float] = None) -> str:
    """The one sentence the dialog, the alert and the tooltip use for a stop the broker cannot afford.

    TastyTrade margin-checks a STOP as if it filled AT ITS TRIGGER (A11), so a stop far below the market
    RESERVES buying power for the loss at that price. Pure; the numbers are optional (never invented).
    """
    detail = ""
    if needed is not None and available is not None:
        detail = f" (needs ~${needed:,.2f}, available ${available:,.2f})"
    elif available is not None:
        detail = f" (available ${available:,.2f})"
    advice = (f" Raise the stop to ~${suggested:,.2f} (approx.) or free buying power."
              if suggested is not None else " Raise the stop or free buying power.")
    return ("This stop is far below the market: TastyTrade reserves buying power for the loss at the stop "
            "price" + detail + "." + advice)


def target_taken(item: Dict[str, Any]) -> float:
    """How much of a stored target is already TAKEN (0..1): 1 when marked filled, else its ``taken``."""
    if item.get("filled"):
        return 1.0
    stored = item.get("taken")
    return 0.0 if stored is None else min(1.0, max(0.0, float(stored)))


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
    """The orders to place for ``shares`` whole shares, plus notes about what could not be placed.

    The position is split by largest remainder over the targets (cheapest price first) and the
    RUNNER (``1 - sum(fractions)``, last): every take-profit target becomes an OCO and the runner
    becomes ONE plain stop-only order, so the quantities always sum to ``shares`` and the whole
    position is always covered by a stop. A target that receives 0 shares is dropped (named in
    the notes) -- its share is simply not carved out of the runner or the others. Prices are
    snapped to the tick grid: TP to nearest, SL down. With no targets the plan is a single stop
    for everything.
    """
    if shares < 1:
        return [], ["no whole share to protect"]
    notes: List[str] = []
    ordered = sorted(enumerate(targets), key=lambda it: it[1].price)
    runner = runner_fraction(targets)
    fractions = [t.fraction for _, t in ordered] + [runner if runner > _FRACTION_TOLERANCE else 0.0]
    quantities = split_quantity(shares, fractions)
    runner_qty = quantities[-1]
    tp_quantities = quantities[:-1]
    tick = tick_for_price(last_price, tick_sizes)
    sl = round_price_to_tick(sl_price, tick, "down")
    plans: List[SlicePlan] = []
    for (idx, t), q in zip(ordered, tp_quantities):
        if q == 0:
            notes.append(f"Take-profit target {idx + 1} ({t.price:g}) gets 0 shares of {shares} "
                         f"and is not placed.")
            continue
        plans.append(SlicePlan(slice_index=len(plans), target_index=idx, quantity=q,
                               tp_price=round_price_to_tick(t.price, tick, "nearest"), sl_price=sl,
                               kind=ORDER_KIND_OCO))
    if runner_qty > 0:
        plans.append(SlicePlan(slice_index=len(plans), target_index=-1, quantity=runner_qty,
                               tp_price=None, sl_price=sl, kind=ORDER_KIND_STOP))
    return plans, notes


def plan_add_only(*, total_shares: int, to_cover: int, targets: Sequence[TpTarget],
                  target_ids: Sequence[int], existing_by_target: Dict[int, int], existing_runner: int,
                  sl_price: float, last_price: float, tick_sizes: Optional[Sequence[Any]] = None
                  ) -> List[SlicePlan]:
    """Orders for ``to_cover`` shares that join a protection whose older orders still rest (growth, repair).

    The plan is made for the WHOLE position (``total_shares``) and each order only fills the DEFICIT of its
    target against what already rests there (``existing_by_target`` by original target id, and the stop-only
    runner), so the per-target totals stay as close to the template as the shares allow instead of every
    added share being split by the template again (which would pile extra take-profit shares on whatever
    target is left). A deficit total above ``to_cover`` is scaled down by largest remainder; shares beyond
    the deficits go to the stop-only runner, so the stop always covers everything. ``target_ids`` map the
    positions in ``targets`` to the ids the slices carry (original target indices). Pure.
    """
    if to_cover < 1 or total_shares < 1:
        return []
    total_plan, _ = plan_slices(shares=total_shares, targets=targets, sl_price=sl_price,
                                last_price=last_price, tick_sizes=tick_sizes)
    if not total_plan:
        return []
    keyed = []
    for plan in total_plan:
        key = -1 if plan.kind == ORDER_KIND_STOP else int(target_ids[plan.target_index])
        have = int(existing_runner) if key == -1 else int(existing_by_target.get(key, 0))
        keyed.append((key, plan, max(0, plan.quantity - have)))
    deficit_sum = sum(d for _, _, d in keyed)
    sl = total_plan[0].sl_price
    if deficit_sum > to_cover:
        amounts = split_quantity(to_cover, [float(d) for _, _, d in keyed])
    else:
        amounts = [d for _, _, d in keyed]
        extra = to_cover - deficit_sum
        if extra:
            runner_at = next((i for i, (k, _, _) in enumerate(keyed) if k == -1), None)
            if runner_at is None:
                keyed.append((-1, SlicePlan(slice_index=0, target_index=-1, quantity=0, tp_price=None,
                                            sl_price=sl, kind=ORDER_KIND_STOP), 0))
                amounts.append(extra)
            else:
                amounts[runner_at] += extra
    out: List[SlicePlan] = []
    for (key, plan, _), amount in zip(keyed, amounts):
        if amount > 0:
            out.append(SlicePlan(slice_index=len(out), target_index=key, quantity=int(amount),
                                 tp_price=plan.tp_price, sl_price=plan.sl_price, kind=plan.kind))
    return out


def _plan_line(p: SlicePlan) -> str:
    if p.kind == ORDER_KIND_STOP:
        return f"STOP {p.quantity} sh @ {p.sl_price:g} (GTC, runner: no take-profit)"
    return (f"OCO {p.slice_index + 1}: sell {p.quantity} sh -- limit {p.tp_price:g} "
            f"OR stop {p.sl_price:g} (GTC)")


def preview_summary(plans: Sequence[SlicePlan]) -> str:
    """One line for the dialog: ``3 orders: OCO 5 sh TP 12.5 / SL 8, OCO 5 sh TP 15 / SL 8,
    STOP 6 sh @ 8``. Pure."""
    parts = [(f"STOP {p.quantity} sh @ {p.sl_price:g}" if p.kind == ORDER_KIND_STOP else
              f"OCO {p.quantity} sh TP {p.tp_price:g} / SL {p.sl_price:g}") for p in plans]
    noun = "order" if len(parts) == 1 else "orders"
    return f"{len(parts)} {noun}: " + ", ".join(parts) if parts else "no orders"


def preview_orders(*, shares: int, position_quantity: float, targets: Sequence[TpTarget],
                   sl_price: float, last_price: float,
                   tick_sizes: Optional[Sequence[Any]] = None) -> List[str]:
    """Human sentences for the dialog's preview: the one-line summary, one line per order, the
    notes, and the fractional-remainder warning. Pure."""
    plans, notes = plan_slices(shares=shares, targets=targets, sl_price=sl_price,
                               last_price=last_price, tick_sizes=tick_sizes)
    lines = [preview_summary(plans)] if plans else []
    lines.extend(_plan_line(p) for p in plans)
    lines.extend(notes)
    rest = fractional_remainder(position_quantity)
    if rest > 1e-9:
        lines.append(f"{rest:g} fractional share(s) cannot carry a limit or stop on "
                     f"TastyTrade and stay UNPROTECTED.")
    return lines


# =========================================================================================
# presets: fill the dialog from the position's AVERAGE COST (pure; the form stays editable)
# =========================================================================================

@dataclass(frozen=True)
class PresetSpec:
    """A named recipe: stop at ``sl_pct`` from average cost (negative) and take-profits at
    ``(multiple_of_average_cost, share_of_position)``. The part no take-profit covers is the
    stop-only runner."""
    key: str
    label: str
    description: str
    sl_pct: float
    take_profits: Tuple[Tuple[float, float], ...]


PRESETS: Tuple[PresetSpec, ...] = (
    PresetSpec("double_up", "Double-up: take half at 2x",
               "Sell 50% at 2.0x average cost; the other 50% has no take-profit (a runner) and "
               "is protected by the stop. Stop -25% from average cost.", -25.0, ((2.0, 0.5),)),
    PresetSpec("ladder", "Ladder +25/+50/+100%",
               "Three take-profits of one third each at +25%, +50% and +100% over average cost. "
               "Stop -15%.", -15.0, ((1.25, 1 / 3), (1.5, 1 / 3), (2.0, 1 / 3))),
    PresetSpec("two_r", "2R scale-out",
               "Stop -10% (the risk R); sell 50% at +20% (2R); the other 50% is a stop-only "
               "runner.", -10.0, ((1.2, 0.5),)),
    PresetSpec("income_stop", "Income: stop only",
               "No take-profit; stop -20% from average cost. For income / wheel ETFs: keep "
               "collecting the yield, cut a collapse.", -20.0, ()),
)


def preset_by_key(key: str) -> PresetSpec:
    for spec in PRESETS:
        if spec.key == key:
            return spec
    raise KeyError(f"unknown protection preset {key!r}")


@dataclass
class PresetResult:
    sl_price: float
    targets: List[TpTarget]


def apply_preset(key: str, average_cost: Optional[float],
                 tick_sizes: Optional[Sequence[Any]] = None) -> PresetResult:
    """The stop and the take-profit targets a preset gives for ``average_cost``, on the tick grid
    (TP to nearest, SL down). Pure. An unknown or non-positive average cost RAISES: a preset
    computed from a guessed cost would be a fabricated price.

    The result may fail validation against the CURRENT price (a position far under water has its
    -25% stop above the market; one far in profit has a +25% target below it): the dialog
    shows that as the usual validation message and leaves every number editable.
    """
    if average_cost is None or average_cost <= 0:
        raise ValueError("the position's average cost is unknown, so a preset cannot be computed")
    spec = preset_by_key(key)
    tick = tick_for_price(average_cost, tick_sizes)
    sl = round_price_to_tick(average_cost * (1.0 + spec.sl_pct / 100.0), tick, "down")
    targets = [TpTarget(price=round_price_to_tick(average_cost * multiple, tick, "nearest"),
                        fraction=fraction) for multiple, fraction in spec.take_profits]
    return PresetResult(sl_price=sl, targets=targets)


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
           STATUS_UNPROTECTED: "negative", STATUS_CANCEL_UNCONFIRMED: "warning"}


def _as_utc_naive(value: Optional[DateTime]) -> Optional[DateTime]:
    if value is None:
        return None
    return value.replace(tzinfo=None) if value.tzinfo is not None else value


def protection_status(*, enabled: bool, pending_replace: bool, disarmed_note: Optional[str] = None,
                      pending_replace_since: Optional[DateTime],
                      slice_states: Sequence[Tuple[str, float]], position_quantity: Optional[float],
                      alert_message: Optional[str] = None, last_fill_note: Optional[str] = None,
                      now: Optional[DateTime] = None) -> ProtectionStatus:
    """The page status of one symbol. Pure; see section 3 of the design.

    ``slice_states`` is ``[(state, remaining_live_qty)]`` over this protection's slices (ALL of
    them: history rows are ignored here, alarm rows are what make it UNPROTECTED).
    ``position_quantity`` ``None`` (position unreadable) yields UNPROTECTED with that reason on
    an enabled protection: unknown is never "fine". A protection NEVER excludes the symbol from
    allocation, so there is no "held" status; ``last_fill_note`` ("TP1 filled 2026-10-03: share
    6% -> 3%") is appended to the tooltip of whatever the status is.
    """
    now = _as_utc_naive(now) or DateTime.utcnow()
    live_qty = sum(q for state, q in slice_states if state in SLICE_RESTING_STATES)
    alarms = [state for state, _ in slice_states if state in SLICE_ALARM_STATES]
    tail = f" Last fill: {last_fill_note}." if last_fill_note else ""

    if not enabled and any(state == SLICE_CANCELLING for state, _ in slice_states):
        return ProtectionStatus(
            STATUS_CANCEL_UNCONFIRMED, "Cancel unconfirmed", _COLORS[STATUS_CANCEL_UNCONFIRMED],
            "TP/SL was switched off but the broker has not confirmed the cancel of every protective order: "
            "they may still rest at the broker. The next refresh closes this once they are gone; otherwise "
            "check the TastyTrade site." + tail, alarm=True)
    if not enabled:
        gone = f" Disarmed after the position was exited (was: {disarmed_note}); set TP/SL again on re-entry." if disarmed_note else ""
        return ProtectionStatus(STATUS_OFF, "Set TP/SL", _COLORS[STATUS_OFF],
                                "TP/SL protection is off for this symbol." + gone + tail)

    n_whole = None if position_quantity is None else whole_shares(position_quantity)
    if position_quantity is None:
        return ProtectionStatus(STATUS_UNPROTECTED, "Unprotected", _COLORS[STATUS_UNPROTECTED],
                                "The position could not be read, so protection cannot be "
                                "confirmed." + tail, alarm=True)
    if pending_replace:
        since = _as_utc_naive(pending_replace_since)
        stale = since is not None and (now - since).total_seconds() > PENDING_REPLACE_STALE_SECONDS
        if stale:
            return ProtectionStatus(
                STATUS_UNPROTECTED, "Unprotected", _COLORS[STATUS_UNPROTECTED],
                "The protective orders were cancelled for a rebalance and the replacement "
                "has not been placed. " + (alert_message or "") + tail, alarm=True,
                whole_shares=n_whole)
        return ProtectionStatus(STATUS_REPLACING, "Re-placing", _COLORS[STATUS_REPLACING],
                                "Protective orders are being re-placed after a trade." + tail,
                                covered_quantity=live_qty, whole_shares=n_whole)
    if n_whole == 0 and not alarms:
        return ProtectionStatus(STATUS_NO_POSITION, "Armed, no position", _COLORS[STATUS_NO_POSITION],
                                "Enabled, but there is no whole share to protect yet; it is "
                                "placed again as soon as the position is bought." + tail,
                                whole_shares=0)
    if alarms:
        return ProtectionStatus(
            STATUS_UNPROTECTED, "Unprotected", _COLORS[STATUS_UNPROTECTED],
            (f"A protective order was lost ({', '.join(sorted(set(alarms)))}). "
             f"{alert_message or ''}").strip() + tail, alarm=True, covered_quantity=live_qty,
            whole_shares=n_whole)
    if live_qty <= 0:
        return ProtectionStatus(STATUS_UNPROTECTED, "Unprotected", _COLORS[STATUS_UNPROTECTED],
                                "Enabled, but no protective order is live. "
                                + (alert_message or "") + tail, alarm=True, whole_shares=n_whole)
    if abs(live_qty - n_whole) < 1e-9:
        rest = fractional_remainder(position_quantity)
        note = (f" {rest:g} fractional share(s) cannot carry a stop on TastyTrade and are "
                f"unprotected." if rest > 1e-9 else "")
        return ProtectionStatus(STATUS_PROTECTED, "Protected", _COLORS[STATUS_PROTECTED],
                                f"{live_qty:g} of {position_quantity:g} shares are covered by "
                                f"resting TP/SL orders.{note}{tail}", covered_quantity=live_qty,
                                whole_shares=n_whole)
    direction = ("The orders cover MORE shares than are held" if live_qty > n_whole
                 else "Only part of the position is covered")
    return ProtectionStatus(
        STATUS_PARTIAL, "Size mismatch", _COLORS[STATUS_PARTIAL],
        f"{direction}: {live_qty:g} covered, {n_whole} whole shares held. "
        f"{alert_message or 'Use Resize protection to re-place it at the held quantity.'}{tail}",
        alarm=True, covered_quantity=live_qty, whole_shares=n_whole)


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

class PlacementOutcomeUnknown(Exception):
    """The LIVE placement call raised AFTER it may have reached the broker (a timeout, a dropped
    connection) or the order was accepted but could not be read back. The order MAY be resting.
    Never a refusal: the service records the slice UNKNOWN with every id it knows and the tag, and
    resolves it by finding the tag at the broker (adopt or cancel). ``broker_id`` is the complex-order
    id (OCO) or the order id (stop-only) when the broker had already named one."""

    def __init__(self, message: str, *, tag: str, kind: str, broker_id: Optional[int] = None):
        super().__init__(message)
        self.tag = tag
        self.kind = kind
        self.broker_id = broker_id


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
class ProtectiveStopResult:
    """What ``place_protective_stop`` hands back after the broker accepted the stop-only order."""
    order_id: int
    status: str
    gtc_date: Optional[Date]
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
