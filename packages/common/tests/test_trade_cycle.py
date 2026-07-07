"""Shared enter-cycle helpers (Phase 3): build_entry_candidate + entry_side_for — the transient
candidate both the live and backtest enter paths build for the temp-order-list flow."""
from ba2_common.core.models import ExpertRecommendation
from ba2_common.core.trade_cycle import build_entry_candidate, entry_side_for
from ba2_common.core.types import (OrderDirection, OrderRecommendation, OrderStatus, OrderType,
                                   RiskLevel, TimeHorizon)


def _rec(action, symbol="AAPL", data=None):
    return ExpertRecommendation(
        id=42, instance_id=1, symbol=symbol, recommended_action=action,
        expected_profit_percent=10.0, price_at_date=100.0, confidence=80.0,
        risk_level=RiskLevel.MEDIUM, time_horizon=TimeHorizon.MEDIUM_TERM, data=data)


def test_entry_side_long_for_bullish():
    assert entry_side_for(_rec(OrderRecommendation.BUY)) == OrderDirection.BUY
    assert entry_side_for(_rec(OrderRecommendation.OVERWEIGHT)) == OrderDirection.BUY


def test_entry_side_short_for_bearish():
    assert entry_side_for(_rec(OrderRecommendation.SELL)) == OrderDirection.SELL
    assert entry_side_for(_rec(OrderRecommendation.UNDERWEIGHT)) == OrderDirection.SELL


def test_build_candidate_is_transient_pending_qty0_linked():
    c = build_entry_candidate(_rec(OrderRecommendation.BUY, data={"lot_size": 100}), account_id=7)
    assert c.id is None                              # transient (not persisted)
    assert c.account_id == 7 and c.symbol == "AAPL"
    assert c.quantity == 0.0                          # RM sizes it later
    assert c.side == OrderDirection.BUY
    assert c.order_type == OrderType.MARKET and c.status == OrderStatus.PENDING
    assert c.expert_recommendation_id == 42
    assert c.data == {"lot_size": 100}                # carried for lot-sizing


def test_build_candidate_none_data():
    c = build_entry_candidate(_rec(OrderRecommendation.SELL), account_id=1)
    assert c.data is None and c.side == OrderDirection.SELL
