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
            
            # Step 4: Also check all WAITING_TRIGGER orders to ensure none are stuck
            # This catches cases where status changes weren't detected
            self._check_all_waiting_trigger_orders()
            
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
                        try:
                            submitted_order = account.submit_order(dependent_order)
                            
                            if submitted_order:
                                # Update dependent order status to OPEN
                                dependent_order.status = OrderStatus.OPEN
                                session.add(dependent_order)
                                self.logger.info(f"Successfully submitted dependent order {dependent_order.id} triggered by parent order {parent_order_id}")
                                triggered_orders.append(dependent_order.id)
                            else:
                                self.logger.error(f"Failed to submit dependent order {dependent_order.id} - setting to ERROR status")
                                # Set to ERROR status so it doesn't stay stuck
                                dependent_order.status = OrderStatus.ERROR
                                session.add(dependent_order)
                        except Exception as submit_error:
                            self.logger.error(f"Exception submitting dependent order {dependent_order.id}: {submit_error}", exc_info=True)
                            # Set to ERROR status on exception
                            dependent_order.status = OrderStatus.ERROR
                            session.add(dependent_order)
                        
                if triggered_orders:
                    session.commit()
                    self.logger.info(f"Triggered {len(triggered_orders)} dependent orders: {triggered_orders}")
                else:
                    self.logger.debug("No dependent orders were triggered")
                    
                session.close()
                
        except Exception as e:
            self.logger.error(f"Error checking order status changes: {e}", exc_info=True)
    
    def _check_all_waiting_trigger_orders(self):
        """
        Check all orders in WAITING_TRIGGER status to see if their parent orders have reached the trigger status.
        
        This method is called periodically to ensure no orders get stuck waiting for triggers,
        catching cases where status change detection may have missed an update.
        """
        try:
            from sqlmodel import select
            from .db import get_db
            
            with get_db() as session:
                # Get all orders in WAITING_TRIGGER status
                statement = select(TradingOrder).where(
                    TradingOrder.status == OrderStatus.WAITING_TRIGGER,
                    TradingOrder.depends_on_order.isnot(None),
                    TradingOrder.depends_order_status_trigger.isnot(None)
                )
                waiting_orders = session.exec(statement).all()
                
                if not waiting_orders:
                    self.logger.debug("No orders in WAITING_TRIGGER status")
                    return
                
                self.logger.info(f"Checking {len(waiting_orders)} orders in WAITING_TRIGGER status")
                
                triggered_orders = []
                
                for dependent_order in waiting_orders:
                    try:
                        parent_order_id = dependent_order.depends_on_order
                        trigger_status = dependent_order.depends_order_status_trigger
                        
                        # Get current parent order status
                        parent_order = session.get(TradingOrder, parent_order_id)
                        if not parent_order:
                            self.logger.warning(f"Parent order {parent_order_id} not found for dependent order {dependent_order.id} - setting to ERROR")
                            dependent_order.status = OrderStatus.ERROR
                            session.add(dependent_order)
                            continue
                        
                        current_status = parent_order.status
                        
                        self.logger.debug(f"Checking order {dependent_order.id}: parent {parent_order_id} status is {current_status}, trigger is {trigger_status}")
                        
                        # Check if parent order has reached the trigger status
                        if current_status == trigger_status:
                            self.logger.info(f"Parent order {parent_order_id} is in trigger status {trigger_status}, processing dependent order {dependent_order.id}")
                            
                            # Get the account for this dependent order
                            from ..modules.accounts import get_account_class
                            from .models import AccountDefinition
                            
                            account_def = session.get(AccountDefinition, dependent_order.account_id)
                            if not account_def:
                                self.logger.error(f"Account definition {dependent_order.account_id} not found for dependent order {dependent_order.id} - setting to ERROR")
                                dependent_order.status = OrderStatus.ERROR
                                session.add(dependent_order)
                                continue
                            
                            account_class = get_account_class(account_def.provider)
                            if not account_class:
                                self.logger.error(f"Account provider {account_def.provider} not found for dependent order {dependent_order.id} - setting to ERROR")
                                dependent_order.status = OrderStatus.ERROR
                                session.add(dependent_order)
                                continue
                            
                            account = account_class(account_def.id)
                            
                            # Set quantity based on parent order's filled quantity (for TP/SL orders)
                            if dependent_order.quantity == 0 and parent_order.quantity > 0:
                                self.logger.info(f"Setting dependent order {dependent_order.id} quantity to {parent_order.quantity} (matching parent order)")
                                dependent_order.quantity = parent_order.quantity
                                session.add(dependent_order)
                            
                            # Submit the dependent order
                            try:
                                submitted_order = account.submit_order(dependent_order)
                                
                                if submitted_order:
                                    # Update dependent order status to OPEN
                                    dependent_order.status = OrderStatus.OPEN
                                    session.add(dependent_order)
                                    self.logger.info(f"Successfully submitted dependent order {dependent_order.id}")
                                    triggered_orders.append(dependent_order.id)
                                else:
                                    self.logger.error(f"Failed to submit dependent order {dependent_order.id} - setting to ERROR status")
                                    dependent_order.status = OrderStatus.ERROR
                                    session.add(dependent_order)
                            except Exception as submit_error:
                                self.logger.error(f"Exception submitting dependent order {dependent_order.id}: {submit_error}", exc_info=True)
                                dependent_order.status = OrderStatus.ERROR
                                session.add(dependent_order)
                    
                    except Exception as order_error:
                        self.logger.error(f"Error processing waiting order {dependent_order.id}: {order_error}", exc_info=True)
                        # Set to ERROR on any exception during processing
                        try:
                            dependent_order.status = OrderStatus.ERROR
                            session.add(dependent_order)
                        except:
                            pass  # If we can't even set error status, log and continue
                
                if triggered_orders:
                    session.commit()
                    self.logger.info(f"Processed {len(triggered_orders)} waiting trigger orders: {triggered_orders}")
                else:
                    # Commit any ERROR status changes
                    session.commit()
                    self.logger.debug("No waiting trigger orders were ready to execute")
                
                session.close()
                
        except Exception as e:
            self.logger.error(f"Error checking all waiting trigger orders: {e}", exc_info=True)
    
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
        DEPRECATED: This method is no longer used. Ruleset evaluation is now handled by TradeActionEvaluator.
        See process_expert_recommendations_after_analysis() for the new implementation.
        
        Apply rulesets to determine if a recommendation should be executed.
        
        Args:
            recommendation: The expert recommendation
            expert_instance: The expert instance
            
        Returns:
            True (always allows - deprecated method)
        """
        # This method is deprecated and no longer evaluates rulesets
        # Ruleset evaluation is now done by TradeActionEvaluator in process_expert_recommendations_after_analysis
        self.logger.warning("_apply_rulesets called but is deprecated - returning True (allow)")
        return True
    
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
            # Convert side to uppercase to match OrderDirection enum
            side_upper = side.upper() if isinstance(side, str) else side
            order = TradingOrder(
                symbol=recommendation.symbol,
                quantity=abs(quantity),  # Ensure positive quantity
                side=side_upper,
                order_type="market",  # Default to market order
                status=OrderStatus.PENDING,
                limit_price=None,  # Market order
                stop_price=None,
                comment=f"expert_{expert_instance.expert}-{expert_instance.id}_{recommendation.id}",
                # Link to the recommendation and mark as automatic
                expert_recommendation_id=recommendation.id,
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
                # Note: add_instance will set the ID on the instance before committing
                db_id = add_instance(submitted_order)
                if db_id:
                    # The submitted_order instance is now detached, but should have the ID set
                    # Create a simple return value to avoid detached instance issues
                    self.logger.info(f"Order {db_id} successfully placed for {order.symbol}")
                    
                    # Return a fresh instance from the database to avoid detached instance errors
                    from .db import get_instance
                    return get_instance(TradingOrder, db_id)
                    
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
    
    def process_expert_recommendations_after_analysis(self, expert_instance_id: int, lookback_days: int = 1) -> List[TradingOrder]:
        """
        Process expert recommendations after all market analysis jobs for enter_market are completed.
        
        This function is called when there are no more pending analysis jobs for a given expert.
        It evaluates recommendations through the enter_market ruleset using TradeActionEvaluator
        and executes the resulting actions (if automated trading is enabled).
        
        Args:
            expert_instance_id: The expert instance ID to process recommendations for
            lookback_days: Number of days to look back for recommendations (default: 1)
            
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
            from .TradeActionEvaluator import TradeActionEvaluator
            from ..modules.accounts import get_account_class
            
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
            
            # Check if there are still pending analysis jobs for this expert
            if self._has_pending_analysis_jobs(expert_instance_id):
                self.logger.debug(f"Still has pending analysis jobs for expert {expert_instance_id}, skipping automated order creation")
                return created_orders
            
            # Get the account instance for this expert
            account_def = get_instance(AccountDefinition, expert_instance.account_id)
            if not account_def:
                self.logger.error(f"Account definition {expert_instance.account_id} not found", exc_info=True)
                return created_orders
                
            account_class = get_account_class(account_def.provider)
            if not account_class:
                self.logger.error(f"Account provider {account_def.provider} not found", exc_info=True)
                return created_orders
                
            account = account_class(account_def.id)
            
            # Get recent recommendations based on lookback_days parameter
            cutoff_time = datetime.now(timezone.utc) - timedelta(days=lookback_days)
            
            with Session(get_db().bind) as session:
                # Get all recommendations for this expert instance within the time window
                statement = select(ExpertRecommendation).where(
                    ExpertRecommendation.instance_id == expert_instance_id,
                    ExpertRecommendation.created_at >= cutoff_time,
                    ExpertRecommendation.recommended_action != OrderRecommendation.HOLD
                ).order_by(ExpertRecommendation.created_at.desc())  # Most recent first
                
                all_recommendations = session.exec(statement).all()
                
                if not all_recommendations:
                    self.logger.info(f"No actionable recommendations found for expert {expert_instance_id}")
                    return created_orders
                
                # Filter to get only the latest recommendation per instrument
                # This prevents processing multiple recommendations for the same symbol
                latest_per_instrument = {}
                for rec in all_recommendations:
                    # Keep only the first (most recent) recommendation for each symbol
                    if rec.symbol not in latest_per_instrument:
                        latest_per_instrument[rec.symbol] = rec
                
                # Convert to list and sort by profit potential
                recommendations = sorted(
                    latest_per_instrument.values(),
                    key=lambda r: r.expected_profit_percent,
                    reverse=True
                )
                
                self.logger.info(f"Found {len(recommendations)} unique instruments with recommendations for expert {expert_instance_id} (filtered from {len(all_recommendations)} total recommendations)")
                self.logger.info(f"Evaluating recommendations through enter_market ruleset: {expert_instance.enter_market_ruleset_id}")
                
                # Process each recommendation through the enter_market ruleset
                for recommendation in recommendations:
                    try:
                        # Create TradeActionEvaluator with instrument_name for this recommendation
                        # No existing_transactions for entering_markets use case
                        evaluator = TradeActionEvaluator(
                            account=account,
                            instrument_name=recommendation.symbol,
                            existing_transactions=None
                        )
                        
                        # Evaluate recommendation through the enter_market ruleset
                        self.logger.debug(f"Evaluating recommendation {recommendation.id} for {recommendation.symbol}")
                        
                        action_summaries = evaluator.evaluate(
                            instrument_name=recommendation.symbol,
                            expert_recommendation=recommendation,
                            ruleset_id=expert_instance.enter_market_ruleset_id,
                            existing_order=None  # No existing order when entering market
                        )
                        
                        # Check if evaluation produced any actions
                        if not action_summaries:
                            self.logger.debug(f"Recommendation {recommendation.id} for {recommendation.symbol} - no actions to execute (conditions not met)")
                            continue
                        
                        # Check for evaluation errors
                        if any('error' in summary for summary in action_summaries):
                            errors = [s.get('error') for s in action_summaries if 'error' in s]
                            self.logger.warning(f"Recommendation {recommendation.id} evaluation had errors: {errors}")
                            continue
                        
                        self.logger.info(f"Recommendation {recommendation.id} for {recommendation.symbol} passed ruleset - {len(action_summaries)} action(s) to execute")
                        
                        # Execute the actions using TradeActionEvaluator
                        # Actions are already sorted by priority (BUY/SELL first, then TP/SL)
                        # Thanks to _sort_actions_by_priority in execute() method
                        execution_results = evaluator.execute()
                        
                        # Track the main order (BUY/SELL) that was created so we can use it for TP/SL
                        main_order = None
                        
                        # Process execution results
                        for result in execution_results:
                            if result.get('success', False):
                                self.logger.info(f"Action executed successfully: {result.get('description', 'Unknown action')}")
                                
                                # Check if a TradingOrder was created (data field should contain order_id)
                                if result.get('data') and isinstance(result['data'], dict):
                                    order_id = result['data'].get('order_id')
                                    if order_id:
                                        # Query the order from the current session (not using get_instance)
                                        # to ensure it's attached to this session before expunge
                                        from sqlmodel import select
                                        statement = select(TradingOrder).where(TradingOrder.id == order_id)
                                        order = session.exec(statement).first()
                                        if order:
                                            session.expunge(order)
                                            created_orders.append(order)
                                            # Track the main order (first BUY/SELL order created)
                                            # This will be the pending order that TP/SL can reference
                                            if main_order is None and order.side in [OrderDirection.BUY, OrderDirection.SELL]:
                                                main_order = order
                                                self.logger.debug(f"Main order {order_id} will be used for TP/SL actions")
                                            self.logger.info(f"Created order {order_id} for {recommendation.symbol}")
                            else:
                                self.logger.warning(f"Action execution failed: {result.get('message', 'Unknown error')}")
                        
                    except Exception as e:
                        self.logger.error(f"Error processing recommendation {recommendation.id}: {e}", exc_info=True)
                        continue
                
                self.logger.info(f"Created {len(created_orders)} orders from {len(recommendations)} recommendations for expert {expert_instance_id}")
                
                # If we created orders and automated trading is enabled, run risk management
                if created_orders and allow_automated_trade_opening:
                    self.logger.info(f"Running risk management for {len(created_orders)} pending orders")
                    try:
                        from .TradeRiskManagement import get_risk_management
                        risk_management = get_risk_management()
                        updated_orders = risk_management.review_and_prioritize_pending_orders(expert_instance_id)
                        self.logger.info(f"Risk management completed: updated {len(updated_orders)} orders with quantities")
                        
                        # Auto-submit orders with quantity > 0 to broker
                        submitted_count = 0
                        for order in updated_orders:
                            if order.quantity and order.quantity > 0:
                                try:
                                    self.logger.info(f"Auto-submitting order {order.id} for {order.symbol}: {order.quantity} shares")
                                    submitted_order = account.submit_order(order)
                                    if submitted_order:
                                        submitted_count += 1
                                        self.logger.info(f"Successfully submitted order {order.id} to broker")
                                    else:
                                        self.logger.warning(f"Failed to submit order {order.id} to broker")
                                except Exception as submit_error:
                                    self.logger.error(f"Error submitting order {order.id}: {submit_error}", exc_info=True)
                        
                        self.logger.info(f"Auto-submitted {submitted_count}/{len(updated_orders)} orders to broker")
                        
                        # Refresh order statuses from broker to detect if any orders are already FILLED
                        # This is important for market orders which fill immediately
                        if submitted_count > 0:
                            self.logger.info("Refreshing order statuses from broker after submission")
                            try:
                                account.refresh_orders()
                                self.logger.info("Order status refresh completed")
                            except Exception as refresh_error:
                                self.logger.error(f"Error refreshing order statuses: {refresh_error}", exc_info=True)
                        
                        # After submitting orders, check for any dependent orders (e.g., TP/SL WAITING_TRIGGER)
                        # that may now be ready to execute
                        self.logger.info("Checking for dependent orders after risk management")
                        self._check_all_waiting_trigger_orders()
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
    
    def force_sync_all_transactions(self):
        """
        Force synchronization of all transactions based on their linked order states.
        
        This method is intended to be run at startup to ensure all transaction states
        are in sync with their orders, without waiting for order state change triggers.
        
        It calls refresh_transactions for all accounts which will:
        - Update WAITING -> OPENED when market entry orders are FILLED
        - Update OPENED -> CLOSED when closing orders are FILLED
        - Update WAITING -> CLOSED when orders are canceled/rejected
        - Set open_price, close_price, open_date, close_date appropriately
        """
        try:
            from .models import AccountDefinition
            from ..modules.accounts import get_account_class
            
            self.logger.info("Starting force sync of all transactions at startup...")
            
            # Get all account definitions
            account_definitions = get_all_instances(AccountDefinition)
            
            total_synced = 0
            
            for account_def in account_definitions:
                try:
                    # Get the account class for this provider
                    account_class = get_account_class(account_def.provider)
                    if not account_class:
                        self.logger.warning(f"No account class found for provider {account_def.provider}")
                        continue
                    
                    # Create account instance
                    account = account_class(account_def.id)
                    
                    # Force sync transactions based on current order states
                    if hasattr(account, 'refresh_transactions'):
                        self.logger.info(f"Force syncing transactions for account {account_def.name}...")
                        success = account.refresh_transactions()
                        if success:
                            total_synced += 1
                            self.logger.info(f"Successfully synced transactions for {account_def.name}")
                        else:
                            self.logger.warning(f"Failed to sync transactions for {account_def.name}")
                    else:
                        self.logger.warning(f"Account {account_def.name} does not support transaction refresh")
                        
                except Exception as e:
                    self.logger.error(f"Error syncing transactions for account {account_def.name}: {e}", exc_info=True)
                    continue
            
            self.logger.info(f"Force sync completed: {total_synced}/{len(account_definitions)} accounts synced")
            
        except Exception as e:
            self.logger.error(f"Error during force sync of transactions: {e}", exc_info=True)


# Global trade manager instance
_trade_manager = None

def get_trade_manager() -> TradeManager:
    """Get the global trade manager instance."""
    global _trade_manager
    if _trade_manager is None:
        _trade_manager = TradeManager()
    return _trade_manager