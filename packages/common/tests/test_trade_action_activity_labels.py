"""log_trade_action_activity: readable description, TP_SL_ADJUSTED type, unknown action raises."""
import pytest

from ba2_common.core import db as db_mod
from ba2_common.core.types import (
    ACTION_ADJUST_TP_SL, ActivityLogSeverity, ActivityLogType, ExpertActionType,
    get_activity_type_display_label,
)
from ba2_common.core.utils import log_trade_action_activity


@pytest.fixture
def logged(monkeypatch):
    calls = []
    monkeypatch.setattr(db_mod, "log_activity", lambda **kw: calls.append(kw))
    return calls


def _log(action, open_position=True, success=True):
    log_trade_action_activity(action_type=action, symbol="SBSW", account_id=1, expert_id=2,
                              success=success, message="m", is_open_position=open_position)


def test_enum_input_gives_readable_description(logged):
    _log(ExpertActionType.ADJUST_STOP_LOSS)
    assert logged[0]["description"] == (
        "✓ Successfully executed Adjust Stop Loss for SBSW on open position")
    assert logged[0]["data"]["action_type"] == "adjust_stop_loss"


def test_value_string_is_normalised(logged):
    _log("adjust_take_profit")
    assert "Adjust Take Profit" in logged[0]["description"]


@pytest.mark.parametrize("action", [ExpertActionType.ADJUST_STOP_LOSS,
                                    ExpertActionType.ADJUST_TAKE_PROFIT, ACTION_ADJUST_TP_SL])
def test_tp_sl_adjustments_use_tp_sl_adjusted(logged, action):
    _log(action)
    assert logged[0]["activity_type"] == ActivityLogType.TP_SL_ADJUSTED


def test_merged_label(logged):
    _log(ACTION_ADJUST_TP_SL, success=False)
    assert logged[0]["description"].startswith("✗ Failed to execute Adjust TP/SL for SBSW")
    assert logged[0]["severity"] == ActivityLogSeverity.FAILURE


def test_other_open_position_action_keeps_trade_action_open(logged):
    _log(ExpertActionType.CLOSE)
    assert logged[0]["activity_type"] == ActivityLogType.TRADE_ACTION_OPEN
    assert "Close for SBSW on open position" in logged[0]["description"]


def test_new_entry_keeps_trade_action_new(logged):
    _log(ExpertActionType.BUY, open_position=False)
    assert logged[0]["activity_type"] == ActivityLogType.TRADE_ACTION_NEW
    assert "Buy for SBSW for new entry" in logged[0]["description"]


@pytest.mark.parametrize("bad", ["ExpertActionType.ADJUST_STOP_LOSS", "nonsense", None, 5])
def test_unknown_action_raises(logged, bad):
    with pytest.raises(ValueError):
        _log(bad)
    assert logged == []


def test_logging_failure_does_not_break_caller(monkeypatch):
    def boom(**kw):
        raise RuntimeError("db down")
    monkeypatch.setattr(db_mod, "log_activity", boom)
    _log(ExpertActionType.BUY)  # swallowed, as before


def test_activity_type_labels():
    assert get_activity_type_display_label(ActivityLogType.TRADE_ACTION_OPEN) == "Trade Action (Open Position)"
    assert get_activity_type_display_label("trade_action_new") == "Trade Action (New Entry)"
    assert get_activity_type_display_label(ActivityLogType.TP_SL_ADJUSTED) == "TP/SL Adjusted"
    assert get_activity_type_display_label(ActivityLogType.ORDER_FILLED) == "Order Filled"
