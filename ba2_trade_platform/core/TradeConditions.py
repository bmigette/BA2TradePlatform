"""
TradeConditions - Core component for evaluating trading conditions

This module provides base classes and implementations for evaluating various trading conditions
that can be used in rulesets and automated trading decisions.
"""

from abc import ABC, abstractmethod
from typing import List, Optional, Any, Dict
from datetime import datetime, timezone, timedelta
import operator

from .AccountInterface import AccountInterface
from .models import TradingOrder, ExpertRecommendation, ExpertInstance
from .types import OrderRecommendation, ExpertEventType, RiskLevel
from .db import get_db
from ..logger import logger
from sqlmodel import select, Session


class TradeCondition(ABC):
    """
    Base class for all trading conditions.
    
    Provides common functionality for evaluating trading conditions based on:
    - Account state
    - Instrument information  
    - Current order recommendation
    - Existing orders
    """
    
    def __init__(self, account: AccountInterface, instrument_name: str, 
                 order_recommendation: OrderRecommendation, existing_order: Optional[TradingOrder] = None):
        """
        Initialize the trade condition.
        
        Args:
            account: Account interface for accessing account data
            instrument_name: Name of the instrument being evaluated
            order_recommendation: The recommendation being evaluated
            existing_order: Optional existing order related to this evaluation
        """
        self.account = account
        self.instrument_name = instrument_name
        self.order_recommendation = order_recommendation
        self.existing_order = existing_order
        
    @abstractmethod
    def evaluate(self) -> bool:
        """
        Evaluate the condition and return True/False.
        
        Returns:
            bool: True if condition is met, False otherwise
        """
        pass
    
    def get_previous_recommendations(self, expert_instance_id: int, limit: int = 10) -> List[ExpertRecommendation]:
        """
        Get previous recommendations for this expert and instrument.
        
        Args:
            expert_instance_id: ID of the expert instance
            limit: Maximum number of recommendations to return
            
        Returns:
            List of previous recommendations ordered by creation date (newest first)
        """
        try:
            with get_db() as session:
                statement = (
                    select(ExpertRecommendation)
                    .where(
                        ExpertRecommendation.instance_id == expert_instance_id,
                        ExpertRecommendation.symbol == self.instrument_name
                    )
                    .order_by(ExpertRecommendation.created_at.desc())
                    .limit(limit)
                )
                
                recommendations = session.exec(statement).all()
                return list(recommendations)
                
        except Exception as e:
            logger.error(f"Error getting previous recommendations: {e}", exc_info=True)
            return []
    
    def get_current_position(self) -> Optional[float]:
        """
        Get current position quantity for the instrument.
        
        Returns:
            Position quantity (positive for long, negative for short, None if no position)
        """
        try:
            positions = self.account.get_positions()
            for position in positions:
                if hasattr(position, 'symbol') and position.symbol == self.instrument_name:
                    return getattr(position, 'qty', None)
            return None
        except Exception as e:
            logger.error(f"Error getting current position: {e}", exc_info=True)
            return None
    
    def get_current_price(self) -> Optional[float]:
        """
        Get current market price for the instrument.
        
        Returns:
            Current price or None if unavailable
        """
        try:
            return self.account.get_instrument_current_price(self.instrument_name)
        except Exception as e:
            logger.error(f"Error getting current price: {e}", exc_info=True)
            return None
    
    def has_position(self) -> bool:
        """
        Check if there's an open position for this instrument.
        
        Returns:
            True if position exists, False otherwise
        """
        position = self.get_current_position()
        return position is not None and position != 0


class FlagCondition(TradeCondition):
    """
    Base class for flag-based (boolean) conditions.
    """
    pass


class CompareCondition(TradeCondition):
    """
    Base class for comparison-based conditions.
    """
    
    def __init__(self, account: AccountInterface, instrument_name: str,
                 order_recommendation: OrderRecommendation, operator_str: str, value: float,
                 existing_order: Optional[TradingOrder] = None):
        """
        Initialize comparison condition.
        
        Args:
            account: Account interface
            instrument_name: Instrument name
            order_recommendation: Order recommendation
            operator_str: Comparison operator ('>', '<', '>=', '<=', '==', '!=')
            value: Value to compare against
            existing_order: Optional existing order
        """
        super().__init__(account, instrument_name, order_recommendation, existing_order)
        self.operator_str = operator_str
        self.value = value
        
        # Map operator strings to functions
        self.operator_map = {
            '>': operator.gt,
            '<': operator.lt,
            '>=': operator.ge,
            '<=': operator.le,
            '==': operator.eq,
            '!=': operator.ne
        }
        
        if operator_str not in self.operator_map:
            raise ValueError(f"Invalid operator: {operator_str}")
            
        self.operator_func = self.operator_map[operator_str]


# Flag Condition Implementations

class BearishCondition(FlagCondition):
    """Check if market sentiment is bearish."""
    
    def evaluate(self) -> bool:
        try:
            # Get recent recommendations to assess bearish sentiment
            # For now, check if recent recommendations are predominantly SELL
            recommendations = self.get_previous_recommendations(self.account.id, limit=5)
            if not recommendations:
                return False
                
            sell_count = sum(1 for rec in recommendations if rec.recommended_action == OrderRecommendation.SELL)
            return sell_count >= len(recommendations) * 0.6  # 60% or more SELL recommendations
            
        except Exception as e:
            logger.error(f"Error evaluating bearish condition: {e}", exc_info=True)
            return False


class BullishCondition(FlagCondition):
    """Check if market sentiment is bullish."""
    
    def evaluate(self) -> bool:
        try:
            # Get recent recommendations to assess bullish sentiment
            recommendations = self.get_previous_recommendations(self.account.id, limit=5)
            if not recommendations:
                return False
                
            buy_count = sum(1 for rec in recommendations if rec.recommended_action == OrderRecommendation.BUY)
            return buy_count >= len(recommendations) * 0.6  # 60% or more BUY recommendations
            
        except Exception as e:
            logger.error(f"Error evaluating bullish condition: {e}", exc_info=True)
            return False


class HasNoPositionCondition(FlagCondition):
    """Check if there's no open position for the instrument."""
    
    def evaluate(self) -> bool:
        return not self.has_position()


class HasPositionCondition(FlagCondition):
    """Check if there's an open position for the instrument."""
    
    def evaluate(self) -> bool:
        return self.has_position()


class RatingChangeCondition(FlagCondition):
    """Check if rating changed from one recommendation type to another."""
    
    def __init__(self, account: AccountInterface, instrument_name: str,
                 order_recommendation: OrderRecommendation, from_rating: OrderRecommendation,
                 to_rating: OrderRecommendation, existing_order: Optional[TradingOrder] = None):
        """
        Initialize rating change condition.
        
        Args:
            account: Account interface
            instrument_name: Instrument name
            order_recommendation: Current order recommendation
            from_rating: Expected previous rating
            to_rating: Expected current rating
            existing_order: Optional existing order
        """
        super().__init__(account, instrument_name, order_recommendation, existing_order)
        self.from_rating = from_rating
        self.to_rating = to_rating
    
    def evaluate(self) -> bool:
        try:
            recommendations = self.get_previous_recommendations(self.account.id, limit=2)
            if len(recommendations) < 2:
                return False
                
            previous = recommendations[1]  # Second most recent
            current = recommendations[0]   # Most recent
            
            return (previous.recommended_action == self.from_rating and 
                   current.recommended_action == self.to_rating)
                   
        except Exception as e:
            logger.error(f"Error evaluating rating change condition ({self.from_rating} -> {self.to_rating}): {e}", exc_info=True)
            return False


# Convenience classes for specific rating changes (optional - can be removed if not needed)
class RatingNegativeToNeutralCondition(RatingChangeCondition):
    """Check if rating changed from negative to neutral."""
    def __init__(self, account: AccountInterface, instrument_name: str,
                 order_recommendation: OrderRecommendation, existing_order: Optional[TradingOrder] = None):
        super().__init__(account, instrument_name, order_recommendation, 
                        OrderRecommendation.SELL, OrderRecommendation.HOLD, existing_order)


class RatingNegativeToPositiveCondition(RatingChangeCondition):
    """Check if rating changed from negative to positive."""
    def __init__(self, account: AccountInterface, instrument_name: str,
                 order_recommendation: OrderRecommendation, existing_order: Optional[TradingOrder] = None):
        super().__init__(account, instrument_name, order_recommendation, 
                        OrderRecommendation.SELL, OrderRecommendation.BUY, existing_order)


class RatingNeutralToNegativeCondition(RatingChangeCondition):
    """Check if rating changed from neutral to negative."""
    def __init__(self, account: AccountInterface, instrument_name: str,
                 order_recommendation: OrderRecommendation, existing_order: Optional[TradingOrder] = None):
        super().__init__(account, instrument_name, order_recommendation, 
                        OrderRecommendation.HOLD, OrderRecommendation.SELL, existing_order)


class RatingNeutralToPositiveCondition(RatingChangeCondition):
    """Check if rating changed from neutral to positive."""
    def __init__(self, account: AccountInterface, instrument_name: str,
                 order_recommendation: OrderRecommendation, existing_order: Optional[TradingOrder] = None):
        super().__init__(account, instrument_name, order_recommendation, 
                        OrderRecommendation.HOLD, OrderRecommendation.BUY, existing_order)


class RatingPositiveToNegativeCondition(RatingChangeCondition):
    """Check if rating changed from positive to negative."""
    def __init__(self, account: AccountInterface, instrument_name: str,
                 order_recommendation: OrderRecommendation, existing_order: Optional[TradingOrder] = None):
        super().__init__(account, instrument_name, order_recommendation, 
                        OrderRecommendation.BUY, OrderRecommendation.SELL, existing_order)


class RatingPositiveToNeutralCondition(RatingChangeCondition):
    """Check if rating changed from positive to neutral."""
    def __init__(self, account: AccountInterface, instrument_name: str,
                 order_recommendation: OrderRecommendation, existing_order: Optional[TradingOrder] = None):
        super().__init__(account, instrument_name, order_recommendation, 
                        OrderRecommendation.BUY, OrderRecommendation.HOLD, existing_order)


# Numeric Condition Implementations

class ExpectedProfitTargetPercentCondition(CompareCondition):
    """Compare expected profit target percentage."""
    
    def evaluate(self) -> bool:
        try:
            recommendations = self.get_previous_recommendations(self.account.id, limit=1)
            if not recommendations:
                return False
                
            expected_profit = recommendations[0].expected_profit_percent
            if expected_profit is None:
                return False
                
            return self.operator_func(expected_profit, self.value)
            
        except Exception as e:
            logger.error(f"Error evaluating expected profit target condition: {e}", exc_info=True)
            return False


class PercentToTargetCondition(CompareCondition):
    """Compare percent to target price."""
    
    def evaluate(self) -> bool:
        try:
            if not self.existing_order:
                return False
                
            current_price = self.get_current_price()
            if current_price is None or not hasattr(self.existing_order, 'limit_price') or self.existing_order.limit_price is None:
                return False
                
            # Calculate percent to target
            target_price = self.existing_order.limit_price
            percent_to_target = ((target_price - current_price) / current_price) * 100
            
            return self.operator_func(percent_to_target, self.value)
            
        except Exception as e:
            logger.error(f"Error evaluating percent to target condition: {e}", exc_info=True)
            return False


class ProfitLossAmountCondition(CompareCondition):
    """Compare profit/loss amount."""
    
    def evaluate(self) -> bool:
        try:
            if not self.existing_order:
                return False
                
            current_price = self.get_current_price()
            if current_price is None or not hasattr(self.existing_order, 'limit_price') or self.existing_order.limit_price is None:
                return False
                
            # Calculate P&L amount
            entry_price = self.existing_order.limit_price
            quantity = self.existing_order.quantity
            pl_amount = (current_price - entry_price) * quantity
            
            # Adjust for short positions
            if self.existing_order.side == "sell":
                pl_amount = -pl_amount
                
            return self.operator_func(pl_amount, self.value)
            
        except Exception as e:
            logger.error(f"Error evaluating profit loss amount condition: {e}", exc_info=True)
            return False


class ProfitLossPercentCondition(CompareCondition):
    """Compare profit/loss percentage."""
    
    def evaluate(self) -> bool:
        try:
            if not self.existing_order:
                return False
                
            current_price = self.get_current_price()
            if current_price is None or not hasattr(self.existing_order, 'limit_price') or self.existing_order.limit_price is None:
                return False
                
            # Calculate P&L percentage
            entry_price = self.existing_order.limit_price
            pl_percent = ((current_price - entry_price) / entry_price) * 100
            
            # Adjust for short positions
            if self.existing_order.side == "sell":
                pl_percent = -pl_percent
                
            return self.operator_func(pl_percent, self.value)
            
        except Exception as e:
            logger.error(f"Error evaluating profit loss percent condition: {e}", exc_info=True)
            return False


class TimeOpenedCondition(CompareCondition):
    """Compare time since order was opened (in hours)."""
    
    def evaluate(self) -> bool:
        try:
            if not self.existing_order or not self.existing_order.created_at:
                return False
                
            # Calculate hours since order was opened
            now = datetime.now(timezone.utc)
            created_at = self.existing_order.created_at
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)
                
            time_diff = now - created_at
            hours_opened = time_diff.total_seconds() / 3600
            
            return self.operator_func(hours_opened, self.value)
            
        except Exception as e:
            logger.error(f"Error evaluating time opened condition: {e}", exc_info=True)
            return False


# Factory function to create conditions based on event type
def create_condition(event_type: ExpertEventType, account: AccountInterface, 
                    instrument_name: str, order_recommendation: OrderRecommendation,
                    existing_order: Optional[TradingOrder] = None,
                    operator_str: Optional[str] = None, value: Optional[float] = None) -> TradeCondition:
    """
    Factory function to create appropriate condition based on event type.
    
    Args:
        event_type: Type of event/condition to create
        account: Account interface
        instrument_name: Instrument name
        order_recommendation: Order recommendation
        existing_order: Optional existing order
        operator_str: Operator for numeric conditions
        value: Value for numeric conditions
        
    Returns:
        Appropriate TradeCondition instance
    """
    # Define rating change mappings
    rating_changes = {
        ExpertEventType.F_RATING_NEGATIVE_TO_NEUTRAL: (OrderRecommendation.SELL, OrderRecommendation.HOLD),
        ExpertEventType.F_RATING_NEGATIVE_TO_POSITIVE: (OrderRecommendation.SELL, OrderRecommendation.BUY),
        ExpertEventType.F_RATING_NEUTRAL_TO_NEGATIVE: (OrderRecommendation.HOLD, OrderRecommendation.SELL),
        ExpertEventType.F_RATING_NEUTRAL_TO_POSITIVE: (OrderRecommendation.HOLD, OrderRecommendation.BUY),
        ExpertEventType.F_RATING_POSITIVE_TO_NEGATIVE: (OrderRecommendation.BUY, OrderRecommendation.SELL),
        ExpertEventType.F_RATING_POSITIVE_TO_NEUTRAL: (OrderRecommendation.BUY, OrderRecommendation.HOLD),
    }
    
    # Handle rating change conditions
    if event_type in rating_changes:
        from_rating, to_rating = rating_changes[event_type]
        return RatingChangeCondition(account, instrument_name, order_recommendation, 
                                   from_rating, to_rating, existing_order)
    
    condition_map = {
        ExpertEventType.F_BEARISH: BearishCondition,
        ExpertEventType.F_BULLISH: BullishCondition,
        ExpertEventType.F_HAS_NO_POSITION: HasNoPositionCondition,
        ExpertEventType.F_HAS_POSITION: HasPositionCondition,
        ExpertEventType.N_EXPECTED_PROFIT_TARGET_PERCENT: ExpectedProfitTargetPercentCondition,
        ExpertEventType.N_PERCENT_TO_TARGET: PercentToTargetCondition,
        ExpertEventType.N_PROFIT_LOSS_AMOUNT: ProfitLossAmountCondition,
        ExpertEventType.N_PROFIT_LOSS_PERCENT: ProfitLossPercentCondition,
        ExpertEventType.N_TIME_OPENED: TimeOpenedCondition,
    }
    
    condition_class = condition_map.get(event_type)
    if not condition_class:
        raise ValueError(f"Unknown event type: {event_type}")
    
    # Create flag conditions (no operator or value needed)
    if issubclass(condition_class, FlagCondition):
        return condition_class(account, instrument_name, order_recommendation, existing_order)
    
    # Create comparison conditions (require operator and value)
    elif issubclass(condition_class, CompareCondition):
        if operator_str is None or value is None:
            raise ValueError(f"Operator and value required for numeric condition: {event_type}")
        return condition_class(account, instrument_name, order_recommendation, operator_str, value, existing_order)
    
    else:
        raise ValueError(f"Unknown condition class type for: {event_type}")