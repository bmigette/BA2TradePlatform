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

from typing import Any, Iterable, Optional

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


# Recommendation directions that open a SHORT (sell-entry); everything else opens a long (buy).
_SHORT_ENTRY_ACTIONS = (OrderRecommendation.SELL, OrderRecommendation.UNDERWEIGHT)


def entry_side_for(recommendation: ExpertRecommendation) -> OrderDirection:
    """The order side a passing enter recommendation opens: SELL for bearish/underweight
    (short entry), BUY otherwise — matching which order-creating action the ruleset fires."""
    action = getattr(recommendation, "recommended_action", None)
    return OrderDirection.SELL if action in _SHORT_ENTRY_ACTIONS else OrderDirection.BUY


def build_entry_candidate(recommendation: ExpertRecommendation, account_id: int) -> TradingOrder:
    """Build the TRANSIENT (unpersisted) candidate entry order for the temp-order-list flow.

    Carries exactly what ``TradeRiskManagement.size_candidate_orders`` needs to size it — symbol,
    side, the linked recommendation (for expected-profit prioritization), and ``data`` (lot_size for
    option-overlay strategies). It is NOT added to the DB: only candidates the RM funds are later
    persisted + submitted, so unfunded recs never create qty=0 rows (no churn, no deletes).

    Used identically by the live and backtest enter paths so the candidate shape can't drift.
    """
    return TradingOrder(
        account_id=account_id,
        symbol=recommendation.symbol,
        quantity=0.0,
        side=entry_side_for(recommendation),
        order_type=OrderType.MARKET,
        status=OrderStatus.PENDING,
        expert_recommendation_id=recommendation.id,
        data=(getattr(recommendation, "data", None) or None),
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
