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
import uuid
from dataclasses import dataclass, field
from datetime import datetime as DateTime, timezone
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from sqlmodel import select

from ..logger import logger
from .allocator_exclusion import reduce_symbol_weights
from .allocator_protection import (
    AUTO_ACTION_MIN_INTERVAL_SECONDS, AUTO_FAILURE_LIMIT, UNKNOWN_MIN_AGE_SECONDS, CODE_SALE_SETTLING,
    effective_targets, plan_add_only, target_taken, is_margin_refusal, margin_sentence, round_price_to_tick,
    tick_for_price, CODE_AUTO_STOPPED, no_acceptable_stop_sentence,
    CODE_CANCEL_UNCONFIRMED, CODE_FILL, CODE_LOST_CANCELLED, CODE_LOST_EXPIRED, CODE_LOST_REJECTED,
    CODE_PLACEMENT_REFUSED, CODE_QUANTITY_MISMATCH, CODE_RECONCILE_FETCH_FAILED, CODE_REPLACE_FAILED,
    CODE_REPLACE_STALE, CODE_UNKNOWN_STATE, FILL_SETTLE_SECONDS, GTC_RENEW_DAYS, KIND_SL, KIND_TP,
    PENDING_REPLACE_STALE_SECONDS, PLACING_STALE_SECONDS, PlacementOutcomeUnknown, ProtectionRefused,
    ProtectionStatus, SliceObservation, TpTarget, WARNING_CODES, classify_complex_order,
    protection_status, targets_from_dicts, validate_protection, whole_shares, plan_slices,
    fractional_remainder,
)
from .allocator_protection_models import (
    AllocatorProtection, AllocatorProtectionOrder, ORDER_KIND_OCO, ORDER_KIND_STOP,
    SLICE_ALARM_STATES, SLICE_CANCELLED_BY_US, SLICE_CANCELLING, SLICE_FILLED_SL,
    SLICE_FILLED_TP, SLICE_LIVE, SLICE_LOST_CANCELLED, SLICE_LOST_EXPIRED, SLICE_LOST_REJECTED,
    SLICE_PLACING, SLICE_RESTING_STATES, SLICE_UNKNOWN, WEIGHT_REASON_PINNED, WEIGHT_REASON_SL_FILL,
    WEIGHT_REASON_TP_FILL,
)
from .db import add_instance, get_db, get_instance, log_activity, update_instance
from .models import TradingOrder, Transaction
from .types import (
    ActivityLogSeverity, ActivityLogType, OrderDirection, OrderOpenType, OrderStatus, OrderType,
)

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


def _blocks(slice_: AllocatorProtectionOrder) -> bool:
    """A slice that BLOCKS any trade or new placement on the symbol: it rests (or may rest) at the
    broker, or its outcome is UNKNOWN (an order may exist that we cannot name). UNKNOWN never
    counts as COVERED shares (``covered_quantity``), but it is never ignored either."""
    return _is_resting(slice_) or (slice_.state == SLICE_UNKNOWN and slice_.closed_at is None)


def _remaining_qty(slice_: AllocatorProtectionOrder) -> float:
    return max(0.0, float(slice_.quantity) - float(slice_.filled_qty))   # both NOT NULL


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
    if (p.alert_code and p.alert_code != code and p.alert_code not in WARNING_CODES
            and code in WARNING_CODES):
        # NEVER overwrite a failure-class alert with a warning: the warning is logged, the red
        # alert stays on the row until it is resolved.
        logger.warning(f"allocator TP/SL alert (kept {p.alert_code}) {line}")
        _log(p.account_id, severity, f"{p.symbol}: {message}", code=code, symbol=p.symbol)
        return p
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
    if p.alert_code == CODE_AUTO_STOPPED and not _auto_allowed(p):
        return p                  # only an operator action (Resize / Save: auto_failures back to 0) clears it
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
                       obs: SliceObservation, fills: Optional[list] = None) -> AllocatorProtection:
    """Write what the broker said onto the slice row and act on it (fill / alarm).

    Returns the (possibly updated) protection. The ONE place a fill becomes a weight reduction
    and a note and a lost order becomes an alert, so the reconcile and the cancel path cannot
    disagree. A fill does NOT exclude the symbol. With ``fills`` (a list) the fill EFFECTS are
    deferred into it so a reconcile that sees several slices fill computes the weight factor from
    the HELD shares once, in order (``_flush_fills``).
    """
    covered_before = covered_quantity(get_slices(p.id))
    s.tp_order_id = obs.tp_order_id or s.tp_order_id
    s.sl_order_id = obs.sl_order_id or s.sl_order_id
    if obs.gtc_date is not None:
        s.gtc_date = DateTime(obs.gtc_date.year, obs.gtc_date.month, obs.gtc_date.day)
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
        newly = max(0.0, float(obs.filled_qty) - float(s.weight_applied_qty))   # NOT NULL, default 0.0
        if newly > 1e-9:
            if fills is not None:
                fills.append((s, obs, newly))
            else:
                held = _held_before(account, p, newly, covered_before)
                p = _on_protective_fill(account, p, s, obs, newly, held)
    elif obs.state in SLICE_ALARM_STATES:
        code = {SLICE_LOST_EXPIRED: CODE_LOST_EXPIRED, SLICE_LOST_CANCELLED: CODE_LOST_CANCELLED,
                SLICE_LOST_REJECTED: CODE_LOST_REJECTED}.get(obs.state, CODE_UNKNOWN_STATE)
        p = _alert(p, code, f"slice {s.slice_index + 1} ({s.quantity} sh, {_describe(s)}): "
                            f"{obs.detail}. The position is "
                            f"{'partly ' if covered_quantity(get_slices(p.id)) > 0 else ''}"
                            f"UNPROTECTED -- re-place protection or close the position.")
    return _reload(p.id)


def _held_before(account, p: AllocatorProtection, newly: float,
                 covered_fallback: float) -> Optional[float]:
    """The shares HELD just before ``newly`` were sold: the broker position now plus what just left.

    The weight must follow what was HELD, not what was covered (a lost slice, or growth nobody
    covered, would otherwise make a small fill look like the whole position). A position read that
    lags the fill can only make this too large (a smaller cut), never too small. When the read
    fails the covered quantity is the fallback; ``None`` means nothing to scale against.
    """
    try:
        qty, _ = _read_position(account, p.symbol)
        return qty + newly
    except Exception as e:  # noqa: BLE001
        logger.warning(f"allocator TP/SL: position read for {p.symbol} failed ({e}); weight factor "
                       f"falls back to the covered quantity")
        return covered_fallback if covered_fallback > 1e-9 else None


class ImplicitWeightError(RuntimeError):
    """The shares a label's unstored symbols were running on could not be measured."""


def _open_cost(account_id: int, symbol: str) -> Optional[float]:
    """What the symbol's OPEN transactions cost (quantity x open price), or None. Used only when the
    broker's own cost basis is gone (the position was exited)."""
    from .portfolio_allocation_service import _open_transaction_ids
    total, seen = 0.0, False
    for txn_id in _open_transaction_ids(account_id, [symbol]).get(symbol, []):
        txn = get_instance(Transaction, txn_id)
        if txn is not None and txn.open_price is not None:
            total += float(txn.quantity) * float(txn.open_price)
            seen = True
    return total if seen else None


def _implicit_weights(account, p: AllocatorProtection, held_before: Optional[float]
                      ) -> Dict[str, Dict[str, float]]:
    """``{label: {symbol: share}}``: for every managed label holding ``p.symbol`` in which ANY member has no
    stored row, the share each such member is running on -- the one the page shows and the plan solves
    against (``resolve_symbol_weights``) -- measured BEFORE the fill. Written down as explicit rows, this
    symbol's reduced by the fill and every other member PINNED, so the label total drops by exactly the
    freed share (the derived default would otherwise hand it to the unstored members).

    Pre-fill measuring: the position read happens AFTER the fill. Market valuation uses the held quantity
    from before the fill at the current price; cost valuation rescales the broker's (already reduced) cost
    basis by held_before / held_now, or, when the position is gone, uses the open transactions' cost.

    Raises ``ImplicitWeightError`` (naming the label and the unpriced symbols) when a share cannot be
    measured: nothing is guessed, and the caller says so loudly instead of claiming the share is freed.
    """
    from dataclasses import replace
    from ba2_common.core.portfolio_allocation import current_value
    from ..ui.utils.portfolio_allocation_view import resolve_symbol_weights
    from .portfolio_allocation_service import build_position_states
    from .portfolio_allocation_store import get_allocation_config, get_managed_labels, get_symbol_rows
    from .utils import get_symbols_by_label
    labels = [m.label for m in get_managed_labels(p.account_id)]
    members = {lb: [_norm(x) for x in syms] for lb, syms in get_symbols_by_label(labels).items()}
    stored = {lb: {_norm(k): v for k, v in get_symbol_rows(p.account_id, lb).items()} for lb in labels}
    todo = [lb for lb in labels if p.symbol in members.get(lb, [])
            and any(m not in stored[lb] for m in members[lb])]
    if not todo:
        return {}
    if held_before is None:
        raise ImplicitWeightError(f"the quantity held before the fill is unknown")
    mode = get_allocation_config(p.account_id).valuation_mode
    states = build_position_states(account, sorted({x for lb in todo for x in members[lb]}))
    mine = states.get(p.symbol)
    if mine is None:
        raise ImplicitWeightError(f"no position state for {p.symbol}")
    if mode == "cost":
        if mine.quantity > 1e-9:
            cost_before = float(mine.cost_basis) * float(held_before) / float(mine.quantity)
        else:
            cost_before = _open_cost(p.account_id, p.symbol)
            if cost_before is None:
                raise ImplicitWeightError(f"the cost of {p.symbol} before the fill is unknown (position gone)")
        states[p.symbol] = replace(mine, quantity=float(held_before), cost_basis=cost_before)
    else:
        states[p.symbol] = replace(mine, quantity=float(held_before))
    out: Dict[str, Dict[str, float]] = {}
    for lb in todo:
        blind = [x for x in members[lb] if mode == "market" and states[x].quantity and states[x].price is None]
        resolved = resolve_symbol_weights(
            members[lb], saved={k: float(v.weight_pct) for k, v in stored[lb].items() if k in members[lb]},
            values={x: current_value(states[x], mode) for x in members[lb]}, unmeasurable=blind)
        unstored = [x for x in members[lb] if x not in stored[lb]]
        if any(resolved[x].weight_pct is None for x in unstored):
            raise ImplicitWeightError(
                f"label {lb}: no price for {', '.join(blind) or p.symbol}, so the shares of "
                f"{', '.join(unstored)} cannot be measured")
        out[lb] = {x: float(resolved[x].weight_pct) for x in unstored}
    return out


def _flush_fills(account, p: AllocatorProtection, fills: list) -> AllocatorProtection:
    """Apply the deferred fill effects of one reconcile, in order, against the HELD shares."""
    if not fills:
        return p
    total = sum(n for _, _, n in fills)
    held = _held_before(account, p, total, covered_quantity(get_slices(p.id)) + total)
    for s, obs, newly in fills:
        p = _on_protective_fill(account, p, s, obs, newly, held)
        if held is not None:
            held = max(0.0, held - newly)
    return p


def _fill_name(s: AllocatorProtectionOrder, obs: SliceObservation) -> Tuple[str, str]:
    """``(what, weight_reason)`` for a fill: ``("TP1 filled", tp_fill)`` or ``("SL hit", sl_fill)``."""
    if obs.kind == KIND_TP and s.kind == ORDER_KIND_OCO:
        label = f"TP{s.target_index + 1} filled" if s.target_index >= 0 else "TP filled"
        return label, WEIGHT_REASON_TP_FILL
    return "SL hit", WEIGHT_REASON_SL_FILL


def _on_protective_fill(account, p: AllocatorProtection, s: AllocatorProtectionOrder,
                        obs: SliceObservation, newly: float,
                        held_before: Optional[float]) -> AllocatorProtection:
    """A protective order sold ``newly`` shares: reduce the symbol's allocator weight in
    proportion (``new = old x (held - sold) / held``; 0 when the position was exited), record the
    sale in the platform's own books, write the note.

    The freed share is NOT given to the label's other symbols: it stays unallocated until the
    operator reassigns it. Every automatic change is written to ``allocator_weight_change``.
    """
    what, reason = _fill_name(s, obs)
    if held_before is None or held_before <= 1e-9:
        factor = 1.0
        shown = "an unknown number of"
    else:
        factor = min(1.0, max(0.0, (held_before - newly) / held_before))
        shown = f"{held_before:g}"
    detail = (f"{what}: {newly:g} of {shown} held sh sold"
              + (f" @ {obs.fill_price:g}" if obs.fill_price is not None else ""))
    implicit: Dict[str, Dict[str, float]] = {}
    weight_failed = False
    if factor < 1.0:
        try:
            implicit = _implicit_weights(account, p, held_before)
        except Exception as e:  # noqa: BLE001 -- the fill is real whatever the weight write does
            weight_failed = True
            logger.error(f"allocator TP/SL: label shares of {p.symbol} could not be measured: {e}",
                         exc_info=True)
            _log(p.account_id, ActivityLogSeverity.FAILURE,
                 f"{p.symbol}: {what}, but its label share could not be measured ({e}); NOTHING was "
                 f"written for the unstored shares -- lower the share by hand or the next rebalance "
                 f"buys it back", code="WEIGHT_FAILED", symbol=p.symbol)
    try:
        changes = reduce_symbol_weights(p.account_id, p.symbol, factor, reason=reason, detail=detail,
                                        implicit=implicit)
    except Exception as e:  # noqa: BLE001 -- the fill is real whatever the weight write does
        weight_failed = True
        logger.error(f"allocator TP/SL: weight reduction for {p.symbol} failed: {e}", exc_info=True)
        _log(p.account_id, ActivityLogSeverity.FAILURE,
             f"{p.symbol}: {what}, but its allocator weight could not be reduced ({e}); "
             f"lower it by hand or the next rebalance may buy it back", code="WEIGHT_FAILED",
             symbol=p.symbol)
        changes = []
    pins = [c for c in changes if c.reason == WEIGHT_REASON_PINNED]
    changes = [c for c in changes if c.reason != WEIGHT_REASON_PINNED]
    note = f"{what} {_now():%Y-%m-%d}"
    if changes:
        if len({c.label for c in changes}) == 1:
            note += f": share {changes[0].before_pct:g}% -> {changes[0].after_pct:g}%"
        else:
            note += ": " + "; ".join(f"{c.label} share {c.before_pct:g}% -> {c.after_pct:g}%"
                                     for c in changes)
    if pins:
        many = len({c.label for c in pins}) > 1
        note += "; pinned " + ", ".join(f"{c.symbol} {c.after_pct:g}%" + (f" ({c.label})" if many else "")
                                        for c in pins)
    # Shares the TAKE-PROFIT member newly sold (a stop fill after a take-profit part-fill is not credited to
    # the target): the slice's earlier applied quantity is taken to be the take-profit's own.
    applied_before = float(s.weight_applied_qty)
    tp_new = max(0.0, float(obs.tp_filled_qty) - min(applied_before, float(obs.tp_filled_qty)))
    s.weight_applied_qty = applied_before + newly
    _save(s)
    if tp_new > 0 and s.kind == ORDER_KIND_OCO:
        p = _mark_target_filled(p, s, obs, tp_new)
    _record_protective_sale(p.account_id, p.symbol, newly, obs.fill_price,
                            s.external_tag or f"ba2prot:{p.id}:{s.slice_index}")
    p.last_fill_at = _now()
    p.last_fill_note = note
    _save(p)
    freed = ("the allocator weight was NOT (fully) reduced, see the WEIGHT_FAILED entry: the next "
             "rebalance may buy the shares back" if weight_failed else
             "the freed share stays unallocated and the next rebalance treats the symbol normally")
    _log(p.account_id, ActivityLogSeverity.WARNING,
         f"{p.symbol}: {note}. The remaining protective orders keep protecting the rest; {freed}.",
         code=CODE_FILL, symbol=p.symbol, kind=obs.kind, slice=s.slice_index,
         filled_qty=newly, fill_price=obs.fill_price, weight_factor=factor)
    logger.warning(f"allocator TP/SL: {p.symbol} {note}")
    return _reload(p.id)


def _mark_target_filled(p: AllocatorProtection, s: AllocatorProtectionOrder, obs: SliceObservation,
                        newly: float) -> AllocatorProtection:
    """Record on the stored target how much of it was TAKEN, counted in SHARES.

    Each target keeps ``planned`` (every share ever planned for it: the first placement plus each growth
    lot) and ``sold`` (shares its orders have sold); ``taken = sold / planned`` feeds the next placement
    and the target is marked ``filled`` only when everything planned has been sold. A growth lot filling
    while the original order is lost or still resting therefore never marks the whole target. The slice's
    ``target_index`` is the target's position in the stored list; its price must still agree with the stored
    target (a slice left over from an earlier configuration is never marked against the new one)."""
    raw = [dict(t) for t in (p.tp_targets or [])]
    i = s.target_index
    if not (0 <= i < len(raw)) or s.tp_price is None \
            or abs(float(raw[i]["price"]) - float(s.tp_price)) > 0.0101:
        logger.warning(f"allocator TP/SL: {p.symbol}: the filled slice {s.slice_index + 1} matches no stored "
                       f"target (index {i}, tp {s.tp_price}); no target marked")
        return p
    t = raw[i]
    if t.get("filled"):
        return p
    if t.get("planned") is None:
        before = target_taken(t)                              # a row stored before share counting
        t["planned"] = float(s.quantity) / (1.0 - before) if before < 1.0 - 1e-9 else float(s.quantity)
        t["sold"] = before * t["planned"]
    t["sold"] = float(t.get("sold") or 0.0) + float(newly)
    planned = float(t["planned"])
    t["taken"] = min(1.0, t["sold"] / planned)
    if t["sold"] >= planned - 0.5:
        t["filled"] = True
        t["taken"] = 1.0
    p.tp_targets = raw
    return _save(p)


def _record_protective_sale(account_id: int, symbol: str, quantity: float,
                            price: Optional[float], tag: str) -> None:
    """Record a protective sale in the platform's books so the next rebalance sizes correctly.

    ``refresh_transactions`` RECOMPUTES a transaction's quantity every cycle from its linked FILLED
    orders (buys minus sells), so lowering ``Transaction.quantity`` alone is undone on the next
    refresh. The sale is therefore written as a synthetic FILLED SELL ``TradingOrder`` linked to
    the transaction (FIFO, oldest first), marked ``allocator_protection`` in ``data`` and the
    comment, and the transaction quantity is set to the value the refresh will derive. A transaction
    sold in full is closed through the platform's close helper (P&L + activity log) at the fill
    price. Never raises (the fill is real whatever the bookkeeping does); a failure is loud.
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
            have = float(txn.quantity)          # Transaction.quantity is a required float
            if have <= 1e-9:
                continue
            take = min(have, remaining)
            add_instance(TradingOrder(
                account_id=account_id, symbol=symbol, quantity=take, filled_qty=take,
                side=OrderDirection.SELL, order_type=OrderType.MARKET, good_for="gtc",
                status=OrderStatus.FILLED, open_type=OrderOpenType.MANUAL,
                open_price=(float(price) if price is not None else None), transaction_id=txn_id,
                comment=f"allocator_protection sale {tag}",
                data={"source": "allocator_protection", "tag": tag}))
            remaining -= take
            if take + 1e-9 >= have:
                if price is not None:
                    txn.close_price = float(price)
                close_transaction_with_logging(txn, account_id, "tp_sl_filled")
            else:
                txn.quantity = have - take
            update_instance(txn)
        if remaining > 1e-6:
            logger.warning(f"allocator TP/SL: {symbol}: {remaining:g} sh of a protective fill had no "
                           f"open transaction to take it from")
    except Exception as e:  # noqa: BLE001
        logger.error(f"allocator TP/SL: could not record the protective sale of {symbol} in the "
                     f"transactions: {e}", exc_info=True)
        _log(account_id, ActivityLogSeverity.FAILURE,
             f"{symbol}: a protective order sold {quantity:g} sh but the open transactions could "
             f"not be updated ({e}); reconcile them before rebalancing", code="TXN_FAILED",
             symbol=symbol)


def _ack_alarm_slices(p: AllocatorProtection) -> None:
    """Mark LOST alarm slices as superseded (a fresh placement attempt replaces them). An UNKNOWN
    slice is NEVER superseded: an order may exist; it is resolved (found by its tag: adopted or
    cancelled), not forgotten."""
    for s in get_slices(p.id):
        if s.state in SLICE_ALARM_STATES and s.state != SLICE_UNKNOWN and s.closed_at is None:
            s.closed_at = _now()
            s.detail = f"{s.detail or ''} (superseded by a new placement)".strip()
            _save(s)


def _resolve_unknown_slice(account, p: AllocatorProtection, s: AllocatorProtectionOrder) -> bool:
    """Try to settle an UNKNOWN slice that has no broker id by finding its tag at the broker.

    Found -> the id is adopted and the slice takes whatever state the broker reports (LIVE: it now
    covers shares and will be cancelled like any other; filled/cancelled: handled by the usual
    rules). Not found in live orders nor recent history -> it was never placed: the slice is closed
    as rejected. A failed SEARCH resolves nothing (``False``): not finding is only meaningful when
    we could look. Returns whether the slice is no longer UNKNOWN-without-id.
    """
    if _broker_id(s) is not None:
        return True
    since = (_naive(s.placed_at) or _now()) - timedelta_seconds(600)
    try:
        found = account.find_protective_orders_by_tag(
            s.external_tag, since=since.replace(tzinfo=timezone.utc), kind=s.kind)
    except Exception as e:  # noqa: BLE001
        logger.error(f"allocator TP/SL: could not search for tag {s.external_tag}: {e}", exc_info=True)
        return False
    same_kind = [f for f in found if f[0] == s.kind]
    # The tag carries a nonce, so it is unique; the SYMBOL is the one extra thing checked. An order for
    # another symbol under the same tag is ignored (loudly), never adopted.
    ours = [f for f in same_kind if _order_symbols(f[2]) == {p.symbol}]
    for f in same_kind:
        if f not in ours:
            _log(p.account_id, ActivityLogSeverity.WARNING,
                 f"{p.symbol}: an order tagged {s.external_tag} exists at the broker for "
                 f"{', '.join(sorted(_order_symbols(f[2]))) or 'an unknown symbol'}; it was NOT adopted",
                 code="FOREIGN_TAG", symbol=p.symbol, broker_id=f[1])
    age = _unknown_age_seconds(s)
    if not ours and age < UNKNOWN_MIN_AGE_SECONDS:
        # N2: the broker may not have LISTED an accepted order yet. "Not found" is only evidence after the
        # slice is old enough; until then it stays UNKNOWN and BLOCKS (no second order is placed).
        return False
    if not ours:
        s.state = SLICE_LOST_REJECTED
        s.detail = (f"no order tagged {s.external_tag} was found at the broker (live and history since "
                    f"{since:%Y-%m-%d %H:%M} UTC): the placement never reached it")
        s.closed_at = _now()
        _save(s)
        _log(p.account_id, ActivityLogSeverity.INFO, f"{p.symbol}: {s.detail}", code="UNKNOWN_RESOLVED",
             symbol=p.symbol, tag=s.external_tag)
        return True
    observed = [(f, classify_complex_order(f[2], slice_quantity=s.quantity, we_requested_cancel=False))
                for f in ours]
    live = [(f, o) for f, o in observed if not (o.state in _ENDED_STATES and o.filled_qty <= 0)]
    if not live:
        # Every tagged order is terminal and none filled: cancelled on the site, expired, rejected. That is
        # the operator's (or the broker's) decision: adopt the id and raise the usual LOST_* alarm. It is
        # NEVER re-placed automatically (``has_alarm`` stops the growth rule); the operator re-places.
        (kind, broker_id, wrapper), obs = observed[0]
        if kind == ORDER_KIND_STOP:
            s.sl_order_id = broker_id
        else:
            s.complex_order_id = broker_id
        _save(s)
        _apply_observation(account, p, s, obs)
        _log(p.account_id, ActivityLogSeverity.WARNING,
             f"{p.symbol}: the order {broker_id} (tag {s.external_tag}) was found ended at the broker "
             f"without a fill ({obs.state}); NOT re-placed", code="UNKNOWN_RESOLVED", symbol=p.symbol,
             broker_id=broker_id)
        return True
    matches = [f for f, _ in live]
    kind, broker_id, wrapper = matches[0]
    broker_size = _warn_if_mismatch(p, s, wrapper, broker_id)
    if len(matches) > 1:
        logger.error(f"allocator TP/SL: {len(matches)} orders carry tag {s.external_tag}: adopting "
                     f"{broker_id}; check the TastyTrade site for duplicates")
        _log(p.account_id, ActivityLogSeverity.FAILURE,
             f"{p.symbol}: {len(matches)} orders carry tag {s.external_tag}; adopted {broker_id}, "
             f"check the others on the TastyTrade site", code="DUPLICATE_TAG", symbol=p.symbol)
    if kind == ORDER_KIND_STOP:
        s.sl_order_id = broker_id
    else:
        s.complex_order_id = broker_id
    slice_said = s.quantity
    if broker_size is not None:
        s.quantity = int(round(broker_size))          # the broker's size is what is really reserved
    _save(s)
    obs = classify_complex_order(wrapper, slice_quantity=s.quantity, we_requested_cancel=False)
    p = _apply_observation(account, p, s, obs)
    _log(p.account_id, ActivityLogSeverity.INFO,
         f"{p.symbol}: adopted the order {broker_id} found by tag {s.external_tag} ({obs.state})",
         code="UNKNOWN_RESOLVED", symbol=p.symbol, broker_id=broker_id)
    if broker_size is not None:
        _alert(p, CODE_QUANTITY_MISMATCH,
               f"the adopted order {broker_id} reserves {s.quantity} sh at the broker, not the {slice_said} "
               f"the slice said; the slice was resized to the broker's figure. Check the position on the "
               f"TastyTrade site.")
    return True


#: Observed states in which an order is over (a fill, if any, is read from ``filled_qty``).
_ENDED_STATES = frozenset({SLICE_LOST_EXPIRED, SLICE_LOST_CANCELLED, SLICE_LOST_REJECTED})


def _unknown_age_seconds(s: AllocatorProtectionOrder) -> float:
    placed = _naive(s.placed_at)
    return (_now() - placed).total_seconds() if placed is not None else 0.0


def _unknown_wait_left(s: AllocatorProtectionOrder) -> float:
    """Seconds an UNKNOWN slice still has to wait before 'not found' counts as 'never placed'."""
    return max(0.0, UNKNOWN_MIN_AGE_SECONDS - _unknown_age_seconds(s))


def _order_symbols(wrapper) -> Set[str]:
    return {_norm(getattr(m, "underlying_symbol", "") or "") for m in (getattr(wrapper, "orders", None) or [])} - {""}


def _warn_if_mismatch(p: AllocatorProtection, s: AllocatorProtectionOrder, wrapper, broker_id) -> Optional[float]:
    """The adopted order's received time or size disagrees with the slice: adopted all the same (the tag
    and symbol identify it; refusing would freeze the symbol), but the operator is told. Returns the
    broker's size when it differs from the slice's (the caller resizes the slice), else None."""
    placed = _naive(s.placed_at)
    differing: Optional[float] = None
    for m in (getattr(wrapper, "orders", None) or []):
        size = getattr(m, "size", None)
        if size is not None and abs(float(size) - float(s.quantity)) > 1e-9:
            differing = float(size)
            _log(p.account_id, ActivityLogSeverity.WARNING,
                 f"{p.symbol}: the adopted order {broker_id} has quantity {float(size):g} at the broker but "
                 f"the slice says {s.quantity}; adopted by tag, check the TastyTrade site",
                 code="ADOPTED_MISMATCH", symbol=p.symbol, broker_id=broker_id)
        when = _naive(getattr(m, "received_at", None) or getattr(m, "updated_at", None))
        if when is not None and placed is not None and abs((when - placed).total_seconds()) > 120:
            _log(p.account_id, ActivityLogSeverity.WARNING,
                 f"{p.symbol}: the adopted order {broker_id} was received {when:%Y-%m-%d %H:%M:%S} UTC but the "
                 f"slice was placed {placed:%Y-%m-%d %H:%M:%S} UTC (clock skew? check the broker time); "
                 f"adopted by tag", code="ADOPTED_MISMATCH", symbol=p.symbol, broker_id=broker_id)
    return differing


def _unresolved_unknowns(account, p: AllocatorProtection) -> List[AllocatorProtectionOrder]:
    """Resolve what can be resolved; return the UNKNOWN slices that still cannot be."""
    left = []
    for s in get_slices(p.id):
        if s.state == SLICE_UNKNOWN and s.closed_at is None:
            if _broker_id(s) is None:
                _resolve_unknown_slice(account, _reload(p.id), s)
                s = get_instance(AllocatorProtectionOrder, s.id)
                if s.state == SLICE_UNKNOWN and s.closed_at is None:
                    left.append(s)
            # an UNKNOWN WITH an id is read by the reconcile and by the cancel; it blocks until then
            else:
                left.append(s)
    return left


# =========================================================================================
# cancelling live slices (confirmed)
# =========================================================================================

@dataclass
class CancelResult:
    all_confirmed: bool = True
    filled: bool = False
    detail: List[str] = field(default_factory=list)


def _cancel_live_slices(account, p: AllocatorProtection) -> Tuple[AllocatorProtection, CancelResult]:
    """Cancel every blocking slice of ``p`` (resting, or UNKNOWN) and require the broker to CONFIRM.

    UNKNOWN slices without an id are first searched for by tag (adopted -> cancelled with the rest;
    not found -> never placed); one that cannot be settled keeps ``all_confirmed`` False. All
    cancels share ONE confirmation poll (``cancel_protective_batch``). A fill that lands before the
    cancel does is applied as a fill (weight reduced). An unconfirmed cancel leaves the slice
    CANCELLING, raises CANCEL_UNCONFIRMED and returns ``all_confirmed=False``: the shares may still
    be reserved, so the caller must not trade.
    """
    result = CancelResult()
    for s in get_slices(p.id):
        if s.state == SLICE_UNKNOWN and s.closed_at is None and _broker_id(s) is None:
            _resolve_unknown_slice(account, _reload(p.id), s)
    items, by_item = [], {}
    for s in get_slices(p.id):
        if not _blocks(s):
            continue
        bid = _broker_id(s)
        if bid is None:
            # PLACING/UNKNOWN with no broker id that the tag search could not settle.
            if s.state != SLICE_UNKNOWN:
                s.state = SLICE_UNKNOWN
                s.detail = "a placement never reported back; an order may exist at the broker"
                _save(s)
            result.all_confirmed = False
            result.detail.append(f"slice {s.slice_index + 1} has no broker id")
            p = _alert(p, CODE_UNKNOWN_STATE,
                       f"slice {s.slice_index + 1} was being placed and never reported back (tag "
                       f"{s.external_tag}); check the TastyTrade site for a GTC order on {p.symbol}")
            continue
        s.cancel_requested = True
        s.state = SLICE_CANCELLING
        _save(s)
        item = (ORDER_KIND_STOP if s.kind == ORDER_KIND_STOP else "OCO", int(bid))
        items.append(item)
        by_item[item] = s
    outcomes = account.cancel_protective_batch(items) if items else {}
    for item, s in by_item.items():
        outcome = outcomes[item]
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
    #: Refused by the pre-placement validation (the PRICE moved through the stop or a target, or the position
    #: is not protectable): nothing was sent, and restoring the old prices would be wrong.
    refused_by_validation: bool = False

    @property
    def ok(self) -> bool:
        return not self.errors


def _place_slices(account, p: AllocatorProtection, stop_only: bool = False) -> Tuple[AllocatorProtection, PlaceResult]:
    """Place the OCOs for whatever whole-share quantity is not covered yet.

    Reads a FRESH position and price, validates against them (a price that moved through the
    stop or a target while orders were cancelled is REFUSED, never traded through), plans the
    slices and places them one by one. A slice row is written PLACING before its broker call.
    Placed slices are KEPT when a later one is refused (partial protection beats none): the
    refusal raises PLACEMENT_REFUSED and the status is PARTIAL/UNPROTECTED.
    """
    result = PlaceResult()
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

    unresolved = _unresolved_unknowns(account, p)
    if unresolved:
        msg = (f"an earlier placement of {p.symbol} is unresolved (tag "
               f"{', '.join(str(u.external_tag) for u in unresolved)}): no new order is placed "
               f"until it is found at the broker and adopted or cancelled")
        result.errors.append(msg)
        return _alert(p, CODE_UNKNOWN_STATE, msg), result
    if not stop_only:
        _ack_alarm_slices(p)       # a stop-only cover leaves the operator's alarm standing
    covered = covered_quantity(get_slices(p.id))
    to_cover = whole_shares(qty) - int(round(covered))
    if to_cover <= 0:
        return _reload(p.id), result

    ticks = _tick_sizes(account, p.symbol)
    # N8: a target at/below the price now (the one that just filled) cannot be placed; its fraction folds
    # into the stop-only remainder, so the stop ALWAYS remains and the placement is not refused for it.
    # The filled marks are honoured EVERYWHERE (re-placement, growth, repair): a target that was taken is
    # never placed again; new shares get what is left of the plan. Saving the protection again re-arms it.
    stored = [] if stop_only else list(p.tp_targets or [])
    eff = effective_targets(stored, last, ticks)
    targets = eff.usable
    for i in eff.filled:
        result.notes.append(f"Take-profit {float(stored[i]['price']):g} already filled; not placed again.")
    for i in eff.reached:
        result.notes.append(f"Take-profit {float(stored[i]['price']):g} is at or below the price now "
                            f"({last:g}); not re-placed, its share stays under the stop.")
    errors = validate_protection(sl_price=p.sl_price, targets=targets, last_price=last,
                                 position_quantity=float(to_cover), tick_sizes=ticks)
    if errors:
        result.errors.extend(errors)
        result.refused_by_validation = True
        p.last_error = " ".join(errors)
        _save(p)
        return _alert(p, CODE_PLACEMENT_REFUSED,
                      "protection not placed: " + " ".join(errors)), result

    if covered > 0:
        # Orders still rest for older shares (growth, repair): each new order fills its target's DEFICIT
        # against the plan for the whole position (S1), so per-target totals stay near the template.
        resting = [x for x in get_slices(p.id) if _is_resting(x)]
        by_target: Dict[int, int] = {}
        for x in resting:
            if x.kind == ORDER_KIND_OCO and x.target_index >= 0:
                by_target[x.target_index] = by_target.get(x.target_index, 0) + int(round(_remaining_qty(x)))
        runner_have = sum(int(round(_remaining_qty(x))) for x in resting if x.kind == ORDER_KIND_STOP)
        plans = plan_add_only(total_shares=whole_shares(qty), to_cover=to_cover, targets=targets,
                              target_ids=eff.usable_index, existing_by_target=by_target,
                              existing_runner=runner_have, sl_price=p.sl_price, last_price=last,
                              tick_sizes=ticks)
        origin = [plan.target_index for plan in plans]
    else:
        plans, notes = plan_slices(shares=to_cover, targets=targets, sl_price=p.sl_price,
                                   last_price=last, tick_sizes=ticks)
        result.notes.extend(notes)
        origin = [(eff.usable_index[plan.target_index] if plan.target_index >= 0 else -1) for plan in plans]
    placed_oco: List[Tuple[AllocatorProtectionOrder, int]] = []
    existing = len(get_slices(p.id))
    for plan, target_id in zip(plans, origin):
        index = existing + plan.slice_index
        tag = f"ba2prot:{p.id}:{index}:{uuid.uuid4().hex[:8]}"   # unique: SQLite reuses deleted ids (N1)
        row = _save(AllocatorProtectionOrder(
            protection_id=p.id, slice_index=index,
            target_index=target_id,
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
        except PlacementOutcomeUnknown as e:
            # The call raised after it may have reached the broker (or the accepted order could not
            # be read back): record UNKNOWN with the tag and every id the broker named, and STOP
            # placing -- another order now could double the position's cover.
            row.state = SLICE_UNKNOWN
            row.detail = str(e)[:500]
            if e.broker_id is not None:
                if e.kind == ORDER_KIND_STOP:
                    row.sl_order_id = e.broker_id
                else:
                    row.complex_order_id = e.broker_id
            _save(row)
            result.errors.append(str(e))
            logger.error(f"allocator TP/SL: placement outcome UNKNOWN for {p.symbol}: {e}")
            break
        except Exception as e:  # noqa: BLE001 -- the call's outcome is unknown: alarm, do not guess
            row.state = SLICE_UNKNOWN
            row.detail = f"placement raised {type(e).__name__}: {e}"[:500]
            _save(row)
            result.errors.append(f"placement of slice {index + 1} raised {type(e).__name__}: {e}; "
                                 f"an order may exist at the broker")
            logger.error(f"allocator TP/SL: placement raised for {p.symbol}: {e}", exc_info=True)
            break
        if plan.kind == ORDER_KIND_STOP:
            row.sl_order_id = placed.order_id
            row.sl_price = placed.sl_price
        else:
            row.complex_order_id = placed.complex_order_id
            row.tp_order_id = placed.tp_order_id
            row.sl_order_id = placed.sl_order_id
            row.tp_price, row.sl_price = placed.tp_price, placed.sl_price
        row.state = SLICE_LIVE
        # The broker's own gtc_date, or NULL: a missing date is never guessed (and never renewed).
        row.gtc_date = (DateTime(placed.gtc_date.year, placed.gtc_date.month, placed.gtc_date.day)
                        if placed.gtc_date is not None else None)
        row.placed_at = _now()
        _save(row)
        if plan.kind != ORDER_KIND_STOP:
            placed_oco.append((row, target_id))
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
    if placed_oco:
        # Shares PLANNED per target (the share-counting base of ``_mark_target_filled``): a first placement
        # sets it, a growth lot adds to it, a re-placement of the same shares leaves it alone.
        raw = [dict(t) for t in (p.tp_targets or [])]
        by_idx: Dict[int, int] = {}
        for row, idx in placed_oco:
            by_idx[idx] = by_idx.get(idx, 0) + int(row.quantity)
        for idx, q in by_idx.items():
            if not 0 <= idx < len(raw):
                continue
            t = raw[idx]
            if covered > 0 and t.get("planned") is not None:
                t["planned"] = float(t["planned"]) + q              # a growth lot adds to what was planned
            else:
                # A FULL (re-)placement: what is planned now is what has been sold plus what is placed.
                sold = t.get("sold")
                if sold is None:
                    before = target_taken(t)
                    if 0 < before < 1.0 - 1e-9:                     # a row stored before share counting
                        t["planned"] = q / (1.0 - before)
                        t["sold"] = before * t["planned"]
                    else:
                        t["planned"] = float(q)
                else:
                    t["planned"] = float(sold) + q
                    t["taken"] = min(1.0, float(sold) / t["planned"])
        p.tp_targets = raw
        p = _save(p)
    if result.errors:
        p.last_error = " | ".join(result.errors)[:1000]
        raw = " | ".join(result.errors)
        if any(is_margin_refusal(e) for e in result.errors):
            # A11: the broker reserves buying power for the loss at a stop's trigger price.
            bare = int(to_cover) - int(result.shares_covered)
            try:
                available = account.get_account_snapshot().buying_power
            except Exception:  # noqa: BLE001 -- only context for the sentence
                available = None
            message = (margin_sentence(available=available)
                       + f" {result.placed} of {len(plans)} protective order(s) placed; {bare} "
                       + ("share has" if bare == 1 else "shares have") + f" no stop. Details: {raw}")
        else:
            message = f"{result.placed} of {len(plans)} protective order(s) placed; refused: {raw}"
        p = _alert(p, CODE_PLACEMENT_REFUSED, message)
    else:
        p.last_error = None
        p.protected_quantity = covered_quantity(get_slices(p.id))
        _save(p)
        if not stop_only:
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
                    targets: List[TpTarget], kept_filled: Optional[List[TpTarget]] = None,
                    taken: Optional[List[Optional[float]]] = None) -> ActionResult:
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
        if _run_in_flight(account):
            return ActionResult(False, f"{symbol}: an allocator run is in flight; it cancels and "
                                       f"re-places protective orders itself. Try again when it has finished.")
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
        kept = list(kept_filled or [])
        if sum(t.fraction for t in targets) + sum(t.fraction for t in kept) > 1.0 + 1e-9:
            errors.append("The take-profit shares, together with the targets already taken, exceed 100%.")
        if errors:
            return ActionResult(False, "The TP/SL is not valid.", errors=errors)

        if p is None:
            p = _save(AllocatorProtection(account_id=account.id, symbol=symbol))
        p.enabled = True
        p.sl_price = float(sl_price)
        stored = [t.to_dict() for t in targets]
        for entry, mark in zip(stored, list(taken or [])):
            if mark is not None and mark > 0:
                entry["taken"] = float(mark)             # a partly taken target stays partly taken
        # Targets the operator left as TAKEN stay marked; leaving them out of ``kept_filled`` re-arms them.
        stored += [dict(t.to_dict(), filled=True, taken=1.0) for t in kept]
        p.tp_targets = stored
        p.last_error = None
        p.auto_failures = 0                  # an operator action re-arms the automatic paths
        p.expected_qty = None
        p.disarmed_at = None
        p.disarmed_note = None
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
        p.expected_qty = None
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
    """Switch off and forget the configuration (only once every cancel is confirmed).

    CANCEL-ONLY: it reaches the broker only through the confirmed cancel of the protective orders, never a
    place call, so it can never sell. The protection row and its slice rows are DELETED; the audit trail
    (``allocator_weight_change``, the activity log) and the operator's exclusions are kept. Refused, with
    the config kept, while an allocator run is in flight, when a cancel cannot be confirmed, or while an
    UNKNOWN slice may still rest at the broker.
    """
    symbol = _norm(symbol)
    with protection_lock(account.id):
        if _run_in_flight(account):
            return ActionResult(False, f"{symbol}: an allocator run is in flight; it cancels and re-places "
                                       f"protective orders itself. Try again when it has finished.")
        before = get_protection(account.id, symbol)
        was = ({"was_sl_price": before.sl_price, "was_tp_targets": [dict(t) for t in (before.tp_targets or [])]}
               if before is not None else {})
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
        _log(account.id, ActivityLogSeverity.INFO, f"{symbol}: TP/SL configuration deleted by the operator",
             code="DELETED", symbol=symbol, **was)
        return ActionResult(True, f"{symbol}: protection removed.")


def forget_unknown_slices(account, symbol: str) -> ActionResult:
    """The operator checked the TastyTrade site: forget every UNKNOWN slice that has no broker id.

    Allowed ONLY when the automatic resolution is impossible: the slice is at least
    ``UNKNOWN_MIN_AGE_SECONDS`` old AND the tag search itself FAILS right now. A search that works either
    finds the order (it is adopted by the refresh) or finds nothing (the refresh closes the slice), so
    forgetting by hand could only create a double protection; it is refused with that explanation. The
    search runs inside the lock. An activity-log entry per forgotten slice.
    """
    symbol = _norm(symbol)
    with protection_lock(account.id):
        p = get_protection(account.id, symbol)
        if p is None:
            return ActionResult(False, f"{symbol}: there is no protection.")
        candidates = [s for s in get_slices(p.id)
                      if s.state == SLICE_UNKNOWN and s.closed_at is None and _broker_id(s) is None]
        if not candidates:
            return ActionResult(False, f"{symbol}: no unresolved slice without a broker id to forget.")
        young = [s for s in candidates if _unknown_wait_left(s) > 0]
        if young:
            return ActionResult(False, f"{symbol}: the unresolved slice is only {_unknown_age_seconds(young[0]):.0f} s "
                                       f"old; the broker may not list it yet. Wait {_unknown_wait_left(young[0]):.0f} s.")
        for s in candidates:
            since = (_naive(s.placed_at) or _now()) - timedelta_seconds(600)
            try:
                found = account.find_protective_orders_by_tag(
                    s.external_tag, since=since.replace(tzinfo=timezone.utc), kind=s.kind)
            except Exception as e:  # noqa: BLE001 -- the ONE case in which forgetting is allowed
                logger.warning(f"allocator TP/SL: tag search for {s.external_tag} failed ({e}); forgetting allowed")
                continue
            return ActionResult(
                False, f"{symbol}: the tag search works (it found "
                       f"{'an order' if found else 'nothing'}), so the refresh will "
                       f"{'adopt it' if found else 'close the slice as never placed'} by itself; forgetting "
                       f"it by hand could create a double protection. Refresh the page.")
        for s in candidates:
            s.state = SLICE_LOST_REJECTED
            s.detail = (f"forgotten by the operator after checking the TastyTrade site (tag "
                        f"{s.external_tag}); the tag search could not be completed; whatever was there is "
                        f"no longer tracked")
            s.closed_at = _now()
            _save(s)
            _log(account.id, ActivityLogSeverity.WARNING,
                 f"{symbol}: the operator forgot the unresolved slice {s.slice_index + 1} "
                 f"({s.quantity} sh, tag {s.external_tag}) after checking the TastyTrade site",
                 code="UNKNOWN_FORGOTTEN", symbol=symbol, tag=s.external_tag)
        p = _reload(p.id)
        if p.alert_code == CODE_UNKNOWN_STATE and not any(
                x.state in SLICE_ALARM_STATES and x.closed_at is None for x in get_slices(p.id)):
            _clear_alert(p, [CODE_UNKNOWN_STATE])
        return ActionResult(True, f"{symbol}: {len(candidates)} unresolved slice(s) forgotten.")


@dataclass
class SliceCheck:
    """The broker's dry-run verdict on one order of the plan."""
    kind: str
    quantity: int
    tp_price: Optional[float]
    sl_price: float
    ok: bool
    bp_change: Optional[float] = None
    message: str = ""
    margin_failed: bool = False


@dataclass
class CheckReport:
    """What 'Check with broker' shows: per-order verdicts, the buying power, and a warning when the
    stops' reservation is not affordable (with an approximate stop the broker would accept)."""
    slices: List[SliceCheck] = field(default_factory=list)
    available: Optional[float] = None
    needed: Optional[float] = None
    suggested_stop: Optional[float] = None
    warning: str = ""
    notes: List[str] = field(default_factory=list)

    @property
    def all_ok(self) -> bool:
        return all(x.ok for x in self.slices) and not self.warning


#: How many dry-run rounds the stop suggestion may spend (each round is one call per order of the plan).
SUGGEST_STOP_ROUNDS = 7
#: A suggested stop is never closer to the market than this (and the search starts there): a stop one tick
#: under the price would trigger on noise. 'Use a stop the broker accepts' refuses anything nearer than
#: ``CHANGE_STOP_MIN_DISTANCE_PCT``.
SUGGEST_MIN_DISTANCE_PCT = 3.0
CHANGE_STOP_MIN_DISTANCE_PCT = 2.0


def _dry_run_plan(account, symbol: str, plans, sl_price: float) -> List[SliceCheck]:
    out = []
    for plan in plans:
        verdict = account.dry_run_protective(symbol, plan.quantity, sl_price=sl_price, tp_price=plan.tp_price)
        out.append(SliceCheck(kind=plan.kind, quantity=plan.quantity, tp_price=plan.tp_price, sl_price=sl_price,
                              ok=verdict.ok, bp_change=verdict.bp_change, message=verdict.message,
                              margin_failed=verdict.margin_failed))
    return out


def _reserved(checks: List[SliceCheck]) -> float:
    return sum(-c.bp_change for c in checks if c.ok and c.bp_change is not None and c.bp_change < 0)


def check_with_broker(account, symbol: str, sl_price: float, targets: List[TpTarget],
                      kept_filled: Optional[List[TpTarget]] = None,
                      taken: Optional[List[Optional[float]]] = None) -> CheckReport:
    """Ask the broker (DRY RUNS only, nothing is placed) whether the plan would be accepted and what it
    does to buying power. Read-only; never on page load (the dialog's explicit 'Check with broker').

    TastyTrade margin-checks a stop as if it filled at its trigger (A11): the check sums the reservation
    of the plan's stops against the account's buying power and, when it does not fit, bisects a stop the
    broker accepts (a few extra dry-run rounds, labelled approximate).
    """
    symbol = _norm(symbol)
    qty, is_long = _read_position(account, symbol)
    last = _read_price(account, symbol)
    ticks = _tick_sizes(account, symbol)
    report = CheckReport()
    shares = whole_shares(qty)
    if not is_long and qty > 0 or shares < 1:
        report.notes.append("no whole share to protect" if shares < 1 else "short position")
        return report
    raw = [dict(t.to_dict(), taken=m) if m else t.to_dict()
           for t, m in zip(targets, list(taken or []) + [None] * len(targets))]
    raw += [dict(t.to_dict(), filled=True, taken=1.0) for t in (kept_filled or [])]
    eff = effective_targets(raw, last, ticks)
    plans, notes = plan_slices(shares=shares, targets=eff.usable, sl_price=sl_price, last_price=last,
                               tick_sizes=ticks)
    report.notes.extend(notes)
    report.slices = _dry_run_plan(account, symbol, plans, plans[0].sl_price if plans else sl_price)
    try:
        report.available = account.get_account_snapshot().buying_power
    except Exception as e:  # noqa: BLE001 -- the verdicts stand without it
        logger.warning(f"allocator TP/SL: buying power unreadable for the check: {e}")
    refused = any(not c.ok and c.margin_failed for c in report.slices)
    reserved = _reserved(report.slices)
    too_much = report.available is not None and reserved > report.available + 1e-9
    if not (refused or too_much):
        report.needed = reserved if report.available is not None else None
        return report

    # Bisect the lowest stop the broker accepts, between the operator's stop and just under the market.
    tick = tick_for_price(last, ticks)
    top = round_price_to_tick(last * (1.0 - SUGGEST_MIN_DISTANCE_PCT / 100.0), tick, "down")

    def accepted(stop: float):
        checks = _dry_run_plan(account, symbol, plans, stop)
        ok = all(c.ok for c in checks) and (report.available is None or _reserved(checks) <= report.available + 1e-9)
        return ok, checks
    lo, hi = float(plans[0].sl_price), top
    suggested, at_hi = None, None
    if hi > lo:
        good, checks = accepted(hi)
        if good:
            suggested, at_hi = hi, checks
            for _ in range(SUGGEST_STOP_ROUNDS):
                mid = round_price_to_tick((lo + hi) / 2.0, tick, "down")
                if mid <= lo or mid >= hi:
                    break
                good, checks = accepted(mid)
                if good:
                    hi, suggested, at_hi = mid, mid, checks
                else:
                    lo = mid
    needed = None
    if at_hi is not None and all(c.bp_change is not None for c in at_hi):
        total_q = sum(c.quantity for c in at_hi if c.kind == ORDER_KIND_STOP or c.tp_price is not None)
        change_at_hi = sum(c.bp_change for c in at_hi)
        needed = max(0.0, -(change_at_hi + total_q * (float(plans[0].sl_price) - float(suggested))))
    report.needed = needed
    report.suggested_stop = suggested
    report.warning = (margin_sentence(needed=needed, available=report.available, suggested=suggested)
                      if suggested is not None else
                      no_acceptable_stop_sentence(report.available, SUGGEST_MIN_DISTANCE_PCT))
    return report


def change_stop_and_replace(account, symbol: str, new_sl: float) -> ActionResult:
    """The operator's explicit 'Use a stop the broker accepts': set the stop and re-place the protection.

    The stop is never changed silently: this is the action behind a button. A stop that is not below the
    market is refused; if the re-placement places nothing with the new stop the old stop is put back.
    """
    symbol = _norm(symbol)
    with protection_lock(account.id):
        p = get_protection(account.id, symbol)
        if p is None or not p.enabled:
            return ActionResult(False, f"{symbol}: protection is not enabled.")
        try:
            last = _read_price(account, symbol)
        except BrokerReadError as e:
            return ActionResult(False, f"Cannot change the stop: {e}")
        if not (0 < float(new_sl) < last):
            return ActionResult(False, f"{symbol}: the stop {new_sl:g} must be above 0 and BELOW the current "
                                       f"price {last:g}.")
        if float(new_sl) > last * (1.0 - CHANGE_STOP_MIN_DISTANCE_PCT / 100.0):
            return ActionResult(False, f"{symbol}: the stop must be at least {CHANGE_STOP_MIN_DISTANCE_PCT:g}% "
                                       f"below the market ({last:g}); {new_sl:g} is too close.")
        old = p.sl_price
        p.sl_price = float(new_sl)
        _save(p)
        result = replace_protection(account, symbol)
        if not result.ok:
            fresh = _reload(p.id)
            if not any(_is_resting(x) and abs(float(x.sl_price) - float(new_sl)) < 0.0101 for x in get_slices(p.id)):
                fresh.sl_price = old
                _save(fresh)
        return result


def replace_protection(account, symbol: str) -> ActionResult:
    """Resize / re-place: cancel what is live (confirmed) and place a fresh set from the stored
    numbers at the CURRENT held quantity. Renews the GTC lifetime and repairs a lost, partial or
    size-mismatched protection."""
    symbol = _norm(symbol)
    with protection_lock(account.id):
        p = get_protection(account.id, symbol)
        if p is None or not p.enabled:
            return ActionResult(False, f"{symbol}: protection is not enabled.")
        if p.pending_replace:
            return ActionResult(False, f"{symbol}: protective orders are being re-placed after a "
                                       f"rebalance; wait for that to finish.")
        if _run_in_flight(account):
            return ActionResult(False, f"{symbol}: an allocator run is in flight; it cancels and "
                                       f"re-places protective orders itself. Try again when it has finished.")
        p.auto_failures = 0
        p.expected_qty = None
        _save(p)
        errors = _preflight_errors(account, p)
        if errors:
            _alert(p, CODE_PLACEMENT_REFUSED, "the new orders would be refused, so the existing ones were "
                                              "KEPT: " + " ".join(errors))
            return ActionResult(False, f"{symbol}: the new orders would be refused, so the existing ones "
                                       f"were KEPT.", errors=errors)
        cancel_since = _now() - timedelta_seconds(2)
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
        p = _rollback_if_uncovered(account, p, cancel_since, "the re-placement")
        return ActionResult(False, f"{symbol}: protection was NOT fully placed (the previous orders were "
                                   f"restored where possible).", errors=placed.errors, notes=placed.notes)


# =========================================================================================
# reconcile
# =========================================================================================

#: A non-terminal platform order on the symbol only counts as "the position is not final" when it was
#: created AFTER the protection was cancelled (``pending_replace_since``) or, with nothing pending,
#: within this window: a stale or expert order from last week must not keep a symbol 'Re-placing'.
WORKING_ORDER_WINDOW_SECONDS = 2 * 3600


def _naive(value: Optional[DateTime]) -> Optional[DateTime]:
    """Naive UTC. An aware time is CONVERTED to UTC first (stripping the tzinfo of a +05:00 time would
    shift it by five hours); a naive one is taken to be UTC already."""
    if value is None:
        return None
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def _working_order_symbols(account_id: int, symbols: Iterable[str],
                           since: Optional[Dict[str, Optional[DateTime]]] = None) -> Set[str]:
    """Symbols with a still-working platform order created after ``since[symbol]`` (default: within
    ``WORKING_ORDER_WINDOW_SECONDS``): an order in flight means the position is not final yet."""
    wanted = {_norm(s) for s in symbols}
    if not wanted:
        return set()
    terminal = set(OrderStatus.get_terminal_statuses()) | {OrderStatus.FILLED}
    default_since = _now() - timedelta_seconds(WORKING_ORDER_WINDOW_SECONDS)
    with get_db() as session:
        rows = session.exec(select(TradingOrder).where(
            TradingOrder.account_id == int(account_id),
            TradingOrder.symbol.in_(list(wanted)))).all()
    out = set()
    for o in rows:
        if o.status in terminal:
            continue
        marker = _naive((since or {}).get(_norm(o.symbol)))
        # The sale that triggered the cancel is created a moment BEFORE ``pending_replace_since``
        # (its row exists before the guard runs): allow a few minutes of slack, never days.
        floor = (marker - timedelta_seconds(300)) if marker is not None else default_since
        created = _naive(o.created_at)
        if created is None or created >= floor:
            out.add(_norm(o.symbol))
    return out


def timedelta_seconds(seconds: float):
    from datetime import timedelta
    return timedelta(seconds=seconds)


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


def _describe_config(p: AllocatorProtection) -> str:
    targets = targets_from_dicts(p.tp_targets)
    parts = [f"SL {p.sl_price:g}"] + [f"TP {t.price:g}@{t.fraction * 100:.0f}%" for t in targets]
    return ", ".join(parts) if targets else f"SL {p.sl_price:g}, stop only"


def _disarm(p: AllocatorProtection, why: str) -> AllocatorProtection:
    """The position was EXITED: clear the protection settings and keep a history record.

    A later buy is NOT auto-protected; the operator sets TP/SL again on re-entry. The cleared
    numbers survive as ``disarmed_note`` (and in the activity log).
    """
    if any(_blocks(x) for x in get_slices(p.id)):
        # N5: never disarm while an order rests (or may rest): it would be left untracked at the broker.
        return _alert(p, CODE_QUANTITY_MISMATCH,
                      f"the position reads as exited but protective orders still rest ({why}); NOT "
                      f"disarmed -- check the position on the TastyTrade site")
    note = _describe_config(p) if p.sl_price else (p.disarmed_note or "")
    p.enabled = False
    p.sl_price = 0.0
    p.tp_targets = []
    p.pending_replace = False
    p.pending_replace_since = None
    p.auto_failures = 0
    p.expected_qty = None
    p.disarmed_at = _now()
    p.disarmed_note = note
    p.last_error = None
    p.alert_code = None
    p.alert_message = None
    _save(p)
    _ack_alarm_slices(p)
    _log(p.account_id, ActivityLogSeverity.INFO,
         f"{p.symbol}: TP/SL disarmed ({why}); the settings were cleared (was: {note}). Set TP/SL again "
         f"on re-entry.", code="DISARMED", symbol=p.symbol, was=note)
    logger.info(f"allocator TP/SL: {p.symbol} disarmed ({why}); was {note}")
    return _reload(p.id)


def _auto_allowed(p: AllocatorProtection) -> bool:
    return int(p.auto_failures or 0) < AUTO_FAILURE_LIMIT


def _interval_ok(p: AllocatorProtection) -> bool:
    last = _naive(p.last_auto_action_at)
    return last is None or (_now() - last).total_seconds() >= AUTO_ACTION_MIN_INTERVAL_SECONDS


def _note_auto_result(p: AllocatorProtection, ok: bool, what: str) -> AllocatorProtection:
    """Count consecutive failures of the AUTOMATIC paths; stop retrying at the limit (loud)."""
    p = _reload(p.id)
    if ok:
        p.auto_failures = 0
    else:
        p.auto_failures = int(p.auto_failures or 0) + 1
    _save(p)
    if not ok and p.auto_failures >= AUTO_FAILURE_LIMIT:
        p = _alert(p, CODE_AUTO_STOPPED,
                   f"{what} failed {p.auto_failures} times in a row; the automatic paths have STOPPED "
                   f"for {p.symbol}. Fix the cause, then Resize protection: that resets the failure count and re-arms them.")
    return _reload(p.id)


RESTORED_MARK = "(restored)"


def _snapshot_qty(x: AllocatorProtectionOrder) -> int:
    """Shares a cancelled slice still had to sell: all of it, or what a part-filled one had left."""
    if x.state in _FILLED_STATES:
        return max(0, int(round(float(x.quantity) - float(x.filled_qty))))
    return int(x.quantity)


def _cancelled_snapshot(p: AllocatorProtection, since: DateTime) -> List[AllocatorProtectionOrder]:
    """The slices WE cancelled at or after ``since`` and have not restored: the previous plan. A part-filled
    slice whose remainder we cancelled counts with its unfilled rest."""
    out = []
    for x in get_slices(p.id):
        closed = _naive(x.closed_at)
        if closed is None or closed < since or RESTORED_MARK in (x.detail or ""):
            continue
        cancelled = x.state == SLICE_CANCELLED_BY_US or (
            x.state in _FILLED_STATES and x.cancel_requested and _snapshot_qty(x) > 0)
        if cancelled:
            out.append(x)
    return sorted(out, key=lambda x: x.slice_index)


def _restore_plan(snapshot, left: int, last: float, stop_only: bool):
    """What to put back: ``[(kind, quantity, tp, sl, target)]``, STOPS first.

    The previous plan is scaled to the shares held (floor of ``left / snapshot_total``) and the remainder
    goes to a plain stop, so a shrunk position keeps the plan's proportions. ``stop_only`` (a LOST alarm is
    open, or part of a NEW set already rests) puts everything back as plain stops: the runner is never put on
    a take-profit, and a take-profit at or below the market comes back as a stop too."""
    items = [(old, _snapshot_qty(old)) for old in snapshot]
    items = [(old, q) for old, q in items if q >= 1]
    total = sum(q for _, q in items)
    if not items or left < 1:
        return []
    scale = 1.0 if (stop_only or total <= left) else left / total
    plans, used = [], 0
    for old, q in items:
        qq = q if scale >= 1.0 else int(q * scale)
        qq = min(qq, left - used)
        if qq < 1:
            continue
        used += qq
        as_stop = (old.kind == ORDER_KIND_STOP or stop_only or old.tp_price is None
                   or float(old.tp_price) <= last)
        plans.append((ORDER_KIND_STOP if as_stop else ORDER_KIND_OCO, qq, None if as_stop else old.tp_price,
                      old.sl_price, -1 if as_stop else old.target_index))
    if scale < 1.0 and left - used >= 1:
        plans.append((ORDER_KIND_STOP, left - used, None, items[0][0].sl_price, -1))
    plans.sort(key=lambda t: 0 if t[0] == ORDER_KIND_STOP else 1)
    return plans


def _restore_previous(account, p: AllocatorProtection, snapshot, left: int, last: float,
                      alarm_open: bool, stop_only: bool = False) -> bool:
    """Place the PREVIOUS orders again (see ``_restore_plan``), validated against the market now: a stop at or
    above the price is skipped. True when every share asked for is covered."""
    ok = True
    existing = len(get_slices(p.id))
    plans = _restore_plan(snapshot, left, last, stop_only or alarm_open)
    for n, (kind, q, tp, sl_price, target) in enumerate(plans):
        if float(sl_price) >= last:
            _log(p.account_id, ActivityLogSeverity.FAILURE,
                 f"{p.symbol}: the previous stop {float(sl_price):g} is at or above the market ({last:g}); "
                 f"{q} sh were NOT restored", code="ROLLBACK_SKIPPED", symbol=p.symbol)
            ok = False
            continue
        tag = f"ba2prot:{p.id}:{existing + n}:{uuid.uuid4().hex[:8]}"
        row = _save(AllocatorProtectionOrder(
            protection_id=p.id, slice_index=existing + n, target_index=target, kind=kind,
            quantity=q, tp_price=tp, sl_price=sl_price, external_tag=tag,
            state=SLICE_PLACING, placed_at=_now()))
        try:
            if kind == ORDER_KIND_STOP:
                placed = account.place_protective_stop(symbol=p.symbol, quantity=q, sl_price=sl_price, tag=tag)
            else:
                placed = account.place_protective_oco(symbol=p.symbol, quantity=q, tp_price=tp,
                                                      sl_price=sl_price, tag=tag)
        except PlacementOutcomeUnknown as e:
            row.state = SLICE_UNKNOWN
            row.detail = str(e)[:500]
            _save(row)
            return False
        except Exception as e:  # noqa: BLE001 -- the rollback failing is reported by the caller
            row.state = SLICE_LOST_REJECTED
            row.detail = str(e)[:500]
            _save(row)                 # stays an open alarm: it blocks an automatic retry
            logger.error(f"allocator TP/SL: rollback placement for {p.symbol} failed: {e}")
            ok = False
            continue
        if kind == ORDER_KIND_STOP:
            row.sl_order_id = placed.order_id
            row.sl_price = placed.sl_price
        else:
            row.complex_order_id = placed.complex_order_id
            row.tp_order_id = placed.tp_order_id
            row.sl_order_id = placed.sl_order_id
            row.tp_price, row.sl_price = placed.tp_price, placed.sl_price
        row.state = SLICE_LIVE
        row.gtc_date = (DateTime(placed.gtc_date.year, placed.gtc_date.month, placed.gtc_date.day)
                        if placed.gtc_date is not None else None)
        row.placed_at = _now()
        _save(row)
        left -= q
    for old in snapshot:
        old.detail = f"{old.detail or ''} {RESTORED_MARK}".strip()
        _save(old)
    return ok and left <= 0


def _rollback_if_uncovered(account, p: AllocatorProtection, since: DateTime, why: str) -> AllocatorProtection:
    """A re-placement failed AFTER the confirmed cancel: put the PREVIOUS orders back (capped to the shares
    held, validated against the market now), loudly, and say how many shares are protected. If nothing could
    be restored the position is UNPROTECTED: an activity-log FAILURE and an alert."""
    p = _reload(p.id)
    if any(x.state == SLICE_UNKNOWN and x.closed_at is None for x in get_slices(p.id)):
        return p                                   # an order may exist: never place a second one on a guess
    snapshot = _cancelled_snapshot(p, since)
    if not snapshot:
        return p
    try:
        qty, is_long = _read_position(account, p.symbol)
        last = _read_price(account, p.symbol)
    except BrokerReadError as e:
        _log(p.account_id, ActivityLogSeverity.FAILURE, f"{p.symbol}: {why}; the position or price could not be "
             f"read for the rollback ({e}): NOT restored", code="ROLLBACK_FAILED", symbol=p.symbol)
        return p
    whole = whole_shares(qty)
    left = whole - int(round(covered_quantity(get_slices(p.id))))
    if not is_long or left < 1:
        return p
    # A LOST order from BEFORE this attempt (the operator cancelled it): never brought back as a take-profit.
    # This attempt's own refused rows are not that.
    cut = min((_naive(x.closed_at) for x in snapshot), default=since)     # the moment WE cancelled
    alarm_open = any(x.state in SLICE_ALARM_STATES and x.state != SLICE_UNKNOWN and x.closed_at is None
                     and (_naive(x.placed_at) or cut) < cut for x in get_slices(p.id))
    refused_text = p.alert_message or ""
    part_placed = covered_quantity(get_slices(p.id)) > 0          # some of the NEW set rests: no take-profit again
    ok = _restore_previous(account, p, snapshot, left, last, alarm_open, stop_only=part_placed)
    for x in (get_slices(p.id) if ok else []):     # a restored plan: this attempt's refused rows are history
        placed_at = _naive(x.placed_at)
        if x.state == SLICE_LOST_REJECTED and x.closed_at is None and placed_at is not None and placed_at >= since:
            x.closed_at = _now()
            x.detail = f"{x.detail or ''} (refused; rolled back)".strip()
            _save(x)
    p = _reload(p.id)
    refused_text = refused_text or (p.last_error or "")
    covered = int(round(covered_quantity(get_slices(p.id))))
    counts = f"{covered} of {whole} shares protected" + (f", {whole - covered} UNPROTECTED" if covered < whole else "")
    if ok:
        _log(p.account_id, ActivityLogSeverity.WARNING,
             f"{p.symbol}: {why}: the new orders were refused, so the PREVIOUS protective orders were restored "
             f"({counts})", code="ROLLBACK", symbol=p.symbol)
        return _alert(p, CODE_REPLACE_FAILED, f"{why}: the new orders were refused (see the log); the previous "
                      f"protective orders were restored: {counts}. Refused: {refused_text}")
    _log(p.account_id, ActivityLogSeverity.FAILURE,
         f"{p.symbol}: {why}: the new orders were refused and the previous orders could not be fully restored "
         f"({counts})", code="ROLLBACK_FAILED", symbol=p.symbol)
    return _alert(p, CODE_REPLACE_FAILED, f"{why}: the new orders were refused and the previous orders could not "
                  f"be fully restored: {counts}. Set TP/SL again or free buying power. Refused: {refused_text}")


def _preflight_bp(account, p: AllocatorProtection, shares: int, last: float, ticks) -> List[str]:
    """Would the broker afford the new stop AFTER the old orders are cancelled? (A11)

    1. Dry-run the REAL stop at the real quantity. Accepted (even with the old orders still reserving buying
       power) means it fits for certain: no error. A non-margin refusal is not a buying-power question.
    2. Refused for margin: estimate with a shallow reference stop (it yields the broker's own p0):
       need = shares x (p0 - stop) against the buying power available now PLUS what the orders about to be
       cancelled reserve. A reference change of 0 or None is not an estimate; a need of 0 or less never blocks.
    Empty when it fits or nothing can be estimated (the rollback then covers a refusal)."""
    dry = getattr(account, "dry_run_protective", None)
    if dry is None:
        return []
    try:
        available = account.get_account_snapshot().buying_power
        if available is None:
            return []
        real = dry(p.symbol, shares, sl_price=float(p.sl_price))
        if real.ok or not real.margin_failed:
            return []
        tick = tick_for_price(last, ticks)
        ref = round_price_to_tick(last * 0.9, tick, "down")
        verdict = dry(p.symbol, 1, sl_price=ref)
    except Exception as e:  # noqa: BLE001 -- an estimate that cannot be made blocks nothing
        logger.warning(f"allocator TP/SL: buying-power pre-check for {p.symbol} unavailable: {e}", exc_info=True)
        return []
    if not verdict.ok or not verdict.bp_change:
        return []
    p0 = ref - float(verdict.bp_change)
    need = shares * (p0 - float(p.sl_price))
    if need <= 0:
        return []
    reserved = sum(max(0.0, _remaining_qty(x) * (p0 - float(x.sl_price)))
                   for x in get_slices(p.id) if _is_resting(x))
    if need <= float(available) + reserved + 1e-9:
        return []
    return [margin_sentence(needed=need, available=float(available) + reserved)
            + " The existing protective orders were NOT touched."]


def _preflight_errors(account, p: AllocatorProtection) -> List[str]:
    """What a re-placement from the stored template would be refused for, computed from a FRESH position
    and price BEFORE anything is cancelled (N8). Empty means it would be accepted. Targets already
    reached are dropped (folded into the stop-only remainder) exactly as ``_place_slices`` does."""
    try:
        qty, is_long = _read_position(account, p.symbol)
        last = _read_price(account, p.symbol)
    except BrokerReadError as e:
        return [f"position or price unreadable: {e}"]
    if not is_long and qty > 0:
        return [f"{p.symbol} is a short position"]
    shares = whole_shares(qty)
    if shares < 1:
        return []
    ticks = _tick_sizes(account, p.symbol)
    usable = effective_targets(p.tp_targets, last, ticks).usable
    errors = validate_protection(sl_price=p.sl_price, targets=usable, last_price=last,
                                 position_quantity=float(shares), tick_sizes=ticks)
    return errors or _preflight_bp(account, p, shares, last, ticks)


def _resize_now(account, p: AllocatorProtection, reason: str) -> Tuple[AllocatorProtection, bool]:
    """Cancel (confirmed) and re-place from the stored template at the CURRENT quantity: the
    background resize / GTC renewal. Callers guarantee: no run in flight, nothing pending, no
    unresolved UNKNOWN. ``pending_replace`` is set BEFORE the first cancel. One activity-log entry
    each time; a failure is loud and counted."""
    errors = _preflight_errors(account, p)
    if errors:
        # N8: plan and validate BEFORE cancelling; a re-placement that would be refused leaves the
        # existing orders exactly as they are.
        p.last_auto_action_at = _now()
        _save(p)
        p = _alert(p, CODE_PLACEMENT_REFUSED,
                   f"the {reason} was NOT attempted (the existing orders were kept): " + " ".join(errors))
        p = _note_auto_result(p, False, f"the {reason}")
        _log(p.account_id, ActivityLogSeverity.FAILURE,
             f"{p.symbol}: automatic {reason} skipped, existing orders kept: " + " ".join(errors),
             code="AUTO_RESIZE_FAILED", symbol=p.symbol, reason=reason)
        return p, False
    p.pending_replace = True
    p.pending_replace_since = _now()
    p.expected_qty = None
    p.last_auto_action_at = _now()
    _save(p)
    cancel_since = _now() - timedelta_seconds(2)
    p, cancelled = _cancel_live_slices(account, p)
    if not cancelled.all_confirmed:
        p.pending_replace = False
        p.pending_replace_since = None
        _save(p)
        p = _note_auto_result(p, False, f"the {reason} (cancel not confirmed)")
        _log(p.account_id, ActivityLogSeverity.FAILURE,
             f"{p.symbol}: automatic {reason} failed: the cancel was not confirmed",
             code="AUTO_RESIZE_FAILED", symbol=p.symbol, reason=reason)
        return p, False
    p, placed = _place_slices(account, p)
    if not placed.ok:
        p = _rollback_if_uncovered(account, p, cancel_since, f"the {reason}")
    p = _reload(p.id)
    p.pending_replace = False
    p.pending_replace_since = None
    p.expected_qty = None
    _save(p)
    p = _note_auto_result(p, placed.ok, f"the {reason}")
    _log(p.account_id, ActivityLogSeverity.SUCCESS if placed.ok else ActivityLogSeverity.FAILURE,
         f"{p.symbol}: automatic {reason}: " + (f"{placed.placed} protective order(s) re-placed"
                                                if placed.ok else "re-placement FAILED: " + " | ".join(placed.errors)),
         code="AUTO_RESIZE" if placed.ok else "AUTO_RESIZE_FAILED", symbol=p.symbol, reason=reason)
    return p, placed.ok


@dataclass
class ReconcileReport:
    checked: int = 0
    new_fills: List[str] = field(default_factory=list)
    alarms: List[str] = field(default_factory=list)
    failed_symbols: List[str] = field(default_factory=list)
    resumed: List[str] = field(default_factory=list)
    extended: List[str] = field(default_factory=list)
    resized: List[str] = field(default_factory=list)
    disarmed: List[str] = field(default_factory=list)


#: ``(account id, symbol) -> whole shares`` seen the LAST refresh while the orders covered more than was held.
#: A shrink is acted on only when the SAME smaller position shows on two consecutive refreshes.
_SHRINK_SEEN: Dict[Tuple[int, str], Tuple[int, DateTime]] = {}
#: Keys the CURRENT reconcile call reached the shrink rule for; a call that returned earlier (pending, an alarm,
#: a fill settling, no whole share) clears its key, so the two sightings are strictly consecutive.
_SHRINK_TOUCHED: Set[Tuple[int, str]] = set()
#: The second sighting must be at least this long after the first (about one refresh interval): a refresh and
#: 'Check and repair' landing seconds apart are one observation, not two.
SHRINK_CONFIRM_SECONDS = 60


def _reconcile_one(account, p: AllocatorProtection, report: ReconcileReport,
                   position: Optional[Tuple[float, bool]], may_place: bool = False) -> None:
    key = (account.id, p.symbol)
    _SHRINK_TOUCHED.discard(key)
    try:
        _reconcile_one_inner(account, p, report, position, may_place)
    finally:
        if key not in _SHRINK_TOUCHED:
            _SHRINK_SEEN.pop(key, None)


def _reconcile_one_inner(account, p: AllocatorProtection, report: ReconcileReport,
                         position: Optional[Tuple[float, bool]], may_place: bool = False) -> None:
    """Reconcile one protection with the broker.

    ``may_place`` True lets it act on its own (growth, shrink resize, disarm); ``prepare_for_trade``
    calls it False because it is about to cancel everything anyway.
    """
    fills: list = []
    fills_before = p.last_fill_at
    for s in get_slices(p.id):
        if s.state not in _RECONCILABLE_STATES or (s.closed_at is not None and s.state != SLICE_UNKNOWN):
            continue
        if _broker_id(s) is None:
            age = (_now() - s.placed_at).total_seconds() if s.placed_at else 0
            if s.state == SLICE_UNKNOWN or (s.state == SLICE_PLACING and age > PLACING_STALE_SECONDS):
                if s.state == SLICE_PLACING:
                    s.state = SLICE_UNKNOWN
                    s.detail = "a placement never reported back; an order may exist at the broker"
                    _save(s)
                if not _resolve_unknown_slice(account, _reload(p.id), s):
                    left = _unknown_wait_left(s)
                    if left > 0:
                        message = (f"slice {s.slice_index + 1} (tag {s.external_tag}) is UNKNOWN: an order may "
                                   f"exist at the broker; waiting {left:.0f} s before concluding it was never "
                                   f"placed (no second order is placed meanwhile)")
                    else:
                        message = (f"slice {s.slice_index + 1} (tag {s.external_tag}) is UNKNOWN and the "
                                   f"tag search could not be completed; an order may exist at the broker")
                    p = _alert(_reload(p.id), CODE_UNKNOWN_STATE, message)
                    report.alarms.append(p.symbol)
                p = _reload(p.id)
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
        p = _apply_observation(account, p, s, obs, fills)
        if obs.state in SLICE_ALARM_STATES and before != obs.state:
            report.alarms.append(p.symbol)
    p = _flush_fills(account, _reload(p.id), fills)
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

    has_alarm = any(x.state in SLICE_ALARM_STATES and x.closed_at is None for x in slices)
    if (not p.enabled and p.alert_code and not any(_blocks(x) for x in slices) and not has_alarm):
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
            p.expected_qty = None
            _save(p)
            _clear_alert(p, [CODE_QUANTITY_MISMATCH, CODE_CANCEL_UNCONFIRMED, CODE_REPLACE_STALE])
        else:
            since = _naive(p.pending_replace_since)
            if since is not None and (_now() - since).total_seconds() > PENDING_REPLACE_STALE_SECONDS:
                _alert(p, CODE_REPLACE_STALE,
                       "the protective orders were cancelled for a trade and the replacement has not "
                       "been placed for over 15 minutes; the position is UNPROTECTED. Use Resize "
                       "protection.")
                report.alarms.append(p.symbol)
        return
    if has_alarm:
        return
    fresh = _naive(p.last_fill_at)
    if fresh is not None and (_now() - fresh).total_seconds() < FILL_SETTLE_SECONDS:
        return        # the broker's position read may lag a fill: wait before acting on a mismatch

    # EXITED: nothing whole is held any more. Resting orders on a position we read as flat is a
    # mismatch to look at (a position read that is wrong must not disarm a live protection).
    if n_whole == 0:
        if live > 0:
            _alert(p, CODE_QUANTITY_MISMATCH,
                   f"live protective orders cover {live:g} sh but no whole share is held; check the "
                   f"position on the TastyTrade site", severity=ActivityLogSeverity.FAILURE)
            report.alarms.append(p.symbol)
        elif may_place:
            _disarm(p, "the position was exited")
            report.disarmed.append(p.symbol)
        return

    working = _working_order_symbols(account.id, [p.symbol])
    if live <= n_whole + 1e-9:
        _SHRINK_SEEN.pop((account.id, p.symbol), None)
    # GROWTH: whole shares nobody covers (a manual buy, a DRIP). Protection is ADDED for exactly
    # those shares -- no cancel, so there is NO gap in which existing shares are unprotected.
    if n_whole > live + 1e-9:
        if not may_place or working or not _auto_allowed(p):
            if may_place and not _auto_allowed(p):
                _alert(p, CODE_AUTO_STOPPED, f"{n_whole - live:g} whole share(s) are uncovered and the "
                       f"automatic paths are stopped for {p.symbol}; use Resize protection")
            return
        p, placed = _place_slices(account, p)
        p = _note_auto_result(p, placed.ok, "adding protection for grown shares")
        if placed.placed:
            report.extended.append(p.symbol)
            _log(account.id, ActivityLogSeverity.INFO,
                 f"{p.symbol}: the position grew outside the allocator; added protection for "
                 f"{placed.shares_covered} sh (the existing orders were not touched)",
                 code="EXTENDED", symbol=p.symbol, added=placed.shares_covered)
        if not placed.ok:
            report.alarms.append(p.symbol)
        elif p.alert_code and p.alert_code not in WARNING_CODES:
            _clear_alert(p)
        return
    # SHRINK: live orders cover MORE than is held. Resized in the background (cancel, confirm,
    # re-place at the held quantity), rate-limited, with an activity-log entry each time; never
    # during a run, never with a trade in flight, never with an unresolved UNKNOWN.
    if live > n_whole + 1e-9:
        if not may_place or working:
            return
        key = (account.id, p.symbol)
        _SHRINK_TOUCHED.add(key)
        seen = _SHRINK_SEEN.get(key)
        if seen is None or seen[0] != n_whole:
            _SHRINK_SEEN[key] = (n_whole, _now())   # first sighting (could be one stale read): wait for a second
            return
        if (_now() - seen[1]).total_seconds() < SHRINK_CONFIRM_SECONDS:
            return
        if not _auto_allowed(p) or not _interval_ok(p):
            if not _auto_allowed(p):
                _alert(p, CODE_QUANTITY_MISMATCH,
                       f"live protective orders cover {live:g} sh but {n_whole} whole share(s) are held "
                       f"and the automatic resize has stopped; use Resize protection",
                       severity=ActivityLogSeverity.FAILURE)
                report.alarms.append(p.symbol)
            return
        p, ok = _resize_now(account, p, "resize after the position shrank")
        (report.resized if ok else report.alarms).append(p.symbol)
        return
    if p.alert_code and p.alert_code not in WARNING_CODES:
        # Matched and no alarm slice: every open failure alert is stale.
        _clear_alert(p)


def renew_expiring(account) -> List[str]:
    """GTC renewal, run by the account refresh (``TradeManager``), never on page load.

    A resting slice whose BROKER-REPORTED ``gtc_date`` is within ``GTC_RENEW_DAYS`` is renewed by a
    confirmed cancel and a re-placement from the stored template (same prices). A slice with no
    ``gtc_date`` is never renewed. Safety, as everywhere: never while an allocator run is in
    flight, never while a re-placement is pending or an UNKNOWN/alarm slice exists, one symbol per
    call (the one expiring soonest, so many orders sharing a date spread over refresh cycles and
    each still renews before its date), rate-limited per symbol, an activity-log entry per renewal,
    a loud alert on failure. Returns the symbols renewed. Never raises.
    """
    renewed: List[str] = []
    if not has_protection(account):
        return renewed
    try:
        if _run_in_flight(account):
            return renewed
        with protection_lock(account.id):
            today = _now().date()
            due = []
            for p in list_protections(account.id):
                if not p.enabled or p.pending_replace or not _auto_allowed(p) or not _interval_ok(p):
                    continue
                slices = get_slices(p.id)
                if any((x.state in SLICE_ALARM_STATES and x.closed_at is None) for x in slices):
                    continue
                dates = [x.gtc_date for x in slices if _is_resting(x) and x.gtc_date is not None]
                if not dates:
                    continue
                soonest = min(dates)
                if (soonest.date() - today).days <= GTC_RENEW_DAYS:
                    due.append((soonest, p.id))
            if not due:
                return renewed
            due.sort()
            p = _reload(due[0][1])
            if _working_order_symbols(account.id, [p.symbol]):
                return renewed
            p, ok = _resize_now(account, p, "GTC renewal")
            if ok:
                renewed.append(p.symbol)
            else:
                _alert(p, CODE_REPLACE_FAILED, "the GTC renewal failed; the orders expire soon -- use "
                       "Resize protection")
    except Exception as e:  # noqa: BLE001 -- never raises into the refresh
        logger.error(f"allocator TP/SL: renewal failed: {e}", exc_info=True)
        _log(account.id, ActivityLogSeverity.FAILURE, f"GTC renewal raised {type(e).__name__}: {e}",
             code="RENEW_ERROR")
    return renewed


def before_sale(account, symbol: str, quantity: Optional[float] = None) -> Optional[str]:
    """Called before ANY SELL the platform sends on a TastyTrade symbol outside the allocator run
    (expert exits, Live Trades manual close, Smart Risk Manager, the breached-stop force close all
    reach ``TastyTradeAccount._submit_order_impl``): resting protective orders reserve the shares the
    sale needs, so they are cancelled (broker-confirmed) first and re-placement is owed
    (``pending_replace``; the refresh completes it once no order is in flight).

    Returns ``None`` when the sale may go, else the reason it must not (the cancel was not
    confirmed, so the shares may still be reserved: the sale is refused, never sent blind). The
    allocator's own sells find nothing resting (``prepare_for_trade`` already cancelled) and pass.
    """
    if not has_protection(account):
        return None
    symbol = _norm(symbol)
    # N6: the cheap lookup first, WITHOUT the account lock -- but ONLY where it is safe: no row, or a
    # protection that is OFF and has nothing resting. An ENABLED protection always takes the lock: a
    # re-placement holding it may be between its reads and its PLACING row, with no slice visible yet, and
    # a sale that slipped past would be left with an OCO and a stop on shares it is selling.
    p = get_protection(account.id, symbol)
    if p is None or (not p.enabled and not any(_blocks(x) for x in get_slices(p.id))):
        return None
    with protection_lock(account.id):
        p = get_protection(account.id, symbol)
        if p is None or not any(_blocks(x) for x in get_slices(p.id)):
            return None
        if p.enabled:
            p.pending_replace = True
            p.pending_replace_since = _now()
            p.expected_qty = None
            if quantity is not None:
                try:
                    held, _ = _read_position(account, symbol)
                    p.expected_qty = max(0.0, held - float(quantity))     # N7: what the broker should read after
                except Exception:  # noqa: BLE001 -- only a hint
                    p.expected_qty = None
            _save(p)
        p, cancelled = _cancel_live_slices(account, p)
        if not cancelled.all_confirmed:
            return ("its protective TP/SL orders could not be confirmed cancelled, so the shares may "
                    "still be reserved at the broker (" + "; ".join(cancelled.detail) + ")")
        _log(account.id, ActivityLogSeverity.INFO,
             f"{symbol}: protective orders cancelled before a sale by the platform; they are "
             f"re-placed at the new quantity once the sale settles", code="CANCELLED_FOR_SALE",
             symbol=symbol)
        return None


def _sale_outcome(account_id: int, symbol: str, since: Optional[DateTime]) -> str:
    """What became of the platform's SELL of ``symbol`` made around ``since``: ``"filled"`` (any filled
    sale), ``"failed"`` (sales exist and none filled) or ``"none"``. The protective fills' own synthetic
    sales are not counted."""
    floor = (since - timedelta_seconds(300)) if since is not None else \
        _now() - timedelta_seconds(WORKING_ORDER_WINDOW_SECONDS)
    with get_db() as session:
        rows = session.exec(select(TradingOrder).where(
            TradingOrder.account_id == int(account_id), TradingOrder.symbol == symbol,
            TradingOrder.side == OrderDirection.SELL)).all()
    sales = [o for o in rows
             if (_naive(o.created_at) is None or _naive(o.created_at) >= floor)
             and (o.data or {}).get("source") != "allocator_protection"]
    if not sales:
        return "none"
    if any(o.status == OrderStatus.FILLED or (o.filled_qty is not None and float(o.filled_qty) > 0) for o in sales):
        return "filled"
    return "failed"


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

    Detects fills (note, weight reduction, books), lost/expired/cancelled orders (-> alarm),
    resolves UNKNOWN placements by tag, ADDS protection for shares that appeared outside the
    allocator (growth, no cancel), resizes after a shrink (confirmed cancel + re-place,
    rate-limited), disarms an exited position, completes a ``pending_replace`` that WE started once
    no order is in flight. Does NOT re-place a lost order (the operator may have cancelled it on the
    broker's site on purpose). GTC renewal is ``renew_expiring``.
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
    could not be cancelled, so shares may still be reserved).
    ``pending`` -- symbols whose protection was cancelled and needs ``resume_protection``.
    ``filled`` -- symbols for which a protective fill was OBSERVED while preparing (an unseen SL/TP
    sale, or a fill that landed during the cancel). The plan for them was sized on the PRE-fill
    position, so the run must DROP the row and report it SKIPPED ('re-run the dry run'); their
    protection is still re-placed (they are also in ``pending``).
    """
    blocked: Dict[str, str] = field(default_factory=dict)
    pending: List[str] = field(default_factory=list)
    filled: List[str] = field(default_factory=list)


def has_protection(account) -> bool:
    return getattr(account, "supports_allocator_protection", False) is True


def prepare_for_trade(account, symbols: Iterable[str]) -> PrepareResult:
    """Before the allocator SELLS ``symbols``: reconcile, cancel (confirmed), mark pending.

    Called under the allocator's submission lock, after the run's gates passed and before the plan
    is recorded; ``symbols`` are the symbols the plan SELLS (a buy needs no cancel: the shares it
    adds are covered afterwards by the growth rule). Every protected symbol has ALL its protective
    orders (OCOs and stop-only) cancelled and the cancel CONFIRMED. A symbol whose cancel cannot be
    confirmed is ``blocked`` -- ENABLED OR NOT: an UNKNOWN slice or a CANCELLING one of a disabled
    protection still reserves shares. ``pending_replace`` is written BEFORE the first cancel. A
    protection never excludes the symbol, but a symbol with a fill observed here is returned in
    ``filled`` (see ``PrepareResult``).
    """
    result = PrepareResult()
    if not has_protection(account):
        return result
    with protection_lock(account.id):
        for symbol in sorted({_norm(s) for s in symbols}):
            p = get_protection(account.id, symbol)
            if p is None:
                continue
            if p.enabled and not _auto_allowed(p) and any(_blocks(x) for x in get_slices(p.id)):
                # The automatic paths are stopped: cancelling here could not be undone by an automatic
                # re-placement. Leave the orders alone and keep the symbol out of this run.
                result.blocked[symbol] = ("its TP/SL automatic paths are stopped (an alert is open); its "
                                          "protective orders were left untouched. Re-run after fixing the alert")
                continue
            fills_before = p.last_fill_at
            if any(_blocks(x) for x in get_slices(p.id)):
                _reconcile_one(account, p, ReconcileReport(), None, may_place=False)
                p = _reload(p.id)
            blocking = any(_blocks(x) for x in get_slices(p.id))
            if not blocking and not p.enabled:
                continue
            if p.enabled:
                p.pending_replace = True
                p.pending_replace_since = _now()
                p.expected_qty = None
                _save(p)
            filled = False
            if blocking:
                p, cancelled = _cancel_live_slices(account, p)
                filled = cancelled.filled
                if not cancelled.all_confirmed:
                    result.blocked[symbol] = (
                        "its protective TP/SL orders could not be confirmed cancelled, so the shares "
                        "may still be reserved at the broker (" + "; ".join(cancelled.detail) + ")")
                    continue
            if p.last_fill_at != fills_before:
                filled = True
            if not p.enabled:
                continue
            result.pending.append(symbol)
            if filled:
                result.filled.append(symbol)
    return result


def resume_protection(account, symbols: Iterable[str], *,
                      working_symbols: Optional[Set[str]] = None) -> List[str]:
    """Re-place protection for symbols flagged ``pending_replace``. Never raises.

    ``working_symbols`` are symbols with an order still working (their position is not final):
    they stay REPLACING. When ``None`` the DB is asked, counting only orders created after the
    protection was cancelled. A re-placement the broker or the validation refuses clears
    ``pending_replace`` (no endless retry), raises REPLACE_FAILED and leaves the symbol UNPROTECTED
    for the operator. A position that is no longer held (whole position sold) DISARMS the
    protection: the settings are cleared with a history record.
    """
    resumed: List[str] = []
    if not has_protection(account):
        return resumed
    try:
        with protection_lock(account.id):
            wanted = sorted({_norm(s) for s in symbols})
            since = {sym: (get_protection(account.id, sym).pending_replace_since
                           if get_protection(account.id, sym) else None) for sym in wanted}
            busy = working_symbols if working_symbols is not None else \
                _working_order_symbols(account.id, wanted, since)
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
                expected = p.expected_qty
                unsettled = None
                if expected is not None and abs(qty - expected) > 1e-6:
                    started = _naive(p.pending_replace_since)
                    if _sale_outcome(account.id, symbol, started) == "filled":
                        waited = (_now() - started).total_seconds() if started is not None else None
                        if waited is not None and waited < FILL_SETTLE_SECONDS:
                            # N7: the sale is FILLED in our books but the broker's position read has not
                            # caught up: re-placing now would protect shares that were just sold.
                            _alert(p, CODE_SALE_SETTLING,
                                   f"the sale of {symbol} is filled but the broker still reads {qty:g} sh "
                                   f"(expected {expected:g}); waiting up to {FILL_SETTLE_SECONDS:.0f} s "
                                   f"before re-placing protection",
                                   severity=ActivityLogSeverity.WARNING)
                            continue
                        unsettled = (f"{symbol}: after {FILL_SETTLE_SECONDS:.0f} s the broker still reads "
                                     f"{qty:g} sh but the sale left {expected:g}; protection is re-placed on "
                                     f"the broker's reading")
                        _log(account.id, ActivityLogSeverity.WARNING, unsettled, code="SALE_UNSETTLED",
                             symbol=symbol)
                    # else: the sale ended unfilled / rejected (or none is on record): the position is what it
                    # is, nothing to wait for
                if whole_shares(qty) < 1:
                    # The trade sold the whole position (or left only a fractional remainder):
                    # DISARM -- the settings are cleared (history kept), so a later buy is not
                    # auto-protected; the operator sets TP/SL again on re-entry. ``_disarm`` refuses
                    # (and alerts) while an order still rests (N5).
                    p.pending_replace = False
                    p.pending_replace_since = None
                    p.expected_qty = None
                    _save(p)
                    _disarm(_reload(p.id), "the position was sold")
                    continue
                cancel_since = (_naive(p.pending_replace_since) or _now()) - timedelta_seconds(5)
                if not _auto_allowed(p):
                    # The automatic paths are STOPPED for this symbol: nothing is re-placed on its own.
                    p.pending_replace = False
                    p.pending_replace_since = None
                    p.expected_qty = None
                    _save(p)
                    # Nothing is RE-PLANNED, but the orders the sale cancelled are put back (capped to the shares
                    # held and validated against the market) so the position is not left bare.
                    p = _rollback_if_uncovered(account, p, cancel_since, "the automatic paths are stopped")
                    held = whole_shares(qty)
                    covered_now = int(round(covered_quantity(get_slices(p.id))))
                    _alert(_reload(p.id), CODE_AUTO_STOPPED,
                           f"{symbol}: the automatic paths are stopped, so protection was not re-planned after "
                           f"the trade; the previous orders were put back where possible: {covered_now} of "
                           f"{held} shares protected. Resize protection resets the failure count and re-arms.")
                    continue
                alarm_open = any(x.state in SLICE_ALARM_STATES and x.state != SLICE_UNKNOWN and x.closed_at is None
                                 for x in get_slices(p.id))
                if alarm_open:
                    # An order was LOST (cancelled on the site, expired, rejected): it is never re-placed on its
                    # own. Only a stop-only cover of the whole position goes back, logged explicitly.
                    _log(account.id, ActivityLogSeverity.WARNING,
                         f"{symbol}: a lost protective order is still open, so only a stop-only cover was "
                         f"re-placed after the trade (no take-profit); use Resize protection to re-place the rest",
                         code="STOP_ONLY_COVER", symbol=symbol)
                p, placed = _place_slices(account, p, stop_only=alarm_open)
                if not placed.ok:
                    p = _rollback_if_uncovered(account, p, cancel_since, "the re-placement after the trade")
                p = _reload(p.id)
                p.pending_replace = False
                p.pending_replace_since = None
                p.expected_qty = None
                _save(p)
                if unsettled is not None:
                    p = _alert(p, CODE_QUANTITY_MISMATCH, unsettled, severity=ActivityLogSeverity.WARNING)
                if placed.ok:
                    resumed.append(symbol)
                else:
                    now_p = _reload(p.id)
                    if now_p.alert_code == CODE_REPLACE_FAILED and "restored" in (now_p.alert_message or ""):
                        pass                       # the rollback already said, truthfully, what is protected
                    else:
                        whole = whole_shares(qty)
                        covered = int(round(covered_quantity(get_slices(p.id))))
                        _alert(p, CODE_REPLACE_FAILED,
                               f"protection was NOT fully re-placed after the trade ({covered} of {whole} "
                               f"shares protected): " + " | ".join(placed.errors))
    except Exception as e:  # noqa: BLE001 -- never raises into the allocator run
        logger.error(f"allocator TP/SL: resume failed: {e}", exc_info=True)
        _log(account.id, ActivityLogSeverity.FAILURE,
             f"re-placement after the trade raised {type(e).__name__}: {e}", code="REPLACE_FAILED")
    return resumed


def extend_after_buys(account, symbols: Iterable[str]) -> List[str]:
    """After a run BOUGHT ``symbols``: add protection for the new whole shares (the growth rule,
    add-only, no cancel). Skipped for a symbol with an order still in flight (the refresh does it
    later). Never raises."""
    extended: List[str] = []
    if not has_protection(account):
        return extended
    try:
        with protection_lock(account.id):
            for symbol in sorted({_norm(s) for s in symbols}):
                p = get_protection(account.id, symbol)
                if p is None or not p.enabled or p.pending_replace or not _auto_allowed(p):
                    continue
                if any(x.state in SLICE_ALARM_STATES and x.closed_at is None for x in get_slices(p.id)):
                    continue          # an alarm keeps the protection suspended until Resize (as in the refresh)
                if _working_order_symbols(account.id, [symbol]):
                    continue
                try:
                    qty, _ = _read_position(account, symbol)
                except BrokerReadError:
                    continue
                if whole_shares(qty) > covered_quantity(get_slices(p.id)) + 1e-9:
                    p, placed = _place_slices(account, p)
                    _note_auto_result(p, placed.ok, "adding protection after a buy")
                    if placed.placed:
                        extended.append(symbol)
    except Exception as e:  # noqa: BLE001
        logger.error(f"allocator TP/SL: extension after buys failed: {e}", exc_info=True)
    return extended


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
        enabled=p.enabled, pending_replace=p.pending_replace, disarmed_note=p.disarmed_note,
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
            if p.alert_code and p.alert_code not in WARNING_CODES]


# Public names for the UI (the underscored ones stay the implementation).
read_position = _read_position
read_price = _read_price
read_average_cost = _read_average_cost
read_tick_sizes = _tick_sizes
