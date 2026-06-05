"""Tests for option ACTION wiring in TradeActionEvaluator (Phase 2 Task 6).

Two coverage angles:
(a) Unit: _create_trade_action builds a BuyCallAction from an option action_config
    with the 7 option params populated, and _get_action_type_from_action maps the
    class back to ExpertActionType.BUY_CALL.
(b) Integration: a Ruleset + EventAction whose triggers pass (BUY recommendation
    -> F_BULLISH) and whose actions contain a buy_call option action actually
    executes (submit_option_order is called) instead of being silently skipped.
"""
import pytest

from ba2_trade_platform.core.TradeActionEvaluator import TradeActionEvaluator
from ba2_trade_platform.core.TradeActions import BuyCallAction
from ba2_trade_platform.core.types import (
    ExpertActionType, ExpertEventType, ExpertEventRuleType, OrderRecommendation,
    OptionRight, OrderDirection, OrderType, OrderStatus, AnalysisUseCase,
)
from ba2_trade_platform.core.models import Ruleset, EventAction, TradingOrder
from ba2_trade_platform.core.db import add_instance
from ba2_trade_platform.core.option_types import OptionContract
from tests.factories import (
    create_account_definition, create_expert_instance, create_recommendation,
    link_rule_to_ruleset,
)
from tests.conftest import MockAccount


def _option_cfg():
    return {
        "action_type": "buy_call",
        "strike_method": "delta",
        "strike_param": 0.5,
        "dte_min": 20,
        "dte_max": 45,
        "sizing": 2.0,
        "min_open_interest": 100,
        "max_spread_pct": 20.0,
    }


# ---------------------------------------------------------------------------
# (a) Unit: _create_trade_action builds a BuyCallAction with params populated
# ---------------------------------------------------------------------------
def test_create_trade_action_builds_buy_call(mock_account, sample_recommendation):
    ev = TradeActionEvaluator(account=mock_account)
    cfg = _option_cfg()
    action = ev._create_trade_action(
        ExpertActionType.BUY_CALL, cfg, "AAPL",
        OrderRecommendation.BUY, None, sample_recommendation,
    )
    assert isinstance(action, BuyCallAction)
    assert action.strike_method == "delta"
    assert action.strike_param == 0.5
    assert action.dte_min == 20
    assert action.dte_max == 45
    assert action.sizing == 2.0
    assert action.min_open_interest == 100
    assert action.max_spread_pct == 20.0
    # and the class maps back to the enum
    assert ev._get_action_type_from_action(action) == ExpertActionType.BUY_CALL


def test_get_action_type_maps_all_option_actions(mock_account, sample_recommendation):
    """All four option action classes map back to their enum members."""
    ev = TradeActionEvaluator(account=mock_account)
    cases = {
        ExpertActionType.BUY_CALL: _option_cfg(),
        ExpertActionType.OPEN_BULL_CALL_SPREAD: {**_option_cfg(), "action_type": "open_bull_call_spread"},
        ExpertActionType.SELL_COVERED_CALL: {**_option_cfg(), "action_type": "sell_covered_call"},
        ExpertActionType.CLOSE_OPTION: {**_option_cfg(), "action_type": "close_option"},
    }
    for enum_member, cfg in cases.items():
        action = ev._create_trade_action(
            enum_member, cfg, "AAPL", OrderRecommendation.BUY, None, sample_recommendation,
        )
        assert action is not None, f"action build failed for {enum_member}"
        assert ev._get_action_type_from_action(action) == enum_member


def test_option_actions_sorted_as_order_creating(mock_account, sample_recommendation):
    """Option actions get an order-creating priority (not the unknown=99 bucket)."""
    ev = TradeActionEvaluator(account=mock_account)
    buy_call = ev._create_trade_action(
        ExpertActionType.BUY_CALL, _option_cfg(), "AAPL",
        OrderRecommendation.BUY, None, sample_recommendation,
    )
    close_opt = ev._create_trade_action(
        ExpertActionType.CLOSE_OPTION, {"action_type": "close_option"}, "AAPL",
        OrderRecommendation.BUY, None, sample_recommendation,
    )
    # _sort_actions_by_priority must not drop them to the unknown bucket; CLOSE_OPTION
    # (priority 2) sorts after BUY_CALL (priority 1).
    ordered = ev._sort_actions_by_priority([close_opt, buy_call])
    assert isinstance(ordered[0], BuyCallAction)


def test_dedup_distinguishes_option_params(mock_account, mock_expert_instance):
    """Two buy_call actions differing only by option params must not collide."""
    rec = create_recommendation(
        instance_id=mock_expert_instance.id, recommended_action=OrderRecommendation.BUY,
    )
    rs = Ruleset(name="Two calls", type=ExpertEventRuleType.TRADING_RECOMMENDATION_RULE)
    rs_id = add_instance(rs)
    ea = EventAction(
        name="Two buy calls",
        type=ExpertEventRuleType.TRADING_RECOMMENDATION_RULE,
        triggers={"trigger_0": {"event_type": ExpertEventType.F_BULLISH.value}},
        actions={
            "action_0": {"action_type": "buy_call", "strike_method": "delta",
                         "strike_param": 0.3, "dte_min": 20, "dte_max": 45, "sizing": 2.0},
            "action_1": {"action_type": "buy_call", "strike_method": "delta",
                         "strike_param": 0.6, "dte_min": 20, "dte_max": 45, "sizing": 2.0},
        },
        continue_processing=False,
    )
    ea_id = add_instance(ea)
    link_rule_to_ruleset(rs_id, ea_id, order_index=0)

    ev = TradeActionEvaluator(account=mock_account)
    ev.evaluate("AAPL", rec, rs_id)
    # Both option actions must survive dedup (they differ only by strike_param).
    assert len(ev.trade_actions) == 2


# ---------------------------------------------------------------------------
# (b) Integration: option action actually executes end-to-end
# ---------------------------------------------------------------------------
def _canned_chain():
    return [OptionContract(
        symbol="AAPL150C", underlying="AAPL", option_type=OptionRight.CALL,
        strike=150.0, expiry=None, bid=4.9, ask=5.1, last=5.0,
        implied_volatility=0.30, delta=0.50, gamma=0.02, theta=-0.03, vega=0.1,
        open_interest=2000, volume=250)]


def test_buy_call_option_action_executes(monkeypatch):
    from datetime import date, timedelta
    acct_def = create_account_definition()
    account = MockAccount(acct_def.id)
    ei = create_expert_instance(account_id=acct_def.id)
    rec = create_recommendation(instance_id=ei.id, recommended_action=OrderRecommendation.BUY)

    # Canned 1-call chain with a valid expiry inside the dte window.
    chain = _canned_chain()
    for c in chain:
        c.expiry = date.today() + timedelta(days=35)
    monkeypatch.setattr(account, "get_option_chain", lambda *a, **k: chain, raising=False)

    captured = {}

    def fake_submit(legs, quantity, order_type="limit", limit_price=None,
                    option_strategy=None, expert_recommendation_id=None, transaction_id=None):
        captured.update(called=True, legs=legs, quantity=quantity,
                        option_strategy=option_strategy, limit_price=limit_price)
        return TradingOrder(account_id=account.id, symbol="AAPL", quantity=quantity,
                            side=OrderDirection.BUY, order_type=OrderType.BUY_LIMIT,
                            status=OrderStatus.FILLED)

    monkeypatch.setattr(account, "submit_option_order", fake_submit, raising=False)

    rs = Ruleset(name="Enter via call", type=ExpertEventRuleType.TRADING_RECOMMENDATION_RULE,
                 subtype=AnalysisUseCase.ENTER_MARKET.value)
    rs_id = add_instance(rs)
    ea = EventAction(
        name="Buy call on bullish",
        type=ExpertEventRuleType.TRADING_RECOMMENDATION_RULE,
        triggers={"trigger_0": {"event_type": ExpertEventType.F_BULLISH.value}},
        actions={"action_0": {"action_type": "buy_call", "strike_method": "delta",
                              "strike_param": 0.50, "dte_min": 20, "dte_max": 45,
                              "sizing": 2.0, "min_open_interest": 100, "max_spread_pct": 20.0}},
        continue_processing=False,
    )
    ea_id = add_instance(ea)
    link_rule_to_ruleset(rs_id, ea_id, order_index=0)

    ev = TradeActionEvaluator(account=account)
    summaries = ev.evaluate("AAPL", rec, rs_id)
    assert len(summaries) > 0
    assert len(ev.trade_actions) == 1

    results = ev.execute(submit_to_broker=True)
    # The option action executed (not skipped as unknown type).
    assert captured.get("called") is True
    assert captured["option_strategy"] == "long_call"
    assert any(r.get("success") for r in results)
