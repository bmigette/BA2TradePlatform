"""Allocator per-symbol TP/SL protection: the IO and orchestration layer.

LIVE-ONLY and in-tree (see ``allocator_protection_models``). Every DECISION is a pure function
in ``allocator_protection``; the broker calls are ``TastyTradeAccount`` methods
(``place_protective_oco`` / ``cancel_complex_order`` / ``get_complex_order_state``); this module
is the DB bookkeeping and the lifecycle that ties them together:

    save / disable / delete / replace (resize)          (operator actions from the page)
    reconcile_account                                  (account refresh job + page Refresh)
    prepare_for_trade / resume_protection              (the allocator run boundary)

Design: ``docs/plans/2026-10-04-allocator-tp-sl-design.md``. THE RULE OF THIS MODULE: nothing
fails silently. Every refusal, unconfirmed cancel, lost order and unreadable state goes to the
log, to the activity log, and onto the protection row (``alert_code``) that the page renders.

Nothing here is ever called with a real broker by a test: tests use a faithful fake of the SDK.
"""
import threading
from dataclasses import dataclass, field
from datetime import datetime as DateTime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from sqlmodel import select

from ..logger import logger
from .allocator_exclusion import reduce_symbol_weights
from .allocator_protection import (
    ASSUMED_GTC_LIFETIME_DAYS, CODE_CANCEL_UNCONFIRMED, CODE_FILL, CODE_GTC_EXPIRING,
    CODE_LOST_CANCELLED, CODE_LOST_EXPIRED, CODE_LOST_REJECTED, CODE_PLACEMENT_REFUSED,
    CODE_QUANTITY_MISMATCH, CODE_RECONCILE_FETCH_FAILED, CODE_REPLACE_FAILED,
    CODE_UNKNOWN_STATE, KIND_SL, KIND_TP, PLACING_STALE_SECONDS, ProtectionRefused,
    ProtectionStatus, SliceObservation, TpTarget, classify_complex_order, gtc_expiry_due,
    protection_status, targets_from_dicts, validate_protection, whole_shares,
    plan_slices, fractional_remainder,
)
from .allocator_protection_models import (
    AllocatorProtection, AllocatorProtectionOrder, ORDER_KIND_OCO, ORDER_KIND_STOP,
    SLICE_ALARM_STATES, SLICE_CANCELLED_BY_US, SLICE_CANCELLING, SLICE_FILLED_SL,
    SLICE_FILLED_TP, SLICE_LIVE, SLICE_LOST_CANCELLED, SLICE_LOST_EXPIRED, SLICE_LOST_REJECTED,
    SLICE_PLACING, SLICE_RESTING_STATES, SLICE_UNKNOWN, WEIGHT_REASON_SL_FILL,
    WEIGHT_REASON_TP_FILL,
)
from .db import add_instance, get_db, get_instance, log_activity, update_instance
from .models import TradingOrder, Transaction
from .types import ActivityLogSeverity, ActivityLogType, OrderDirection, OrderStatus

#: Slices still worth asking the broker about: resting ones, a partially filled one whose
#: remainder rests (state FILLED_* with no ``closed_at``), and UNKNOWN (retry the read).
_RECONCILABLE_STATES = frozenset({SLICE_PLACING, SLICE_LIVE, SLICE_CANCELLING,
                                  SLICE_FILLED_TP, SLICE_FILLED_SL, SLICE_UNKNOWN})
_FILLED_STATES = frozenset({SLICE_FILLED_TP, SLICE_FILLED_SL})

_LOCKS: Dict[int, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


def protection_lock(account_id: int) -> threading.RLock:
    """One re-entrant lock per account serialising every mutation of its protections.

    ORDER: an allocator run takes the submission lock first and this lock inside it; the
    operator actions take only this one, so the two can never deadlock.
    """
    with _LOCKS_GUARD:
        lock = _LOCKS.get(int(account_id))
        if lock is None:
            lock = _LOCKS[int(account_id)] = threading.RLock()
        return lock


def _now() -> DateTime:
    """Naive UTC, matching the rest of the models' ``DateTime`` columns."""
    return DateTime.now(timezone.utc).replace(tzinfo=None)


def _norm(symbol: str) -> str:
    return (symbol or "").strip().upper()


# =========================================================================================
# store
# =========================================================================================

def get_protection(account_id: int, symbol: str) -> Optional[AllocatorProtection]:
    with get_db() as session:
        return session.exec(select(AllocatorProtection).where(
            AllocatorProtection.account_id == int(account_id),
            AllocatorProtection.symbol == _norm(symbol))).first()


def list_protections(account_id: int) -> List[AllocatorProtection]:
    with get_db() as session:
        return list(session.exec(select(AllocatorProtection).where(
            AllocatorProtection.account_id == int(account_id))).all())


def get_slices(protection_id: int) -> List[AllocatorProtectionOrder]:
    with get_db() as session:
        return list(session.exec(select(AllocatorProtectionOrder).where(
            AllocatorProtectionOrder.protection_id == int(protection_id))
            .order_by(AllocatorProtectionOrder.id)).all())


def _save(obj):
    """Insert or update, refreshing ``updated_at`` on a protection. Returns the object."""
    if isinstance(obj, AllocatorProtection):
        obj.updated_at = _now()
    if obj.id is None:
        obj.id = add_instance(obj, expunge_after_flush=True)
    else:
        update_instance(obj)
    return obj


def _reload(protection_id: int) -> AllocatorProtection:
    return get_instance(AllocatorProtection, protection_id)


def _is_resting(slice_: AllocatorProtectionOrder) -> bool:
    """A slice that (may) still reserves shares at the broker."""
    if slice_.state in SLICE_RESTING_STATES:
        return True
    return slice_.state in _FILLED_STATES and slice_.closed_at is None


def _remaining_qty(slice_: AllocatorProtectionOrder) -> float:
    return max(0.0, float(slice_.quantity) - float(slice_.filled_qty or 0.0))


def covered_quantity(slices: Iterable[AllocatorProtectionOrder]) -> float:
    return sum(_remaining_qty(s) for s in slices if _is_resting(s))


def _broker_id(s: AllocatorProtectionOrder) -> Optional[int]:
    """The id the broker knows this slice by: the complex-order id of an OCO, the plain order
    id of a stop-only slice. None while the placement has not reported back."""
    return s.sl_order_id if s.kind == ORDER_KIND_STOP else s.complex_order_id


def _describe(s: AllocatorProtectionOrder) -> str:
    if s.kind == ORDER_KIND_STOP:
        return f"stop-only order {s.sl_order_id}"
    return f"complex order {s.complex_order_id}"


def _read_slice(account, s: AllocatorProtectionOrder):
    """The broker's current state of one slice (a complex order, or a one-member wrapper for a
    stop-only order: both classify the same way). Raises on a failed read."""
    if s.kind == ORDER_KIND_STOP:
        return account.get_protective_order_state(s.sl_order_id)
    return account.get_complex_order_state(s.complex_order_id)


def _cancel_slice(account, s: AllocatorProtectionOrder):
    if s.kind == ORDER_KIND_STOP:
        return account.cancel_protective_stop(s.sl_order_id)
    return account.cancel_complex_order(s.complex_order_id)


# =========================================================================================
# alerts: log + activity log + persisted on the row; deduped on the code
# =========================================================================================

def _log(account_id: int, severity: ActivityLogSeverity, message: str, **data) -> None:
    try:
        log_activity(severity, ActivityLogType.TP_SL_ADJUSTED, f"[allocator TP/SL] {message}",
                     data={"kind": "allocator_protection", **data}, source_account_id=account_id)
    except Exception as e:  # noqa: BLE001 -- a log write may not mask the event it describes
        logger.error(f"allocator TP/SL: activity log write failed: {e}", exc_info=True)


def _alert(p: AllocatorProtection, code: str, message: str, *,
           severity: ActivityLogSeverity = ActivityLogSeverity.FAILURE) -> AllocatorProtection:
    """Raise a loud alert on ``p``. The same code is not re-written to the activity log on
    every refresh; it fires again when the code changes or after being cleared."""
    line = f"{p.symbol} (account {p.account_id}): {code}: {message}"
    if severity == ActivityLogSeverity.FAILURE:
        logger.error(f"allocator TP/SL ALERT {line}")
    else:
        logger.warning(f"allocator TP/SL alert {line}")
    if p.alert_code != code:
        _log(p.account_id, severity, f"{p.symbol}: {message}", code=code, symbol=p.symbol)
        p.alerted_at = _now()
    p.alert_code = code
    p.alert_message = message
    return _save(p)


def _clear_alert(p: AllocatorProtection, codes: Optional[Iterable[str]] = None) -> AllocatorProtection:
    """Clear the alert (only when its code is in ``codes`` if given)."""
    if p.alert_code is None:
        return p
    if codes is not None and p.alert_code not in set(codes):
        return p
    _log(p.account_id, ActivityLogSeverity.INFO, f"{p.symbol}: alert {p.alert_code} cleared",
         code="CLEARED", symbol=p.symbol, was=p.alert_code)
    p.alert_code = None
    p.alert_message = None
    return _save(p)


# =========================================================================================
# broker reads
# =========================================================================================

class BrokerReadError(RuntimeError):
    """A position or price could not be read. Never turned into a number."""


def _read_position(account, symbol: str) -> Tuple[float, bool]:
    """``(quantity, is_long)`` of ``symbol`` from a FRESH broker read; ``(0.0, True)`` when the
    broker is readable and holds none. Raises ``BrokerReadError`` when the read failed."""
    positions = account.get_positions()
    if positions is None:
        raise BrokerReadError(f"the position fetch for account {account.id} failed")
    for pos in positions:
        if _norm(pos.symbol) == symbol:
            qty = float(pos.qty)
            return qty, pos.side == OrderDirection.BUY
    return 0.0, True


def _read_average_cost(account, symbol: str) -> Optional[float]:
    """The broker's AVERAGE COST per share of ``symbol`` (what the presets are computed from),
    or None when the broker did not state one. Raises ``BrokerReadError`` when the position read
    failed. Never a fabricated number."""
    positions = account.get_positions()
    if positions is None:
        raise BrokerReadError(f"the position fetch for account {account.id} failed")
    for pos in positions:
        if _norm(pos.symbol) == symbol:
            avg = getattr(pos, "avg_entry_price", None)
            return float(avg) if avg is not None and float(avg) > 0 else None
    return None


def _read_price(account, symbol: str) -> float:
    fetched = account.get_instrument_current_price([symbol], price_type="mark")
    price = fetched.get(symbol) if isinstance(fetched, dict) else fetched
    if price is None or float(price) <= 0:
        raise BrokerReadError(f"no current price for {symbol}")
    return float(price)


def _tick_sizes(account, symbol: str):
    try:
        return account.equity_tick_sizes(symbol)
    except Exception as e:  # noqa: BLE001 -- falls back to the documented US-equity tick
        logger.warning(f"allocator TP/SL: tick table for {symbol} unavailable ({e}); "
                       f"using the default tick")
        return None


# =========================================================================================
# applying a broker observation to a slice
# =========================================================================================

def _apply_observation(account, p: AllocatorProtection, s: AllocatorProtectionOrder,
                       obs: SliceObservation) -> AllocatorProtection:
    """Write what the broker said onto the slice row and act on it (fill / alarm).

    Returns the (possibly updated) protection. The ONE place a fill becomes a weight reduction
    and a note and a lost order becomes an alert, so the reconcile and the cancel path cannot
    disagree. A fill does NOT exclude the symbol: the remaining orders keep protecting the rest
    and the next rebalance treats the symbol normally.
    """
    covered_before = covered_quantity(get_slices(p.id))
    s.tp_order_id = obs.tp_order_id or s.tp_order_id
    s.sl_order_id = obs.sl_order_id or s.sl_order_id
    if obs.gtc_date is not None:
        s.gtc_date = DateTime(obs.gtc_date.year, obs.gtc_date.month, obs.gtc_date.day)
        s.gtc_date_assumed = False
    s.detail = obs.detail
    s.state = obs.state
    if obs.state in _FILLED_STATES:
        s.filled_qty = obs.filled_qty
        s.fill_price = obs.fill_price
        if obs.remaining_live_qty <= 0 and s.closed_at is None:
            s.closed_at = _now()
    elif obs.state == SLICE_LIVE:
        s.cancel_requested = False if not s.cancel_requested else s.cancel_requested
    elif obs.state == SLICE_CANCELLED_BY_US:
        s.closed_at = s.closed_at or _now()
    _save(s)

    if obs.state in _FILLED_STATES:
        newly = max(0.0, float(obs.filled_qty) - float(s.weight_applied_qty or 0.0))
        if newly > 1e-9:
            p = _on_protective_fill(account, p, s, obs, newly, covered_before)
    elif obs.state in SLICE_ALARM_STATES:
        code = {SLICE_LOST_EXPIRED: CODE_LOST_EXPIRED, SLICE_LOST_CANCELLED: CODE_LOST_CANCELLED,
                SLICE_LOST_REJECTED: CODE_LOST_REJECTED}.get(obs.state, CODE_UNKNOWN_STATE)
        p = _alert(p, code, f"slice {s.slice_index + 1} ({s.quantity} sh, {_describe(s)}): "
                            f"{obs.detail}. The position is "
                            f"{'partly ' if covered_quantity(get_slices(p.id)) > 0 else ''}"
                            f"UNPROTECTED -- re-place protection or close the position.")
    return _reload(p.id)


def _fill_name(s: AllocatorProtectionOrder, obs: SliceObservation) -> Tuple[str, str]:
    """``(what, weight_reason)`` for a fill: ``("TP1 filled", tp_fill)`` or ``("SL hit", sl_fill)``."""
    if obs.kind == KIND_TP and s.kind == ORDER_KIND_OCO:
        label = f"TP{s.target_index + 1} filled" if s.target_index >= 0 else "TP filled"
        return label, WEIGHT_REASON_TP_FILL
    return "SL hit", WEIGHT_REASON_SL_FILL


def _on_protective_fill(account, p: AllocatorProtection, s: AllocatorProtectionOrder,
                        obs: SliceObservation, newly: float,
                        covered_before: float) -> AllocatorProtection:
    """A protective order sold ``newly`` shares: reduce the symbol's allocator weight in
    proportion (``new = old x remaining protected qty / protected qty before``; 0 when the
    protection exited the position), keep the platform's own records in step, write the note.

    The freed share is NOT given to the label's other symbols: it stays unallocated until the
    operator reassigns it. Every automatic change is written to ``allocator_weight_change``.
    """
    what, reason = _fill_name(s, obs)
    remaining = max(0.0, covered_before - newly)
    factor = min(1.0, max(0.0, remaining / covered_before)) if covered_before > 1e-9 else 1.0
    detail = (f"{what}: {newly:g} of {covered_before:g} protected sh sold"
              + (f" @ {obs.fill_price:g}" if obs.fill_price is not None else ""))
    try:
        changes = reduce_symbol_weights(p.account_id, p.symbol, factor, reason=reason, detail=detail)
    except Exception as e:  # noqa: BLE001 -- the fill is real whatever the weight write does
        logger.error(f"allocator TP/SL: weight reduction for {p.symbol} failed: {e}", exc_info=True)
        _log(p.account_id, ActivityLogSeverity.FAILURE,
             f"{p.symbol}: {what}, but its allocator weight could not be reduced ({e}); "
             f"lower it by hand or the next rebalance may buy it back", code="WEIGHT_FAILED",
             symbol=p.symbol)
        changes = []
    note = f"{what} {_now():%Y-%m-%d}"
    if changes:
        if len({c.label for c in changes}) == 1:
            note += f": share {changes[0].before_pct:g}% -> {changes[0].after_pct:g}%"
        else:
            note += ": " + "; ".join(f"{c.label} share {c.before_pct:g}% -> {c.after_pct:g}%"
                                     for c in changes)
    s.weight_applied_qty = float(s.weight_applied_qty or 0.0) + newly
    _save(s)
    _shrink_transactions(p.account_id, p.symbol, newly, obs.fill_price)
    p.last_fill_at = _now()
    p.last_fill_note = note
    _save(p)
    _log(p.account_id, ActivityLogSeverity.WARNING,
         f"{p.symbol}: {note}. The remaining protective orders keep protecting the rest; the "
         f"freed share stays unallocated and the next rebalance treats the symbol normally.",
         code=CODE_FILL, symbol=p.symbol, kind=obs.kind, slice=s.slice_index,
         filled_qty=newly, fill_price=obs.fill_price, weight_factor=factor)
    logger.warning(f"allocator TP/SL: {p.symbol} {note}")
    return _reload(p.id)


def _shrink_transactions(account_id: int, symbol: str, quantity: float,
                         price: Optional[float]) -> None:
    """Keep the platform's open Transactions in step with a protective sale, FIFO.

    A protective fill sells shares the platform never ordered, so without this the open
    Transaction rows still carry the pre-fill quantity and the NEXT rebalance (which splits its
    delta across them) would size against shares that are gone. A transaction sold in full is
    closed at the fill price; a partly sold one is reduced. Never raises (the fill is real
    whatever the bookkeeping does); a failure is logged loudly.
    """
    try:
        from .portfolio_allocation_service import _open_transaction_ids
        from .utils import close_transaction_with_logging
        ids = _open_transaction_ids(account_id, [symbol]).get(symbol, [])
        remaining = float(quantity)
        for txn_id in ids:
            if remaining <= 1e-9:
                break
            txn = get_instance(Transaction, txn_id)
            have = float(txn.quantity or 0.0)
            if have <= 1e-9:
                continue
            if remaining + 1e-9 >= have:
                remaining -= have
                if price is not None:
                    txn.close_price = float(price)
                close_transaction_with_logging(txn, account_id, "tp_sl_filled")
            else:
                txn.quantity = have - remaining
                remaining = 0.0
            update_instance(txn)
        if remaining > 1e-6:
            logger.warning(f"allocator TP/SL: {symbol}: {remaining:g} sh of a protective fill had no "
                           f"open transaction to take it from")
    except Exception as e:  # noqa: BLE001
        logger.error(f"allocator TP/SL: could not update the transactions of {symbol} after a "
                     f"protective fill: {e}", exc_info=True)
        _log(account_id, ActivityLogSeverity.FAILURE,
             f"{symbol}: a protective order sold {quantity:g} sh but the open transactions could "
             f"not be reduced ({e}); reconcile them before rebalancing", code="TXN_FAILED",
             symbol=symbol)


def _ack_alarm_slices(p: AllocatorProtection) -> None:
    """Mark alarm slices as superseded (a fresh placement attempt replaces them)."""
    for s in get_slices(p.id):
        if s.state in SLICE_ALARM_STATES and s.closed_at is None:
            s.closed_at = _now()
            s.detail = f"{s.detail or ''} (superseded by a new placement)".strip()
            _save(s)


# =========================================================================================
# cancelling live slices (confirmed)
# =========================================================================================

@dataclass
class CancelResult:
    all_confirmed: bool = True
    filled: bool = False
    detail: List[str] = field(default_factory=list)


def _cancel_live_slices(account, p: AllocatorProtection) -> Tuple[AllocatorProtection, CancelResult]:
    """Cancel every resting slice of ``p`` and require the broker to CONFIRM each.

    A fill that lands before the cancel does is applied as a fill (weight reduced). An unconfirmed
    cancel leaves the slice CANCELLING, raises CANCEL_UNCONFIRMED and returns
    ``all_confirmed=False``: the shares may still be reserved, so the caller must not trade.
    """
    result = CancelResult()
    for s in get_slices(p.id):
        if not _is_resting(s):
            continue
        if _broker_id(s) is None:
            # PLACING with no broker id: an order may exist that we cannot name.
            s.state = SLICE_UNKNOWN
            s.detail = "a placement never reported back; an order may exist at the broker"
            _save(s)
            result.all_confirmed = False
            result.detail.append(f"slice {s.slice_index + 1} has no broker id")
            p = _alert(p, CODE_UNKNOWN_STATE,
                       f"slice {s.slice_index + 1} was being placed and never reported back; "
                       f"check the TastyTrade site for a GTC order on {p.symbol}")
            continue
        s.cancel_requested = True
        s.state = SLICE_CANCELLING
        _save(s)
        outcome = _cancel_slice(account, s)
        if not outcome.confirmed:
            result.all_confirmed = False
            result.detail.append(f"slice {s.slice_index + 1}: {outcome.detail}")
            p = _alert(p, CODE_CANCEL_UNCONFIRMED,
                       f"could not confirm the cancel of {_describe(s)} "
                       f"({s.quantity} sh): {outcome.detail}. The orders may still be live; "
                       f"check the TastyTrade site.")
            continue
        obs = classify_complex_order(outcome.final, slice_quantity=s.quantity,
                                     we_requested_cancel=True)
        if obs.state in _FILLED_STATES:
            result.filled = True
        p = _apply_observation(account, p, s, obs)
    return _reload(p.id), result


# =========================================================================================
# placing slices
# =========================================================================================

@dataclass
class PlaceResult:
    placed: int = 0
    shares_covered: int = 0
    errors: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def _place_slices(account, p: AllocatorProtection) -> Tuple[AllocatorProtection, PlaceResult]:
    """Place the OCOs for whatever whole-share quantity is not covered yet.

    Reads a FRESH position and price, validates against them (a price that moved through the
    stop or a target while orders were cancelled is REFUSED, never traded through), plans the
    slices and places them one by one. A slice row is written PLACING before its broker call.
    Placed slices are KEPT when a later one is refused (partial protection beats none): the
    refusal raises PLACEMENT_REFUSED and the status is PARTIAL/UNPROTECTED.
    """
    result = PlaceResult()
    targets = targets_from_dicts(p.tp_targets)
    try:
        qty, is_long = _read_position(account, p.symbol)
        last = _read_price(account, p.symbol)
    except BrokerReadError as e:
        result.errors.append(str(e))
        return _alert(p, CODE_PLACEMENT_REFUSED, f"protection not placed: {e}"), result
    if not is_long and qty > 0:
        msg = f"{p.symbol} is a SHORT position ({qty:g}); protection supports long equity only"
        result.errors.append(msg)
        return _alert(p, CODE_PLACEMENT_REFUSED, msg), result

    _ack_alarm_slices(p)
    covered = covered_quantity(get_slices(p.id))
    to_cover = whole_shares(qty) - int(round(covered))
    if to_cover <= 0:
        return _reload(p.id), result

    ticks = _tick_sizes(account, p.symbol)
    errors = validate_protection(sl_price=p.sl_price, targets=targets, last_price=last,
                                 position_quantity=float(to_cover), tick_sizes=ticks)
    if errors:
        result.errors.extend(errors)
        p.last_error = " ".join(errors)
        _save(p)
        return _alert(p, CODE_PLACEMENT_REFUSED,
                      "protection not placed: " + " ".join(errors)), result

    plans, notes = plan_slices(shares=to_cover, targets=targets, sl_price=p.sl_price,
                               last_price=last, tick_sizes=ticks)
    result.notes.extend(notes)
    existing = len(get_slices(p.id))
    for plan in plans:
        index = existing + plan.slice_index
        tag = f"ba2prot:{p.id}:{index}"
        row = _save(AllocatorProtectionOrder(
            protection_id=p.id, slice_index=index, target_index=plan.target_index,
            kind=plan.kind, quantity=plan.quantity, tp_price=plan.tp_price,
            sl_price=plan.sl_price, external_tag=tag, state=SLICE_PLACING, placed_at=_now()))
        try:
            if plan.kind == ORDER_KIND_STOP:
                placed = account.place_protective_stop(
                    symbol=p.symbol, quantity=plan.quantity, sl_price=plan.sl_price, tag=tag)
            else:
                placed = account.place_protective_oco(
                    symbol=p.symbol, quantity=plan.quantity, tp_price=plan.tp_price,
                    sl_price=plan.sl_price, tag=tag)
        except ProtectionRefused as e:
            row.state = SLICE_LOST_REJECTED
            row.detail = str(e)[:500]
            _save(row)
            result.errors.append(str(e))
            continue
        except Exception as e:  # noqa: BLE001 -- the call's outcome is unknown: alarm, do not guess
            row.state = SLICE_UNKNOWN
            row.detail = f"placement raised {type(e).__name__}: {e}"[:500]
            _save(row)
            result.errors.append(f"placement of slice {index + 1} raised {type(e).__name__}: {e}; "
                                 f"an order may exist at the broker")
            logger.error(f"allocator TP/SL: placement raised for {p.symbol}: {e}", exc_info=True)
            continue
        if plan.kind == ORDER_KIND_STOP:
            row.sl_order_id = placed.order_id
            row.sl_price = placed.sl_price
        else:
            row.complex_order_id = placed.complex_order_id
            row.tp_order_id = placed.tp_order_id
            row.sl_order_id = placed.sl_order_id
            row.tp_price, row.sl_price = placed.tp_price, placed.sl_price
        row.state = SLICE_LIVE
        if placed.gtc_date is not None:
            row.gtc_date = DateTime(placed.gtc_date.year, placed.gtc_date.month,
                                    placed.gtc_date.day)
            row.gtc_date_assumed = False
        else:
            row.gtc_date = _now() + timedelta(days=ASSUMED_GTC_LIFETIME_DAYS)
            row.gtc_date_assumed = True
        row.placed_at = _now()
        _save(row)
        result.placed += 1
        result.shares_covered += plan.quantity
        if plan.kind == ORDER_KIND_STOP:
            _log(p.account_id, ActivityLogSeverity.SUCCESS,
                 f"{p.symbol}: placed protective stop {placed.order_id}: sell {plan.quantity} sh "
                 f"on a stop at {plan.sl_price:g} (GTC, no take-profit)",
                 code="PLACED", symbol=p.symbol, order_id=placed.order_id,
                 quantity=plan.quantity, sl=plan.sl_price)
        else:
            _log(p.account_id, ActivityLogSeverity.SUCCESS,
                 f"{p.symbol}: placed protective OCO {placed.complex_order_id}: sell "
                 f"{plan.quantity} sh @ {plan.tp_price:g} or stop {plan.sl_price:g} (GTC)",
                 code="PLACED", symbol=p.symbol, complex_order_id=placed.complex_order_id,
                 quantity=plan.quantity, tp=plan.tp_price, sl=plan.sl_price)

    p = _reload(p.id)
    if result.errors:
        p.last_error = " | ".join(result.errors)[:1000]
        p = _alert(p, CODE_PLACEMENT_REFUSED,
                   f"{result.placed} of {len(plans)} protective order(s) placed; refused: "
                   f"{' | '.join(result.errors)}")
    else:
        p.last_error = None
        p.protected_quantity = covered_quantity(get_slices(p.id))
        _save(p)
        p = _clear_alert(p)
    return _reload(p.id), result


# =========================================================================================
# operator actions
# =========================================================================================

@dataclass
class ActionResult:
    ok: bool
    message: str
    errors: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)


def save_protection(account, symbol: str, sl_price: float,
                    targets: List[TpTarget]) -> ActionResult:
    """Validate, store and PLACE a protection (the dialog's Save). Real orders on success.

    Refused (nothing written, nothing sent) on any validation error and while a rebalance
    re-placement is pending. If orders already exist they are
    cancelled with confirmation first; an unconfirmed cancel aborts before anything new is sent.
    """
    symbol = _norm(symbol)
    with protection_lock(account.id):
        p = get_protection(account.id, symbol)
        if p is not None and p.pending_replace:
            return ActionResult(False, f"{symbol}: protective orders are being re-placed after a "
                                       f"rebalance; wait for that to finish.")
        try:
            qty, is_long = _read_position(account, symbol)
            last = _read_price(account, symbol)
        except BrokerReadError as e:
            return ActionResult(False, f"Cannot validate: {e}. Refresh and try again.")
        if not is_long and qty > 0:
            return ActionResult(False, f"{symbol} is a short position; protection supports long "
                                       f"equity only.")
        ticks = _tick_sizes(account, symbol)
        errors = validate_protection(sl_price=sl_price, targets=targets, last_price=last,
                                     position_quantity=qty, tick_sizes=ticks)
        if errors:
            return ActionResult(False, "The TP/SL is not valid.", errors=errors)

        if p is None:
            p = _save(AllocatorProtection(account_id=account.id, symbol=symbol))
        p.enabled = True
        p.sl_price = float(sl_price)
        p.tp_targets = [t.to_dict() for t in targets]
        p.last_error = None
        _save(p)

        p, cancelled = _cancel_live_slices(account, p)
        if not cancelled.all_confirmed:
            return ActionResult(False, f"{symbol}: the existing protective orders could not be "
                                       f"confirmed cancelled, so nothing new was placed.",
                                errors=cancelled.detail)
        p, placed = _place_slices(account, p)
        if placed.ok:
            p.pending_replace = False
            _save(p)
            return ActionResult(True, f"{symbol}: {placed.placed} protective order(s) placed "
                                      f"covering {placed.shares_covered} share(s).",
                                notes=placed.notes + _fraction_note(qty))
        return ActionResult(False, f"{symbol}: protection was NOT fully placed.",
                            errors=placed.errors, notes=placed.notes)


def _fraction_note(qty: float) -> List[str]:
    rest = fractional_remainder(qty)
    return [f"{rest:g} fractional share(s) cannot carry a limit or stop on TastyTrade and "
            f"stay unprotected."] if rest > 1e-9 else []


def disable_protection(account, symbol: str) -> ActionResult:
    """Switch protection OFF: cancel every live order (confirmed), keep the numbers."""
    symbol = _norm(symbol)
    with protection_lock(account.id):
        p = get_protection(account.id, symbol)
        if p is None:
            return ActionResult(True, f"{symbol}: protection was already off.")
        p.enabled = False
        p.pending_replace = False
        p.pending_replace_since = None
        _save(p)
        p, cancelled = _cancel_live_slices(account, p)
        if not cancelled.all_confirmed:
            return ActionResult(False, f"{symbol}: switched off, but the cancel of the protective "
                                       f"orders was NOT confirmed -- they may still be live.",
                                errors=cancelled.detail)
        _ack_alarm_slices(p)
        _clear_alert(p)
        _log(account.id, ActivityLogSeverity.INFO, f"{symbol}: protection switched off",
             code="DISABLED", symbol=symbol)
        return ActionResult(True, f"{symbol}: protection switched off, orders cancelled.")


def delete_protection(account, symbol: str) -> ActionResult:
    """Switch off and forget the configuration (only once every cancel is confirmed)."""
    symbol = _norm(symbol)
    with protection_lock(account.id):
        result = disable_protection(account, symbol)
        if not result.ok:
            return result
        p = get_protection(account.id, symbol)
        if p is not None:
            with get_db() as session:
                for s in session.exec(select(AllocatorProtectionOrder).where(
                        AllocatorProtectionOrder.protection_id == p.id)).all():
                    session.delete(s)
                session.delete(session.get(AllocatorProtection, p.id))
                session.commit()
        return ActionResult(True, f"{symbol}: protection removed.")


def replace_protection(account, symbol: str) -> ActionResult:
    """Resize / re-place: cancel what is live (confirmed) and place a fresh set from the stored
    numbers at the CURRENT held quantity. Renews the GTC lifetime and repairs a lost, partial or
    size-mismatched protection."""
    symbol = _norm(symbol)
    with protection_lock(account.id):
        p = get_protection(account.id, symbol)
        if p is None or not p.enabled:
            return ActionResult(False, f"{symbol}: protection is not enabled.")
        p, cancelled = _cancel_live_slices(account, p)
        if not cancelled.all_confirmed:
            return ActionResult(False, f"{symbol}: the cancel was not confirmed; nothing re-placed.",
                                errors=cancelled.detail)
        p, placed = _place_slices(account, p)
        if placed.ok:
            p.pending_replace = False
            _save(p)
            return ActionResult(True, f"{symbol}: {placed.placed} protective order(s) placed.",
                                notes=placed.notes)
        return ActionResult(False, f"{symbol}: protection was NOT fully placed.",
                            errors=placed.errors, notes=placed.notes)


# =========================================================================================
# reconcile
# =========================================================================================

@dataclass
class ReconcileReport:
    checked: int = 0
    new_fills: List[str] = field(default_factory=list)
    alarms: List[str] = field(default_factory=list)
    failed_symbols: List[str] = field(default_factory=list)
    resumed: List[str] = field(default_factory=list)
    extended: List[str] = field(default_factory=list)


def _working_order_symbols(account_id: int, symbols: Iterable[str]) -> Set[str]:
    """Symbols that still have a non-terminal platform order (an allocator buy/sell in flight)."""
    wanted = {_norm(s) for s in symbols}
    if not wanted:
        return set()
    terminal = set(OrderStatus.get_terminal_statuses()) | {OrderStatus.FILLED}
    with get_db() as session:
        rows = session.exec(select(TradingOrder).where(
            TradingOrder.account_id == int(account_id),
            TradingOrder.symbol.in_(list(wanted)))).all()
    return {_norm(o.symbol) for o in rows if o.status not in terminal}


def _run_in_flight(account) -> bool:
    """True when an allocator run holds this account's submission lock right now (best effort:
    the answer can change a microsecond later, which is why every placement path is also safe
    against a run starting after it -- the run's prepare cancels whatever is live)."""
    from .portfolio_allocation_service import _submission_lock
    lock = _submission_lock(account.id)
    if lock.acquire(blocking=False):
        lock.release()
        return False
    return True


def _reconcile_one(account, p: AllocatorProtection, report: ReconcileReport,
                   position: Optional[Tuple[float, bool]], may_place: bool = False) -> None:
    """Reconcile one protection with the broker.

    ``may_place`` True lets it ADD protection for shares nobody covers (see the growth rule
    below); ``prepare_for_trade`` calls it False because it is about to cancel everything anyway.
    """
    fills_before = p.last_fill_at
    slices = get_slices(p.id)
    for s in slices:
        if s.state not in _RECONCILABLE_STATES or (s.closed_at is not None and s.state != SLICE_UNKNOWN):
            continue
        if _broker_id(s) is None:
            age = (_now() - s.placed_at).total_seconds() if s.placed_at else 0
            if s.state == SLICE_PLACING and age > PLACING_STALE_SECONDS:
                obs = SliceObservation(
                    state=SLICE_UNKNOWN,
                    detail="a placement never reported back; an order may exist at the broker "
                           f"(tag {s.external_tag})")
                p = _apply_observation(account, p, s, obs)
                report.alarms.append(p.symbol)
            continue
        try:
            placed = _read_slice(account, s)
        except Exception as e:  # noqa: BLE001 -- a failed READ never flips a state
            report.failed_symbols.append(p.symbol)
            p = _alert(p, CODE_RECONCILE_FETCH_FAILED,
                       f"could not read {_describe(s)}: {e}; its state is "
                       f"UNCHANGED and unverified",
                       severity=ActivityLogSeverity.WARNING)
            break    # the broker is unreachable: one report per symbol, not one per slice
        report.checked += 1
        obs = classify_complex_order(placed, slice_quantity=s.quantity,
                                     we_requested_cancel=bool(s.cancel_requested))
        before = s.state
        p = _apply_observation(account, p, s, obs)
        if obs.state in SLICE_ALARM_STATES and before != obs.state:
            report.alarms.append(p.symbol)
    if p.last_fill_at != fills_before:
        report.new_fills.append(p.symbol)

    p = _reload(p.id)
    slices = get_slices(p.id)
    if p.symbol in report.failed_symbols:
        # The broker could not be read: nothing below (the quantity check, clearing alerts) may
        # draw a conclusion from state we could not verify.
        return
    if p.alert_code == CODE_RECONCILE_FETCH_FAILED:
        p = _clear_alert(p, [CODE_RECONCILE_FETCH_FAILED])

    live = covered_quantity(slices)
    if p.protected_quantity != live:
        p.protected_quantity = live
        _save(p)

    # GTC expiry warning, once per slice.
    for s in slices:
        if _is_resting(s) and not s.gtc_warned and gtc_expiry_due(s.gtc_date):
            s.gtc_warned = True
            _save(s)
            p = _alert(p, CODE_GTC_EXPIRING,
                       f"slice {s.slice_index + 1} ({_describe(s)}) is "
                       f"GTC until {s.gtc_date:%Y-%m-%d}"
                       f"{' (assumed, the broker gave no date)' if s.gtc_date_assumed else ''}; "
                       f"re-place protection to renew it",
                       severity=ActivityLogSeverity.WARNING)

    has_alarm = any(x.state in SLICE_ALARM_STATES and x.closed_at is None for x in slices)
    if (not p.enabled and p.alert_code and not any(_is_resting(x) for x in slices)
            and not has_alarm):
        # Switched off and every order is gone: an alert about orders that no longer exist
        # (an unconfirmed cancel that has since landed) is stale.
        p = _clear_alert(p)
    if not p.enabled or position is None:
        return
    qty, is_long = position
    n_whole = whole_shares(qty) if is_long else 0
    if p.pending_replace:
        if n_whole > 0 and abs(live - n_whole) < 1e-9:
            p.pending_replace = False
            p.pending_replace_since = None
            _save(p)
            _clear_alert(p, [CODE_QUANTITY_MISMATCH, CODE_CANCEL_UNCONFIRMED])
        return
    if has_alarm:
        return

    # GROWTH: whole shares nobody covers (a manual buy, a dividend reinvestment, shares bought back
    # after the stop fired). Protection is ADDED for exactly those shares -- no cancel, so there
    # is NO gap in which the existing shares are unprotected. Not while an allocator run is in
    # flight or an order on the symbol is still working (the position is not final then).
    if n_whole > live + 1e-9:
        if not may_place or _working_order_symbols(account.id, [p.symbol]):
            return
        p, placed = _place_slices(account, p)
        if placed.placed:
            report.extended.append(p.symbol)
            _log(account.id, ActivityLogSeverity.INFO,
                 f"{p.symbol}: the position grew outside the allocator; added protection for "
                 f"{placed.shares_covered} sh (the existing orders were not touched)",
                 code="EXTENDED", symbol=p.symbol, added=placed.shares_covered)
        if not placed.ok:
            report.alarms.append(p.symbol)
        elif p.alert_code and p.alert_code != CODE_GTC_EXPIRING:
            _clear_alert(p)
        return
    if abs(live - n_whole) > 1e-9:
        # SHRINK or a mismatch the platform will not auto-fix: the orders cover MORE shares than
        # are held. Resizing means cancelling live orders (a gap), so it is the operator's
        # one-click Resize protection, not something a background job does.
        _alert(p, CODE_QUANTITY_MISMATCH,
               f"live protective orders cover {live:g} sh but {n_whole} whole share(s) are held; "
               f"use Resize protection to re-place them at the held quantity",
               severity=ActivityLogSeverity.FAILURE)
        report.alarms.append(p.symbol)
    elif p.alert_code and p.alert_code != CODE_GTC_EXPIRING:
        # Matched (or nothing to protect) and no alarm slice: every open alert but the GTC
        # expiry warning (which only a renewal resolves) is stale.
        _clear_alert(p)


def _resume_unless_run_in_progress(account, symbols: List[str]) -> List[str]:
    """The background reconcile may complete a ``pending_replace`` only when NO allocator run is
    in flight: between a run's cancel and its first order a re-placement would reserve the very
    shares the run is about to sell. The run's own ``resume_protection`` is the normal path."""
    if _run_in_flight(account):
        logger.info(f"allocator TP/SL: account {account.id} has an allocator run in flight; "
                    f"re-placement left to the run")
        return []
    return resume_protection(account, symbols)


def reconcile_account(account) -> ReconcileReport:
    """Reconcile every protection of ``account`` with the broker. Never raises.

    Detects fills (note + weight reduction), lost/expired/cancelled orders (-> alarm), size
    mismatches, GTC expiry; completes a ``pending_replace`` that WE started once the symbol has no
    working platform order; ADDS protection for shares that appeared outside the allocator
    (growth, no cancel). Does NOT re-place a lost order (the operator may have cancelled it on the
    broker's site on purpose) and does NOT resize a shrink: it alerts, and the page offers
    Resize protection.
    """
    report = ReconcileReport()
    if getattr(account, "supports_allocator_protection", False) is not True:
        return report
    try:
        protections = [p for p in list_protections(account.id)]
        if not protections:
            return report
        with protection_lock(account.id):
            try:
                positions = account.get_positions()
            except Exception as e:  # noqa: BLE001
                logger.error(f"allocator TP/SL: position read failed: {e}", exc_info=True)
                positions = None
            by_symbol: Dict[str, Tuple[float, bool]] = {}
            if positions is not None:
                for pos in positions:
                    by_symbol[_norm(pos.symbol)] = (float(pos.qty), pos.side == OrderDirection.BUY)
            may_place = not _run_in_flight(account)
            for p in protections:
                try:
                    position = None if positions is None else by_symbol.get(p.symbol, (0.0, True))
                    _reconcile_one(account, _reload(p.id), report, position, may_place)
                except Exception as e:  # noqa: BLE001 -- one symbol must not cost the others
                    report.failed_symbols.append(p.symbol)
                    logger.error(f"allocator TP/SL: reconcile of {p.symbol} failed: {e}",
                                 exc_info=True)
                    _log(account.id, ActivityLogSeverity.FAILURE,
                         f"{p.symbol}: reconcile raised {type(e).__name__}: {e}",
                         code="RECONCILE_ERROR", symbol=p.symbol)
            report.resumed = _resume_unless_run_in_progress(account, [p.symbol for p in protections])
    except Exception as e:  # noqa: BLE001
        logger.error(f"allocator TP/SL: reconcile of account {account.id} failed: {e}",
                     exc_info=True)
    return report


# =========================================================================================
# the allocator run boundary
# =========================================================================================

@dataclass
class PrepareResult:
    """What ``prepare_for_trade`` tells the run.

    ``blocked`` -- ``{symbol: reason}`` the run must DROP and report FAILED (protective orders
    could not be cancelled, so shares are still reserved).
    ``pending`` -- symbols whose protection was cancelled/armed and needs ``resume_protection``.
    """
    blocked: Dict[str, str] = field(default_factory=dict)
    pending: List[str] = field(default_factory=list)


def has_protection(account) -> bool:
    return getattr(account, "supports_allocator_protection", False) is True


def prepare_for_trade(account, symbols: Iterable[str]) -> PrepareResult:
    """Before the allocator trades ``symbols``: reconcile, cancel (confirmed), mark pending.

    Called under the allocator's submission lock, after the run's gates passed and before the
    plan is recorded. Every protected (enabled) symbol the plan touches has ALL its protective
    orders (OCOs and stop-only) cancelled and the cancel CONFIRMED by the broker; a symbol whose
    cancel cannot be confirmed is ``blocked`` (its shares may still be reserved, so the run must
    not trade it). ``pending_replace`` is written BEFORE the first cancel, so a crash between
    cancel and re-placement leaves a durable marker. A protection never excludes the symbol.
    """
    result = PrepareResult()
    if not has_protection(account):
        return result
    with protection_lock(account.id):
        for symbol in sorted({_norm(s) for s in symbols}):
            p = get_protection(account.id, symbol)
            if p is None:
                continue
            if any(_is_resting(x) for x in get_slices(p.id)):
                _reconcile_one(account, p, ReconcileReport(), None, may_place=False)
                p = _reload(p.id)
            if not p.enabled:
                continue
            p.pending_replace = True
            p.pending_replace_since = _now()
            _save(p)
            p, cancelled = _cancel_live_slices(account, p)
            if not cancelled.all_confirmed:
                result.blocked[symbol] = (
                    "its protective TP/SL orders could not be confirmed cancelled, so the shares "
                    "may still be reserved at the broker (" + "; ".join(cancelled.detail) + ")")
                continue
            result.pending.append(symbol)
    return result


def resume_protection(account, symbols: Iterable[str], *,
                      working_symbols: Optional[Set[str]] = None) -> List[str]:
    """Re-place protection for symbols flagged ``pending_replace``. Never raises.

    ``working_symbols`` are symbols with an order still working (their position is not final):
    they stay REPLACING. When ``None`` the DB is asked. A re-placement the broker or the
    validation refuses clears ``pending_replace`` (no endless retry), raises REPLACE_FAILED and
    leaves the symbol UNPROTECTED for the operator.
    """
    resumed: List[str] = []
    if not has_protection(account):
        return resumed
    try:
        with protection_lock(account.id):
            wanted = sorted({_norm(s) for s in symbols})
            busy = working_symbols if working_symbols is not None else \
                _working_order_symbols(account.id, wanted)
            for symbol in wanted:
                p = get_protection(account.id, symbol)
                if p is None or not p.pending_replace or not p.enabled:
                    continue
                if symbol in busy:
                    continue
                try:
                    qty, _ = _read_position(account, symbol)
                except BrokerReadError as e:
                    _alert(p, CODE_REPLACE_FAILED, f"re-placement postponed: {e}")
                    continue
                if whole_shares(qty) < 1:
                    # The rebalance sold the whole position (or left only a fractional remainder):
                    # protection is inactive (status 'no position', config kept) and is placed
                    # again when the symbol is bought. Not an alarm; pending_replace is done.
                    p.pending_replace = False
                    p.pending_replace_since = None
                    _save(p)
                    continue
                p, placed = _place_slices(account, p)
                p.pending_replace = False
                p.pending_replace_since = None
                _save(p)
                if placed.ok:
                    resumed.append(symbol)
                else:
                    _alert(p, CODE_REPLACE_FAILED,
                           "protection was NOT re-placed after the trade: "
                           + " | ".join(placed.errors))
    except Exception as e:  # noqa: BLE001 -- never raises into the allocator run
        logger.error(f"allocator TP/SL: resume failed: {e}", exc_info=True)
        _log(account.id, ActivityLogSeverity.FAILURE,
             f"re-placement after the trade raised {type(e).__name__}: {e}", code="REPLACE_FAILED")
    return resumed


# =========================================================================================
# status for the page (DB only; the position comes from the caller)
# =========================================================================================

def status_for(p: Optional[AllocatorProtection], position_quantity: Optional[float],
               slices: Optional[List[AllocatorProtectionOrder]] = None) -> ProtectionStatus:
    """The page status of one symbol. ``p`` None -> OFF. Pure over its arguments."""
    if p is None:
        return protection_status(enabled=False, pending_replace=False, pending_replace_since=None,
                                 slice_states=[], position_quantity=position_quantity)
    rows = slices if slices is not None else get_slices(p.id)
    # A slice that part-filled and still rests counts as LIVE for the shares it still reserves.
    states = [(SLICE_LIVE if s.state in _FILLED_STATES else s.state, _remaining_qty(s)) for s in rows
              if _is_resting(s) or (s.state in SLICE_ALARM_STATES and s.closed_at is None)]
    return protection_status(
        enabled=p.enabled, pending_replace=p.pending_replace,
        pending_replace_since=p.pending_replace_since, slice_states=states,
        position_quantity=position_quantity, alert_message=p.alert_message,
        last_fill_note=p.last_fill_note)


def statuses_for_account(account_id: int, quantities: Dict[str, float]
                         ) -> Dict[str, Tuple[AllocatorProtection, ProtectionStatus]]:
    """``{symbol: (protection, status)}`` for every protection of the account. DB only."""
    out: Dict[str, Tuple[AllocatorProtection, ProtectionStatus]] = {}
    for p in list_protections(account_id):
        out[p.symbol] = (p, status_for(p, quantities.get(p.symbol, 0.0)))
    return out


def open_alerts(account_id: int) -> List[AllocatorProtection]:
    """Protections with an open (non-GTC-warning) alert, for the page banner."""
    return [p for p in list_protections(account_id)
            if p.alert_code and p.alert_code != CODE_GTC_EXPIRING]


# Public names for the UI (the underscored ones stay the implementation).
read_position = _read_position
read_price = _read_price
read_average_cost = _read_average_cost
read_tick_sizes = _tick_sizes
