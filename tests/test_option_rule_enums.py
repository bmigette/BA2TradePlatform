from ba2_trade_platform.core.types import (
    ExpertEventType, ExpertActionType,
    get_numeric_event_values, is_numeric_event,
    get_option_action_values, is_option_action,
)


def test_new_event_enum_values():
    assert ExpertEventType.N_PERCENT_BELOW_RECENT_HIGH.value == "percent_below_recent_high"
    assert ExpertEventType.N_PERCENT_ABOVE_RECENT_LOW.value == "percent_above_recent_low"
    assert ExpertEventType.N_IV_RANK.value == "iv_rank"
    assert ExpertEventType.F_HAS_OPTION_POSITION.value == "has_option_position"
    assert ExpertEventType.F_HAS_COVERED_CALL.value == "has_covered_call"


def test_new_action_enum_values():
    assert ExpertActionType.BUY_CALL.value == "buy_call"
    assert ExpertActionType.OPEN_BULL_CALL_SPREAD.value == "open_bull_call_spread"
    assert ExpertActionType.SELL_COVERED_CALL.value == "sell_covered_call"
    assert ExpertActionType.CLOSE_OPTION.value == "close_option"


def test_numeric_events_include_new_option_events():
    for v in ("percent_below_recent_high", "percent_above_recent_low", "iv_rank"):
        assert is_numeric_event(v), v
        assert v in get_numeric_event_values()


def test_option_action_classifier():
    vals = get_option_action_values()
    for a in ("buy_call", "open_bull_call_spread", "sell_covered_call", "close_option"):
        assert a in vals
        assert is_option_action(a)
    assert not is_option_action("buy")
    assert not is_option_action("adjust_take_profit")
