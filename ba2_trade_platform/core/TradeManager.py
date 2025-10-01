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
from .types import OrderRecommendation, OrderStatus, OrderDirection, OrderOpenType, OrderType
from .db import get_instance, get_all_instances, add_instance, update_instance


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
    
    def trigger_and_place_order(self, account, order: TradingOrder, parent_status: OrderStatus, trigger_status: OrderStatus):
        """
        Place the order using the account's submit_order if the parent_status matches the trigger_status.
        If successful, set the order status to OPEN and update in the database.
        """
        from .db import update_instance
        if parent_status == trigger_status:
            submitted_order = account.submit_order(order)
            if submitted_order:
                order.status = OrderStatus.OPEN
                update_instance(order)
                self.logger.info(f"Order {order.id} placed and status set to OPEN.")
                return order
            else:
                self.logger.error(f"Order {order.id} failed to place.")
                return None
        else:
            self.logger.info(f"Order {order.id} not triggered. Parent status: {parent_status}, trigger: {trigger_status}")
            return None
        
    def refresh_accounts(self):
        """
        Refresh account information for all registered accounts.
        
        This method iterates through all account definitions and calls their
        refresh methods to update account information, positions, and orders.
        """
        try:
            from .models import AccountDefinition
            from ..modules.accounts import get_account_class
            from sqlmodel import select
            from .db import get_db
            
            # Get all account definitions
            account_definitions = get_all_instances(AccountDefinition)
            
            self.logger.info(f"Starting account refresh for {len(account_definitions)} accounts")
            
            # Step 1: Capture order statuses before refresh
            pre_refresh_order_statuses = {}
            orders = get_all_instances(TradingOrder)
            for order in orders:
                pre_refresh_order_statuses[order.id] = order.status
            
            self.logger.debug(f"Captured {len(pre_refresh_order_statuses)} order statuses before refresh")
            
            # Step 2: Perform account refresh
            for account_def in account_definitions:
                try:
                    # Get the account class for this provider
                    account_class = get_account_class(account_def.provider)
                    if not account_class:
                        self.logger.warning(f"No account class found for provider {account_def.provider}")
                        continue
                    
                    # Create account instance
                    account = account_class(account_def.id)
                    
                    # Refresh account data if the method exists
                    if hasattr(account, 'refresh_positions'):
                        account.refresh_positions()
                        self.logger.debug(f"Refreshed positions for {account_def.name}")
                    
                    if hasattr(account, 'refresh_orders'):
                        account.refresh_orders()
                        self.logger.debug(f"Refreshed orders for {account_def.name}")
                    
                    if hasattr(account, 'refresh_transactions'):
                        account.refresh_transactions()
                        self.logger.debug(f"Refreshed transactions for {account_def.name}")
                        
                except Exception as e:
                    self.logger.error(f"Error refreshing account {account_def.name} (ID: {account_def.id}): {e}", exc_info=True)
                    continue
            
            # Step 3: Check for order status changes and trigger dependent orders
            self._check_order_status_changes_and_trigger_dependents(pre_refresh_order_statuses)
            
            self.logger.info("Account refresh completed")
            
        except Exception as e:
            self.logger.error(f"Error during account refresh: {e}", exc_info=True)
    
    def _check_order_status_changes_and_trigger_dependents(self, pre_refresh_statuses: Dict[int, OrderStatus]):
        """
        Check for order status changes and trigger dependent orders that are in WAITING_TRIGGER state.
        
        Args:
            pre_refresh_statuses: Dictionary mapping order IDs to their status before refresh
        """
        try:
            from sqlmodel import select
            from .db import get_db
            
            with get_db() as session:
                # Get all orders that have dependencies (depends_on_order is not None)
                statement = select(TradingOrder).where(TradingOrder.depends_on_order.isnot(None))
                dependent_orders = session.exec(statement).all()
                
                self.logger.debug(f"Checking {len(dependent_orders)} orders with dependencies for triggering")
                
                triggered_orders = []
                
                for dependent_order in dependent_orders:
                    if dependent_order.status != OrderStatus.WAITING_TRIGGER:
                        self.logger.debug(f"Skipping order {dependent_order.id} - status is {dependent_order.status}, not WAITING_TRIGGER")
                        continue  # Only process orders waiting for triggers
                        
                    parent_order_id = dependent_order.depends_on_order
                    trigger_status = dependent_order.depends_order_status_trigger
                    
                    if not trigger_status:
                        self.logger.debug(f"Skipping order {dependent_order.id} - no trigger status defined")
                        continue  # No trigger status defined
                        
                    # Get current parent order status
                    parent_order = session.get(TradingOrder, parent_order_id)
                    if not parent_order:
                        self.logger.warning(f"Parent order {parent_order_id} not found for dependent order {dependent_order.id}")
                        continue
                        
                    current_status = parent_order.status
                    
                    self.logger.debug(f"Checking dependent order {dependent_order.id}: parent order {parent_order_id} status is {current_status}, trigger status is {trigger_status}")
                    
                    # Check if parent order has reached the trigger status
                    # We don't check pre_status because the order might have already been in this status,
                    # or it might have transitioned through multiple states in a single refresh
                    if current_status == trigger_status:
                        self.logger.info(f"Order {parent_order_id} is in status {trigger_status}, triggering dependent order {dependent_order.id}")
                        
                        # Get the account for this dependent order using its account_id
                        from ..modules.accounts import get_account_class
                        from .models import AccountDefinition
                        
                        # Get the account definition for this order
                        account_def = session.get(AccountDefinition, dependent_order.account_id)
                        if not account_def:
                            self.logger.error(f"Account definition {dependent_order.account_id} not found for dependent order {dependent_order.id}")
                            continue
                            
                        account_class = get_account_class(account_def.provider)
                        if not account_class:
                            self.logger.error(f"Account provider {account_def.provider} not found for dependent order {dependent_order.id}")
                            continue
                            
                        account = account_class(account_def.id)
                        
                        # Submit the dependent order
                        submitted_order = account.submit_order(dependent_order)
                        
                        if submitted_order:
                            # Update dependent order status to OPEN
                            dependent_order.status = OrderStatus.OPEN
                            session.add(dependent_order)
                            self.logger.info(f"Successfully submitted dependent order {dependent_order.id} triggered by parent order {parent_order_id}")
                            triggered_orders.append(dependent_order.id)
                        else:
                            self.logger.error(f"Failed to submit dependent order {dependent_order.id}")
                            # Keep the order in WAITING_TRIGGER status so it can be retried
                        
                if triggered_orders:
                    session.commit()
                    self.logger.info(f"Triggered {len(triggered_orders)} dependent orders: {triggered_orders}")
                else:
                    self.logger.debug("No dependent orders were triggered")
                    
                session.close()
                
        except Exception as e:
            self.logger.error(f"Error checking order status changes: {e}", exc_info=True)
    
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
                self.logger.error(f"Expert instance {recommendation.instance_id} not found", exc_info=True)
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
                
            # Create the order. Do npt place it yet.
            order = self._create_order_from_recommendation(recommendation, expert_instance)

                    
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
                self.logger.error(f"Expert instance {expert_instance.id} not found or invalid expert type {expert_instance.expert}", exc_info=True)
                return {}
            
            # Check for legacy automatic_trading setting and new settings
            legacy_automatic_trading = expert.settings.get('automatic_trading', False)
            allow_automated_trade_opening = expert.settings.get('allow_automated_trade_opening', legacy_automatic_trading)
            allow_automated_trade_modification = expert.settings.get('allow_automated_trade_modification', legacy_automatic_trading)
            
            return {
                'enable_buy': expert.settings.get('enable_buy', True),
                'enable_sell': expert.settings.get('enable_sell', False),
                'allow_automated_trade_opening': allow_automated_trade_opening,
                'allow_automated_trade_modification': allow_automated_trade_modification,
                # Keep legacy setting for backward compatibility
                'automatic_trading': legacy_automatic_trading
            }
            
        except Exception as e:
            self.logger.error(f"Error getting trading permissions for expert {expert_instance.id}: {e}", exc_info=True)
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
            # Get enter market ruleset (for new positions) from the model field
            if expert_instance.enter_market_ruleset_id:
                ruleset = self._get_ruleset_with_relations(expert_instance.enter_market_ruleset_id)
                if ruleset:
                    if not self._evaluate_ruleset(ruleset, recommendation, expert_instance):
                        self.logger.info(f"Recommendation rejected by enter market ruleset {ruleset.name}")
                        return False
                        
            # Get open positions ruleset (for managing existing positions) from the model field
            if expert_instance.open_positions_ruleset_id:
                ruleset = self._get_ruleset_with_relations(expert_instance.open_positions_ruleset_id)
                if ruleset:
                    if not self._evaluate_ruleset(ruleset, recommendation, expert_instance):
                        self.logger.info(f"Recommendation rejected by open positions ruleset {ruleset.name}")
                        return False
                        
            return True
            
        except Exception as e:
            self.logger.error(f"Error applying rulesets: {e}", exc_info=True)
            return True  # Allow by default if error
    
    def _get_ruleset_with_relations(self, ruleset_id: int) -> Optional[Ruleset]:
        """
        Get a ruleset with its event_actions relationship eagerly loaded.
        
        Args:
            ruleset_id: The ID of the ruleset to fetch
            
        Returns:
            Ruleset with event_actions loaded, or None if not found
        """
        try:
            from sqlmodel import select
            from sqlalchemy.orm import selectinload
            from .db import get_db
            
            with get_db() as session:
                # Use selectinload to eagerly load the event_actions relationship
                statement = select(Ruleset).where(Ruleset.id == ruleset_id).options(
                    selectinload(Ruleset.event_actions)
                )
                ruleset = session.exec(statement).first()
                
                if not ruleset:
                    self.logger.warning(f"Ruleset {ruleset_id} not found")
                    return None
                
                # Make the ruleset persistent by expunging it from the session
                # This allows it to be used outside the session context
                session.expunge(ruleset)
                
                return ruleset
                
        except Exception as e:
            self.logger.error(f"Error fetching ruleset {ruleset_id}: {e}", exc_info=True)
            return None
            
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
            self.logger.error(f"Error evaluating ruleset {ruleset.id}: {e}", exc_info=True)
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

            quantity = 0
            
            # Create the order
            order = TradingOrder(
                symbol=recommendation.symbol,
                quantity=abs(quantity),  # Ensure positive quantity
                side=side,
                order_type="market",  # Default to market order
                status=OrderStatus.PENDING,
                limit_price=None,  # Market order
                stop_price=None,
                comment=f"expert_{expert_instance.expert}-{expert_instance.id}_{recommendation.id}",
                # Link to the recommendation and mark as automatic
                order_recommendation_id=recommendation.id,
                open_type=OrderOpenType.AUTOMATIC
            )
            
            return order
            
        except Exception as e:
            self.logger.error(f"Error creating order from recommendation {recommendation.id}: {e}", exc_info=True)
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
                self.logger.error(f"Account definition {expert_instance.account_id} not found", exc_info=True)
                return None
                
            account_class = get_account_class(account_def.provider)
            if not account_class:
                self.logger.error(f"Account provider {account_def.provider} not found", exc_info=True)
                return None
                
            account = account_class(account_def.id)
            
            # Submit the order through the account interface
            submitted_order = account.submit_order(order)
            if submitted_order:
                # Save the order to database
                db_id = add_instance(submitted_order)
                if db_id:
                    submitted_order.id = db_id
                    self.logger.info(f"Order {db_id} successfully placed for {order.symbol}")
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
            from sqlmodel import select, Session
            from .db import engine
            
            cutoff_time = datetime.now(timezone.utc) - timedelta(hours=1)  # Process recommendations from last hour
            
            with Session(engine) as session:
                statement = select(ExpertRecommendation).where(ExpertRecommendation.created_at >= cutoff_time)
                recent_recommendations = session.exec(statement).all()
            
            for recommendation in recent_recommendations:
                if recommendation.recommended_action != OrderRecommendation.HOLD:
                    order = self.process_recommendation(recommendation)
                    if order:
                        placed_orders.append(order)
                        
        except Exception as e:
            self.logger.error(f"Error processing pending recommendations: {e}", exc_info=True)
            
        return placed_orders
    
    def process_expert_recommendations_after_analysis(self, expert_instance_id: int) -> List[TradingOrder]:
        """
        Process expert recommendations after all market analysis jobs for enter_market are completed.
        
        This function is called when there are no more pending analysis jobs for a given expert.
        It evaluates recommendations through the enter_market ruleset and creates pending orders
        for those that pass all rules (if automated trading is enabled).
        
        Args:
            expert_instance_id: The expert instance ID to process recommendations for
            
        Returns:
            List of TradingOrder objects that were created (in PENDING state)
        """
        created_orders = []
        
        try:
            from sqlmodel import select, Session
            from .db import get_db
            from .models import Transaction, AccountDefinition
            from .types import AnalysisUseCase, TransactionStatus
            from .utils import get_expert_instance_from_id
            from datetime import timedelta
            
            # Get the expert instance (with loaded settings)
            expert = get_expert_instance_from_id(expert_instance_id)
            if not expert:
                self.logger.error(f"Expert instance {expert_instance_id} not found", exc_info=True)
                return created_orders
            
            # Get the expert instance model (for ruleset IDs)
            expert_instance = get_instance(ExpertInstance, expert_instance_id)
            if not expert_instance:
                self.logger.error(f"Expert instance model {expert_instance_id} not found", exc_info=True)
                return created_orders
            
            # Check if "Allow automated trade opening" is enabled
            allow_automated_trade_opening = expert.settings.get('allow_automated_trade_opening', False)
            if not allow_automated_trade_opening:
                self.logger.debug(f"Automated trade opening disabled for expert {expert_instance_id}, skipping recommendation processing")
                return created_orders
            
            # Check if there's an enter_market ruleset configured
            if not expert_instance.enter_market_ruleset_id:
                self.logger.debug(f"No enter_market ruleset configured for expert {expert_instance_id}, skipping automated order creation")
                return created_orders
            
            # Get the enter_market ruleset with relationships eagerly loaded
            enter_market_ruleset = self._get_ruleset_with_relations(expert_instance.enter_market_ruleset_id)
            if not enter_market_ruleset:
                self.logger.warning(f"Enter market ruleset {expert_instance.enter_market_ruleset_id} not found for expert {expert_instance_id}")
                return created_orders
            
            # Check if there are still pending analysis jobs for this expert
            if self._has_pending_analysis_jobs(expert_instance_id):
                self.logger.debug(f"Still has pending analysis jobs for expert {expert_instance_id}, skipping automated order creation")
                return created_orders
            
            # Get recent recommendations (less than 24 hours old) for this expert
            cutoff_time = datetime.now(timezone.utc) - timedelta(hours=24)
            
            with Session(get_db().bind) as session:
                statement = select(ExpertRecommendation).where(
                    ExpertRecommendation.instance_id == expert_instance_id,
                    ExpertRecommendation.created_at >= cutoff_time,
                    ExpertRecommendation.recommended_action != OrderRecommendation.HOLD
                ).order_by(ExpertRecommendation.expected_profit_percent.desc())  # Order by profit potential
                
                recommendations = session.exec(statement).all()
                
                if not recommendations:
                    self.logger.info(f"No actionable recommendations found for expert {expert_instance_id}")
                    return created_orders
                
                self.logger.info(f"Found {len(recommendations)} actionable recommendations for expert {expert_instance_id}")
                self.logger.info(f"Evaluating recommendations through enter_market ruleset: {enter_market_ruleset.name}")
                
                # Process recommendations through the ruleset
                for recommendation in recommendations:
                    # Evaluate recommendation through the enter_market ruleset
                    if not self._evaluate_ruleset(enter_market_ruleset, recommendation, expert_instance):
                        self.logger.debug(f"Recommendation {recommendation.id} for {recommendation.symbol} rejected by ruleset")
                        continue
                    
                    # If recommendation passes the ruleset, check if there are actions to execute
                    # For now, we'll create a pending order for approved recommendations
                    # The order will be reviewed and prioritized once all analysis is complete
                    
                    # Map recommendation action to order direction
                    if recommendation.recommended_action == OrderRecommendation.BUY:
                        side = OrderDirection.BUY
                    elif recommendation.recommended_action == OrderRecommendation.SELL:
                        side = OrderDirection.SELL
                    else:
                        continue
                    
                    # Create a pending order (quantity will be determined later during review)
                    order = TradingOrder(
                        account_id=expert_instance.account_id,
                        symbol=recommendation.symbol,
                        quantity=0,  # To be determined during review/prioritization
                        side=side,
                        order_type=OrderType.MARKET,
                        status=OrderStatus.PENDING,  # Keep as PENDING for review
                        limit_price=None,
                        stop_price=None,
                        comment=f"Auto-created from recommendation {recommendation.id} (awaiting review)",
                        order_recommendation_id=recommendation.id,
                        open_type=OrderOpenType.AUTOMATIC,
                        created_at=datetime.now(timezone.utc)
                    )
                    
                    # Add order to database
                    order_id = add_instance(order)
                    if order_id:
                        order.id = order_id
                        created_orders.append(order)
                        self.logger.info(f"Created pending order {order_id} for {recommendation.symbol} (recommendation {recommendation.id} passed ruleset)")
                
                self.logger.info(f"Created {len(created_orders)} pending orders for expert {expert_instance_id} awaiting review and prioritization")
                
                # If we created orders and automated trading is enabled, run risk management
                if created_orders and allow_automated_trade_opening:
                    self.logger.info(f"Running risk management for {len(created_orders)} pending orders")
                    try:
                        from .TradeRiskManagement import get_risk_management
                        risk_management = get_risk_management()
                        updated_orders = risk_management.review_and_prioritize_pending_orders(expert_instance_id)
                        self.logger.info(f"Risk management completed: updated {len(updated_orders)} orders with quantities")
                    except Exception as e:
                        self.logger.error(f"Error during risk management for expert {expert_instance_id}: {e}", exc_info=True)
                
        except Exception as e:
            self.logger.error(f"Error processing expert recommendations after analysis for expert {expert_instance_id}: {e}", exc_info=True)
        
        return created_orders
    
    def _has_pending_analysis_jobs(self, expert_instance_id: int) -> bool:
        """
        Check if there are pending market analysis jobs for a given expert instance.
        
        Args:
            expert_instance_id: The expert instance ID to check
            
        Returns:
            True if there are pending jobs, False otherwise
        """
        try:
            from .WorkerQueue import get_worker_queue
            from .types import AnalysisUseCase, WorkerTaskStatus
            
            worker_queue = get_worker_queue()
            all_tasks = worker_queue.get_all_tasks()
            
            # Check for pending enter_market tasks for this expert
            for task in all_tasks.values():
                if (task.expert_instance_id == expert_instance_id and
                    task.subtype == AnalysisUseCase.ENTER_MARKET and
                    task.status in [WorkerTaskStatus.PENDING, WorkerTaskStatus.RUNNING]):
                    return True
            
            return False
            
        except Exception as e:
            self.logger.error(f"Error checking for pending analysis jobs for expert {expert_instance_id}: {e}", exc_info=True)
            return True  # Assume there are pending jobs if we can't check


# Global trade manager instance
_trade_manager = None

def get_trade_manager() -> TradeManager:
    """Get the global trade manager instance."""
    global _trade_manager
    if _trade_manager is None:
        _trade_manager = TradeManager()
    return _trade_manager