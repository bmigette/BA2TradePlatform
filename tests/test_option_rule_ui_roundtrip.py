"""Data-level round-trip tests for the option-action rules-editor widgets (Phase 2 Task 8).

NiceGUI widgets can't be browser-driven here, so instead of driving the widgets we
assert that the action_config dict the save handler (_save_rule in
ui/pages/settings.py) WOULD produce is consumed correctly by the evaluator
(TradeActionEvaluator._create_trade_action, Task 6). This locks the UI-produced
shape to the evaluator-consumed shape:
  - single-leg actions write a float strike_param,
  - spread actions write a {"long":..,"short":..} dict strike_param,
  - the 6 numeric/select params round-trip onto the built action.
"""
from ba2_trade_platform.core.TradeActionEvaluator import TradeActionEvaluator
from ba2_trade_platform.core.TradeActions import BuyCallAction, OpenBullCallSpreadAction
from ba2_trade_platform.core.types import (
    ExpertActionType, OrderRecommendation, is_option_action,
)


def test_is_option_action_classifies_all_four():
    for v in ("buy_call", "open_bull_call_spread", "sell_covered_call", "close_option"):
        assert is_option_action(v)


def test_ui_action_config_shape_parses_in_evaluator(mock_account, sample_recommendation):
    ev = TradeActionEvaluator(account=mock_account)

    # single-leg shape (as the save handler writes it: strike_param is a float)
    cfg = {"action_type": "buy_call", "strike_method": "delta", "strike_param": 0.30,
           "dte_min": 20, "dte_max": 45, "sizing": 2.0,
           "min_open_interest": 100, "max_spread_pct": 15.0}
    action = ev._create_trade_action(
        ExpertActionType.BUY_CALL, cfg, "AAPL",
        OrderRecommendation.BUY, None, sample_recommendation,
    )
    assert isinstance(action, BuyCallAction)
    assert action.strike_method == "delta"
    assert action.strike_param == 0.30
    assert action.dte_min == 20
    assert action.dte_max == 45
    assert action.sizing == 2.0
    assert action.min_open_interest == 100
    assert action.max_spread_pct == 15.0

    # spread shape (save handler parses a JSON dict from the strike_param input)
    cfg2 = {"action_type": "open_bull_call_spread", "strike_method": "delta",
            "strike_param": {"long": 0.45, "short": 0.25}, "dte_min": 20, "dte_max": 45,
            "sizing": 5.0, "min_open_interest": 100, "max_spread_pct": 15.0}
    action2 = ev._create_trade_action(
        ExpertActionType.OPEN_BULL_CALL_SPREAD, cfg2, "AAPL",
        OrderRecommendation.BUY, None, sample_recommendation,
    )
    assert isinstance(action2, OpenBullCallSpreadAction)
    assert action2.strike_param == {"long": 0.45, "short": 0.25}


def test_close_option_config_has_no_params(mock_account, sample_recommendation):
    """CLOSE_OPTION is built from a param-free config (the UI renders no widgets)."""
    ev = TradeActionEvaluator(account=mock_account)
    cfg = {"action_type": "close_option"}
    action = ev._create_trade_action(
        ExpertActionType.CLOSE_OPTION, cfg, "AAPL",
        OrderRecommendation.BUY, None, sample_recommendation,
    )
    assert action is not None
    assert ev._get_action_type_from_action(action) == ExpertActionType.CLOSE_OPTION
