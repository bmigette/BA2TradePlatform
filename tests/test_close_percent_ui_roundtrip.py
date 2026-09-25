"""Data-level round trip for the rules editor's buy/sell "Close %" field.

NiceGUI widgets can't be driven here (see test_option_rule_ui_roundtrip.py), so this pins the
two ends: the save handler in ui/pages/settings.py writes the percent as the action's ``value``
for BUY/SELL, and the evaluator builds the action with that ``close_percent``. An empty field
writes nothing, which is a full close (and byte-identical to every rule saved before the field).
"""
import inspect

from ba2_trade_platform.core.TradeActionEvaluator import TradeActionEvaluator
from ba2_trade_platform.core.TradeActions import BuyAction, SellAction
from ba2_trade_platform.core.types import ExpertActionType, OrderRecommendation


def test_the_saved_shape_reaches_the_action(mock_account, sample_recommendation):
    ev = TradeActionEvaluator(account=mock_account)
    for at, cls in ((ExpertActionType.SELL, SellAction), (ExpertActionType.BUY, BuyAction)):
        action = ev._create_trade_action(at, {"action_type": at.value, "value": 50.0}, "AAPL",
                                         OrderRecommendation.SELL, None, sample_recommendation)
        assert isinstance(action, cls) and action.close_percent == 50.0
        full = ev._create_trade_action(at, {"action_type": at.value}, "AAPL",
                                       OrderRecommendation.SELL, None, sample_recommendation)
        assert full.close_percent is None


def test_the_editor_renders_and_saves_the_field():
    from ba2_trade_platform.ui.pages import settings
    src = inspect.getsource(settings)
    render = src.index("label='Close % (optional)'")
    assert "selected_type in (ExpertActionType.BUY.value, ExpertActionType.SELL.value)" in src[
        render - 1200:render]
    save = src.index("Close percent must be between 1 and 100")
    assert "action_config['value'] = close_pct" in src[save:save + 300]


def test_switching_a_row_to_buy_sell_does_not_prefill_an_adjust_offset():
    """A row saved as adjust_stop_loss -8 and switched to sell must not show -8 as a close
    percent: the field prefills only from a row SAVED as the selected buy/sell type."""
    from ba2_trade_platform.ui.pages import settings
    src = inspect.getsource(settings)
    render = src.index("label='Close % (optional)'")
    block = src[render - 700:render]
    assert "if saved_type == selected_type else ''" in block
    assert "value=prefill" in src[render:render + 120]
