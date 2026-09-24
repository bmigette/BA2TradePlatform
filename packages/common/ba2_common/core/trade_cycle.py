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

from typing import Any, Optional

from ba2_common.core.models import ExpertRecommendation, TradingOrder, Transaction
from ba2_common.core.position_sizing import sized_on_stop, with_max_loss_stop
from ba2_common.core.types import AssetClass, OrderDirection, OrderRecommendation, OrderStatus, OrderType
from ba2_common.logger import logger

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
    ``order.transaction_id`` on this same object). After the submit every entry has one. The
    transaction is re-read here, so the write starts from the row as the submit left it and
    carries every other ``meta_data`` key forward.

    WRITTEN ONCE. ``with_max_loss_stop`` refuses when the key already exists, so a wash-trade
    re-submit, a DB-lock retry or a later stop adjustment can never replace the entry's value.
    A scale-in is not a second write either: an ``increase_instrument_share`` order carries no
    transaction and opens its own, and ``TransactionHelper``'s add-to-position order never comes
    through here.

    SCOPE. Equity MARKET entries only. An option transaction has its own max-loss machinery and
    is skipped. So is any other order type, because on a stop/stop-limit entry ``stop_price`` is
    the entry TRIGGER, not a protective stop.

    ADDITIVE ONLY, AND IT NEVER RAISES. This is metadata. No order, price, size, fill or P&L reads
    it, and the entry has already been submitted when this runs. A failure is logged at ERROR and
    swallowed, because propagating it would reach code that decides the ENTRY's fate. Live, the
    funded loop's handler would compensate (cancel) a wash-trade-locked entry over a metadata
    write. A missing key only means "no max-loss stop known", which is also what an older
    transaction reads as.

    Returns the recorded stop, or None when nothing was written."""
    if order is None or getattr(order, "transaction_id", None) is None:
        return None
    if getattr(order, "order_type", None) != OrderType.MARKET or getattr(order, "depends_on_order", None):
        return None
    try:
        from ba2_common.core.db import get_instance, update_instance

        transaction = get_instance(Transaction, order.transaction_id)
        if transaction.asset_class != AssetClass.EQUITY:
            return None
        stop = sized_on_stop(ruleset_sl=transaction.stop_loss, safeguard_sl=safeguard_sl)
        new_meta = with_max_loss_stop(transaction.meta_data, stop)
        if new_meta is None:
            return None
        transaction.meta_data = new_meta   # a NEW dict: SQLModel only persists what it can see
        update_instance(transaction)
        return stop
    except Exception as e:  # noqa: BLE001 -- metadata must never decide an entry; see docstring
        logger.error(
            f"Could not record the max-loss stop for order {getattr(order, 'id', None)} "
            f"(transaction {getattr(order, 'transaction_id', None)}): {e}", exc_info=True)
        return None
