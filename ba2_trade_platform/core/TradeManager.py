"""
TradeManager - Core component for handling trade recommendations and order execution

This class reviews trade recommendations from market experts and places orders based on
expert settings, rulesets, and trading permissions.
"""

from typing import Dict, List, Optional, Any
from datetime import datetime, timezone
import logging
from ..logger import logger
from .models import ExpertRecommendation, ExpertInstance, TradingOrder, Ruleset
from .types import OrderRecommendation, OrderStatus, OrderDirection
from .db import get_instance, get_instances, add_instance, update_instance


class TradeManager:
    """
    Manages the execution of trade recommendations from market experts.
    
    Responsibilities:
    - Review expert recommendations
    - Apply trading rules and rulesets
    - Check expert permissions and settings
    - Execute trades through account interfaces
    - Track order status and results
    """
    
    def __init__(self):
        """Initialize the trade manager."""
        self.logger = logger.getChild("TradeManager")
        
    def process_recommendation(self, recommendation: ExpertRecommendation) -> Optional[TradingOrder]:
        """
        Process a single expert recommendation and potentially place an order.
        
        Args:
            recommendation: The expert recommendation to process
            
        Returns:
            TradingOrder if an order was placed, None otherwise
        """
        try:
            # Get the expert instance
            expert_instance = get_instance(ExpertInstance, recommendation.instance_id)
            if not expert_instance:
                self.logger.error(f"Expert instance {recommendation.instance_id} not found")
                return None
                
            # Check if expert is enabled
            if not expert_instance.enabled:
                self.logger.debug(f"Expert instance {expert_instance.id} is disabled, skipping recommendation")
                return None
                
            # Get expert trading permissions
            trading_permissions = self._get_expert_trading_permissions(expert_instance)
            
            # Check if the recommended action is allowed
            if not self._is_action_allowed(recommendation.recommended_action, trading_permissions):
                self.logger.info(f"Action {recommendation.recommended_action} not allowed for expert {expert_instance.id}")
                return None
                
            # Check if automatic trading is enabled
            if not trading_permissions.get('automatic_trading', False):
                self.logger.info(f"Automatic trading disabled for expert {expert_instance.id}, recommendation logged only")
                return None
                
            # Apply rulesets for the recommendation type
            if not self._apply_rulesets(recommendation, expert_instance):
                self.logger.info(f"Recommendation rejected by rulesets for expert {expert_instance.id}")
                return None
                
            # Create and place the order
            order = self._create_order_from_recommendation(recommendation, expert_instance)
            if order:
                placed_order = self._place_order(order, expert_instance)
                if placed_order:
                    self.logger.info(f"Successfully placed order {placed_order.id} for {recommendation.symbol}")
                    return placed_order
                    
        except Exception as e:
            self.logger.error(f"Error processing recommendation {recommendation.id}: {e}", exc_info=True)
            
        return None
        
    def _get_expert_trading_permissions(self, expert_instance: ExpertInstance) -> Dict[str, Any]:
        """
        Get trading permissions for an expert instance.
        
        Args:
            expert_instance: The expert instance
            
        Returns:
            Dictionary of trading permissions
        """
        try:
            # Load expert instance with appropriate class
            from .utils import get_expert_instance_from_id
            expert = get_expert_instance_from_id(expert_instance.id)
            if not expert:
                self.logger.error(f"Expert instance {expert_instance.id} not found or invalid expert type {expert_instance.expert}")
                return {}
            
            return {
                'enable_buy': expert.settings.get('enable_buy', True),
                'enable_sell': expert.settings.get('enable_sell', False),
                'automatic_trading': expert.settings.get('automatic_trading', True)
            }
            
        except Exception as e:
            self.logger.error(f"Error getting trading permissions for expert {expert_instance.id}: {e}")
            return {}
            
    def _is_action_allowed(self, action: OrderRecommendation, permissions: Dict[str, Any]) -> bool:
        """
        Check if a trading action is allowed based on expert permissions.
        
        Args:
            action: The recommended action
            permissions: Expert trading permissions
            
        Returns:
            True if action is allowed, False otherwise
        """
        if action == OrderRecommendation.BUY:
            return permissions.get('enable_buy', False)
        elif action == OrderRecommendation.SELL:
            return permissions.get('enable_sell', False)
        elif action == OrderRecommendation.HOLD:
            return True  # HOLD is always allowed as it means no action
        else:
            return False  # ERROR and unknown actions are not allowed
            
    def _apply_rulesets(self, recommendation: ExpertRecommendation, expert_instance: ExpertInstance) -> bool:
        """
        Apply rulesets to determine if a recommendation should be executed.
        
        Args:
            recommendation: The expert recommendation
            expert_instance: The expert instance
            
        Returns:
            True if recommendation passes all rulesets, False otherwise
        """
        try:
            # Load expert instance with appropriate class
            from .utils import get_expert_instance_from_id
            expert = get_expert_instance_from_id(expert_instance.id)
            if not expert:
                return True  # If no expert instance, allow by default
            
            # Get enter market ruleset (for new positions)
            enter_market_ruleset_id = expert.settings.get('enter_market_ruleset')
            if enter_market_ruleset_id:
                ruleset = get_instance(Ruleset, enter_market_ruleset_id)
                if ruleset:
                    if not self._evaluate_ruleset(ruleset, recommendation, expert_instance):
                        self.logger.info(f"Recommendation rejected by enter market ruleset {ruleset.name}")
                        return False
                        
            # Get open positions ruleset (for managing existing positions)
            open_positions_ruleset_id = expert.settings.get('open_positions_ruleset')
            if open_positions_ruleset_id:
                ruleset = get_instance(Ruleset, open_positions_ruleset_id)
                if ruleset:
                    if not self._evaluate_ruleset(ruleset, recommendation, expert_instance):
                        self.logger.info(f"Recommendation rejected by open positions ruleset {ruleset.name}")
                        return False
                        
            return True
            
        except Exception as e:
            self.logger.error(f"Error applying rulesets: {e}")
            return True  # Allow by default if error
            
    def _evaluate_ruleset(self, ruleset: Ruleset, recommendation: ExpertRecommendation, expert_instance: ExpertInstance) -> bool:
        """
        Evaluate a specific ruleset against a recommendation.
        
        Args:
            ruleset: The ruleset to evaluate
            recommendation: The expert recommendation
            expert_instance: The expert instance
            
        Returns:
            True if recommendation passes the ruleset, False otherwise
        """
        try:
            # For now, implement basic rule evaluation
            # This can be expanded to handle complex rule logic
            
            for event_action in ruleset.event_actions:
                # Check triggers
                triggers = event_action.triggers or {}
                
                # Example trigger checks:
                if 'min_confidence' in triggers:
                    min_confidence = triggers['min_confidence']
                    if recommendation.confidence is None or recommendation.confidence < min_confidence:
                        self.logger.debug(f"Recommendation confidence {recommendation.confidence} below minimum {min_confidence}")
                        return False
                        
                if 'max_risk_level' in triggers:
                    max_risk = triggers['max_risk_level']
                    risk_levels = ['LOW', 'MEDIUM', 'HIGH']
                    if (recommendation.risk_level.value in risk_levels and 
                        max_risk in risk_levels and
                        risk_levels.index(recommendation.risk_level.value) > risk_levels.index(max_risk)):
                        self.logger.debug(f"Recommendation risk level {recommendation.risk_level.value} exceeds maximum {max_risk}")
                        return False
                        
                if 'allowed_actions' in triggers:
                    allowed_actions = triggers['allowed_actions']
                    if isinstance(allowed_actions, list) and recommendation.recommended_action.value not in allowed_actions:
                        self.logger.debug(f"Recommendation action {recommendation.recommended_action.value} not in allowed actions {allowed_actions}")
                        return False
                        
            return True
            
        except Exception as e:
            self.logger.error(f"Error evaluating ruleset {ruleset.id}: {e}")
            return True  # Allow by default if error
            
    def _create_order_from_recommendation(self, recommendation: ExpertRecommendation, expert_instance: ExpertInstance) -> Optional[TradingOrder]:
        """
        Create a trading order from an expert recommendation.
        
        Args:
            recommendation: The expert recommendation
            expert_instance: The expert instance
            
        Returns:
            TradingOrder if created successfully, None otherwise
        """
        try:
            if recommendation.recommended_action == OrderRecommendation.HOLD:
                return None  # No order needed for HOLD
                
            # Map recommendation action to order direction
            if recommendation.recommended_action == OrderRecommendation.BUY:
                side = "buy"
            elif recommendation.recommended_action == OrderRecommendation.SELL:
                side = "sell"
            else:
                return None
                
            # Calculate quantity based on expert's virtual equity and recommendation
            # This is a simplified calculation - can be made more sophisticated
            virtual_equity = expert_instance.virtual_equity or 100.0
            risk_factor = 0.1  # Risk 10% of virtual equity per trade
            if recommendation.risk_level.value == 'HIGH':
                risk_factor = 0.05  # Risk only 5% for high-risk trades
            elif recommendation.risk_level.value == 'LOW':
                risk_factor = 0.15  # Risk up to 15% for low-risk trades
                
            trade_amount = virtual_equity * risk_factor
            quantity = trade_amount / recommendation.price_at_date if recommendation.price_at_date > 0 else 1.0
            
            # Create the order
            order = TradingOrder(
                symbol=recommendation.symbol,
                quantity=abs(quantity),  # Ensure positive quantity
                side=side,
                order_type="market",  # Default to market order
                status=OrderStatus.NEW,
                limit_price=None,  # Market order
                stop_price=None,
                client_order_id=f"expert_{expert_instance.id}_{recommendation.id}_{int(datetime.now().timestamp())}"
            )
            
            return order
            
        except Exception as e:
            self.logger.error(f"Error creating order from recommendation {recommendation.id}: {e}")
            return None
            
    def _place_order(self, order: TradingOrder, expert_instance: ExpertInstance) -> Optional[TradingOrder]:
        """
        Execute the order through the appropriate account interface.
        
        Args:
            order: The trading order to place
            expert_instance: The expert instance
            
        Returns:
            TradingOrder with updated status if successful, None otherwise
        """
        try:
            # Get the account for this expert
            from ..modules.accounts import get_account_class
            from .models import AccountDefinition
            
            account_def = get_instance(AccountDefinition, expert_instance.account_id)
            if not account_def:
                self.logger.error(f"Account definition {expert_instance.account_id} not found")
                return None
                
            account_class = get_account_class(account_def.provider)
            if not account_class:
                self.logger.error(f"Account provider {account_def.provider} not found")
                return None
                
            account = account_class(account_def.id)
            
            # Submit the order through the account interface
            submitted_order = account.submit_order(order)
            if submitted_order:
                # Save the order to database
                order_id = add_instance(submitted_order)
                if order_id:
                    submitted_order.id = order_id
                    self.logger.info(f"Order {order_id} successfully placed for {order.symbol}")
                    return submitted_order
                    
        except Exception as e:
            self.logger.error(f"Error placing order: {e}", exc_info=True)
            
        return None
        
    def process_pending_recommendations(self) -> List[TradingOrder]:
        """
        Process all pending expert recommendations.
        
        Returns:
            List of orders that were placed
        """
        placed_orders = []
        
        try:
            # Get all recent recommendations that haven't been processed
            # This is a simplified query - in production you'd want to track processing status
            from datetime import timedelta
            cutoff_time = datetime.now(timezone.utc) - timedelta(hours=1)  # Process recommendations from last hour
            
            recent_recommendations = get_instances(
                ExpertRecommendation,
                filters={"created_at__gte": cutoff_time}
            )
            
            for recommendation in recent_recommendations:
                if recommendation.recommended_action != OrderRecommendation.HOLD:
                    order = self.process_recommendation(recommendation)
                    if order:
                        placed_orders.append(order)
                        
        except Exception as e:
            self.logger.error(f"Error processing pending recommendations: {e}", exc_info=True)
            
        return placed_orders


# Global trade manager instance
_trade_manager = None

def get_trade_manager() -> TradeManager:
    """Get the global trade manager instance."""
    global _trade_manager
    if _trade_manager is None:
        _trade_manager = TradeManager()
    return _trade_manager