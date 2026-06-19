"""Tests for StrategyExecutor service."""

import pytest
from app.services.strategy_executor import (
    StrategyExecutor,
    evaluate_condition,
    evaluate_comparison,
    ConfirmationTracker,
    ExitActionType
)


class TestEvaluateComparison:
    def test_greater_than(self):
        assert evaluate_comparison(0.7, ">", 0.5) is True
        assert evaluate_comparison(0.5, ">", 0.7) is False

    def test_greater_than_equal(self):
        assert evaluate_comparison(0.7, ">=", 0.7) is True
        assert evaluate_comparison(0.5, ">=", 0.7) is False

    def test_less_than(self):
        assert evaluate_comparison(0.3, "<", 0.5) is True
        assert evaluate_comparison(0.7, "<", 0.5) is False

    def test_equal(self):
        assert evaluate_comparison(1, "==", 1) is True
        assert evaluate_comparison(1, "==", 2) is False

    def test_not_equal(self):
        assert evaluate_comparison(1, "!=", 2) is True
        assert evaluate_comparison(1, "!=", 1) is False

    def test_between(self):
        assert evaluate_comparison(5, "between", [1, 10]) is True
        assert evaluate_comparison(15, "between", [1, 10]) is False


class TestEvaluateCondition:
    def test_simple_condition(self):
        condition = {"field": "price_up", "comparison": ">", "value": 0.6}
        context = {"price_up": 0.7}
        assert evaluate_condition(condition, context) is True

    def test_missing_field(self):
        condition = {"field": "nonexistent", "comparison": ">", "value": 0.6}
        context = {"price_up": 0.7}
        assert evaluate_condition(condition, context) is False

    def test_and_operator(self):
        condition = {
            "operator": "AND",
            "conditions": [
                {"field": "price_up", "comparison": ">", "value": 0.6},
                {"field": "hour", "comparison": ">=", "value": 9}
            ]
        }
        context = {"price_up": 0.7, "hour": 10}
        assert evaluate_condition(condition, context) is True

        context = {"price_up": 0.7, "hour": 8}
        assert evaluate_condition(condition, context) is False

    def test_or_operator(self):
        condition = {
            "operator": "OR",
            "conditions": [
                {"field": "price_up", "comparison": ">", "value": 0.8},
                {"field": "price_down", "comparison": ">", "value": 0.8}
            ]
        }
        context = {"price_up": 0.5, "price_down": 0.9}
        assert evaluate_condition(condition, context) is True


class TestStrategyExecutor:
    def test_check_entry(self):
        config = {
            "entry_conditions": {
                "operator": "AND",
                "conditions": [
                    {"field": "price_up_10pct", "comparison": ">", "value": 0.7}
                ]
            }
        }
        executor = StrategyExecutor(config)

        assert executor.check_entry({"price_up_10pct": 0.8}) is True
        assert executor.check_entry({"price_up_10pct": 0.5}) is False

    def test_check_exits(self):
        config = {
            "entry_conditions": {},
            "exit_conditions": [
                {
                    "conditions": {"field": "bars_in_trade", "comparison": ">", "value": 50},
                    "action": "close"
                },
                {
                    "conditions": {"field": "position_pnl_pct", "comparison": ">", "value": 5},
                    "action": "adjust_sl",
                    "action_value": 0
                }
            ]
        }
        executor = StrategyExecutor(config)

        # No exit triggered
        action = executor.check_exits({"bars_in_trade": 10, "position_pnl_pct": 1})
        assert action is None

        # Close triggered
        action = executor.check_exits({"bars_in_trade": 60, "position_pnl_pct": 1})
        assert action is not None
        assert action.action == ExitActionType.CLOSE

        # Adjust SL triggered (first matching rule)
        action = executor.check_exits({"bars_in_trade": 10, "position_pnl_pct": 6})
        assert action is not None
        assert action.action == ExitActionType.ADJUST_SL
        assert action.value == 0


class TestConfirmationTracker:
    """Tests for ConfirmationTracker confirmation logic."""

    def test_basic_confirmation_pass(self):
        """Test that confirmation passes when condition met required times."""
        tracker = ConfirmationTracker()

        # Need 2 times in 3 bars
        # Bar 1: True -> count=1 (not enough)
        assert tracker.update_and_check("cond1", True, 2, 3) is False
        # Bar 2: True -> count=2 (passed)
        assert tracker.update_and_check("cond1", True, 2, 3) is True
        # Bar 3: False -> count=2 (still passed, 2 Trues in last 3)
        assert tracker.update_and_check("cond1", False, 2, 3) is True

    def test_confirmation_fail(self):
        """Test that confirmation fails when not enough true values."""
        tracker = ConfirmationTracker()

        # Need 3 times in 3 bars (must all be true)
        assert tracker.update_and_check("cond1", True, 3, 3) is False
        assert tracker.update_and_check("cond1", False, 3, 3) is False
        assert tracker.update_and_check("cond1", True, 3, 3) is False

    def test_sliding_window(self):
        """Test that old values slide out of the window."""
        tracker = ConfirmationTracker()

        # Need 2 times in 3 bars
        tracker.update_and_check("cond1", True, 2, 3)   # [T]
        tracker.update_and_check("cond1", True, 2, 3)   # [T, T] -> passes
        tracker.update_and_check("cond1", False, 2, 3)  # [T, T, F] -> still passes

        # Now the first True slides out
        result = tracker.update_and_check("cond1", False, 2, 3)  # [T, F, F] -> count=1
        assert result is False

        result = tracker.update_and_check("cond1", False, 2, 3)  # [F, F, F] -> count=0
        assert result is False

    def test_multiple_conditions(self):
        """Test that different conditions are tracked independently."""
        tracker = ConfirmationTracker()

        tracker.update_and_check("cond1", True, 2, 2)
        tracker.update_and_check("cond2", False, 1, 2)

        # cond1 should pass (1 True, need 2 in 2)
        assert tracker.update_and_check("cond1", True, 2, 2) is True
        # cond2 should fail (0 True, need 1 in 2)
        assert tracker.update_and_check("cond2", False, 1, 2) is False

    def test_reset_clears_all_history(self):
        """Test that reset clears all condition history."""
        tracker = ConfirmationTracker()

        tracker.update_and_check("cond1", True, 2, 2)
        tracker.update_and_check("cond1", True, 2, 2)  # Now passes

        tracker.reset()

        # After reset, history is gone, so need to build up again
        assert tracker.update_and_check("cond1", True, 2, 2) is False

    def test_exact_threshold(self):
        """Test exact boundary conditions."""
        tracker = ConfirmationTracker()

        # Need exactly 1 time in 1 bar
        assert tracker.update_and_check("cond1", True, 1, 1) is True
        assert tracker.update_and_check("cond1", False, 1, 1) is False

    def test_lookback_change(self):
        """Test that changing lookback_bars adjusts the window."""
        tracker = ConfirmationTracker()

        # Start with lookback of 2
        tracker.update_and_check("cond1", True, 1, 2)
        tracker.update_and_check("cond1", True, 1, 2)

        # Now use lookback of 4 - should expand the window
        result = tracker.update_and_check("cond1", False, 1, 4)
        assert result is True  # Still have True values in expanded window


class TestEvaluateConditionWithConfirmation:
    """Tests for evaluate_condition with confirmation logic."""

    def test_condition_with_confirmation(self):
        """Test condition evaluation with confirmation tracker."""
        tracker = ConfirmationTracker()

        condition = {
            "id": "test_cond",
            "field": "price",
            "comparison": ">",
            "value": 100,
            "confirmationRequired": 2,
            "confirmationBars": 3
        }

        context = {"price": 150}  # Condition is true

        # First time - not enough history
        assert evaluate_condition(condition, context, tracker) is False
        # Second time - now have 2 trues
        assert evaluate_condition(condition, context, tracker) is True

    def test_condition_without_confirmation(self):
        """Test that conditions without confirmation work normally."""
        tracker = ConfirmationTracker()

        condition = {
            "field": "price",
            "comparison": ">",
            "value": 100
        }

        context = {"price": 150}
        # Should pass immediately without confirmation
        assert evaluate_condition(condition, context, tracker) is True

    def test_condition_with_snake_case_confirmation(self):
        """Test condition with snake_case confirmation fields."""
        tracker = ConfirmationTracker()

        condition = {
            "id": "test_cond",
            "field": "price",
            "comparison": ">",
            "value": 100,
            "confirmation_required": 2,
            "confirmation_bars": 3
        }

        context = {"price": 150}

        # First time - not enough history
        assert evaluate_condition(condition, context, tracker) is False
        # Second time - now have 2 trues
        assert evaluate_condition(condition, context, tracker) is True
