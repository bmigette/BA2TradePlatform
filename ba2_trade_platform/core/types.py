from enum import Enum
from sqlmodel import Field, Session, SQLModel
from dataclasses import dataclass
from datetime import datetime
from typing import Optional


class TimeInterval(str, Enum):
    """
    Standard timeframe intervals for market data.
    
    Maps user-friendly names to market data provider API formats.
    """
    # Minutes
    M1 = "1m"   # 1 minute
    M5 = "5m"   # 5 minutes
    M15 = "15m" # 15 minutes
    M30 = "30m" # 30 minutes
    
    # Hours
    H1 = "1h"   # 1 hour
    H4 = "4h"   # 4 hours
    
    # Days/Weeks/Months
    D1 = "1d"   # 1 day (daily)
    W1 = "1wk"  # 1 week (weekly)
    MO1 = "1mo" # 1 month (monthly)
    
    @classmethod
    def to_yfinance_interval(cls, interval: str) -> str:
        """
        Convert interval to yfinance-compatible format.
        
        Args:
            interval: TimeInterval value or string
        
        Returns:
            yfinance-compatible interval string
        
        Note: Most intervals are passed through as-is. 
        If a specific provider doesn't support an interval, 
        the provider implementation should handle the conversion.
        """
        # Return interval as-is - let provider handle any necessary conversions
        return interval
    
    
    @classmethod
    def get_all_intervals(cls) -> list:
        """Get list of all supported intervals."""
        return [member.value for member in cls]


@dataclass
class MarketDataPoint:
    """
    Represents a single market data point with OHLC data.
    
    Attributes:
        symbol: The ticker symbol (e.g., 'AAPL', 'MSFT')
        timestamp: The datetime of the data point
        open: Opening price
        high: Highest price
        low: Lowest price
        close: Closing price
        volume: Trading volume
        interval: The timeframe interval (e.g., '1d', '1h', '5m')
    """
    symbol: str
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    interval: str = '1d'
    
    def __repr__(self):
        return (f"MarketDataPoint(symbol={self.symbol}, "
                f"timestamp={self.timestamp.strftime('%Y-%m-%d %H:%M')}, "
                f"O={self.open:.2f}, H={self.high:.2f}, L={self.low:.2f}, "
                f"C={self.close:.2f}, V={self.volume:.0f})")


class OrderStatus(str, Enum):
    OPEN = "open"
    CLOSED = "closed"
    ALL = "all"
    NEW = "new"
    UNKNOWN = "unknown"
    CANCELED = "canceled"
    PENDING = "pending"
    WAITING_TRIGGER = "waiting_trigger"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    PENDING_NEW = "pending_new"
    # Additional Alpaca order statuses
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    DONE_FOR_DAY = "done_for_day"
    EXPIRED = "expired"
    REPLACED = "replaced"
    PENDING_CANCEL = "pending_cancel"
    PENDING_REPLACE = "pending_replace"
    PENDING_REVIEW = "pending_review"
    ACCEPTED_FOR_BIDDING = "accepted_for_bidding"
    STOPPED = "stopped"
    SUSPENDED = "suspended"
    CALCULATED = "calculated"
    HELD = "held"
    ERROR = "ERROR"
    
    @classmethod
    def get_terminal_statuses(cls):
        """
        Return a set of order statuses that indicate the order is in a terminal/closed state.
        These are statuses where the order will not change anymore.
        
        Terminal statuses include:
        - CLOSED: Order is closed
        - REJECTED: Order was rejected by the broker
        - CANCELED: Order was canceled
        - EXPIRED: Order expired
        - STOPPED: Order was stopped
        - ERROR: Order encountered an error
        - REPLACED: Order was replaced by another order
        
        Returns:
            set: Set of OrderStatus values representing terminal states
        """
        return {
            cls.CLOSED,
            cls.REJECTED,
            cls.CANCELED,
            cls.EXPIRED,
            cls.STOPPED,
            cls.ERROR,
            cls.REPLACED,
        }
    
    @classmethod
    def get_executed_statuses(cls):
        """
        Return a set of order statuses that indicate the order was executed (position opened).
        
        Returns:
            set: Set of OrderStatus values representing executed states
        """
        return {
            cls.FILLED,
            cls.PARTIALLY_FILLED,
        }
    
    @classmethod
    def get_unfilled_statuses(cls):
        """
        Return a set of order statuses that indicate the order is not yet filled.
        These are statuses where the order is still pending or waiting.
        
        Unfilled statuses include:
        - PENDING: Order is pending
        - NEW: Order is new
        - OPEN: Order is open
        - PENDING_NEW: Order is pending creation
        - WAITING_TRIGGER: Order is waiting for a trigger condition
        - ACCEPTED: Order was accepted but not filled
        - PENDING_CANCEL: Order is pending cancellation (special: waiting to be cancelled before replacement)
        - PENDING_REPLACE: Order is pending replacement
        - PENDING_REVIEW: Order is pending review
        - ACCEPTED_FOR_BIDDING: Order accepted for bidding
        - HELD: Order is held
        
        Returns:
            set: Set of OrderStatus values representing unfilled states
        """
        return {
            cls.PENDING,
            cls.NEW,
            cls.OPEN,
            cls.PENDING_NEW,
            cls.ACCEPTED,
            cls.PENDING_CANCEL,
            cls.PENDING_REPLACE,
            cls.PENDING_REVIEW,
            cls.ACCEPTED_FOR_BIDDING,
            cls.HELD,
        }
    
    @classmethod
    def get_unsent_statuses(cls):
        """
        Return a set of order statuses that indicate the order was never sent to the broker.
        These orders only exist in the database and can be safely closed without broker communication.
        
        Unsent statuses include:
        - PENDING: Order is pending submission to broker
        - WAITING_TRIGGER: Order is waiting for trigger condition (legacy)
        
        Returns:
            set: Set of OrderStatus values representing unsent states
        """
        return {
            cls.PENDING,
            cls.WAITING_TRIGGER,
        }



class InstrumentType(str, Enum):
    STOCK = "stock"
    CRYPTO = "crypto"

class OrderType(str, Enum):
    MARKET = "market"
    BUY_LIMIT = "buy_limit"
    SELL_LIMIT = "sell_limit"
    BUY_STOP = "buy_stop"
    SELL_STOP = "sell_stop"
    BUY_STOP_LIMIT = "buy_stop_limit"
    SELL_STOP_LIMIT = "sell_stop_limit"
    TRAILING_STOP = "trailing_stop"
    # Triggered order types for TP/SL management
    OCO = "oco"  # One-Cancels-Other: TP and SL both defined, if one executes the other cancels
    OTO = "oto"  # One-Triggers-Other: Only TP or SL defined, triggers when parent order executes

class OrderDirection(str, Enum):
    SELL = "SELL"
    BUY = "BUY"

class OrderOpenType(str, Enum):
    MANUAL = "manual"
    AUTOMATIC = "automatic"
    EXTERNAL = "external"
    NOTOPENED = "notopened"

class OrderRecommendation(str, Enum):
    SELL = "SELL"
    BUY = "BUY"
    HOLD = "HOLD"
    ERROR = "ERROR"

class TransactionStatus(str, Enum):
    WAITING = "WAITING"
    OPENED = "OPENED"
    CLOSING = "CLOSING"
    CLOSED = "CLOSED"
    FAILED = "FAILED"  # Transaction creation succeeded but order submission failed

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
    F_HAS_NO_POSITION_ACCOUNT = "has_no_position_account"
    F_HAS_POSITION_ACCOUNT = "has_position_account"
    F_RATING_NEGATIVE_TO_NEUTRAL = "rating_negative_to_neutral"
    F_RATING_NEGATIVE_TO_POSITIVE = "rating_negative_to_positive"
    F_RATING_NEUTRAL_TO_NEGATIVE = "rating_neutral_to_negative"
    F_RATING_NEUTRAL_TO_POSITIVE = "rating_neutral_to_positive"
    F_RATING_POSITIVE_TO_NEGATIVE = "rating_positive_to_negative"
    F_RATING_POSITIVE_TO_NEUTRAL = "rating_positive_to_neutral"
    F_CURRENT_RATING_POSITIVE = "current_rating_positive"
    F_CURRENT_RATING_NEUTRAL = "current_rating_neutral"
    F_CURRENT_RATING_NEGATIVE = "current_rating_negative"
    F_SHORT_TERM = "short_term"
    F_MEDIUM_TERM = "medium_term"
    F_LONG_TERM = "long_term"
    F_HIGHRISK = "highrisk"
    F_MEDIUMRISK = "mediumrisk"
    F_LOWRISK = "lowrisk"
    F_NEW_TARGET_HIGHER = "new_target_higher"  # New expert target is higher than current TP (with 2% tolerance)
    F_NEW_TARGET_LOWER = "new_target_lower"    # New expert target is lower than current TP (with 2% tolerance)
    
    # N = Number/Count
    N_EXPECTED_PROFIT_TARGET_PERCENT = "expected_profit_target_percent"
    N_PERCENT_TO_CURRENT_TARGET = "percent_to_current_target"  # Distance from current price to current TP
    N_PERCENT_TO_NEW_TARGET = "percent_to_new_target"          # Distance from current price to new expert target
    N_PROFIT_LOSS_AMOUNT = "profit_loss_amount"
    N_PROFIT_LOSS_PERCENT = "profit_loss_percent"
    N_DAYS_OPENED = "days_opened"
    N_CONFIDENCE = "confidence"
    N_INSTRUMENT_ACCOUNT_SHARE = "instrument_account_share"    # Current instrument value as % of expert virtual equity
    

class ExpertActionType(str, Enum):
    SELL = "sell"
    BUY = "buy"
    CLOSE = "close"
    ADJUST_TAKE_PROFIT = "adjust_take_profit"
    ADJUST_STOP_LOSS = "adjust_stop_loss"
    INCREASE_INSTRUMENT_SHARE = "increase_instrument_share"
    DECREASE_INSTRUMENT_SHARE = "decrease_instrument_share"

class MarketAnalysisStatus(str, Enum):
    PENDING = "pending"
    COMPLETED = "completed"
    FAILED = "failed"
    RUNNING = "running"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"
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

class ActivityLogSeverity(str, Enum):
    """Severity level for activity log entries."""
    SUCCESS = "success"
    INFO = "info"
    WARNING = "warning"
    FAILURE = "failure"
    DEBUG = "debug"

class ActivityLogType(str, Enum):
    """Type of activity being logged."""
    APPLICATION_STATUS_CHANGE = "application_status_change"
    TRANSACTION_CREATED = "transaction_created"
    TRANSACTION_TP_CHANGED = "transaction_tp_changed"
    TRANSACTION_SL_CHANGED = "transaction_sl_changed"
    TRANSACTION_CLOSED = "transaction_closed"
    RISK_MANAGER_RAN = "risk_manager_ran"
    ANALYSIS_COMPLETED = "analysis_completed"
    ANALYSIS_FAILED = "analysis_failed"
    ORDER_SUBMITTED = "order_submitted"
    ORDER_FILLED = "order_filled"
    ORDER_CANCELLED = "order_cancelled"
    ORDER_REJECTED = "order_rejected"
    EXPERT_RECOMMENDATION = "expert_recommendation"
    RULE_EXECUTED = "rule_executed"

def get_reference_value_options():
    """Return dictionary of reference value options with user-friendly labels."""
    return {
        ReferenceValue.ORDER_OPEN_PRICE.value: 'Order Open Price',
        ReferenceValue.CURRENT_PRICE.value: 'Current Market Price',
        ReferenceValue.EXPERT_TARGET_PRICE.value: 'Expert Target Price'
    }


# Helper functions for UI logic
def get_numeric_event_values():
    """Return list of numeric event type values (N_ prefixed enums)."""
    return [
        ExpertEventType.N_EXPECTED_PROFIT_TARGET_PERCENT.value,
        ExpertEventType.N_PERCENT_TO_CURRENT_TARGET.value,
        ExpertEventType.N_PERCENT_TO_NEW_TARGET.value,
        ExpertEventType.N_PROFIT_LOSS_AMOUNT.value,
        ExpertEventType.N_PROFIT_LOSS_PERCENT.value,
        ExpertEventType.N_DAYS_OPENED.value,
        ExpertEventType.N_CONFIDENCE.value,
        ExpertEventType.N_INSTRUMENT_ACCOUNT_SHARE.value
    ]


def get_adjustment_action_values():
    """Return list of adjustment action type values (ADJUST_ prefixed enums)."""
    return [
        ExpertActionType.ADJUST_TAKE_PROFIT.value,
        ExpertActionType.ADJUST_STOP_LOSS.value
    ]


def get_share_adjustment_action_values():
    """Return list of share adjustment action type values (INCREASE/DECREASE_INSTRUMENT_SHARE)."""
    return [
        ExpertActionType.INCREASE_INSTRUMENT_SHARE.value,
        ExpertActionType.DECREASE_INSTRUMENT_SHARE.value
    ]


def is_numeric_event(event_value):
    """Check if an event value corresponds to a numeric event type."""
    return event_value in get_numeric_event_values()


def is_adjustment_action(action_value):
    """Check if an action value corresponds to an adjustment action type."""
    return action_value in get_adjustment_action_values()


def is_share_adjustment_action(action_value):
    """Check if an action value corresponds to a share adjustment action type."""
    return action_value in get_share_adjustment_action_values()


def get_action_type_display_label(action_value):
    """
    Get user-friendly display label for an ExpertActionType value.
    
    Maps enum values to more descriptive labels:
    - 'buy' -> 'bullish (buy)'
    - 'sell' -> 'bearish (sell)'
    - Others are capitalized with underscores replaced by spaces
    
    Args:
        action_value: The ExpertActionType enum value (e.g., 'buy', 'sell')
        
    Returns:
        User-friendly display label string
    """
    if action_value == ExpertActionType.BUY.value:
        return "bullish (buy)"
    elif action_value == ExpertActionType.SELL.value:
        return "bearish (sell)"
    else:
        # Capitalize and replace underscores with spaces for other actions
        return action_value.replace("_", " ").title()