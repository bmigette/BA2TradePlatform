"""Tests for TradeActionEvaluator ruleset evaluation logic."""
import pytest
from ba2_trade_platform.core.TradeActionEvaluator import TradeActionEvaluator
from ba2_trade_platform.core.types import (
    ExpertEventType, ExpertActionType, ExpertEventRuleType,
    OrderRecommendation, RiskLevel, TimeHorizon,
)
from ba2_trade_platform.core.models import (
    Ruleset, EventAction, RulesetEventActionLink, ExpertRecommendation,
)
from ba2_trade_platform.core.db import add_instance
from tests.conftest import MockAccount
from tests.factories import (
    create_account_definition, create_expert_instance,
    create_recommendation, link_rule_to_ruleset,
)


def _setup_bullish_buy_ruleset():
    """Create a ruleset with one rule: if bullish -> buy."""
    rs = Ruleset(
        name="Bullish Buy",
        type=ExpertEventRuleType.TRADING_RECOMMENDATION_RULE,
    )
    rs_id = add_instance(rs)

    ea = EventAction(
        name="Buy on bullish",
        type=ExpertEventRuleType.TRADING_RECOMMENDATION_RULE,
        triggers={"trigger_0": {"event_type": ExpertEventType.F_BULLISH.value}},
        actions={"action_0": {"action_type": ExpertActionType.BUY.value}},
        continue_processing=False,
    )
    ea_id = add_instance(ea)
    link_rule_to_ruleset(rs_id, ea_id, order_index=0)
    return rs_id


class TestEvaluateRuleset:
    def test_matching_rule_returns_actions(self):
        acct_def = create_account_definition()
        account = MockAccount(acct_def.id)
        ei = create_expert_instance(account_id=acct_def.id)
        rec = create_recommendation(instance_id=ei.id, recommended_action=OrderRecommendation.BUY)
        rs_id = _setup_bullish_buy_ruleset()

        evaluator = TradeActionEvaluator(account=account)
        results = evaluator.evaluate("AAPL", rec, rs_id)
        assert len(results) > 0

    def test_no_match_returns_empty(self):
        acct_def = create_account_definition()
        account = MockAccount(acct_def.id)
        ei = create_expert_instance(account_id=acct_def.id)
        rec = create_recommendation(
            instance_id=ei.id, recommended_action=OrderRecommendation.SELL,
        )
        rs_id = _setup_bullish_buy_ruleset()

        evaluator = TradeActionEvaluator(account=account)
        results = evaluator.evaluate("AAPL", rec, rs_id)
        assert len(results) == 0

    def test_nonexistent_ruleset_returns_error(self):
        acct_def = create_account_definition()
        account = MockAccount(acct_def.id)
        ei = create_expert_instance(account_id=acct_def.id)
        rec = create_recommendation(instance_id=ei.id)

        evaluator = TradeActionEvaluator(account=account)
        results = evaluator.evaluate("AAPL", rec, 99999)
        # get_instance raises, evaluator catches and returns error
        assert len(results) == 1
        assert "error" in results[0]

    def test_empty_ruleset_returns_empty(self):
        acct_def = create_account_definition()
        account = MockAccount(acct_def.id)
        ei = create_expert_instance(account_id=acct_def.id)
        rec = create_recommendation(instance_id=ei.id)

        # Ruleset with no linked event actions
        rs = Ruleset(name="Empty", type=ExpertEventRuleType.TRADING_RECOMMENDATION_RULE)
        rs_id = add_instance(rs)

        evaluator = TradeActionEvaluator(account=account)
        results = evaluator.evaluate("AAPL", rec, rs_id)
        assert len(results) == 0
