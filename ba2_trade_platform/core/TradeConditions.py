"""
TradeConditions - Core component for evaluating trading conditions

This module provides base classes and implementations for evaluating various trading conditions
that can be used in rulesets and automated trading decisions.
"""

from abc import ABC, abstractmethod
from typing import List, Optional, Any, Dict
from datetime import datetime, timezone, timedelta
import operator

from .interfaces import AccountInterface
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
    - Current trade recommendation
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
    
    def has_expert_position(self) -> bool:
        """
        Check if this expert has an open position for this instrument by checking transactions.
        
        Returns:
            True if expert has open transactions for this instrument, False otherwise
        """
        try:
            from .db import get_db
            from .models import Transaction
            from .types import TransactionStatus
            from sqlmodel import select
            
            expert_id = self.expert_recommendation.instance_id
            
            with get_db() as session:
                # Check for open transactions for this expert and instrument
                statement = select(Transaction).where(
                    Transaction.expert_id == expert_id,
                    Transaction.symbol == self.instrument_name,
                    Transaction.status == TransactionStatus.OPENED
                )
                open_transactions = session.exec(statement).all()
                
                return len(open_transactions) > 0
                
        except Exception as e:
            logger.error(f"Error checking expert position for {self.instrument_name}: {e}", exc_info=True)
            return False
    
    def has_account_position(self) -> bool:
        """
        Check if there's an open position for this instrument at the account level.
        This is the original account-level position check behavior.
        
        Returns:
            True if account has position exists, False otherwise
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
        self.calculated_value = None  # Store the actual calculated value
        
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
    
    def get_calculated_value(self) -> Optional[float]:
        """
        Get the last calculated value from condition evaluation.
        
        Returns:
            The calculated value or None if not yet evaluated
        """
        return self.calculated_value


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
    """Check if this expert has no open position for the instrument (expert-level check based on transactions)."""
    
    def evaluate(self) -> bool:
        return not self.has_expert_position()
    
    def get_description(self) -> str:
        """Get description of no position condition."""
        return f"Check if this expert has no open position for {self.instrument_name} (based on transactions)"



class HasPositionCondition(FlagCondition):
    """Check if this expert has an open position for the instrument (expert-level check based on transactions)."""
    def evaluate(self) -> bool:
        return self.has_expert_position()
    def get_description(self) -> str:
        return f"Check if this expert has an open position for {self.instrument_name} (based on transactions)"


class HasBuyPositionCondition(FlagCondition):
    """Check if this expert has an open BUY (long) position for the instrument."""
    def evaluate(self) -> bool:
        try:
            from .db import get_db
            from .models import Transaction
            from .types import TransactionStatus, OrderDirection
            from sqlmodel import select

            expert_id = self.expert_recommendation.instance_id

            with get_db() as session:
                statement = select(Transaction).where(
                    Transaction.expert_id == expert_id,
                    Transaction.symbol == self.instrument_name,
                    Transaction.status == TransactionStatus.OPENED,
                    Transaction.side == OrderDirection.BUY
                )
                return len(session.exec(statement).all()) > 0
        except Exception as e:
            logger.error(f"Error checking BUY position for {self.instrument_name}: {e}", exc_info=True)
            return False
    def get_description(self) -> str:
        return f"Check if this expert has an open BUY position for {self.instrument_name}"


class HasSellPositionCondition(FlagCondition):
    """Check if this expert has an open SELL (short) position for the instrument."""
    def evaluate(self) -> bool:
        try:
            from .db import get_db
            from .models import Transaction
            from .types import TransactionStatus, OrderDirection
            from sqlmodel import select

            expert_id = self.expert_recommendation.instance_id

            with get_db() as session:
                statement = select(Transaction).where(
                    Transaction.expert_id == expert_id,
                    Transaction.symbol == self.instrument_name,
                    Transaction.status == TransactionStatus.OPENED,
                    Transaction.side == OrderDirection.SELL
                )
                return len(session.exec(statement).all()) > 0
        except Exception as e:
            logger.error(f"Error checking SELL position for {self.instrument_name}: {e}", exc_info=True)
            return False
    def get_description(self) -> str:
        return f"Check if this expert has an open SELL position for {self.instrument_name}"


# Account-level Position Conditions
class HasNoPositionAccountCondition(FlagCondition):
    """Check if there's no open position for the instrument at the account level."""
    
    def evaluate(self) -> bool:
        return not self.has_account_position()
    
    def get_description(self) -> str:
        """Get description of account-level no position condition."""
        return f"Check if account has no open position for {self.instrument_name} (account-level)"

class HasPositionAccountCondition(FlagCondition):
    """Check if there's an open position for the instrument at the account level."""
    def evaluate(self) -> bool:
        return self.has_account_position()
    def get_description(self) -> str:
        return f"Check if account has an open position for {self.instrument_name} (account-level)"

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
            # Initialize tracking variables
            self.current_tp_price = None
            self.new_target_price = None
            self.percent_diff = None
            
            if not self.existing_order:
                logger.debug(f"No existing order for new target higher evaluation")
                return False
            
            # Get current TP price from transaction
            # First check metadata for current_target_price (set by adjust_tp TradeAction)
            # Fallback to transaction.take_profit if metadata not available
            current_tp_price = None
            if self.existing_order.transaction_id:
                from .db import get_instance
                from .models import Transaction
                transaction = get_instance(Transaction, self.existing_order.transaction_id)
                if transaction:
                    # Try to get current_target_price from metadata first
                    if transaction.meta_data and "TradeConditionsData" in transaction.meta_data:
                        current_tp_price = transaction.meta_data["TradeConditionsData"].get("current_target_price")
                        if current_tp_price is not None:
                            logger.debug(f"Using current_target_price from transaction metadata: ${current_tp_price:.2f}")
                    
                    # Fallback to take_profit field if metadata not available
                    if current_tp_price is None and transaction.take_profit:
                        current_tp_price = transaction.take_profit
                        logger.debug(f"Using take_profit from transaction field (metadata not available): ${current_tp_price:.2f}")
            
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
            
            # Store values for external access
            self.current_tp_price = current_tp_price
            self.new_target_price = new_target_price
            self.percent_diff = percent_diff
            
            # Check if new target is higher by more than tolerance
            is_higher = percent_diff > self.TOLERANCE_PERCENT
            
            logger.info(f"New target comparison for {self.instrument_name}: current_TP=${current_tp_price:.2f}, new_target=${new_target_price:.2f}, diff={percent_diff:+.2f}%, is_higher={is_higher} (tolerance={self.TOLERANCE_PERCENT}%)")
            
            return is_higher
            
        except Exception as e:
            logger.error(f"Error evaluating new target higher condition: {e}", exc_info=True)
            # Clear tracking variables on error
            self.current_tp_price = None
            self.new_target_price = None
            self.percent_diff = None
            return False
    
    def get_description(self) -> str:
        """Get description of new target higher condition."""
        return f"Check if new expert target is higher than current TP for {self.instrument_name} (>{self.TOLERANCE_PERCENT}% tolerance)"


class NewTargetLowerCondition(FlagCondition):
    """Check if new expert target is lower than current TP (with 2% tolerance)."""
    
    TOLERANCE_PERCENT = 2.0  # 2% tolerance
    
    def evaluate(self) -> bool:
        try:
            # Initialize tracking variables
            self.current_tp_price = None
            self.new_target_price = None
            self.percent_diff = None
            
            if not self.existing_order:
                logger.debug(f"No existing order for new target lower evaluation")
                return False
            
            # Get current TP price from transaction
            # First check metadata for current_target_price (set by adjust_tp TradeAction)
            # Fallback to transaction.take_profit if metadata not available
            current_tp_price = None
            if self.existing_order.transaction_id:
                from .db import get_instance
                from .models import Transaction
                transaction = get_instance(Transaction, self.existing_order.transaction_id)
                if transaction:
                    # Try to get current_target_price from metadata first
                    if transaction.meta_data and "TradeConditionsData" in transaction.meta_data:
                        current_tp_price = transaction.meta_data["TradeConditionsData"].get("current_target_price")
                        if current_tp_price is not None:
                            logger.debug(f"Using current_target_price from transaction metadata: ${current_tp_price:.2f}")
                    
                    # Fallback to take_profit field if metadata not available
                    if current_tp_price is None and transaction.take_profit:
                        current_tp_price = transaction.take_profit
                        logger.debug(f"Using take_profit from transaction field (metadata not available): ${current_tp_price:.2f}")
            
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
            
            # Store values for external access
            self.current_tp_price = current_tp_price
            self.new_target_price = new_target_price
            self.percent_diff = percent_diff
            
            # Check if new target is lower by more than tolerance
            is_lower = percent_diff < -self.TOLERANCE_PERCENT
            
            logger.info(f"New target comparison for {self.instrument_name}: current_TP=${current_tp_price:.2f}, new_target=${new_target_price:.2f}, diff={percent_diff:+.2f}%, is_lower={is_lower} (tolerance={self.TOLERANCE_PERCENT}%)")
            
            return is_lower
            
        except Exception as e:
            logger.error(f"Error evaluating new target lower condition: {e}", exc_info=True)
            # Clear tracking variables on error
            self.current_tp_price = None
            self.new_target_price = None
            self.percent_diff = None
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
                self.calculated_value = None
                return False
            
            self.calculated_value = expected_profit  # Store calculated value
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
                self.calculated_value = None
                return False
            
            # Get current market price
            current_price = self.get_current_price()
            if current_price is None:
                logger.error(f"Cannot get current price for {self.instrument_name}")
                self.calculated_value = None
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
                self.calculated_value = None
                return False
            
            # Calculate percent to current target
            percent_to_current_target = ((current_tp_price - current_price) / current_price) * 100
            
            self.calculated_value = percent_to_current_target  # Store calculated value
            
            logger.info(f"Percent to CURRENT target for {self.instrument_name}: current=${current_price:.2f}, TP=${current_tp_price:.2f}, distance={percent_to_current_target:+.2f}%")
            
            return self.operator_func(percent_to_current_target, self.value)
            
        except Exception as e:
            logger.error(f"Error evaluating percent to current target condition: {e}", exc_info=True)
            self.calculated_value = None
            return False
    
    def get_description(self) -> str:
        """Get description of percent to current target condition."""
        return f"Check if percent from current price to current TP for {self.instrument_name} is {self.operator_str} {self.value}%"


class NewTargetPercentCondition(CompareCondition):
    """Compare percent change from current TP to new expert target (positive if higher, negative if lower)."""
    
    def evaluate(self) -> bool:
        try:
            if not self.existing_order:
                logger.debug(f"No existing order for new target percent evaluation")
                self.calculated_value = None
                return False
            
            # Get current TP price from transaction
            # First check metadata for current_target_price (set by adjust_tp TradeAction)
            # Fallback to transaction.take_profit if metadata not available
            current_tp_price = None
            if self.existing_order.transaction_id:
                from .db import get_instance
                from .models import Transaction
                transaction = get_instance(Transaction, self.existing_order.transaction_id)
                if transaction:
                    # Try to get current_target_price from metadata first
                    if transaction.meta_data and "TradeConditionsData" in transaction.meta_data:
                        current_tp_price = transaction.meta_data["TradeConditionsData"].get("current_target_price")
                        if current_tp_price is not None:
                            logger.debug(f"Using current_target_price from transaction metadata: ${current_tp_price:.2f}")
                    
                    # Fallback to take_profit field if metadata not available
                    if current_tp_price is None and transaction.take_profit:
                        current_tp_price = transaction.take_profit
                        logger.debug(f"Using take_profit from transaction field (metadata not available): ${current_tp_price:.2f}")
            
            if current_tp_price is None:
                logger.debug(f"No current TP price available for order {self.existing_order.id}")
                self.calculated_value = None
                return False
            
            # Calculate new expert target price
            if not self.expert_recommendation:
                logger.debug(f"No expert recommendation for new target evaluation")
                self.calculated_value = None
                return False
            
            if not hasattr(self.expert_recommendation, 'price_at_date') or not hasattr(self.expert_recommendation, 'expected_profit_percent'):
                logger.error(f"Expert recommendation missing price_at_date or expected_profit_percent")
                self.calculated_value = None
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
                self.calculated_value = None
                return False
            
            # Calculate percent difference: positive if new target higher, negative if lower
            new_target_percent = ((new_target_price - current_tp_price) / current_tp_price) * 100
            
            self.calculated_value = new_target_percent  # Store calculated value
            
            logger.info(f"New target percent for {self.instrument_name}: current_TP=${current_tp_price:.2f}, new_target=${new_target_price:.2f}, change={new_target_percent:+.2f}%")
            
            return self.operator_func(new_target_percent, self.value)
            
        except Exception as e:
            logger.error(f"Error evaluating new target percent condition: {e}", exc_info=True)
            self.calculated_value = None
            return False
    
    def get_description(self) -> str:
        """Get description of new target percent condition."""
        return f"Check if new target percent change for {self.instrument_name} is {self.operator_str} {self.value}%"


class PercentToNewTargetCondition(CompareCondition):
    """Compare percent from current price to new expert target price."""
    
    def evaluate(self) -> bool:
        try:
            if not self.existing_order:
                logger.debug(f"No existing order for percent to new target evaluation")
                self.calculated_value = None
                return False
            
            # Get current market price
            current_price = self.get_current_price()
            if current_price is None:
                logger.error(f"Cannot get current price for {self.instrument_name}")
                self.calculated_value = None
                return False
            
            # Calculate new expert target price from current recommendation
            if not self.expert_recommendation:
                logger.debug(f"No expert recommendation for new target evaluation")
                self.calculated_value = None
                return False
            
            if not hasattr(self.expert_recommendation, 'price_at_date') or not hasattr(self.expert_recommendation, 'expected_profit_percent'):
                logger.error(f"Expert recommendation missing price_at_date or expected_profit_percent")
                self.calculated_value = None
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
                self.calculated_value = None
                return False
            
            # Calculate percent to new target
            percent_to_new_target = ((new_target_price - current_price) / current_price) * 100
            
            self.calculated_value = percent_to_new_target  # Store calculated value
            
            logger.info(f"Percent to NEW target for {self.instrument_name}: current=${current_price:.2f}, new_target=${new_target_price:.2f} (base=${base_price:.2f}, profit={expected_profit:.1f}%), distance={percent_to_new_target:+.2f}%")
            
            return self.operator_func(percent_to_new_target, self.value)
            
        except Exception as e:
            logger.error(f"Error evaluating percent to new target condition: {e}", exc_info=True)
            self.calculated_value = None
            return False
    
    def get_description(self) -> str:
        """Get description of percent to new target condition."""
        return f"Check if percent from current price to new expert target for {self.instrument_name} is {self.operator_str} {self.value}%"


class ProfitLossAmountCondition(CompareCondition):
    """Compare profit/loss amount."""
    
    def evaluate(self) -> bool:
        try:
            if not self.existing_order:
                self.calculated_value = None
                return False
                
            current_price = self.get_current_price()
            if current_price is None or not hasattr(self.existing_order, 'limit_price') or self.existing_order.limit_price is None:
                self.calculated_value = None
                return False
                
            # Calculate P&L amount
            entry_price = self.existing_order.limit_price
            quantity = self.existing_order.quantity
            pl_amount = (current_price - entry_price) * quantity
            
            # Adjust for short positions
            if self.existing_order.side == "sell":
                pl_amount = -pl_amount
            
            self.calculated_value = pl_amount  # Store calculated value
                
            return self.operator_func(pl_amount, self.value)
            
        except Exception as e:
            logger.error(f"Error evaluating profit loss amount condition: {e}", exc_info=True)
            self.calculated_value = None
            return False
    
    def get_description(self) -> str:
        """Get description of profit/loss amount condition."""
        return f"Check if profit/loss amount for {self.instrument_name} is {self.operator_str} ${self.value}"



class ProfitLossPercentCondition(CompareCondition):
    """Compare profit/loss percentage."""
    def evaluate(self) -> bool:
        try:
            if not self.existing_order:
                self.calculated_value = None
                return False
            current_price = self.get_current_price()
            if current_price is None or not hasattr(self.existing_order, 'limit_price') or self.existing_order.limit_price is None:
                self.calculated_value = None
                return False
            # Calculate P&L percentage
            entry_price = self.existing_order.limit_price
            pl_percent = ((current_price - entry_price) / entry_price) * 100
            # Adjust for short positions
            if self.existing_order.side == "sell":
                pl_percent = -pl_percent
            
            self.calculated_value = pl_percent  # Store calculated value
            return self.operator_func(pl_percent, self.value)
        except Exception as e:
            logger.error(f"Error evaluating profit loss percent condition: {e}", exc_info=True)
            self.calculated_value = None
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
                self.calculated_value = None
                return False
            
            self.calculated_value = confidence  # Store calculated value
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
                self.calculated_value = None
                return False
                
            # Calculate days since order was opened
            now = datetime.now(timezone.utc)
            created_at = self.existing_order.created_at
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)
                
            time_diff = now - created_at
            days_opened = time_diff.total_seconds() / 86400  # 86400 seconds in a day
            
            self.calculated_value = days_opened  # Store calculated value
            
            return self.operator_func(days_opened, self.value)
            
        except Exception as e:
            logger.error(f"Error evaluating days opened condition: {e}", exc_info=True)
            self.calculated_value = None
            return False
    
    def get_description(self) -> str:
        """Get description of days opened condition."""
        return f"Check if days since {self.instrument_name} order was opened is {self.operator_str} {self.value} days"


class InstrumentAccountShareCondition(CompareCondition):
    """Compare current instrument value as percentage of expert virtual equity."""
    
    def evaluate(self) -> bool:
        try:
            # Get current position value
            position_value = self._get_instrument_position_value()
            if position_value is None:
                logger.debug(f"No position value available for {self.instrument_name}")
                self.calculated_value = None
                return False
            
            # Get expert virtual equity
            virtual_equity = self._get_expert_virtual_equity()
            if virtual_equity is None or virtual_equity <= 0:
                logger.error(f"Invalid virtual equity for expert {self.expert_recommendation.instance_id} ({self.instrument_name}): virtual_equity={virtual_equity}")
                self.calculated_value = None
                return False
            
            # Calculate share percentage
            share_percent = (position_value / virtual_equity) * 100.0
            
            self.calculated_value = share_percent  # Store calculated value
            
            logger.debug(f"Instrument {self.instrument_name} share: {share_percent:.2f}% "
                        f"(position_value=${position_value:.2f}, virtual_equity=${virtual_equity:.2f})")
            
            return self.operator_func(share_percent, self.value)
            
        except Exception as e:
            logger.error(f"Error evaluating instrument account share condition: {e}", exc_info=True)
            return False
    
    def _get_instrument_position_value(self) -> Optional[float]:
        """Get current market value of instrument position."""
        try:
            # Get current position quantity
            position_qty = self.get_current_position()
            if position_qty is None or position_qty == 0:
                return 0.0  # No position means 0% share
            
            # Get current price
            current_price = self.get_current_price()
            if current_price is None:
                logger.error(f"Cannot get current price for {self.instrument_name}")
                return None
            
            # Calculate market value
            position_value = abs(position_qty) * current_price
            return position_value
            
        except Exception as e:
            logger.error(f"Error getting instrument position value: {e}", exc_info=True)
            return None
    
    def _get_expert_virtual_equity(self) -> Optional[float]:
        """Get expert's virtual equity (available balance)."""
        try:
            # Get expert instance from recommendation
            expert_instance_id = self.expert_recommendation.instance_id
            
            # Load expert instance with loaded settings
            from .utils import get_expert_instance_from_id
            expert = get_expert_instance_from_id(expert_instance_id)
            if not expert:
                logger.error(f"Expert instance {expert_instance_id} not found")
                return None
            
            # Get available balance (virtual equity)
            available_balance = expert.get_available_balance()
            if available_balance is None:
                logger.error(f"Could not get available balance for expert {expert_instance_id}")
                return None
            
            return available_balance
            
        except Exception as e:
            logger.error(f"Error getting expert virtual equity: {e}", exc_info=True)
            return None
    
    def get_description(self) -> str:
        return f"Check if {self.instrument_name} position value as % of expert virtual equity is {self.operator_str} {self.value}%"


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
        ExpertEventType.F_HAS_BUY_POSITION: HasBuyPositionCondition,
        ExpertEventType.F_HAS_SELL_POSITION: HasSellPositionCondition,
        ExpertEventType.F_HAS_NO_POSITION_ACCOUNT: HasNoPositionAccountCondition,
        ExpertEventType.F_HAS_POSITION_ACCOUNT: HasPositionAccountCondition,
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
        ExpertEventType.N_NEW_TARGET_PERCENT: NewTargetPercentCondition,
        ExpertEventType.N_PROFIT_LOSS_AMOUNT: ProfitLossAmountCondition,
        ExpertEventType.N_PROFIT_LOSS_PERCENT: ProfitLossPercentCondition,
        ExpertEventType.N_DAYS_OPENED: DaysOpenedCondition,
        ExpertEventType.N_CONFIDENCE: ConfidenceCondition,
        ExpertEventType.N_INSTRUMENT_ACCOUNT_SHARE: InstrumentAccountShareCondition,
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