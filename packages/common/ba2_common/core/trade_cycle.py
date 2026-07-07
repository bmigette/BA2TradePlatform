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

from ba2_common.core.models import ExpertRecommendation, TradingOrder
from ba2_common.core.types import OrderDirection, OrderRecommendation, OrderStatus, OrderType

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
