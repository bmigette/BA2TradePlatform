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
    RatingUpgradedCondition, RatingDowngradedCondition,
    RatingNeutralToPositiveCondition,
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


class TestRatingUpgradeDowngradeConditions:
    """rating_upgraded / rating_downgraded compare the ordinal rank of the two
    most recent recommendations for the same instance+symbol.
    Rank: SELL < UNDERWEIGHT < HOLD < OVERWEIGHT < BUY."""

    def _setup(self, prev_action, curr_action):
        acct_def = create_account_definition()
        # Offset so expert-instance id differs from account id (guards the
        # historical bug of scoping previous recs by account.id).
        create_expert_instance(account_id=acct_def.id)
        ei = create_expert_instance(account_id=acct_def.id)
        assert ei.id != acct_def.id
        now = datetime.now(timezone.utc)
        create_recommendation(instance_id=ei.id, symbol="AAPL",
                              recommended_action=prev_action,
                              created_at=now - timedelta(hours=1))
        curr = create_recommendation(instance_id=ei.id, symbol="AAPL",
                              recommended_action=curr_action, created_at=now)
        return MockAccount(acct_def.id), curr

    def test_upgraded_hold_to_overweight(self):
        account, curr = self._setup(OrderRecommendation.HOLD, OrderRecommendation.OVERWEIGHT)
        assert RatingUpgradedCondition(account, "AAPL", curr).evaluate() is True

    def test_upgraded_overweight_to_buy(self):
        account, curr = self._setup(OrderRecommendation.OVERWEIGHT, OrderRecommendation.BUY)
        assert RatingUpgradedCondition(account, "AAPL", curr).evaluate() is True

    def test_upgraded_false_when_unchanged(self):
        account, curr = self._setup(OrderRecommendation.OVERWEIGHT, OrderRecommendation.OVERWEIGHT)
        assert RatingUpgradedCondition(account, "AAPL", curr).evaluate() is False

    def test_upgraded_false_on_downgrade(self):
        account, curr = self._setup(OrderRecommendation.BUY, OrderRecommendation.HOLD)
        assert RatingUpgradedCondition(account, "AAPL", curr).evaluate() is False

    def test_downgraded_buy_to_overweight(self):
        account, curr = self._setup(OrderRecommendation.BUY, OrderRecommendation.OVERWEIGHT)
        assert RatingDowngradedCondition(account, "AAPL", curr).evaluate() is True

    def test_downgraded_hold_to_sell(self):
        account, curr = self._setup(OrderRecommendation.HOLD, OrderRecommendation.SELL)
        assert RatingDowngradedCondition(account, "AAPL", curr).evaluate() is True

    def test_downgraded_false_on_upgrade(self):
        account, curr = self._setup(OrderRecommendation.UNDERWEIGHT, OrderRecommendation.HOLD)
        assert RatingDowngradedCondition(account, "AAPL", curr).evaluate() is False

    def test_insufficient_history_returns_false(self):
        acct_def = create_account_definition()
        ei = create_expert_instance(account_id=acct_def.id)
        curr = create_recommendation(instance_id=ei.id, symbol="AAPL",
                                     recommended_action=OrderRecommendation.BUY)
        assert RatingUpgradedCondition(MockAccount(acct_def.id), "AAPL", curr).evaluate() is False


class TestRatingChangeScopesByInstanceId:
    """Regression: rating-change conditions must scope previous recommendations
    by the recommendation's instance_id, not the account id."""

    def test_neutral_to_positive_reads_by_instance_id(self):
        acct_def = create_account_definition()
        create_expert_instance(account_id=acct_def.id)  # offset ids
        ei = create_expert_instance(account_id=acct_def.id)
        assert ei.id != acct_def.id
        now = datetime.now(timezone.utc)
        create_recommendation(instance_id=ei.id, symbol="AAPL",
                              recommended_action=OrderRecommendation.HOLD,
                              created_at=now - timedelta(hours=1))
        curr = create_recommendation(instance_id=ei.id, symbol="AAPL",
                              recommended_action=OrderRecommendation.BUY, created_at=now)
        cond = RatingNeutralToPositiveCondition(MockAccount(acct_def.id), "AAPL", curr)
        assert cond.evaluate() is True


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
