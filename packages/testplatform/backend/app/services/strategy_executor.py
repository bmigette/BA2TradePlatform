"""
Strategy Executor Service

Evaluates strategy conditions against data to generate trade signals.
"""

import logging
from collections import deque, defaultdict
from typing import Dict, Any, Optional, List
from dataclasses import dataclass
from enum import Enum

logger = logging.getLogger(__name__)

# Aggregated logging to reduce verbosity - based on bars, not evaluations
LOG_BAR_INTERVAL = 200  # Log summary every N bars


class EvaluationStats:
    """Tracks condition evaluation statistics for aggregated logging."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.bars_since_log = 0
        self.total_bars = 0
        self.total_evals = 0  # Total condition evaluations
        self.condition_results: Dict[str, Dict[str, int]] = defaultdict(lambda: {'true': 0, 'false': 0})
        self.tree_results: Dict[str, Dict[str, int]] = defaultdict(lambda: {'true': 0, 'false': 0})
        # Cumulative stats for end-of-backtest summary
        self.cumulative_tree_results: Dict[str, Dict[str, int]] = defaultdict(lambda: {'true': 0, 'false': 0})
        self.cumulative_condition_results: Dict[str, Dict[str, int]] = defaultdict(lambda: {'true': 0, 'false': 0})

    def record_condition(self, field: str, result: bool):
        self.condition_results[field]['true' if result else 'false'] += 1
        self.cumulative_condition_results[field]['true' if result else 'false'] += 1
        self.total_evals += 1

    def record_tree(self, label: str, result: bool):
        self.tree_results[label]['true' if result else 'false'] += 1
        self.cumulative_tree_results[label]['true' if result else 'false'] += 1

    def next_bar(self):
        """Call once per bar to track bar-based logging interval."""
        self.bars_since_log += 1
        self.total_bars += 1

        if self.bars_since_log >= LOG_BAR_INTERVAL:
            self._log_summary()
            # Reset interval stats but keep cumulative
            self.bars_since_log = 0
            self.condition_results = defaultdict(lambda: {'true': 0, 'false': 0})
            self.tree_results = defaultdict(lambda: {'true': 0, 'false': 0})

    def get_summary(self) -> Dict[str, Any]:
        """Get cumulative statistics for end-of-backtest summary."""
        return {
            'total_bars': self.total_bars,
            'total_evaluations': self.total_evals,
            'tree_results': dict(self.cumulative_tree_results),
            'condition_results': dict(self.cumulative_condition_results),
        }

    def _log_summary(self):
        if not self.tree_results:
            return

        # Log tree results summary
        tree_summary = []
        for label, counts in self.tree_results.items():
            tree_summary.append(f"{label}: {counts['true']}T/{counts['false']}F")
        logger.debug(f"[Bars {self.total_bars - self.bars_since_log + 1}-{self.total_bars}] Conditions: {', '.join(tree_summary)}")


# Module-level stats tracker
_eval_stats = EvaluationStats()


class ConfirmationTracker:
    """Tracks condition history for confirmation logic."""

    def __init__(self):
        # Maps condition_id -> deque of last N boolean results
        self._history: Dict[str, deque] = {}

    def update_and_check(
        self,
        condition_id: str,
        current_result: bool,
        required_times: int,
        lookback_bars: int
    ) -> bool:
        """
        Update history and check if confirmation is met.

        Args:
            condition_id: Unique ID for this condition
            current_result: Whether condition is true this bar
            required_times: Must be true X times
            lookback_bars: In the last Y bars

        Returns:
            True if condition met required_times in lookback_bars
        """
        if condition_id not in self._history:
            self._history[condition_id] = deque(maxlen=lookback_bars)
        else:
            # Update maxlen if lookback_bars changed
            old_history = self._history[condition_id]
            if old_history.maxlen != lookback_bars:
                self._history[condition_id] = deque(old_history, maxlen=lookback_bars)

        history = self._history[condition_id]
        history.append(current_result)

        # Count True values in history
        true_count = sum(1 for x in history if x)
        return true_count >= required_times

    def reset(self):
        """Clear all history (call on position close if desired)."""
        self._history.clear()


class ExitActionType(Enum):
    CLOSE = "close"
    ADJUST_TP = "adjust_tp"
    ADJUST_SL = "adjust_sl"


@dataclass
class ExitAction:
    action: ExitActionType
    value: Optional[float] = None


@dataclass
class Position:
    entry_price: float
    entry_time: Any
    size: float
    direction: str  # "long" or "short"
    tp_percent: float
    sl_percent: float
    bars_held: int = 0
    days_held: int = 0

    @property
    def unrealized_pnl_pct(self) -> float:
        """Calculate unrealized P&L percentage (placeholder - needs current price)."""
        return 0.0


class StrategyExecutionError(Exception):
    """Raised when strategy execution encounters an unrecoverable error."""
    pass


def evaluate_comparison(left: Any, operator: str, right: Any) -> bool:
    """Evaluate a comparison operation."""
    try:
        # Support both symbol and word-based operators
        if operator in (">", "gt"):
            return float(left) > float(right)
        elif operator in (">=", "gte", "ge"):
            return float(left) >= float(right)
        elif operator in ("<", "lt"):
            return float(left) < float(right)
        elif operator in ("<=", "lte", "le"):
            return float(left) <= float(right)
        elif operator in ("==", "eq", "equals"):
            return left == right
        elif operator in ("!=", "ne", "neq", "not_equals"):
            return left != right
        elif operator == "between":
            if isinstance(right, (list, tuple)) and len(right) == 2:
                return float(right[0]) <= float(left) <= float(right[1])
            raise StrategyExecutionError(f"'between' operator requires [min, max] array, got: {right}")
        elif operator == "is_true":
            # Check if value is truthy (1, True, "true", etc.)
            return bool(left) and left != 0
        elif operator == "is_false":
            # Check if value is falsy (0, False, "false", etc.)
            return not left or left == 0
        else:
            raise StrategyExecutionError(f"Unknown operator: '{operator}'. Valid operators: >, >=, <, <=, ==, !=, gt, gte, lt, lte, eq, ne, between, is_true, is_false")
    except StrategyExecutionError:
        raise
    except (TypeError, ValueError) as e:
        raise StrategyExecutionError(f"Comparison error ({left} {operator} {right}): {e}")


def evaluate_condition(
    condition: dict,
    context: Dict[str, Any],
    confirmation_tracker: Optional['ConfirmationTracker'] = None
) -> bool:
    """
    Evaluate a single condition against the context.

    Args:
        condition: Condition dict with field, comparison, value
        context: Dict with current values for all fields
        confirmation_tracker: Optional tracker for confirmation logic

    Returns:
        True if condition is met, False otherwise
    """
    # Handle nested AND/OR operators
    operator = condition.get("operator")
    if operator in ("AND", "OR"):
        sub_conditions = condition.get("conditions", [])
        if not sub_conditions:
            return True

        if operator == "AND":
            return all(evaluate_condition(c, context, confirmation_tracker) for c in sub_conditions)
        else:  # OR
            return any(evaluate_condition(c, context, confirmation_tracker) for c in sub_conditions)

    # Simple condition
    field = condition.get("field")
    comparison = condition.get("comparison")
    value = condition.get("value")

    if not field or comparison is None:
        import traceback
        logger.error(f"Invalid condition: empty or missing field. condition={condition}\n{''.join(traceback.format_stack())}")
        raise ValueError(f"Invalid strategy condition: empty or missing field (field={repr(field)}, comparison={comparison})")

    # Get field value from context
    field_value = context.get(field)
    if field_value is None:
        # Only log once per missing field to avoid spam
        if not hasattr(evaluate_condition, '_warned_fields'):
            evaluate_condition._warned_fields = set()
        if field not in evaluate_condition._warned_fields:
            evaluate_condition._warned_fields.add(field)
            logger.warning(f"Field '{field}' not found in context (first occurrence, will not repeat)")
        return False

    raw_result = evaluate_comparison(field_value, comparison, value)
    # Record for aggregated logging instead of per-evaluation logging
    _eval_stats.record_condition(field, raw_result)

    # Check if confirmation is required
    confirmation_required = condition.get('confirmationRequired') or condition.get('confirmation_required')
    confirmation_bars = condition.get('confirmationBars') or condition.get('confirmation_bars')

    if confirmation_required and confirmation_bars and confirmation_tracker:
        condition_id = condition.get('id', str(hash(str(condition))))
        return confirmation_tracker.update_and_check(
            condition_id,
            raw_result,
            confirmation_required,
            confirmation_bars
        )

    return raw_result


def evaluate_condition_tree(
    conditions: dict,
    context: Dict[str, Any],
    confirmation_tracker: Optional['ConfirmationTracker'] = None,
    label: str = ""
) -> bool:
    """Evaluate the full condition tree."""
    if not conditions:
        # Only log this once per label to avoid spam
        if not hasattr(evaluate_condition_tree, '_empty_warned'):
            evaluate_condition_tree._empty_warned = set()
        if label not in evaluate_condition_tree._empty_warned:
            evaluate_condition_tree._empty_warned.add(label)
            logger.debug(f"[{label}] No conditions defined, returning False")
        return False
    result = evaluate_condition(conditions, context, confirmation_tracker)
    # Record for aggregated logging
    _eval_stats.record_tree(label, result)
    return result


def reset_evaluation_stats():
    """Reset evaluation statistics. Call at start of new backtest."""
    global _eval_stats
    _eval_stats.reset()
    # Also reset the empty conditions warning tracker
    if hasattr(evaluate_condition_tree, '_empty_warned'):
        evaluate_condition_tree._empty_warned.clear()


def get_evaluation_stats() -> Dict[str, Any]:
    """Get evaluation statistics for end-of-backtest summary."""
    return _eval_stats.get_summary()


def next_evaluation_bar():
    """Call once per bar to track bar-based logging interval."""
    _eval_stats.next_bar()


class StrategyExecutor:
    """Executes strategy conditions against data."""

    def __init__(self, strategy_config: dict):
        """
        Initialize executor with strategy configuration.

        Args:
            strategy_config: Dict with entry_conditions, exit_conditions, tp/sl settings
        """
        self.entry_conditions = strategy_config.get("entry_conditions", {})
        self.exit_conditions = strategy_config.get("exit_conditions", [])
        self.initial_tp_percent = strategy_config.get("initial_tp_percent", 5.0)
        self.initial_sl_percent = strategy_config.get("initial_sl_percent", 2.0)

    def check_entry(self, context: Dict[str, Any]) -> bool:
        """
        Check if entry conditions are met.

        Args:
            context: Dict with bar data and predictions

        Returns:
            True if should enter, False otherwise
        """
        return evaluate_condition_tree(self.entry_conditions, context)

    def check_exits(self, context: Dict[str, Any]) -> Optional[ExitAction]:
        """
        Check exit conditions and return action if any triggered.

        Args:
            context: Dict with bar data, predictions, and position state

        Returns:
            ExitAction if condition triggered, None otherwise
        """
        for exit_rule in self.exit_conditions:
            conditions = exit_rule.get("conditions", {})
            if evaluate_condition_tree(conditions, context):
                action_type = exit_rule.get("action", "close")
                action_value = exit_rule.get("action_value")

                try:
                    action_enum = ExitActionType(action_type)
                except ValueError:
                    action_enum = ExitActionType.CLOSE

                return ExitAction(action=action_enum, value=action_value)

        return None

    def build_context(
        self,
        bar_data: Dict[str, Any],
        predictions: Dict[str, float],
        position: Optional[Position] = None,
        current_price: float = 0.0
    ) -> Dict[str, Any]:
        """
        Build full context for condition evaluation.

        Args:
            bar_data: OHLCV and time data
            predictions: Model prediction probabilities
            position: Current position if any
            current_price: Current market price

        Returns:
            Combined context dict
        """
        context = {**bar_data, **predictions}

        if position:
            context["bars_in_trade"] = position.bars_held
            context["days_in_trade"] = position.days_held

            # Calculate P&L
            if position.direction == "long":
                pnl_pct = (current_price - position.entry_price) / position.entry_price * 100
            else:
                pnl_pct = (position.entry_price - current_price) / position.entry_price * 100

            context["position_pnl_pct"] = pnl_pct
            context["position_pnl_abs"] = pnl_pct * position.size / 100

        return context
