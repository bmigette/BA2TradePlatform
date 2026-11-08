"""Shared enter-market order-cycle helpers (Phase 3 of the live↔backtest engine unification).

Phase 6 shared the DECISION core (TradeActionEvaluator / TradeConditions / TradeActions /
TradeRiskManagement / position_sizing). P1e unified the ENTER ORDER-FLOW behavior: both the live
``TradeManager.process_expert_recommendations_after_analysis`` and the backtest
``daily_engine._run_expert_bar`` now follow the SAME temp-order-list cycle —

    for each passing recommendation:
        evaluate the enter ruleset  (shared TradeActionEvaluator)
        apply the dup + equity gates (shared)
        build an in-memory candidate  <-- build_entry_candidate (this module)
    size ALL candidates in one pass   (shared TradeRiskManagement.size_candidate_orders)
    for each FUNDED candidate:
        persist the real order + transaction + TP/SL bracket (shared evaluator.execute)
        stamp the RM-sized quantity + reconcile the protective stop (shared reconcile_protective_stop)
        submit via the AccountSeam       <-- platform-specific tail

This module holds the platform-agnostic pieces of that cycle so both drivers call one definition
rather than re-implementing it. The remaining platform-specific parts stay in each adapter behind
seams (the LIVE tail also runs refresh_orders(fetch_all=True) + _check_all_waiting_trigger_orders;
the backtest tail submits into the simulator) — see the account-seam contract
(reports/account_seam_contract_2026-07-02.md) for which behaviors are exact-parity vs approximated.

ba2_common-pure: no ba2_providers / ba2_trade_platform imports.
"""
from __future__ import annotations

from typing import Any, Callable, Iterable, List, NamedTuple, Optional

from ba2_common.core.failure_modes import is_never_absorbed
from ba2_common.core.models import ExpertRecommendation, TradingOrder, Transaction
from ba2_common.core.position_sizing import sized_on_stop, with_max_loss_stop
from ba2_common.core.types import (
    AssetClass, OrderDirection, OrderRecommendation, OrderStatus, OrderType,
    get_option_action_values)
from ba2_common.logger import logger

#: The two ways an entry is executed (see :func:`entry_route`).
ENTRY_ROUTE_OPTION = "option"
ENTRY_ROUTE_EQUITY = "equity"


#: ``types.is_option_action``'s set, frozen once: the router runs per fired entry, and the list
#: behind ``is_option_action`` is rebuilt on every call. Compared by VALUE -- a str-Enum member
#: hashes by its NAME, so an enum would silently miss a set of value strings.
_OPTION_ACTION_VALUES = frozenset(get_option_action_values())


class MixedEntryActionsRefused(ValueError):
    """An entry rule fired option AND equity actions together: neither path can size both."""


def entry_route(action_types: Iterable[Any]) -> str:
    """HOW the fired entry executes, from the action types its rule produced -- the ONE routing
    decision both runtimes make (plan 2026-09-25 option-stage2-build, revision Task E1).

    * every action an option action -> ``ENTRY_ROUTE_OPTION``: the option actions size and
      submit themselves through their shared risk/capacity guards (``execute(submit_to_broker=
      True)``). They must never reach the equity RM, which would re-label the contracts as
      shares and stage a STOCK safeguard stop (live 8082, 2026-09-24).
    * no option action -> ``ENTRY_ROUTE_EQUITY``: staged as a transient candidate for the RM
      sizing pass (``build_entry_candidate``), the S-family flow.
    * a MIX -> ``MixedEntryActionsRefused``: the equity RM would stamp a share count onto the
      option order, and the option path would submit the equity action unsized.

    Decided PER FIRED ENTRY, never per run: a style expert composes equity-entry members
    (O_CC / O_PP) with option-entry members in one ruleset. Live ``TradeManager`` and the
    backtest ``daily_engine`` both call this, so the routing cannot drift between them.
    O(number of actions); a pure function of its argument.

    ``action_types`` are ``ExpertActionType`` members or their string values. An empty list is
    refused: both callers stop on "no actions" before routing, so reaching here with none is a
    caller bug, and answering would pick a path for nothing.
    """
    types = list(action_types)
    if not types:
        raise ValueError("entry_route: the fired entry produced no actions to route")
    flags = [getattr(t, "value", t) in _OPTION_ACTION_VALUES for t in types]
    if all(flags):
        return ENTRY_ROUTE_OPTION
    if any(flags):
        raise MixedEntryActionsRefused(
            f"the entry rule mixes option and equity actions "
            f"{[getattr(t, 'value', t) for t in types]}: neither the option self-sizing path "
            f"nor the equity RM can size both halves")
    return ENTRY_ROUTE_EQUITY


def open_or_waiting_position(expert_id: Any, symbol: str, *, session=None) -> Optional[Transaction]:
    """THE DUPLICATE-POSITION GATE: the expert's OPENED or WAITING transaction on ``symbol``
    (the lowest id when there are several), or None when the entry may proceed.

    Applied after the enter ruleset fired and before anything executes, by live ``TradeManager``
    and the backtest ``daily_engine`` alike (plan 2026-09-25 option-stage2-build, Task 8 review
    I1): ONE position per (expert, symbol). WAITING counts too, so a not-yet-filled entry from
    an earlier cycle cannot be stacked (a ruleset's ``has_no_position`` leaf sees OPENED only).
    Keyed on expert and symbol ONLY -- not on a style-group member tag -- so in a style expert
    one member holds a symbol at a time, which is what keeps the members' exits apart (each
    exit reads the one position's entry order, ``entry_order.oldest_entry_order``).

    Reads through ``trade_store.transactions_where``: the backtest's in-memory store when it is
    active, else SQLite (through ``session`` when given, preserving the live session's view).
    """
    from ba2_common.core.trade_store import transactions_where
    from ba2_common.core.types import TransactionStatus

    rows = transactions_where(expert_id=expert_id, symbol=symbol,
                              statuses=[TransactionStatus.OPENED, TransactionStatus.WAITING],
                              session=session)
    if not rows:
        return None
    return min(rows, key=lambda t: t.id or 0)


# Recommendation directions that open a SHORT (sell-entry); everything else opens a long (buy).
_SHORT_ENTRY_ACTIONS = (OrderRecommendation.SELL, OrderRecommendation.UNDERWEIGHT)


def entry_side_for(recommendation: ExpertRecommendation) -> OrderDirection:
    """The order side a passing enter recommendation opens: SELL for bearish/underweight
    (short entry), BUY otherwise — matching which order-creating action the ruleset fires."""
    action = getattr(recommendation, "recommended_action", None)
    return OrderDirection.SELL if action in _SHORT_ENTRY_ACTIONS else OrderDirection.BUY


def fired_entry_lot_size(evaluator: Any) -> Optional[int]:
    """The round lot the fired entry ``BuyAction`` asks for, in the EQUITY BOOK's unit, or None.

    Read off ``evaluator.trade_actions`` (what ``evaluate()`` just built for this symbol) via the
    action's own ``_equity_lot_size()`` -- the SAME value ``BuyAction.execute`` stamps into the
    persisted order's ``data``. Not the raw configured int: on a split-adjusted backtest book one
    contract delivers ``100 x k`` adjusted shares, and that conversion belongs to the action.

    Only the option-overlay keys (O_CC / O_PP) author a lot size; for every other rule this is
    None and the candidate is built exactly as before. Called by BOTH enter paths (the live
    ``TradeManager`` funded-entry loop and the backtest ``daily_engine`` equity entry)."""
    from ba2_common.core.TradeActions import BuyAction

    for action in evaluator.trade_actions:
        if isinstance(action, BuyAction) and action.lot_size:
            return int(action._equity_lot_size())
    return None


def build_entry_candidate(recommendation: ExpertRecommendation, account_id: int,
                          lot_size: Optional[int] = None) -> TradingOrder:
    """Build the TRANSIENT (unpersisted) candidate entry order for the temp-order-list flow.

    Carries exactly what ``TradeRiskManagement.size_candidate_orders`` needs to size it — symbol,
    side, the linked recommendation (for expected-profit prioritization), and ``data``. It is NOT
    added to the DB: only candidates the RM funds are later persisted + submitted, so unfunded
    recs never create qty=0 rows (no churn, no deletes).

    ``lot_size`` (``fired_entry_lot_size(evaluator)``): the fired BuyAction's round lot, merged
    into a COPY of the recommendation's data so the RM (``apply_lot_size`` / the risk-based
    sizer) floors the size to whole lots and refuses below one. Until 2026-09-25 the candidate
    carried only the recommendation's data, so a rule's ``lot_size`` never reached the sizing
    at all. None (every rule but O_CC / O_PP) leaves the candidate exactly as before.

    Used identically by the live and backtest enter paths so the candidate shape can't drift.
    """
    data = getattr(recommendation, "data", None) or None
    if lot_size:
        data = {**(data or {}), "lot_size": int(lot_size)}
    return TradingOrder(
        account_id=account_id,
        symbol=recommendation.symbol,
        quantity=0.0,
        side=entry_side_for(recommendation),
        order_type=OrderType.MARKET,
        status=OrderStatus.PENDING,
        expert_recommendation_id=recommendation.id,
        data=data,
    )


def _max_loss_meta(transaction: Transaction, safeguard_sl: Optional[float]):
    """``(new_meta, stop)`` to write onto ``transaction``, or ``(None, None)`` when nothing is.

    Decided from the row the caller is about to write, never from an older copy."""
    if transaction.asset_class != AssetClass.EQUITY:
        return None, None
    meta = transaction.meta_data
    if meta is not None and not isinstance(meta, dict):
        logger.warning(
            f"Transaction {transaction.id} meta_data is a {type(meta).__name__}, not a dict: "
            f"not recording a max-loss stop over it (it stays unknown, so nothing loosens)")
        return None, None
    stop = sized_on_stop(ruleset_sl=transaction.stop_loss, safeguard_sl=safeguard_sl)
    new_meta = with_max_loss_stop(meta, stop)
    if new_meta is None:
        return None, None
    return new_meta, stop


def record_level_anchor(transaction_id: Optional[int], **levels) -> bool:
    """Stamp the price a protective level was COMPUTED FROM on ``Transaction.meta_data``.

    ``levels``: ``stop=<price>`` and/or ``tp=<price>``; a value of None CLEARS that level's anchor
    (the level now comes from a price of unknown origin, so an older stamp would mislead). Called
    by whoever sets the level, BEFORE the level reaches the account, so the exit order built from
    it (live) and the fill-time re-base (live and backtest) read the true anchor
    (``tpsl_fill_rebase.read_anchor``). A single-column write, same discipline as
    ``record_max_loss_stop``; never raises (a lost stamp falls back to the legacy reference chain
    and is logged at ERROR). Returns True when written."""
    if transaction_id is None or not levels:
        return False
    try:
        from ba2_common.core import trade_store
        from ba2_common.core.db import _db_write_lock, get_db, get_instance, update_instance
        from ba2_common.core.tpsl_fill_rebase import ANCHOR_KEY, stamp_anchor

        def _new_meta(txn):
            new = stamp_anchor(txn.meta_data, **levels)
            return None if new.get(ANCHOR_KEY) == (txn.meta_data or {}).get(ANCHOR_KEY) else new

        if trade_store.inmem_trades_active():
            txn = get_instance(Transaction, transaction_id)
            new_meta = _new_meta(txn)
            if new_meta is None:
                return False
            txn.meta_data = new_meta
            update_instance(txn)
            return True
        with _db_write_lock:
            with get_db() as session:
                db_txn = session.get(Transaction, transaction_id)
                if db_txn is None:
                    logger.warning(f"Transaction {transaction_id} not found: no level anchor stamped")
                    return False
                new_meta = _new_meta(db_txn)
                if new_meta is None:
                    return False
                db_txn.meta_data = new_meta
                session.commit()
                return True
    except Exception as e:  # noqa: BLE001 -- metadata; see the docstring
        if is_never_absorbed(e):
            raise
        logger.error(f"Could not stamp the level anchor {levels} on transaction {transaction_id}: {e}",
                     exc_info=True)
        return False


def stamp_safeguard_anchor(order: Optional[TradingOrder], ruleset_sl: Optional[float],
                           safeguard_sl: Optional[float], submit_sl: Optional[float],
                           anchor_price: Any) -> bool:
    """Stamp the stop anchor when the stop about to be attached is the RM SAFEGUARD.

    ``submit_sl`` is what ``reconcile_protective_stop`` returned. When it is the safeguard (not the
    ruleset's own stop, whose anchor its action already stamped), that stop was computed from the
    price the RM sized on: ``anchor_price`` (a callable returning the then-current price, or the
    price). Called BEFORE ``submit_order``. No transaction yet (a ruleset without TP/SL creates it
    inside the submit) -> nothing to stamp; the reference chain then resolves to the recommendation
    price, which is that same decision price."""
    if order is None or getattr(order, "transaction_id", None) is None:
        return False
    if not submit_sl or not safeguard_sl or submit_sl != safeguard_sl:
        return False
    if ruleset_sl and ruleset_sl == submit_sl:
        return False
    px = anchor_price() if callable(anchor_price) else anchor_price
    if not px:
        logger.warning(f"No current price to stamp as the safeguard-stop anchor of order "
                       f"{getattr(order, 'id', None)}; the fill-time re-base will use the fallback chain")
        return False
    return record_level_anchor(order.transaction_id, stop=float(px))


def record_max_loss_stop(order: Optional[TradingOrder], safeguard_sl: Optional[float]) -> Optional[float]:
    """Record the stop an equity entry was SIZED on as ``Transaction.meta_data["max_loss_stop"]``.

    Called by BOTH enter tails right after a successful entry ``submit_order``: the live
    ``TradeManager._submit_funded_entry_with_retry`` and the backtest
    ``daily_engine._size_and_submit_candidates`` / ``_size_and_submit``. ``safeguard_sl`` is the
    RM safeguard those callers already hand to ``reconcile_protective_stop``; the ruleset stop is
    read off the transaction. Which one is recorded is ``position_sizing.sized_on_stop``'s
    decision (the safeguard when there is one), see there for why.

    WHY AFTER THE SUBMIT. A ruleset with no TP/SL action never runs the evaluator's Phase 1.5,
    so its entry has NO transaction until ``submit_order`` creates one (and stamps
    ``order.transaction_id`` on this same object). After the submit every entry has one.

    A SINGLE-COLUMN WRITE (live / SQLite). ``db.update_instance`` copies EVERY attribute of the
    object it is given back onto the row, so saving a copy read a moment earlier reverts
    whatever another session committed in between: a status, an open_price, a take-profit (the
    2026-07-22 lost-TP defect, fixed the same way in
    ``AccountInterface._recalculate_transaction_quantity``). So the row is read, decided on and
    written inside ONE session that changes only ``meta_data``, under ``db._db_write_lock`` so
    it serialises with the other in-process ``meta_data`` writers (e.g. the TP
    ``current_target_price`` hook, which goes through ``update_instance`` and that same lock).
    The new dict is built from THAT row's ``meta_data``, so every key already on the row is
    carried forward. The backtest's in-memory store holds the one object by identity, so there
    the plain ``update_instance`` persist is kept.

    ONE ATTEMPT, NO RETRY. A lost write fails safe: the key stays absent, which reads as "no
    max-loss stop known" (B4: never loosen). Retrying a locked DB here would stall the funded
    entry loop for nothing.

    WRITTEN ONCE. ``with_max_loss_stop`` refuses when the key already exists, so a wash-trade
    re-submit, a DB-lock retry or a later stop adjustment can never replace the entry's value.
    A scale-in is not a second write either: an ``increase_instrument_share`` order carries no
    transaction and opens its own, and ``TransactionHelper``'s add-to-position order never comes
    through here. ``meta_data`` that is not a dict is never replaced (WARNING, nothing written).

    SCOPE. Equity MARKET entries only. An option transaction has its own max-loss machinery and
    is skipped. So is any other order type, because on a stop/stop-limit entry ``stop_price`` is
    the entry TRIGGER, not a protective stop.

    ADDITIVE ONLY, AND IT NEVER RAISES (short of the never-absorbed exceptions). This is metadata.
    No order, price, size, fill or P&L reads it, and the entry has already been submitted when
    this runs. A failure is logged at ERROR and swallowed, because propagating it would reach
    code that decides the ENTRY's fate. Live, the funded loop's handler would compensate (cancel)
    a wash-trade-locked entry over a metadata write.

    Returns the recorded stop, or None when nothing was written."""
    if order is None or getattr(order, "transaction_id", None) is None:
        return None
    if getattr(order, "order_type", None) != OrderType.MARKET or getattr(order, "depends_on_order", None):
        return None
    try:
        from ba2_common.core import trade_store
        from ba2_common.core.db import _db_write_lock, get_db, get_instance, update_instance

        if trade_store.inmem_trades_active():
            transaction = get_instance(Transaction, order.transaction_id)
            new_meta, stop = _max_loss_meta(transaction, safeguard_sl)
            if new_meta is None:
                return None
            transaction.meta_data = new_meta   # a NEW dict on the stored object itself
            update_instance(transaction)
            return stop

        with _db_write_lock:
            with get_db() as session:
                db_txn = session.get(Transaction, order.transaction_id)
                if db_txn is None:
                    logger.warning(f"Transaction {order.transaction_id} not found: no max-loss stop "
                                   f"recorded for order {getattr(order, 'id', None)}")
                    return None
                new_meta, stop = _max_loss_meta(db_txn, safeguard_sl)
                if new_meta is None:
                    return None
                db_txn.meta_data = new_meta    # the ONLY attribute this session changes
                session.commit()
                return stop
    except Exception as e:  # noqa: BLE001 -- metadata must never decide an entry; see docstring
        if is_never_absorbed(e):
            raise
        logger.error(
            f"Could not record the max-loss stop for order {getattr(order, 'id', None)} "
            f"(transaction {getattr(order, 'transaction_id', None)}): {e}", exc_info=True)
        return None


# ---------------------------------------------------------------------------------------------------
# funded-entry staging (the composition between the RM's sizing and the broker submit)
# ---------------------------------------------------------------------------------------------------
class StagedEntry(NamedTuple):
    """What :func:`stage_funded_entry` did: the evaluator's results and the main entry order it found
    (``None`` when ``execute`` created none)."""

    results: List[Any]
    order: Optional[TradingOrder]


def row_or_none(model_class, row_id):
    """``get_instance`` that answers None instead of raising ``InstanceNotFound``.

    "The row is gone" is a legitimate answer for a compensation routine -- it means there is nothing left
    to compensate -- and must not abort the caller.
    """
    from ba2_common.core.db import InstanceNotFound, get_instance

    if not row_id:
        return None
    try:
        return get_instance(model_class, row_id)
    except InstanceNotFound:
        return None


def persist_funded_entry(order: TradingOrder, quantity: float, stop_price: Optional[float] = None) -> None:
    """Write the risk manager's decision onto the entry row BEFORE the broker is called.

    WHY IT MUST BE BEFORE. At this point the RM-sized quantity and the safeguard stop exist only in
    memory, on a TRANSIENT candidate object that is about to be thrown away; re-deriving them later from
    a stranded row is not possible. Everything that can go wrong from here (a lock, a broker rejection, a
    crash, a restart) leaves a row on disk, and that row is either sized and re-drivable or it is a dead
    qty-0 stub nothing can reconstruct (order 460 on PROD 2026-08-10 sat untouched for 8 hours).
    Persisting first also makes the broker submit the ONLY thing left that can fail, which is what lets
    the compensation for an unsent entry be a simple statement about a row.

    ``stop_price`` mirrors what the DB sizing path already does: there the candidate IS the persisted row;
    the temp-list path sizes a transient candidate and only ever passed the stop as a ``submit_order``
    argument, leaving the row's ``stop_price`` null -- the value the wash-trade re-submit reads to rebuild
    the protective leg (with none it re-sends the entry naked). An explicit stop the ruleset already put
    on the order wins: the RM's is a SAFEGUARD, not an override.

    Safe only because the order is DETACHED (the enter loop's read-only session invariant): on an attached
    row this ``update_instance`` is the self-deadlock.
    """
    from ba2_common.core.db import update_instance

    order.quantity = quantity
    if stop_price and not order.stop_price:
        order.stop_price = stop_price
    update_instance(order)


def stamp_leg_quantities(order: TradingOrder) -> None:
    """Stamp the entry's (RM-sized) quantity onto the protective legs ``execute()`` just created.

    The legs were built from the entry's quantity while it was still the TRANSIENT 0 of
    :func:`build_entry_candidate`, because the bracket is created inside ``execute()`` and the RM size is
    stamped only afterwards. Measured 2026-08-08: 23 of 96 entry-staged legs carried quantity 0 (WKC /
    GNTX / CSTL were refused by the quantity<=0 guard and cancelled, leaving a stop and NO take-profit).
    Scoped to legs of THIS entry, created seconds ago by THIS execute(), so all are full-position legs.

    LIVE-ONLY BY DESIGN: the backtest account builds its legs lazily at fill time from the FILLED entry
    quantity, so it cannot inherit a transient 0; live must stage the leg before the fill because the
    broker needs it resting. Persisted per leg through its own short-lived session, NOT left as a pending
    mutation on the caller's session. A failure here never blocks the entry.
    """
    from sqlmodel import select

    from ba2_common.core.db import get_db, update_instance

    try:
        with get_db() as leg_session:
            leg_ids = [leg.id for leg in leg_session.exec(
                select(TradingOrder).where(TradingOrder.depends_on_order == order.id)).all()]
        for leg_id in leg_ids:
            leg = row_or_none(TradingOrder, leg_id)
            if leg is not None and leg.quantity != order.quantity:
                logger.info(f"Stamped protective leg {leg.id} ({leg.order_type}) "
                            f"qty {leg.quantity} -> {order.quantity} from entry {order.id}")
                leg.quantity = order.quantity
                update_instance(leg)
    except Exception as leg_err:  # noqa: BLE001 -- never block the entry
        logger.error(f"Failed to stamp protective-leg quantity for order {order.id}: {leg_err}",
                     exc_info=True)


def stage_funded_entry(evaluator: Any, symbol: str, quantity: float, stop_price: Optional[float], *,
                       on_executed: Optional[Callable[[List[Any], Optional[TradingOrder]], None]] = None
                       ) -> StagedEntry:
    """Persist ONE funded entry up to (not including) the broker: execute, persist the RM decision, stamp legs.

    1. ``evaluator.execute()`` creates the real order + transaction + TP/SL bracket (called only for
       funded symbols);
    2. the main entry order is the first created order that is a BUY/SELL, fetched DETACHED (no session
       is left holding it; see :func:`persist_funded_entry`);
    3. ``on_executed(results, order)`` (the recorder's hook) runs as soon as the order is known, before
       anything is persisted -- called with ``order=None`` when ``execute`` created none;
    4. the RM-sized quantity and safeguard stop go onto the row (:func:`persist_funded_entry`) and the
       protective legs get the same quantity (:func:`stamp_leg_quantities`).

    Returns ``StagedEntry(results, order)``; ``order`` is ``None`` when no main order was created (logged,
    nothing to submit). An exception from ``execute`` or the persist propagates: the caller owns the
    compensation. The broker submit (DB-lock retry, unsent-entry compensation) is the live tail and is
    not here. Shared by ``TradeManager`` and the offline decision replay.
    """
    results = evaluator.execute()
    order: Optional[TradingOrder] = None
    for result in results:
        if result.get("success") and isinstance(result.get("data"), dict):
            order_id = result["data"].get("order_id")
            if order_id:
                # DETACHED on purpose: get_instance(session=None) opens and closes its own session and
                # hands back a fully loaded, detached row (see the enter loop's block comment).
                row = row_or_none(TradingOrder, order_id)
                if row and row.side in (OrderDirection.BUY, OrderDirection.SELL) and order is None:
                    order = row
    if on_executed is not None:
        on_executed(results, order)
    if order is None:
        logger.warning(f"No main order created for funded {symbol}")
        return StagedEntry(results, None)
    persist_funded_entry(order, quantity, stop_price)
    stamp_leg_quantities(order)
    return StagedEntry(results, order)


def entry_submit_stop(order: TradingOrder, safeguard_sl: Optional[float]):
    """Use the backtest's stop policy without changing the RM sizing input.

    The evaluator has already attached the ruleset stop to the transaction. Sending just the safeguard
    here would overwrite a tighter ruleset leg in AccountInterface's initial_setup block. Read the
    current transaction on each attempt so a delayed retry also preserves subsequent tightening.
    """
    from ba2_common.core.db import get_instance
    from ba2_common.core.position_sizing import reconcile_protective_stop
    from ba2_common.core.types import OrderDirection as _OrderDirection

    transaction = get_instance(Transaction, order.transaction_id) if order.transaction_id else None
    return reconcile_protective_stop(
        ruleset_sl=transaction.stop_loss if transaction else None,
        safeguard_sl=safeguard_sl,
        is_long=order.side == _OrderDirection.BUY,
    )


def submit_funded_entry(account: Any, order: TradingOrder, sl_price: Optional[float] = None, *,
                        retries: int, backoff_s: float, log: Any = None,
                        sleep: Optional[Callable[[float], None]] = None):
    """Submit a funded entry, retrying when the DB (not the broker) is what failed.

    WHY. A sqlite write lock must never cost a trade. Measured on PROD 2026-08-10: CVS was screened,
    passed its ruleset and was FUNDED by the RM (1 share @ $95.69), then submit_order's Transaction insert
    hit "database is locked". db.retry_on_lock burns 4 attempts against a 30s busy_timeout -- ~2 minutes
    -- then re-raises; the caller logged it and moved on. The trade was simply lost, leaving a stranded
    qty-0 PENDING row with no transaction and no broker id. Nothing sweeps those: the only code that
    touches PENDING entries DELETES them, and order 460 sat untouched for 8 hours. Dev is worse -- 161
    lock events in a day and 5 entries lost the same way.

    The lock is transient and the retry is trivially correct HERE, because at this point the RM-sized
    quantity and safeguard stop are still in memory (``sl_price``); re-deriving them later from a stranded
    row is not possible, which is why this belongs in the funded loop and not in a sweeper.

    ONLY lock failures are retried. A broker rejection, a validation error or a wash-trade lock is a real
    answer and must not be re-attempted -- re-sending on those risks a duplicate order, which is far worse
    than a missed one.

    The submit composition (reconcile the RM safeguard with the ruleset stop, stamp the safeguard anchor,
    ``account.submit_order``, record the max-loss stop) is shared by ``TradeManager`` and the offline
    decision replay; only the retry policy (``retries``/``backoff_s``) is the caller's.
    """
    import time as _time

    from ba2_common.core.db import get_instance

    log = log if log is not None else logger
    sleep = sleep if sleep is not None else _time.sleep
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            submit_sl = entry_submit_stop(order, sl_price)
            _txn = get_instance(Transaction, order.transaction_id) if order.transaction_id else None
            stamp_safeguard_anchor(
                order, (_txn.stop_loss if _txn else None), sl_price, submit_sl,
                lambda: account.get_instrument_current_price(order.symbol))
            submitted = account.submit_order(order, sl_price=submit_sl)
        except Exception as e:  # noqa: BLE001 -- classified immediately below
            if "database is locked" not in str(e).lower():
                raise  # a real answer from the broker/validator: never re-send
            last_err = e
            if attempt < retries:
                delay = backoff_s * attempt
                log.warning(
                    f"Entry submit for order {order.id} ({order.symbol}) lost to a DB lock "
                    f"(attempt {attempt}/{retries}); retrying in {delay:.0f}s")
                sleep(delay)
        else:
            # The stop this entry was SIZED on, recorded once as the transaction's max-loss stop.
            # ``sl_price`` is the RM safeguard the candidate was sized off, BEFORE entry_submit_stop
            # reconciled it with the ruleset stop; the helper falls back to the ruleset stop only when
            # there is no safeguard. OUTSIDE the try on purpose: nothing it does may be mistaken for a
            # DB-locked submit and re-send the order. It never raises (see the helper).
            if submitted:
                record_max_loss_stop(order, sl_price)
                # the transaction may only exist since the submit; the fill-time re-base reads the
                # stamp from it either way (idempotent when already stamped before)
                stamp_safeguard_anchor(
                    order, (_txn.stop_loss if _txn else None), sl_price, submit_sl,
                    lambda: account.get_instrument_current_price(order.symbol))
            return submitted
    log.error(
        f"Entry submit for order {order.id} ({order.symbol}) ABANDONED after "
        f"{retries} DB-lock retries: {last_err}. The RM funded this trade "
        f"and it never reached the broker; order {order.id} is left PENDING with no broker id.",
        exc_info=True)
    return None
