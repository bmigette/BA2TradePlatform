"""Tests for ba2_trade_platform.core.types enum helpers and status groups."""
import pytest
from ba2_trade_platform.core.types import (
    OrderStatus, ExpertEventType, ExpertActionType,
    RiskLevel, TimeHorizon,
    is_numeric_event, is_adjustment_action, is_share_adjustment_action,
    get_action_type_display_label, get_numeric_event_values,
    get_adjustment_action_values, get_share_adjustment_action_values,
)


class TestOrderStatusGroups:
    def test_terminal_statuses_contains_expected(self):
        terminal = OrderStatus.get_terminal_statuses()
        expected = {
            OrderStatus.CLOSED, OrderStatus.REJECTED, OrderStatus.CANCELED,
            OrderStatus.EXPIRED, OrderStatus.STOPPED, OrderStatus.ERROR,
            OrderStatus.REPLACED,
        }
        assert terminal == expected

    def test_executed_statuses(self):
        executed = OrderStatus.get_executed_statuses()
        assert OrderStatus.FILLED in executed
        assert OrderStatus.PARTIALLY_FILLED in executed
        assert len(executed) == 2

    def test_unfilled_statuses_does_not_contain_filled(self):
        unfilled = OrderStatus.get_unfilled_statuses()
        assert OrderStatus.FILLED not in unfilled
        assert OrderStatus.PENDING in unfilled

    def test_unsent_statuses(self):
        unsent = OrderStatus.get_unsent_statuses()
        assert unsent == {OrderStatus.PENDING, OrderStatus.WAITING_TRIGGER}

    def test_terminal_and_executed_do_not_overlap(self):
        terminal = OrderStatus.get_terminal_statuses()
        executed = OrderStatus.get_executed_statuses()
        assert terminal.isdisjoint(executed)


class TestNumericEventHelpers:
    @pytest.mark.parametrize("event_value", [
        ExpertEventType.N_EXPECTED_PROFIT_TARGET_PERCENT.value,
        ExpertEventType.N_CONFIDENCE.value,
        ExpertEventType.N_DAYS_OPENED.value,
        ExpertEventType.N_PROFIT_LOSS_PERCENT.value,
        ExpertEventType.N_INSTRUMENT_ACCOUNT_SHARE.value,
        ExpertEventType.N_PROFIT_LOSS_AMOUNT.value,
        ExpertEventType.N_PERCENT_TO_CURRENT_TARGET.value,
        ExpertEventType.N_PERCENT_TO_NEW_TARGET.value,
    ])
    def test_is_numeric_event_true(self, event_value):
        assert is_numeric_event(event_value) is True

    @pytest.mark.parametrize("event_value", [
        ExpertEventType.F_BULLISH.value,
        ExpertEventType.F_BEARISH.value,
        ExpertEventType.F_HAS_POSITION.value,
        ExpertEventType.F_SHORT_TERM.value,
        ExpertEventType.F_HIGHRISK.value,
    ])
    def test_is_numeric_event_false(self, event_value):
        assert is_numeric_event(event_value) is False

    def test_get_numeric_event_values_returns_list(self):
        values = get_numeric_event_values()
        assert isinstance(values, list)
        assert len(values) >= 5


class TestAdjustmentActionHelpers:
    def test_is_adjustment_action_true(self):
        assert is_adjustment_action(ExpertActionType.ADJUST_TAKE_PROFIT.value) is True
        assert is_adjustment_action(ExpertActionType.ADJUST_STOP_LOSS.value) is True

    def test_is_adjustment_action_false(self):
        assert is_adjustment_action(ExpertActionType.BUY.value) is False
        assert is_adjustment_action(ExpertActionType.SELL.value) is False
        assert is_adjustment_action(ExpertActionType.CLOSE.value) is False

    def test_is_share_adjustment_action_true(self):
        assert is_share_adjustment_action(ExpertActionType.INCREASE_INSTRUMENT_SHARE.value) is True
        assert is_share_adjustment_action(ExpertActionType.DECREASE_INSTRUMENT_SHARE.value) is True

    def test_is_share_adjustment_action_false(self):
        assert is_share_adjustment_action(ExpertActionType.BUY.value) is False
        assert is_share_adjustment_action(ExpertActionType.ADJUST_TAKE_PROFIT.value) is False

    def test_get_adjustment_action_values_returns_list(self):
        values = get_adjustment_action_values()
        assert len(values) == 2

    def test_get_share_adjustment_action_values_returns_list(self):
        values = get_share_adjustment_action_values()
        assert len(values) == 2


class TestDisplayLabels:
    def test_buy_label(self):
        assert get_action_type_display_label("buy") == "bullish (buy)"

    def test_sell_label(self):
        assert get_action_type_display_label("sell") == "bearish (sell)"

    def test_close_label(self):
        result = get_action_type_display_label("close")
        assert result == "Close"

    def test_adjust_take_profit_label(self):
        result = get_action_type_display_label("adjust_take_profit")
        assert result == "Adjust Take Profit"

    def test_adjust_stop_loss_label(self):
        result = get_action_type_display_label("adjust_stop_loss")
        assert result == "Adjust Stop Loss"

    def test_increase_instrument_share_label(self):
        result = get_action_type_display_label("increase_instrument_share")
        assert result == "Increase Instrument Share"
