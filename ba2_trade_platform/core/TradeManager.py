"""
TradeManager - Core component for handling trade recommendations and order execution

This class reviews trade recommendations from market experts and places orders based on
expert settings, rulesets, and trading permissions.
"""

from typing import Dict, List, Optional, Any
from datetime import datetime, timezone
import logging
import threading
from ..logger import logger
from .models import ExpertRecommendation, ExpertInstance, TradingOrder, Ruleset, Transaction
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
        # Use the parent logger directly instead of getChild to avoid double logging
        # The parent logger already has all necessary handlers configured
        self.logger = logger
        # Lock dictionary for preventing concurrent processing of recommendations
        # Key format: "expert_{expert_id}_usecase_{use_case}"
        self._processing_locks: Dict[str, threading.Lock] = {}
        self._locks_dict_lock = threading.Lock()  # Lock for accessing the locks dictionary
    
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
            from ..modules.accounts import get_account_class
            from .models import AccountDefinition
            
            # PHASE 1: Collect all orders to process WHILE session is open
            orders_to_submit = []  # List of (order_id, order_data, account_def, symbol, parent_id, trigger_status)
            status_updates = {}  # Map of order_id -> new_status for terminal status syncs
            
            with get_db() as session:
                # Get all orders that have dependencies (depends_on_order is not None)
                statement = select(TradingOrder).where(TradingOrder.depends_on_order.isnot(None))
                dependent_orders = session.exec(statement).all()
                
                self.logger.debug(f"Checking {len(dependent_orders)} orders with dependencies for triggering")
                
                for dependent_order in dependent_orders:
                    if dependent_order.status != OrderStatus.WAITING_TRIGGER:
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
                    
                    # Check if parent order is in a terminal status
                    terminal_statuses = OrderStatus.get_terminal_statuses()
                    if current_status in terminal_statuses:
                        # If parent is in terminal state, sync the dependent order to the same terminal status
                        self.logger.warning(
                            f"Parent order {parent_order_id} is in terminal status {current_status}. "
                            f"Syncing dependent order {dependent_order.id} from WAITING_TRIGGER to {current_status}"
                        )
                        status_updates[dependent_order.id] = current_status
                        continue
                    
                    # Check if parent order has reached the trigger status
                    if current_status == trigger_status:
                        self.logger.info(f"Order {parent_order_id} is in status {trigger_status}, triggering dependent order {dependent_order.id}")
                        
                        # Copy quantity from parent order if dependent order quantity is 0
                        if dependent_order.quantity == 0:
                            if parent_order.quantity > 0:
                                self.logger.info(
                                    f"Copying quantity {parent_order.quantity} from parent order {parent_order_id} "
                                    f"(symbol: {parent_order.symbol}) to dependent order {dependent_order.id} (symbol: {dependent_order.symbol})"
                                )
                                dependent_order.quantity = parent_order.quantity
                            else:
                                self.logger.error(
                                    f"Cannot submit dependent order {dependent_order.id} (symbol: {dependent_order.symbol}): "
                                    f"quantity is 0 and parent order {parent_order_id} (symbol: {parent_order.symbol}) "
                                    f"also has quantity 0. Setting dependent order to ERROR status."
                                )
                                status_updates[dependent_order.id] = OrderStatus.ERROR
                                continue
                        
                        # Get the account for this dependent order
                        account_def = session.get(AccountDefinition, dependent_order.account_id)
                        if not account_def:
                            self.logger.error(f"Account definition {dependent_order.account_id} not found for dependent order {dependent_order.id}")
                            continue
                        
                        # Double-check quantity one more time before adding to submit list
                        if dependent_order.quantity <= 0:
                            self.logger.error(
                                f"Dependent order {dependent_order.id} (symbol: {dependent_order.symbol}) "
                                f"still has invalid quantity {dependent_order.quantity}. "
                                f"Parent order {parent_order_id} (symbol: {parent_order.symbol}) quantity: {parent_order.quantity}. "
                                f"Setting to ERROR status."
                            )
                            status_updates[dependent_order.id] = OrderStatus.ERROR
                            continue
                        
                        # Add to submit list (all data needed is extracted - expunge dependent_order to reduce session load)
                        # Store a copy of order data since we'll lose session access after closing
                        order_copy = {
                            'id': dependent_order.id,
                            'side': dependent_order.side,
                            'quantity': dependent_order.quantity,
                            'symbol': dependent_order.symbol,
                            'order_type': dependent_order.order_type,
                            'account_id': dependent_order.account_id,
                            'account_def': account_def,
                        }
                        orders_to_submit.append((dependent_order, parent_order_id))
                
                # PHASE 1 COMPLETE: Session is still open, now apply any status-only updates
                for order_id, new_status in status_updates.items():
                    order_obj = session.get(TradingOrder, order_id)
                    if order_obj:
                        order_obj.status = new_status
                        session.add(order_obj)
                
                if status_updates:
                    session.commit()
                    self.logger.debug(f"Applied {len(status_updates)} status-only updates")
                # Session will close here
            
            # PHASE 2: Process all order submissions OUTSIDE of session context
            # This prevents the session from holding locks during broker API calls and writes
            submitted_count = 0
            for dependent_order, parent_order_id in orders_to_submit:
                try:
                    account_def = get_instance(AccountDefinition, dependent_order.account_id)
                    if not account_def:
                        self.logger.error(f"Account definition {dependent_order.account_id} not found for dependent order {dependent_order.id}")
                        # Update status to ERROR
                        dependent_order.status = OrderStatus.ERROR
                        update_instance(dependent_order)
                        continue
                    
                    account_class = get_account_class(account_def.provider)
                    if not account_class:
                        self.logger.error(f"Account provider {account_def.provider} not found for dependent order {dependent_order.id}")
                        dependent_order.status = OrderStatus.ERROR
                        update_instance(dependent_order)
                        continue
                    
                    account = account_class(account_def.id)
                    
                    self.logger.info(
                        f"Submitting dependent order {dependent_order.id}: {dependent_order.side.value} "
                        f"{dependent_order.quantity} {dependent_order.symbol} @ {dependent_order.order_type.value} "
                        f"(triggered by parent order {parent_order_id})"
                    )
                    submitted_order = account.submit_order(dependent_order)
                    
                    if submitted_order:
                        self.logger.info(f"Successfully submitted dependent order {dependent_order.id} triggered by parent order {parent_order_id}")
                        submitted_count += 1
                    else:
                        self.logger.error(
                            f"Failed to submit dependent order {dependent_order.id} (symbol: {dependent_order.symbol}) - "
                            f"setting to ERROR status"
                        )
                        dependent_order.status = OrderStatus.ERROR
                        update_instance(dependent_order)
                        
                except Exception as submit_error:
                    self.logger.error(
                        f"Exception submitting dependent order {dependent_order.id} (symbol: {dependent_order.symbol}, "
                        f"qty: {dependent_order.quantity}): {submit_error}",
                        exc_info=True
                    )
                    dependent_order.status = OrderStatus.ERROR
                    try:
                        update_instance(dependent_order)
                    except Exception as update_error:
                        self.logger.error(f"Could not update order {dependent_order.id} to ERROR status: {update_error}")
            
            if submitted_count > 0:
                self.logger.info(f"Triggered {submitted_count} dependent orders")
                
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
            from ..modules.accounts import get_account_class
            from .models import AccountDefinition
            
            # PHASE 1: Collect all orders to process WHILE session is open
            orders_to_submit = []  # List of (order, parent_order_id)
            status_updates = {}  # Map of order_id -> new_status
            
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
                
                for dependent_order in waiting_orders:
                    try:
                        parent_order_id = dependent_order.depends_on_order
                        trigger_status = dependent_order.depends_order_status_trigger
                        
                        # Get current parent order status
                        parent_order = session.get(TradingOrder, parent_order_id)
                        if not parent_order:
                            self.logger.warning(f"Parent order {parent_order_id} not found for dependent order {dependent_order.id} - setting to ERROR")
                            status_updates[dependent_order.id] = OrderStatus.ERROR
                            continue
                        
                        current_status = parent_order.status
                        
                        self.logger.debug(f"Checking order {dependent_order.id}: parent {parent_order_id} status is {current_status}, trigger is {trigger_status}")
                        
                        # Check if parent order is in a terminal status
                        terminal_statuses = OrderStatus.get_terminal_statuses()
                        if current_status in terminal_statuses:
                            # If parent is in terminal state, sync the dependent order to the same terminal status
                            self.logger.warning(
                                f"Parent order {parent_order_id} is in terminal status {current_status}. "
                                f"Syncing dependent order {dependent_order.id} from WAITING_TRIGGER to {current_status}"
                            )
                            status_updates[dependent_order.id] = current_status
                            continue
                        
                        # Check if parent order has reached the trigger status
                        if current_status == trigger_status:
                            self.logger.info(f"Parent order {parent_order_id} is in trigger status {trigger_status}, processing dependent order {dependent_order.id}")
                            
                            # Get the account for this dependent order
                            account_def = session.get(AccountDefinition, dependent_order.account_id)
                            if not account_def:
                                self.logger.error(f"Account definition {dependent_order.account_id} not found for dependent order {dependent_order.id} - setting to ERROR")
                                status_updates[dependent_order.id] = OrderStatus.ERROR
                                continue
                            
                            # Copy quantity from parent order if dependent order quantity is 0
                            if dependent_order.quantity == 0:
                                if parent_order.quantity > 0:
                                    self.logger.info(
                                        f"Copying quantity {parent_order.quantity} from parent order {parent_order_id} "
                                        f"(symbol: {parent_order.symbol}) to dependent order {dependent_order.id} (symbol: {dependent_order.symbol})"
                                    )
                                    dependent_order.quantity = parent_order.quantity
                                else:
                                    self.logger.error(
                                        f"Cannot submit dependent order {dependent_order.id} (symbol: {dependent_order.symbol}): "
                                        f"quantity is 0 and parent order {parent_order_id} (symbol: {parent_order.symbol}) "
                                        f"also has quantity 0. Setting dependent order to ERROR status."
                                    )
                                    status_updates[dependent_order.id] = OrderStatus.ERROR
                                    continue
                            
                            # ===== NEW: Recalculate TP/SL prices from stored percent if available =====
                            # This ensures TP/SL prices use the parent's filled price, not stale market data
                            # If percent is not stored, it will be calculated and stored as a fallback
                            transaction_updated = False
                            if dependent_order.data and isinstance(dependent_order.data, dict):
                                try:
                                    # Check if this is a TP order (has tp_percent in data)
                                    if "tp_percent" in dependent_order.data and parent_order.open_price:
                                        tp_percent = dependent_order.data.get("tp_percent")
                                        old_limit_price = dependent_order.limit_price
                                        
                                        # Recalculate TP price from parent's filled price: price = filled_price * (1 + percent/100)
                                        new_limit_price = parent_order.open_price * (1 + tp_percent / 100)
                                        
                                        # Round price to 4 decimal places (standard for forex/stocks)
                                        new_limit_price = round(new_limit_price, 4)
                                        
                                        # Update the limit price
                                        dependent_order.limit_price = new_limit_price
                                        
                                        self.logger.info(
                                            f"Recalculated TP price for order {dependent_order.id}: "
                                            f"parent filled ${parent_order.open_price:.2f} * (1 + {tp_percent:.2f}%) "
                                            f"= ${new_limit_price:.2f} (was ${old_limit_price:.2f})"
                                        )
                                        
                                        # Update data field to record when recalculation happened
                                        dependent_order.data["parent_filled_price"] = parent_order.open_price
                                        dependent_order.data["recalculated_at_trigger"] = True
                                        
                                        # Mark transaction for update with new TP price
                                        transaction_updated = True
                                    
                                    # Check if this is an SL order (has sl_percent in data)
                                    elif "sl_percent" in dependent_order.data and parent_order.open_price:
                                        sl_percent = dependent_order.data.get("sl_percent")
                                        old_stop_price = dependent_order.stop_price
                                        
                                        # Recalculate SL price from parent's filled price: price = filled_price * (1 + percent/100)
                                        # For SL, percent is typically negative, so 1 + (-5/100) = 0.95 for a 5% loss
                                        new_stop_price = parent_order.open_price * (1 + sl_percent / 100)
                                        
                                        # Round price to 4 decimal places
                                        new_stop_price = round(new_stop_price, 4)
                                        
                                        # Update the stop price
                                        dependent_order.stop_price = new_stop_price
                                        
                                        self.logger.info(
                                            f"Recalculated SL price for order {dependent_order.id}: "
                                            f"parent filled ${parent_order.open_price:.2f} * (1 + {sl_percent:.2f}%) "
                                            f"= ${new_stop_price:.2f} (was ${old_stop_price:.2f})"
                                        )
                                        
                                        # Update data field to record when recalculation happened
                                        dependent_order.data["parent_filled_price"] = parent_order.open_price
                                        dependent_order.data["recalculated_at_trigger"] = True
                                        
                                        # Mark transaction for update with new SL price
                                        transaction_updated = True
                                    
                                    else:
                                        # No tp_percent or sl_percent in data - try to calculate as fallback
                                        # This handles cases where TP/SL orders were created before percent storage was implemented
                                        self.logger.debug(f"No TP/SL percent found in order {dependent_order.id}.data, attempting fallback calculation")
                                        # Note: We can't call AccountInterface method here, but the calculation will happen
                                        # when the account's submit_order is called in PHASE 2 below
                                
                                except (KeyError, TypeError, ValueError) as data_error:
                                    self.logger.warning(
                                        f"Could not recalculate TP/SL price for order {dependent_order.id} from data field: {data_error}"
                                    )
                            else:
                                # No data field yet - will be populated when account submits the order
                                self.logger.debug(f"No data field in order {dependent_order.id}, will ensure percent is calculated during submission")
                            # ===== END: Price recalculation =====
                            
                            # Update the associated Transaction if TP/SL price was recalculated
                            if transaction_updated and dependent_order.transaction_id:
                                transaction = session.get(Transaction, dependent_order.transaction_id)
                                if transaction:
                                    # Update TP or SL price depending on order type
                                    if "tp_percent" in dependent_order.data:
                                        transaction.take_profit = dependent_order.limit_price
                                        self.logger.info(f"Updated Transaction {dependent_order.transaction_id} take_profit to ${dependent_order.limit_price:.2f}")
                                    elif "sl_percent" in dependent_order.data:
                                        transaction.stop_loss = dependent_order.stop_price
                                        self.logger.info(f"Updated Transaction {dependent_order.transaction_id} stop_loss to ${dependent_order.stop_price:.2f}")
                                    session.add(transaction)
                            
                            # Double-check quantity one more time before adding to submit list
                            if dependent_order.quantity <= 0:
                                self.logger.error(
                                    f"Dependent order {dependent_order.id} (symbol: {dependent_order.symbol}) "
                                    f"still has invalid quantity {dependent_order.quantity}. "
                                    f"Parent order {parent_order_id} (symbol: {parent_order.symbol}) quantity: {parent_order.quantity}. "
                                    f"Setting to ERROR status."
                                )
                                status_updates[dependent_order.id] = OrderStatus.ERROR
                                continue
                            
                            # Add to submit list
                            orders_to_submit.append((dependent_order, parent_order_id))
                    
                    except Exception as order_error:
                        self.logger.error(f"Error processing waiting order {dependent_order.id}: {order_error}", exc_info=True)
                        status_updates[dependent_order.id] = OrderStatus.ERROR
                
                # PHASE 1 COMPLETE: Session is still open, now apply any status-only updates
                for order_id, new_status in status_updates.items():
                    order_obj = session.get(TradingOrder, order_id)
                    if order_obj:
                        order_obj.status = new_status
                        session.add(order_obj)
                
                if status_updates:
                    session.commit()
                    self.logger.debug(f"Applied {len(status_updates)} status-only updates")
                # Session will close here
            
            # PHASE 2: Process all order submissions OUTSIDE of session context
            submitted_count = 0
            for dependent_order, parent_order_id in orders_to_submit:
                try:
                    account_def = get_instance(AccountDefinition, dependent_order.account_id)
                    if not account_def:
                        self.logger.error(f"Account definition {dependent_order.account_id} not found for dependent order {dependent_order.id}")
                        dependent_order.status = OrderStatus.ERROR
                        update_instance(dependent_order)
                        continue
                    
                    account_class = get_account_class(account_def.provider)
                    if not account_class:
                        self.logger.error(f"Account provider {account_def.provider} not found for dependent order {dependent_order.id}")
                        dependent_order.status = OrderStatus.ERROR
                        update_instance(dependent_order)
                        continue
                    
                    account = account_class(account_def.id)
                    
                    self.logger.info(
                        f"Submitting dependent order {dependent_order.id}: {dependent_order.side.value} "
                        f"{dependent_order.quantity} {dependent_order.symbol} @ {dependent_order.order_type.value} "
                        f"(triggered by parent order {parent_order_id})"
                    )
                    submitted_order = account.submit_order(dependent_order)
                    
                    if submitted_order:
                        self.logger.info(f"Successfully submitted dependent order {dependent_order.id}")
                        submitted_count += 1
                    else:
                        self.logger.error(
                            f"Failed to submit dependent order {dependent_order.id} (symbol: {dependent_order.symbol}) - "
                            f"setting to ERROR status"
                        )
                        dependent_order.status = OrderStatus.ERROR
                        update_instance(dependent_order)
                        
                except Exception as submit_error:
                    self.logger.error(
                        f"Exception submitting dependent order {dependent_order.id} (symbol: {dependent_order.symbol}, "
                        f"qty: {dependent_order.quantity}): {submit_error}",
                        exc_info=True
                    )
                    dependent_order.status = OrderStatus.ERROR
                    try:
                        update_instance(dependent_order)
                    except Exception as update_error:
                        self.logger.error(f"Could not update order {dependent_order.id} to ERROR status: {update_error}")
            
            if submitted_count > 0:
                self.logger.info(f"Processed {submitted_count} waiting trigger orders")
                
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
                
            # Note: Ruleset evaluation is handled by TradeActionEvaluator in process_expert_recommendations_after_analysis()
            # This method (_process_recommendation) is a legacy path and should eventually be deprecated
                
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
                    # NOTE: do not import get_instance here - an inner import previously created a
                    # local variable shadowing the module-level `get_instance`, which caused an
                    # UnboundLocalError at runtime when exception handling referenced it. Using
                    # the already-imported `get_instance` from the module scope avoids that bug.
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
        
        This method uses thread-safe locking to ensure only one thread processes recommendations
        for a given expert/use_case at a time. If the lock cannot be acquired within 0.5 seconds,
        the method returns immediately (another thread is already processing).
        
        Args:
            expert_instance_id: The expert instance ID to process recommendations for
            lookback_days: Number of days to look back for recommendations (default: 1)
            
        Returns:
            List of TradingOrder objects that were created (in PENDING state)
        """
        # Use enter_market as the use case for this method (it only handles enter_market)
        lock_key = f"expert_{expert_instance_id}_usecase_enter_market"
        
        # Get or create a lock for this expert/use_case combination
        with self._locks_dict_lock:
            if lock_key not in self._processing_locks:
                self._processing_locks[lock_key] = threading.Lock()
            processing_lock = self._processing_locks[lock_key]
        
        # Try to acquire the lock with a very short timeout (0.5 seconds)
        # If we can't get it, another thread is already processing this expert
        lock_acquired = processing_lock.acquire(blocking=True, timeout=0.5)
        
        if not lock_acquired:
            self.logger.info(f"Could not acquire lock for expert {expert_instance_id} (enter_market) - another thread is already processing. Skipping.")
            return []
        
        # We have the lock - make sure we release it when done
        created_orders = []
        
        try:
            self.logger.debug(f"Acquired processing lock for expert {expert_instance_id} (enter_market)")
            
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
                            
                            # Store evaluation details even when no actions are created
                            # This is crucial for analysis, debugging, and understanding why rules didn't trigger
                            evaluation_details = evaluator.get_evaluation_details()
                            if evaluation_details:
                                from ..core.models import TradeActionResult
                                from ..core.db import add_instance
                                
                                evaluation_result = TradeActionResult(
                                    action_type='evaluation_only',
                                    success=True,  # Evaluation succeeded, just no actions needed
                                    message=f'Rule evaluation completed for {recommendation.symbol} - no actions triggered (conditions not met)',
                                    data={'evaluation_details': evaluation_details},
                                    expert_recommendation_id=recommendation.id
                                )
                                add_instance(evaluation_result)
                                self.logger.debug(f"Stored evaluation details for recommendation {recommendation.id} (no actions)")
                            
                            continue
                        
                        # Check for evaluation errors
                        if any('error' in summary for summary in action_summaries):
                            errors = [s.get('error') for s in action_summaries if 'error' in s]
                            self.logger.warning(f"Recommendation {recommendation.id} evaluation had errors: {errors}")
                            
                            # Store evaluation details even when errors occurred
                            evaluation_details = evaluator.get_evaluation_details()
                            if evaluation_details:
                                from ..core.models import TradeActionResult
                                from ..core.db import add_instance
                                
                                evaluation_result = TradeActionResult(
                                    action_type='evaluation_error',
                                    success=False,
                                    message=f'Rule evaluation encountered errors for {recommendation.symbol}: {"; ".join(errors)}',
                                    data={'evaluation_details': evaluation_details, 'errors': errors},
                                    expert_recommendation_id=recommendation.id
                                )
                                add_instance(evaluation_result)
                                self.logger.debug(f"Stored evaluation details for recommendation {recommendation.id} (errors)")
                            
                            continue
                        
                        self.logger.info(f"Recommendation {recommendation.id} for {recommendation.symbol} passed ruleset - {len(action_summaries)} action(s) to execute")
                        
                        # SAFETY CHECK: For enter_market, check if there's already an open/waiting transaction
                        # for this symbol and expert to prevent duplicate positions
                        existing_txn_statement = select(Transaction).where(
                            Transaction.expert_id == expert_instance_id,
                            Transaction.symbol == recommendation.symbol,
                            Transaction.status.in_([TransactionStatus.OPENED, TransactionStatus.WAITING])
                        )
                        existing_txn = session.exec(existing_txn_statement).first()
                        
                        if existing_txn:
                            self.logger.warning(
                                f"SAFETY CHECK: Skipping recommendation {recommendation.id} for {recommendation.symbol} - "
                                f"existing transaction {existing_txn.id} in {existing_txn.status} status for expert {expert_instance_id}"
                            )
                            continue
                        
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
        finally:
            # Always release the lock when we're done
            processing_lock.release()
            self.logger.debug(f"Released processing lock for expert {expert_instance_id} (enter_market)")
        
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
    
    def clean_pending_orders(self) -> Dict[str, Any]:
        """
        Clean unsubmitted pending and error orders (PENDING and ERROR only).
        
        CRITICAL: Do NOT delete WAITING_TRIGGER orders unless their parent order is ALSO being deleted.
        This prevents orphaning valid take-profit/stop-loss orders on existing open positions.
        
        For each PENDING/ERROR order:
        1. Find the associated transaction (if any)
        2. Close the transaction if it exists
        3. Delete the order and ONLY its dependent orders that are WAITING_TRIGGER
           (if the parent is being deleted, the dependent is also deleted)
        
        WAITING_TRIGGER orders whose parents are NOT being deleted are PRESERVED.
        
        Returns:
            Dict with cleanup statistics:
            {
                'orders_deleted': int,
                'transactions_closed': int,
                'dependents_deleted': int,
                'errors': List[str]
            }
        """
        try:
            from sqlmodel import select, Session
            from .db import get_db
            from .models import Transaction, TradingOrder
            from .types import TransactionStatus, OrderStatus
            
            stats = {
                'orders_deleted': 0,
                'transactions_closed': 0,
                'dependents_deleted': 0,
                'errors': []
            }
            
            self.logger.info("Starting cleanup of pending orders (PENDING and ERROR only - preserving valid WAITING_TRIGGER orders)...")
            
            with Session(get_db().bind) as session:
                # CRITICAL: Only clean PENDING and ERROR orders - NOT WAITING_TRIGGER
                # WAITING_TRIGGER orders are preserved unless their parent order is also being deleted
                pending_statuses = [OrderStatus.PENDING, OrderStatus.ERROR]
                statement = select(TradingOrder).where(
                    TradingOrder.status.in_(pending_statuses)
                )
                pending_orders = session.exec(statement).all()
                
                self.logger.info(f"Found {len(pending_orders)} PENDING/ERROR orders to clean")
                
                # Create a set of order IDs being deleted for quick lookup
                orders_to_delete_ids = {order.id for order in pending_orders}
                
                # Track transactions to close
                transactions_to_close = set()
                orders_to_delete = []
                dependents_to_delete = []
                
                for order in pending_orders:
                    # Track associated transaction
                    if order.transaction_id:
                        transactions_to_close.add(order.transaction_id)
                        self.logger.debug(f"Order {order.id} linked to transaction {order.transaction_id}")
                    
                    # Find all dependent orders (orders that depend on this order)
                    dependent_statement = select(TradingOrder).where(
                        TradingOrder.depends_on_order == order.id
                    )
                    dependents = session.exec(dependent_statement).all()
                    
                    if dependents:
                        self.logger.debug(f"Order {order.id} has {len(dependents)} dependent orders")
                        # Only delete dependents - don't preserve WAITING_TRIGGER orders if their parent is deleted
                        dependents_to_delete.extend(dependents)
                    
                    orders_to_delete.append(order)
                
                # PHASE 1: Close transactions
                for txn_id in transactions_to_close:
                    try:
                        txn = session.get(Transaction, txn_id)
                        if txn:
                            # Close the transaction
                            txn.status = TransactionStatus.CLOSED
                            txn.close_date = datetime.now(timezone.utc)
                            session.add(txn)
                            self.logger.info(f"Marked transaction {txn_id} as CLOSED")
                            stats['transactions_closed'] += 1
                        else:
                            error_msg = f"Transaction {txn_id} not found"
                            self.logger.warning(error_msg)
                            stats['errors'].append(error_msg)
                    except Exception as e:
                        error_msg = f"Error closing transaction {txn_id}: {e}"
                        self.logger.error(error_msg)
                        stats['errors'].append(error_msg)
                
                # PHASE 2: Delete dependent orders
                # CRITICAL SAFETY CHECK: Only delete dependents if their parent is being deleted
                # This prevents orphaning valid TP/SL orders on existing open positions
                for dependent_order in dependents_to_delete:
                    try:
                        # Verify the parent order is actually being deleted
                        parent_order_id = dependent_order.depends_on_order
                        if parent_order_id not in orders_to_delete_ids:
                            # Parent is NOT being deleted - PRESERVE this dependent order
                            self.logger.debug(
                                f"Skipping dependent order {dependent_order.id} "
                                f"(parent order {parent_order_id} is not being deleted - preserving valid order)"
                            )
                            continue
                        
                        session.delete(dependent_order)
                        self.logger.debug(f"Deleted dependent order {dependent_order.id}")
                        stats['dependents_deleted'] += 1
                    except Exception as e:
                        error_msg = f"Error deleting dependent order {dependent_order.id}: {e}"
                        self.logger.error(error_msg)
                        stats['errors'].append(error_msg)
                
                # PHASE 3: Delete main orders
                for order in orders_to_delete:
                    try:
                        session.delete(order)
                        self.logger.debug(f"Deleted pending order {order.id} (symbol: {order.symbol}, status: {order.status})")
                        stats['orders_deleted'] += 1
                    except Exception as e:
                        error_msg = f"Error deleting order {order.id}: {e}"
                        self.logger.error(error_msg)
                        stats['errors'].append(error_msg)
                
                # Commit all changes
                try:
                    session.commit()
                    self.logger.info(
                        f"Cleanup completed: deleted {stats['orders_deleted']} orders, "
                        f"{stats['dependents_deleted']} dependents, "
                        f"closed {stats['transactions_closed']} transactions"
                    )
                except Exception as e:
                    error_msg = f"Error committing cleanup: {e}"
                    self.logger.error(error_msg)
                    stats['errors'].append(error_msg)
                    session.rollback()
            
            return stats
            
        except Exception as e:
            self.logger.error(f"Error during pending order cleanup: {e}", exc_info=True)
            return {
                'orders_deleted': 0,
                'transactions_closed': 0,
                'dependents_deleted': 0,
                'errors': [str(e)]
            }


# Global trade manager instance
_trade_manager = None

def get_trade_manager() -> TradeManager:
    """Get the global trade manager instance."""
    global _trade_manager
    if _trade_manager is None:
        _trade_manager = TradeManager()
    return _trade_manager