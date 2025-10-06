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
from .types import OrderRecommendation, ExpertEventType, RiskLevel, TimeHorizon
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
                 expert_recommendation: ExpertRecommendation, existing_order: Optional[TradingOrder] = None):
        """
        Initialize the trade condition.
        
        Args:
            account: Account interface for accessing account data
            instrument_name: Name of the instrument being evaluated
            expert_recommendation: The expert recommendation being evaluated
            existing_order: Optional existing order related to this evaluation
        """
        self.account = account
        self.instrument_name = instrument_name
        self.expert_recommendation = expert_recommendation
        self.existing_order = existing_order
        
    @abstractmethod
    def evaluate(self) -> bool:
        """
        Evaluate the condition and return True/False.
        
        Returns:
            bool: True if condition is met, False otherwise
        """
        pass
    
    @abstractmethod
    def get_description(self) -> str:
        """
        Get a human-readable description of what this condition checks.
        
        Returns:
            str: Description of the condition
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
                 expert_recommendation: ExpertRecommendation, operator_str: str, value: float,
                 existing_order: Optional[TradingOrder] = None):
        """
        Initialize comparison condition.
        
        Args:
            account: Account interface
            instrument_name: Instrument name
            expert_recommendation: Expert recommendation
            operator_str: Comparison operator ('>', '<', '>=', '<=', '==', '!=')
            value: Value to compare against
            existing_order: Optional existing order
        """
        super().__init__(account, instrument_name, expert_recommendation, existing_order)
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
            # Check if current recommendation is bearish (SELL)
            return self.expert_recommendation.recommended_action == OrderRecommendation.SELL
            
        except Exception as e:
            logger.error(f"Error evaluating bearish condition: {e}", exc_info=True)
            return False
    
    def get_description(self) -> str:
        """Get description of bearish condition."""
        return f"Check if current recommendation is bearish (SELL) for {self.instrument_name}"


class BullishCondition(FlagCondition):
    """Check if market sentiment is bullish."""
    
    def evaluate(self) -> bool:
        try:
            # Check if current recommendation is bullish (BUY)
            return self.expert_recommendation.recommended_action == OrderRecommendation.BUY
            
        except Exception as e:
            logger.error(f"Error evaluating bullish condition: {e}", exc_info=True)
            return False
    
    def get_description(self) -> str:
        """Get description of bullish condition."""
        return f"Check if current recommendation is bullish (BUY) for {self.instrument_name}"


class HasNoPositionCondition(FlagCondition):
    """Check if there's no open position for the instrument."""
    
    def evaluate(self) -> bool:
        return not self.has_position()
    
    def get_description(self) -> str:
        """Get description of no position condition."""
        return f"Check if there is no open position for {self.instrument_name}"



class HasPositionCondition(FlagCondition):
    """Check if there's an open position for the instrument."""
    def evaluate(self) -> bool:
        return self.has_position()
    def get_description(self) -> str:
        return f"Check if there is an open position for {self.instrument_name}"

# Time Horizon Flag Conditions
class LongTermCondition(FlagCondition):
    """Check if expert recommendation is long term."""
    def evaluate(self) -> bool:
        try:
            return self.expert_recommendation.time_horizon == TimeHorizon.LONG_TERM
        except Exception as e:
            logger.error(f"Error evaluating long term condition: {e}", exc_info=True)
            return False
    def get_description(self) -> str:
        return f"Check if expert recommendation for {self.instrument_name} is LONG_TERM"

class MediumTermCondition(FlagCondition):
    """Check if expert recommendation is medium term."""
    def evaluate(self) -> bool:
        try:
            return self.expert_recommendation.time_horizon == TimeHorizon.MEDIUM_TERM
        except Exception as e:
            logger.error(f"Error evaluating medium term condition: {e}", exc_info=True)
            return False
    def get_description(self) -> str:
        return f"Check if expert recommendation for {self.instrument_name} is MEDIUM_TERM"

class ShortTermCondition(FlagCondition):
    """Check if expert recommendation is short term."""
    def evaluate(self) -> bool:
        try:
            return self.expert_recommendation.time_horizon == TimeHorizon.SHORT_TERM
        except Exception as e:
            logger.error(f"Error evaluating short term condition: {e}", exc_info=True)
            return False
    def get_description(self) -> str:
        return f"Check if expert recommendation for {self.instrument_name} is SHORT_TERM"


# Current Rating Flag Conditions
class CurrentRatingPositiveCondition(FlagCondition):
    """Check if current recommendation is positive (BUY)."""
    def evaluate(self) -> bool:
        try:
            return self.expert_recommendation.recommended_action == OrderRecommendation.BUY
        except Exception as e:
            logger.error(f"Error evaluating current rating positive condition: {e}", exc_info=True)
            return False
    def get_description(self) -> str:
        return f"Check if current recommendation for {self.instrument_name} is BUY (positive)"


class CurrentRatingNeutralCondition(FlagCondition):
    """Check if current recommendation is neutral (HOLD)."""
    def evaluate(self) -> bool:
        try:
            return self.expert_recommendation.recommended_action == OrderRecommendation.HOLD
        except Exception as e:
            logger.error(f"Error evaluating current rating neutral condition: {e}", exc_info=True)
            return False
    def get_description(self) -> str:
        return f"Check if current recommendation for {self.instrument_name} is HOLD (neutral)"


class CurrentRatingNegativeCondition(FlagCondition):
    """Check if current recommendation is negative (SELL)."""
    def evaluate(self) -> bool:
        try:
            return self.expert_recommendation.recommended_action == OrderRecommendation.SELL
        except Exception as e:
            logger.error(f"Error evaluating current rating negative condition: {e}", exc_info=True)
            return False
    def get_description(self) -> str:
        return f"Check if current recommendation for {self.instrument_name} is SELL (negative)"


# Risk Level Flag Conditions
class HighRiskCondition(FlagCondition):
    """Check if expert recommendation has high risk."""
    def evaluate(self) -> bool:
        try:
            return getattr(self.expert_recommendation, 'risk_level', None) == RiskLevel.HIGH
        except Exception as e:
            logger.error(f"Error evaluating high risk condition: {e}", exc_info=True)
            return False
    def get_description(self) -> str:
        return f"Check if expert recommendation for {self.instrument_name} has HIGH risk"


class MediumRiskCondition(FlagCondition):
    """Check if expert recommendation has medium risk."""
    def evaluate(self) -> bool:
        try:
            return getattr(self.expert_recommendation, 'risk_level', None) == RiskLevel.MEDIUM
        except Exception as e:
            logger.error(f"Error evaluating medium risk condition: {e}", exc_info=True)
            return False
    def get_description(self) -> str:
        return f"Check if expert recommendation for {self.instrument_name} has MEDIUM risk"


class LowRiskCondition(FlagCondition):
    """Check if expert recommendation has low risk."""
    def evaluate(self) -> bool:
        try:
            return getattr(self.expert_recommendation, 'risk_level', None) == RiskLevel.LOW
        except Exception as e:
            logger.error(f"Error evaluating low risk condition: {e}", exc_info=True)
            return False
    def get_description(self) -> str:
        return f"Check if expert recommendation for {self.instrument_name} has LOW risk"


class NewTargetHigherCondition(FlagCondition):
    """Check if new expert target is higher than current TP (with 2% tolerance)."""
    
    TOLERANCE_PERCENT = 2.0  # 2% tolerance
    
    def evaluate(self) -> bool:
        try:
            if not self.existing_order:
                logger.debug(f"No existing order for new target higher evaluation")
                return False
            
            # Get current TP price from transaction
            current_tp_price = None
            if self.existing_order.transaction_id:
                from .db import get_instance
                from .models import Transaction
                transaction = get_instance(Transaction, self.existing_order.transaction_id)
                if transaction and transaction.take_profit:
                    current_tp_price = transaction.take_profit
            
            if current_tp_price is None:
                logger.debug(f"No current TP price available for order {self.existing_order.id}")
                return False
            
            # Calculate new expert target price
            if not self.expert_recommendation:
                logger.debug(f"No expert recommendation for new target evaluation")
                return False
            
            if not hasattr(self.expert_recommendation, 'price_at_date') or not hasattr(self.expert_recommendation, 'expected_profit_percent'):
                logger.error(f"Expert recommendation missing price_at_date or expected_profit_percent")
                return False
            
            base_price = self.expert_recommendation.price_at_date
            expected_profit = self.expert_recommendation.expected_profit_percent
            
            # Calculate new target based on recommendation direction
            from .types import OrderRecommendation
            if self.expert_recommendation.recommended_action == OrderRecommendation.BUY:
                new_target_price = base_price * (1 + expected_profit / 100)
            elif self.expert_recommendation.recommended_action == OrderRecommendation.SELL:
                new_target_price = base_price * (1 - expected_profit / 100)
            else:
                logger.debug(f"Recommendation action is HOLD, cannot calculate target")
                return False
            
            # Calculate percent difference (new_target vs current_tp)
            percent_diff = ((new_target_price - current_tp_price) / current_tp_price) * 100
            
            # Check if new target is higher by more than tolerance
            is_higher = percent_diff > self.TOLERANCE_PERCENT
            
            logger.info(f"New target comparison for {self.instrument_name}: current_TP=${current_tp_price:.2f}, new_target=${new_target_price:.2f}, diff={percent_diff:+.2f}%, is_higher={is_higher} (tolerance={self.TOLERANCE_PERCENT}%)")
            
            return is_higher
            
        except Exception as e:
            logger.error(f"Error evaluating new target higher condition: {e}", exc_info=True)
            return False
    
    def get_description(self) -> str:
        """Get description of new target higher condition."""
        return f"Check if new expert target is higher than current TP for {self.instrument_name} (>{self.TOLERANCE_PERCENT}% tolerance)"


class NewTargetLowerCondition(FlagCondition):
    """Check if new expert target is lower than current TP (with 2% tolerance)."""
    
    TOLERANCE_PERCENT = 2.0  # 2% tolerance
    
    def evaluate(self) -> bool:
        try:
            if not self.existing_order:
                logger.debug(f"No existing order for new target lower evaluation")
                return False
            
            # Get current TP price from transaction
            current_tp_price = None
            if self.existing_order.transaction_id:
                from .db import get_instance
                from .models import Transaction
                transaction = get_instance(Transaction, self.existing_order.transaction_id)
                if transaction and transaction.take_profit:
                    current_tp_price = transaction.take_profit
            
            if current_tp_price is None:
                logger.debug(f"No current TP price available for order {self.existing_order.id}")
                return False
            
            # Calculate new expert target price
            if not self.expert_recommendation:
                logger.debug(f"No expert recommendation for new target evaluation")
                return False
            
            if not hasattr(self.expert_recommendation, 'price_at_date') or not hasattr(self.expert_recommendation, 'expected_profit_percent'):
                logger.error(f"Expert recommendation missing price_at_date or expected_profit_percent")
                return False
            
            base_price = self.expert_recommendation.price_at_date
            expected_profit = self.expert_recommendation.expected_profit_percent
            
            # Calculate new target based on recommendation direction
            from .types import OrderRecommendation
            if self.expert_recommendation.recommended_action == OrderRecommendation.BUY:
                new_target_price = base_price * (1 + expected_profit / 100)
            elif self.expert_recommendation.recommended_action == OrderRecommendation.SELL:
                new_target_price = base_price * (1 - expected_profit / 100)
            else:
                logger.debug(f"Recommendation action is HOLD, cannot calculate target")
                return False
            
            # Calculate percent difference (new_target vs current_tp)
            percent_diff = ((new_target_price - current_tp_price) / current_tp_price) * 100
            
            # Check if new target is lower by more than tolerance
            is_lower = percent_diff < -self.TOLERANCE_PERCENT
            
            logger.info(f"New target comparison for {self.instrument_name}: current_TP=${current_tp_price:.2f}, new_target=${new_target_price:.2f}, diff={percent_diff:+.2f}%, is_lower={is_lower} (tolerance={self.TOLERANCE_PERCENT}%)")
            
            return is_lower
            
        except Exception as e:
            logger.error(f"Error evaluating new target lower condition: {e}", exc_info=True)
            return False
    
    def get_description(self) -> str:
        """Get description of new target lower condition."""
        return f"Check if new expert target is lower than current TP for {self.instrument_name} (<-{self.TOLERANCE_PERCENT}% tolerance)"


class RatingChangeCondition(FlagCondition):
    """Check if rating changed from one recommendation type to another."""
    
    def __init__(self, account: AccountInterface, instrument_name: str,
                 expert_recommendation: ExpertRecommendation, from_rating: OrderRecommendation,
                 to_rating: OrderRecommendation, existing_order: Optional[TradingOrder] = None):
        """
        Initialize rating change condition.
        
        Args:
            account: Account interface
            instrument_name: Instrument name
            expert_recommendation: Current expert recommendation
            from_rating: Expected previous rating
            to_rating: Expected current rating
            existing_order: Optional existing order
        """
        super().__init__(account, instrument_name, expert_recommendation, existing_order)
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
    
    def get_description(self) -> str:
        """Get description of rating change condition."""
        return f"Check if rating changed from {self.from_rating.value} to {self.to_rating.value} for {self.instrument_name}"


# Convenience classes for specific rating changes (optional - can be removed if not needed)
class RatingNegativeToNeutralCondition(RatingChangeCondition):
    """Check if rating changed from negative to neutral."""
    def __init__(self, account: AccountInterface, instrument_name: str,
                 expert_recommendation: ExpertRecommendation, existing_order: Optional[TradingOrder] = None):
        super().__init__(account, instrument_name, expert_recommendation, 
                        OrderRecommendation.SELL, OrderRecommendation.HOLD, existing_order)


class RatingNegativeToPositiveCondition(RatingChangeCondition):
    """Check if rating changed from negative to positive."""
    def __init__(self, account: AccountInterface, instrument_name: str,
                 expert_recommendation: ExpertRecommendation, existing_order: Optional[TradingOrder] = None):
        super().__init__(account, instrument_name, expert_recommendation, 
                        OrderRecommendation.SELL, OrderRecommendation.BUY, existing_order)


class RatingNeutralToNegativeCondition(RatingChangeCondition):
    """Check if rating changed from neutral to negative."""
    def __init__(self, account: AccountInterface, instrument_name: str,
                 expert_recommendation: ExpertRecommendation, existing_order: Optional[TradingOrder] = None):
        super().__init__(account, instrument_name, expert_recommendation, 
                        OrderRecommendation.HOLD, OrderRecommendation.SELL, existing_order)


class RatingNeutralToPositiveCondition(RatingChangeCondition):
    """Check if rating changed from neutral to positive."""
    def __init__(self, account: AccountInterface, instrument_name: str,
                 expert_recommendation: ExpertRecommendation, existing_order: Optional[TradingOrder] = None):
        super().__init__(account, instrument_name, expert_recommendation, 
                        OrderRecommendation.HOLD, OrderRecommendation.BUY, existing_order)


class RatingPositiveToNegativeCondition(RatingChangeCondition):
    """Check if rating changed from positive to negative."""
    def __init__(self, account: AccountInterface, instrument_name: str,
                 expert_recommendation: ExpertRecommendation, existing_order: Optional[TradingOrder] = None):
        super().__init__(account, instrument_name, expert_recommendation, 
                        OrderRecommendation.BUY, OrderRecommendation.SELL, existing_order)


class RatingPositiveToNeutralCondition(RatingChangeCondition):
    """Check if rating changed from positive to neutral."""
    def __init__(self, account: AccountInterface, instrument_name: str,
                 expert_recommendation: ExpertRecommendation, existing_order: Optional[TradingOrder] = None):
        super().__init__(account, instrument_name, expert_recommendation, 
                        OrderRecommendation.BUY, OrderRecommendation.HOLD, existing_order)


# Numeric Condition Implementations

class ExpectedProfitTargetPercentCondition(CompareCondition):
    """Compare expected profit target percentage."""
    
    def evaluate(self) -> bool:
        try:
            expected_profit = self.expert_recommendation.expected_profit_percent
            
            # If no expected profit data, we cannot evaluate
            if expected_profit is None:
                logger.debug(f"No expected profit data available for {self.instrument_name}")
                return False
                
            return self.operator_func(expected_profit, self.value)
            
        except Exception as e:
            logger.error(f"Error evaluating expected profit target condition: {e}", exc_info=True)
            return False
    
    def get_description(self) -> str:
        """Get description of expected profit target condition."""
        return f"Check if expected profit target percent for {self.instrument_name} is {self.operator_str} {self.value}%"


class PercentToCurrentTargetCondition(CompareCondition):
    """Compare percent from current price to current TP target price."""
    
    def evaluate(self) -> bool:
        try:
            if not self.existing_order:
                logger.debug(f"No existing order for percent to current target evaluation")
                return False
            
            # Get current market price
            current_price = self.get_current_price()
            if current_price is None:
                logger.error(f"Cannot get current price for {self.instrument_name}")
                return False
            
            # Get current TP price from transaction or order
            current_tp_price = None
            
            # First try to get from transaction's take_profit field
            if self.existing_order.transaction_id:
                from .db import get_instance
                from .models import Transaction
                transaction = get_instance(Transaction, self.existing_order.transaction_id)
                if transaction and transaction.take_profit:
                    current_tp_price = transaction.take_profit
                    logger.debug(f"Current TP from transaction: ${current_tp_price:.2f}")
            
            # If no TP in transaction, we can't evaluate
            if current_tp_price is None:
                logger.debug(f"No current TP price available for order {self.existing_order.id}")
                return False
            
            # Calculate percent to current target
            percent_to_current_target = ((current_tp_price - current_price) / current_price) * 100
            
            logger.info(f"Percent to CURRENT target for {self.instrument_name}: current=${current_price:.2f}, TP=${current_tp_price:.2f}, distance={percent_to_current_target:+.2f}%")
            
            return self.operator_func(percent_to_current_target, self.value)
            
        except Exception as e:
            logger.error(f"Error evaluating percent to current target condition: {e}", exc_info=True)
            return False
    
    def get_description(self) -> str:
        """Get description of percent to current target condition."""
        return f"Check if percent from current price to current TP for {self.instrument_name} is {self.operator_str} {self.value}%"


class PercentToNewTargetCondition(CompareCondition):
    """Compare percent from current price to new expert target price."""
    
    def evaluate(self) -> bool:
        try:
            if not self.existing_order:
                logger.debug(f"No existing order for percent to new target evaluation")
                return False
            
            # Get current market price
            current_price = self.get_current_price()
            if current_price is None:
                logger.error(f"Cannot get current price for {self.instrument_name}")
                return False
            
            # Calculate new expert target price from current recommendation
            if not self.expert_recommendation:
                logger.debug(f"No expert recommendation for new target evaluation")
                return False
            
            if not hasattr(self.expert_recommendation, 'price_at_date') or not hasattr(self.expert_recommendation, 'expected_profit_percent'):
                logger.error(f"Expert recommendation missing price_at_date or expected_profit_percent")
                return False
            
            base_price = self.expert_recommendation.price_at_date
            expected_profit = self.expert_recommendation.expected_profit_percent
            
            # Calculate new target based on recommendation direction
            from .types import OrderRecommendation
            if self.expert_recommendation.recommended_action == OrderRecommendation.BUY:
                new_target_price = base_price * (1 + expected_profit / 100)
            elif self.expert_recommendation.recommended_action == OrderRecommendation.SELL:
                new_target_price = base_price * (1 - expected_profit / 100)
            else:
                logger.debug(f"Recommendation action is HOLD, cannot calculate target")
                return False
            
            # Calculate percent to new target
            percent_to_new_target = ((new_target_price - current_price) / current_price) * 100
            
            logger.info(f"Percent to NEW target for {self.instrument_name}: current=${current_price:.2f}, new_target=${new_target_price:.2f} (base=${base_price:.2f}, profit={expected_profit:.1f}%), distance={percent_to_new_target:+.2f}%")
            
            return self.operator_func(percent_to_new_target, self.value)
            
        except Exception as e:
            logger.error(f"Error evaluating percent to new target condition: {e}", exc_info=True)
            return False
    
    def get_description(self) -> str:
        """Get description of percent to new target condition."""
        return f"Check if percent from current price to new expert target for {self.instrument_name} is {self.operator_str} {self.value}%"


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
    
    def get_description(self) -> str:
        """Get description of profit/loss amount condition."""
        return f"Check if profit/loss amount for {self.instrument_name} is {self.operator_str} ${self.value}"



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
    def get_description(self) -> str:
        return f"Check if profit/loss percentage for {self.instrument_name} is {self.operator_str} {self.value}%"

# Confidence Condition Implementation
class ConfidenceCondition(CompareCondition):
    """Compare expert confidence value."""
    def evaluate(self) -> bool:
        try:
            confidence = getattr(self.expert_recommendation, 'confidence', None)
            if confidence is None:
                logger.debug(f"No confidence value available for {self.instrument_name}")
                return False
            return self.operator_func(confidence, self.value)
        except Exception as e:
            logger.error(f"Error evaluating confidence condition: {e}", exc_info=True)
            return False
    def get_description(self) -> str:
        return f"Check if expert confidence for {self.instrument_name} is {self.operator_str} {self.value}"


class DaysOpenedCondition(CompareCondition):
    """Compare time since order was opened (in days)."""
    
    def evaluate(self) -> bool:
        try:
            if not self.existing_order or not self.existing_order.created_at:
                return False
                
            # Calculate days since order was opened
            now = datetime.now(timezone.utc)
            created_at = self.existing_order.created_at
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)
                
            time_diff = now - created_at
            days_opened = time_diff.total_seconds() / 86400  # 86400 seconds in a day
            
            return self.operator_func(days_opened, self.value)
            
        except Exception as e:
            logger.error(f"Error evaluating days opened condition: {e}", exc_info=True)
            return False
    
    def get_description(self) -> str:
        """Get description of days opened condition."""
        return f"Check if days since {self.instrument_name} order was opened is {self.operator_str} {self.value} days"


# Factory function to create conditions based on event type


def create_condition(event_type: ExpertEventType, account: AccountInterface, 
                    instrument_name: str, expert_recommendation: ExpertRecommendation,
                    existing_order: Optional[TradingOrder] = None,
                    operator_str: Optional[str] = None, value: Optional[float] = None) -> TradeCondition:
    """
    Factory function to create appropriate condition based on event type.
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
    if event_type in rating_changes:
        from_rating, to_rating = rating_changes[event_type]
        return RatingChangeCondition(account, instrument_name, expert_recommendation, 
                                   from_rating, to_rating, existing_order)
    # Add time horizon flags and N_CONFIDENCE to condition_map
    condition_map = {
        ExpertEventType.F_BEARISH: BearishCondition,
        ExpertEventType.F_BULLISH: BullishCondition,
        ExpertEventType.F_HAS_NO_POSITION: HasNoPositionCondition,
        ExpertEventType.F_HAS_POSITION: HasPositionCondition,
        ExpertEventType.F_LONG_TERM: LongTermCondition,
        ExpertEventType.F_MEDIUM_TERM: MediumTermCondition,
        ExpertEventType.F_SHORT_TERM: ShortTermCondition,
        ExpertEventType.F_CURRENT_RATING_POSITIVE: CurrentRatingPositiveCondition,
        ExpertEventType.F_CURRENT_RATING_NEUTRAL: CurrentRatingNeutralCondition,
        ExpertEventType.F_CURRENT_RATING_NEGATIVE: CurrentRatingNegativeCondition,
        ExpertEventType.F_HIGHRISK: HighRiskCondition,
        ExpertEventType.F_MEDIUMRISK: MediumRiskCondition,
        ExpertEventType.F_LOWRISK: LowRiskCondition,
        ExpertEventType.F_NEW_TARGET_HIGHER: NewTargetHigherCondition,
        ExpertEventType.F_NEW_TARGET_LOWER: NewTargetLowerCondition,
        ExpertEventType.N_EXPECTED_PROFIT_TARGET_PERCENT: ExpectedProfitTargetPercentCondition,
        ExpertEventType.N_PERCENT_TO_CURRENT_TARGET: PercentToCurrentTargetCondition,
        ExpertEventType.N_PERCENT_TO_NEW_TARGET: PercentToNewTargetCondition,
        ExpertEventType.N_PROFIT_LOSS_AMOUNT: ProfitLossAmountCondition,
        ExpertEventType.N_PROFIT_LOSS_PERCENT: ProfitLossPercentCondition,
        ExpertEventType.N_DAYS_OPENED: DaysOpenedCondition,
        ExpertEventType.N_CONFIDENCE: ConfidenceCondition,
    }
    condition_class = condition_map.get(event_type)
    if not condition_class:
        raise ValueError(f"Unknown event type: {event_type}")
    if issubclass(condition_class, FlagCondition):
        return condition_class(account, instrument_name, expert_recommendation, existing_order)
    elif issubclass(condition_class, CompareCondition):
        if operator_str is None or value is None:
            raise ValueError(f"Operator and value required for numeric condition: {event_type}")
        return condition_class(account, instrument_name, expert_recommendation, operator_str, value, existing_order)
    else:
        raise ValueError(f"Unknown condition class type for: {event_type}")