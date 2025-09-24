from enum import Enum
from sqlmodel import  Field, Session, SQLModel, create_engine

class OrderStatus(str, Enum):
    OPEN = "open"
    CLOSED = "closed"
    ALL = "all"
    NEW = "new"
    UNKNOWN = "unknown"
    CANCELED = "canceled"


class InstrumentType(str, Enum):
    STOCK = "stock"
    CRYPTO = "crypto"

class OrderType(str, Enum):
    MARKET = "market"
    BUY_LIMIT = "buy_limit"
    SELL_LIMIT = "sell_limit"
    BUY_STOP = "buy_stop"
    SELL_STOP = "sell_stop"


class OrderDirection(str, Enum):
    SELL = "sell"
    BUY = "buy"

class OrderRecommendation(str, Enum):
    SELL = "SELL"
    BUY = "BUY"
    HOLD = "HOLD"
    ERROR = "ERROR"

class RiskLevel(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"

class TimeHorizon(str, Enum):
    SHORT_TERM = "SHORT_TERM"
    MEDIUM_TERM = "MEDIUM_TERM"
    LONG_TERM = "LONG_TERM"

class ExpertEventRuleType(str, Enum):
    TRADING_RECOMMENDATION_RULE = "trading_recommendation_rule"

class AnalysisUseCase(str, Enum):
    ENTER_MARKET = "enter_market"
    OPEN_POSITIONS = "open_positions"
    
class ExpertEventType(str, Enum):
    # F = Flag/Boolean
    F_BEARISH = "bearish"
    F_BULLISH = "bullish"
    F_HAS_NO_POSITION = "has_no_position"
    F_HAS_POSITION = "has_position"
    F_RATING_NEGATIVE_TO_NEUTRAL = "rating_negative_to_neutral"
    F_RATING_NEGATIVE_TO_POSITIVE = "rating_negative_to_positive"
    F_RATING_NEUTRAL_TO_NEGATIVE = "rating_neutral_to_negative"
    F_RATING_NEUTRAL_TO_POSITIVE = "rating_neutral_to_positive"
    F_RATING_POSITIVE_TO_NEGATIVE = "rating_positive_to_negative"
    F_RATING_POSITIVE_TO_NEUTRAL = "rating_positive_to_neutral"
    # N = Number/Count
    N_EXPECTED_PROFIT_TARGET_PERCENT = "expected_profit_target_percent"
    N_PERCENT_TO_TARGET = "percent_to_target"
    N_PROFIT_LOSS_AMOUNT = "profit_loss_amount"
    N_PROFIT_LOSS_PERCENT = "profit_loss_percent"
    N_TIME_OPENED = "time_opened"

class ExpertActionType(str, Enum):
    SELL = "sell"
    BUY = "buy"
    CLOSE = "close"
    ADJUST_TAKE_PROFIT = "adjust_take_profit"
    ADJUST_STOP_LOSS = "adjust_stop_loss"

class MarketAnalysisStatus(str, Enum):
    PENDING = "pending"
    COMPLETED = "completed"
    FAILED = "failed"
    RUNNING = "running"
    CANCELLED = "cancelled"
class ReferenceValue(str, Enum):
    ORDER_OPEN_PRICE = "order_open_price"
    CURRENT_PRICE = "current_price"
    EXPERT_TARGET_PRICE = "expert_target_price"

class WorkerTaskStatus(Enum):
    """Status of a worker task."""
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"

def get_reference_value_options():
    """Return list of reference value options with user-friendly labels."""
    return [
        {'value': ReferenceValue.ORDER_OPEN_PRICE.value, 'label': 'Order Open Price'},
        {'value': ReferenceValue.CURRENT_PRICE.value, 'label': 'Current Market Price'},
        {'value': ReferenceValue.EXPERT_TARGET_PRICE.value, 'label': 'Expert Target Price'}
    ]


# Helper functions for UI logic
def get_numeric_event_values():
    """Return list of numeric event type values (N_ prefixed enums)."""
    return [
        ExpertEventType.N_EXPECTED_PROFIT_TARGET_PERCENT.value,
        ExpertEventType.N_PERCENT_TO_TARGET.value,
        ExpertEventType.N_PROFIT_LOSS_AMOUNT.value,
        ExpertEventType.N_PROFIT_LOSS_PERCENT.value,
        ExpertEventType.N_TIME_OPENED.value
    ]


def get_adjustment_action_values():
    """Return list of adjustment action type values (ADJUST_ prefixed enums)."""
    return [
        ExpertActionType.ADJUST_TAKE_PROFIT.value,
        ExpertActionType.ADJUST_STOP_LOSS.value
    ]


def is_numeric_event(event_value):
    """Check if an event value corresponds to a numeric event type."""
    return event_value in get_numeric_event_values()


def is_adjustment_action(action_value):
    """Check if an action value corresponds to an adjustment action type."""
    return action_value in get_adjustment_action_values()