"""Tests for TradeCondition subclasses (flag and comparison conditions)."""
import pytest
from unittest.mock import MagicMock
from datetime import datetime, timezone, timedelta

from ba2_trade_platform.core.TradeConditions import (
    BullishCondition, BearishCondition,
    HasNoPositionCondition, HasPositionCondition,
    HasBuyPositionCondition, HasSellPositionCondition,
    HasNoPositionAccountCondition, HasPositionAccountCondition,
    LongTermCondition, MediumTermCondition, ShortTermCondition,
    CurrentRatingPositiveCondition, CurrentRatingNeutralCondition,
    CurrentRatingNegativeCondition,
    HighRiskCondition, MediumRiskCondition, LowRiskCondition,
    ConfidenceCondition, ExpectedProfitTargetPercentCondition,
    DaysOpenedCondition, ProfitLossPercentCondition,
    create_condition, CompareCondition,
)
from ba2_trade_platform.core.types import (
    OrderRecommendation, RiskLevel, TimeHorizon, ExpertEventType,
    OrderDirection, OrderStatus, TransactionStatus,
)
from ba2_trade_platform.core.models import (
    ExpertRecommendation, TradingOrder, Transaction,
)
from tests.conftest import MockAccount
from tests.factories import (
    create_account_definition, create_expert_instance,
    create_recommendation, create_transaction, create_trading_order,
)


def _make_recommendation(action=OrderRecommendation.BUY, confidence=75.0,
                          risk_level=RiskLevel.MEDIUM, time_horizon=TimeHorizon.SHORT_TERM,
                          expected_profit_percent=5.0, price_at_date=150.0,
                          instance_id=1, symbol="AAPL"):
    return ExpertRecommendation(
        instance_id=instance_id, symbol=symbol,
        recommended_action=action, expected_profit_percent=expected_profit_percent,
        price_at_date=price_at_date, confidence=confidence,
        risk_level=risk_level, time_horizon=time_horizon, details="test",
    )


def _make_mock_account():
    acct_def = create_account_definition()
    return MockAccount(acct_def.id)


class TestBullishBearishConditions:
    def test_bullish_with_buy(self):
        account = _make_mock_account()
        rec = _make_recommendation(action=OrderRecommendation.BUY)
        assert BullishCondition(account, "AAPL", rec).evaluate() is True

    def test_bullish_with_sell(self):
        account = _make_mock_account()
        rec = _make_recommendation(action=OrderRecommendation.SELL)
        assert BullishCondition(account, "AAPL", rec).evaluate() is False

    def test_bearish_with_sell(self):
        account = _make_mock_account()
        rec = _make_recommendation(action=OrderRecommendation.SELL)
        assert BearishCondition(account, "AAPL", rec).evaluate() is True

    def test_bearish_with_buy(self):
        account = _make_mock_account()
        rec = _make_recommendation(action=OrderRecommendation.BUY)
        assert BearishCondition(account, "AAPL", rec).evaluate() is False


class TestCurrentRatingConditions:
    def test_positive_with_buy(self):
        account = _make_mock_account()
        rec = _make_recommendation(action=OrderRecommendation.BUY)
        assert CurrentRatingPositiveCondition(account, "AAPL", rec).evaluate() is True

    def test_neutral_with_hold(self):
        account = _make_mock_account()
        rec = _make_recommendation(action=OrderRecommendation.HOLD)
        assert CurrentRatingNeutralCondition(account, "AAPL", rec).evaluate() is True

    def test_negative_with_sell(self):
        account = _make_mock_account()
        rec = _make_recommendation(action=OrderRecommendation.SELL)
        assert CurrentRatingNegativeCondition(account, "AAPL", rec).evaluate() is True

    def test_positive_with_sell_is_false(self):
        account = _make_mock_account()
        rec = _make_recommendation(action=OrderRecommendation.SELL)
        assert CurrentRatingPositiveCondition(account, "AAPL", rec).evaluate() is False


class TestRiskLevelConditions:
    @pytest.mark.parametrize("risk,cond_class,expected", [
        (RiskLevel.HIGH, HighRiskCondition, True),
        (RiskLevel.MEDIUM, HighRiskCondition, False),
        (RiskLevel.MEDIUM, MediumRiskCondition, True),
        (RiskLevel.LOW, LowRiskCondition, True),
        (RiskLevel.HIGH, LowRiskCondition, False),
    ])
    def test_risk_level(self, risk, cond_class, expected):
        account = _make_mock_account()
        rec = _make_recommendation(risk_level=risk)
        assert cond_class(account, "AAPL", rec).evaluate() is expected


class TestTimeHorizonConditions:
    @pytest.mark.parametrize("horizon,cond_class,expected", [
        (TimeHorizon.SHORT_TERM, ShortTermCondition, True),
        (TimeHorizon.MEDIUM_TERM, MediumTermCondition, True),
        (TimeHorizon.LONG_TERM, LongTermCondition, True),
        (TimeHorizon.SHORT_TERM, LongTermCondition, False),
    ])
    def test_time_horizon(self, horizon, cond_class, expected):
        account = _make_mock_account()
        rec = _make_recommendation(time_horizon=horizon)
        assert cond_class(account, "AAPL", rec).evaluate() is expected


class TestPositionConditions:
    def test_has_no_position_when_no_transactions(self):
        account = _make_mock_account()
        rec = _make_recommendation()
        assert HasNoPositionCondition(account, "AAPL", rec).evaluate() is True

    def test_has_position_when_transaction_exists(self):
        acct_def = create_account_definition()
        account = MockAccount(acct_def.id)
        ei = create_expert_instance(account_id=acct_def.id)
        rec = _make_recommendation(instance_id=ei.id)
        create_transaction(symbol="AAPL", expert_id=ei.id, status=TransactionStatus.OPENED)
        assert HasPositionCondition(account, "AAPL", rec).evaluate() is True

    def test_has_no_position_account_when_empty(self):
        account = _make_mock_account()
        account._positions = []
        rec = _make_recommendation()
        assert HasNoPositionAccountCondition(account, "AAPL", rec).evaluate() is True

    def test_has_position_account_when_position_exists(self):
        account = _make_mock_account()
        pos = MagicMock()
        pos.symbol = "AAPL"
        pos.qty = 10.0
        account._positions = [pos]
        rec = _make_recommendation()
        assert HasPositionAccountCondition(account, "AAPL", rec).evaluate() is True


class TestConfidenceCondition:
    @pytest.mark.parametrize("confidence,op,value,expected", [
        (80.0, ">", 50.0, True),
        (50.0, ">", 50.0, False),
        (50.0, ">=", 50.0, True),
        (30.0, "<", 50.0, True),
        (50.0, "==", 50.0, True),
        (50.0, "!=", 50.0, False),
    ])
    def test_confidence_comparison(self, confidence, op, value, expected):
        account = _make_mock_account()
        rec = _make_recommendation(confidence=confidence)
        cond = ConfidenceCondition(account, "AAPL", rec, op, value)
        assert cond.evaluate() is expected

    def test_confidence_none_returns_false(self):
        account = _make_mock_account()
        rec = _make_recommendation(confidence=None)
        cond = ConfidenceCondition(account, "AAPL", rec, ">", 50.0)
        assert cond.evaluate() is False


class TestExpectedProfitCondition:
    def test_expected_profit_greater_than(self):
        account = _make_mock_account()
        rec = _make_recommendation(expected_profit_percent=10.0)
        cond = ExpectedProfitTargetPercentCondition(account, "AAPL", rec, ">", 5.0)
        assert cond.evaluate() is True
        assert cond.calculated_value == 10.0

    def test_expected_profit_none_returns_false(self):
        account = _make_mock_account()
        rec = _make_recommendation(expected_profit_percent=None)
        cond = ExpectedProfitTargetPercentCondition(account, "AAPL", rec, ">", 5.0)
        assert cond.evaluate() is False


class TestDaysOpenedCondition:
    def test_days_opened_with_old_order(self):
        account = _make_mock_account()
        rec = _make_recommendation()
        order = TradingOrder(
            id=1, account_id=account.id, symbol="AAPL",
            quantity=10.0, side=OrderDirection.BUY,
            order_type="market", status=OrderStatus.FILLED,
            created_at=datetime.now(timezone.utc) - timedelta(days=10),
        )
        cond = DaysOpenedCondition(account, "AAPL", rec, ">", 5.0, existing_order=order)
        assert cond.evaluate() is True

    def test_days_opened_no_existing_order(self):
        account = _make_mock_account()
        rec = _make_recommendation()
        cond = DaysOpenedCondition(account, "AAPL", rec, ">", 5.0, existing_order=None)
        assert cond.evaluate() is False


class TestCreateConditionFactory:
    def test_create_flag_condition(self):
        account = _make_mock_account()
        rec = _make_recommendation()
        cond = create_condition(ExpertEventType.F_BULLISH, account, "AAPL", rec)
        assert isinstance(cond, BullishCondition)

    def test_create_numeric_condition(self):
        account = _make_mock_account()
        rec = _make_recommendation()
        cond = create_condition(
            ExpertEventType.N_CONFIDENCE, account, "AAPL", rec,
            operator_str=">", value=50.0,
        )
        assert isinstance(cond, ConfidenceCondition)

    def test_create_numeric_without_operator_raises(self):
        account = _make_mock_account()
        rec = _make_recommendation()
        with pytest.raises(ValueError, match="Operator and value required"):
            create_condition(ExpertEventType.N_CONFIDENCE, account, "AAPL", rec)

    def test_invalid_operator_raises(self):
        account = _make_mock_account()
        rec = _make_recommendation()
        with pytest.raises(ValueError, match="Invalid operator"):
            ConfidenceCondition(account, "AAPL", rec, "INVALID", 5.0)
