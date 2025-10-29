"""
TradeRiskManagement - Risk management system for automated trading

This module implements comprehensive risk management for pending orders,
including profit-based prioritization, position sizing, and diversification.
"""

from typing import Dict, List, Optional, Tuple, Any, TYPE_CHECKING
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
import logging

from .interfaces import AccountInterface
from ..logger import logger
from .models import TradingOrder, ExpertRecommendation, ExpertInstance, Transaction
from .types import OrderStatus, OrderDirection, TransactionStatus
from .db import get_instance, get_all_instances, update_instance, get_db
from sqlmodel import select, Session

if TYPE_CHECKING:
    from .interfaces import MarketExpertInterface


class TradeRiskManagement:
    """
    Risk management system for automated trading orders.
    
    Responsibilities:
    - Review pending orders with quantity=0
    - Prioritize orders based on expected profit
    - Calculate appropriate position sizes
    - Ensure diversification and risk limits
    - Update orders with calculated quantities
    """
    
    def __init__(self):
        """Initialize the risk management system."""
        # Use the parent logger directly instead of getChild to avoid double logging
        # The parent logger already has all necessary handlers configured
        self.logger = logger
    
    def review_and_prioritize_pending_orders(self, expert_instance_id: int) -> List[TradingOrder]:
        """
        Review and prioritize pending orders for an expert instance.
        
        This method processes all pending orders with quantity=0, calculates appropriate
        quantities based on profit potential, risk limits, and diversification requirements.
        
        Args:
            expert_instance_id: The expert instance ID to process orders for
            
        Returns:
            List of TradingOrder objects that were updated with quantities
        """
        updated_orders = []
        
        try:
            from .utils import get_expert_instance_from_id
            
            # Get the expert instance with settings
            expert = get_expert_instance_from_id(expert_instance_id)
            if not expert:
                error_msg = f"Expert instance {expert_instance_id} not found"
                self.logger.error(error_msg, exc_info=True)
                raise ValueError(error_msg)
            
            # Get the expert instance model
            expert_instance = get_instance(ExpertInstance, expert_instance_id)
            if not expert_instance:
                error_msg = f"Expert instance model {expert_instance_id} not found"
                self.logger.error(error_msg, exc_info=True)
                raise ValueError(error_msg)
            
            # Check if automated trade opening is enabled
            allow_automated_trade_opening = expert.settings.get('allow_automated_trade_opening', False)
            if not allow_automated_trade_opening:
                self.logger.debug(f"Automated trade opening disabled for expert {expert_instance_id}, skipping risk management")
                return updated_orders
            
            # Get expert trading permissions
            enable_buy = expert.settings.get('enable_buy', True)
            enable_sell = expert.settings.get('enable_sell', False)
            
            # Get virtual equity per instrument limit (default 10%)
            max_virtual_equity_per_instrument_percent = expert.settings.get('max_virtual_equity_per_instrument_percent', 10.0)
            if max_virtual_equity_per_instrument_percent is None:
                self.logger.warning(f"Expert {expert_instance_id} has no max_virtual_equity_per_instrument_percent setting, defaulting to 10.0%")
                max_virtual_equity_per_instrument_percent = 10.0
            max_equity_per_instrument_ratio = max_virtual_equity_per_instrument_percent / 100.0
            
            self.logger.info(f"Starting risk management for expert {expert_instance_id}: "
                           f"buy={enable_buy}, sell={enable_sell}, "
                           f"max_per_instrument={max_virtual_equity_per_instrument_percent}%")
            self.logger.debug(f"Expert settings retrieved: allow_automated_trade_opening={allow_automated_trade_opening}")
            
            # Step 1: Collect pending orders with quantity=0
            pending_orders = self._get_pending_orders_for_review(expert_instance_id)
            if not pending_orders:
                self.logger.info(f"No pending orders found for expert {expert_instance_id}")
                return updated_orders
            
            # Step 2: Filter orders based on expert permissions
            filtered_orders = self._filter_orders_by_permissions(pending_orders, enable_buy, enable_sell)
            if not filtered_orders:
                self.logger.info(f"No orders remain after permission filtering for expert {expert_instance_id}")
                return updated_orders
            
            # Step 3: Get linked recommendations with profit data
            orders_with_recommendations = self._get_orders_with_recommendations(filtered_orders)
            if not orders_with_recommendations:
                self.logger.warning(f"No orders with valid recommendations found for expert {expert_instance_id}")
                return updated_orders
            
            # Step 4: Sort orders by expected profit (descending)
            prioritized_orders = self._prioritize_orders_by_profit(orders_with_recommendations)
            
            # Step 5: Get available balance from expert interface
            available_balance = expert.get_available_balance()
            if available_balance is None:
                error_msg = f"Could not get available balance for expert {expert_instance_id}"
                self.logger.error(error_msg, exc_info=True)
                raise RuntimeError(error_msg)
            
            # Use available balance for calculations
            total_virtual_balance = available_balance
            max_equity_per_instrument = total_virtual_balance * max_equity_per_instrument_ratio
            
            self.logger.info(f"Virtual balance: ${total_virtual_balance:.2f}, "
                           f"max per instrument: ${max_equity_per_instrument:.2f} "
                           f"(ratio: {max_equity_per_instrument_ratio:.3f})")
            
            # Step 6: Get account instance for price lookups
            from .utils import get_account_instance_from_id
            account = get_account_instance_from_id(expert_instance.account_id)
            if not account:
                error_msg = f"Account {expert_instance.account_id} not found for expert {expert_instance_id}"
                self.logger.error(error_msg, exc_info=True)
                raise ValueError(error_msg)
            
            # Step 7: Get existing positions to account for current allocations
            existing_allocations = self._get_existing_allocations(expert_instance_id)
            self.logger.debug(f"Existing allocations for expert {expert_instance_id}: {existing_allocations}")
            
            # Step 8: Calculate quantities for prioritized orders
            orders_to_update, orders_to_delete = self._calculate_order_quantities(
                prioritized_orders,
                total_virtual_balance,
                max_equity_per_instrument,
                existing_allocations,
                account,
                expert
            )
            
            # Step 9: Update orders in database
            self._update_orders_in_database(orders_to_update)
            
            # Step 10: Delete orders with quantity=0 if automated trade opening is enabled
            if allow_automated_trade_opening and orders_to_delete:
                self._delete_unfunded_orders(orders_to_delete)
            
            updated_orders = orders_to_update  # For return value compatibility
            
            self.logger.info(f"Risk management completed for expert {expert_instance_id}: "
                           f"updated {len(updated_orders)} orders, deleted {len(orders_to_delete) if orders_to_delete else 0} unfunded orders")
            
            # Log activity for risk manager execution
            try:
                from .db import log_activity
                from .types import ActivityLogSeverity, ActivityLogType
                
                log_activity(
                    severity=ActivityLogSeverity.SUCCESS,
                    activity_type=ActivityLogType.RISK_MANAGER_RAN,
                    description=f"Classic risk manager processed {len(updated_orders)} orders",
                    data={
                        "mode": "classic",
                        "orders_updated": len(updated_orders),
                        "orders_deleted": len(orders_to_delete) if orders_to_delete else 0,
                        "available_balance": total_virtual_balance,
                        "max_per_instrument": max_equity_per_instrument
                    },
                    source_expert_id=expert_instance_id,
                    source_account_id=expert_instance.account_id
                )
            except Exception as log_error:
                self.logger.warning(f"Failed to log risk manager activity: {log_error}")
            
        except Exception as e:
            self.logger.error(f"Error in risk management for expert {expert_instance_id}: {e}", exc_info=True)
            
            # Log activity for risk manager failure
            try:
                from .db import log_activity, get_instance
                from .types import ActivityLogSeverity, ActivityLogType
                from .models import ExpertInstance
                
                expert_instance = get_instance(ExpertInstance, expert_instance_id)
                
                log_activity(
                    severity=ActivityLogSeverity.FAILURE,
                    activity_type=ActivityLogType.RISK_MANAGER_RAN,
                    description=f"Classic risk manager failed: {str(e)}",
                    data={
                        "mode": "classic",
                        "error": str(e)
                    },
                    source_expert_id=expert_instance_id,
                    source_account_id=expert_instance.account_id if expert_instance else None
                )
            except Exception as log_error:
                self.logger.warning(f"Failed to log risk manager failure activity: {log_error}")
            
            raise  # Re-raise the exception to allow UI to handle it
        
        return updated_orders
    
    def _get_pending_orders_for_review(self, expert_instance_id: int) -> List[TradingOrder]:
        """Get all pending orders with quantity=0 for an expert."""
        try:
            with get_db() as session:
                # First get all pending orders with quantity=0 and expert_recommendation_id
                statement = select(TradingOrder).where(
                    TradingOrder.status == OrderStatus.PENDING,
                    TradingOrder.expert_recommendation_id.is_not(None)
                )
                
                all_pending_orders = session.exec(statement).all()
                
                # Filter orders that belong to this expert by checking their recommendations
                # Note: session.exec().all() returns tuples, so we need to unpack them
                expert_orders = []
                seen_order_ids = set()  # Track order IDs to prevent duplicates
                
                for order_tuple in all_pending_orders:
                    # Properly unpack tuple - SQLModel exec().all() always returns tuples
                    order = order_tuple[0] if isinstance(order_tuple, tuple) else order_tuple
                    if order and order.expert_recommendation_id:
                        # Skip if we've already seen this order ID
                        if order.id in seen_order_ids:
                            self.logger.warning(f"Skipping duplicate order {order.id} for {order.symbol}")
                            continue
                        
                        recommendation = session.get(ExpertRecommendation, order.expert_recommendation_id)
                        if recommendation and recommendation.instance_id == expert_instance_id:
                            expert_orders.append(order)
                            seen_order_ids.add(order.id)
                            self.logger.debug(f"Found pending order {order.id} for {order.symbol} (side: {order.side.value})")
                
                self.logger.info(f"Found {len(expert_orders)} pending orders for review for expert {expert_instance_id}")
                return expert_orders
                
        except Exception as e:
            self.logger.error(f"Error getting pending orders for expert {expert_instance_id}: {e}", exc_info=True)
            return []
    
    def _filter_orders_by_permissions(self, orders: List[TradingOrder], enable_buy: bool, enable_sell: bool) -> List[TradingOrder]:
        """Filter orders based on expert buy/sell permissions."""
        filtered_orders = []
        
        for order in orders:
            if order.side == OrderDirection.BUY and enable_buy:
                filtered_orders.append(order)
                self.logger.debug(f"Including BUY order {order.id} for {order.symbol}")
            elif order.side == OrderDirection.SELL and enable_sell:
                filtered_orders.append(order)
                self.logger.debug(f"Including SELL order {order.id} for {order.symbol}")
            else:
                self.logger.debug(f"Skipping order {order.id} - {order.side.value} not enabled for {order.symbol}")
        
        self.logger.info(f"Filtered {len(filtered_orders)} orders from {len(orders)} based on permissions")
        return filtered_orders
    
    def _get_orders_with_recommendations(self, orders: List[TradingOrder]) -> List[Tuple[TradingOrder, ExpertRecommendation]]:
        """Get orders with their linked recommendations."""
        orders_with_recommendations = []
        
        try:
            with get_db() as session:
                for order in orders:
                    if order.expert_recommendation_id:
                        recommendation = session.get(ExpertRecommendation, order.expert_recommendation_id)
                        if recommendation and recommendation.expected_profit_percent is not None:
                            orders_with_recommendations.append((order, recommendation))
                        else:
                            self.logger.debug(f"Order {order.id} has no valid recommendation or profit data")
                    else:
                        self.logger.debug(f"Order {order.id} has no linked recommendation")
        
        except Exception as e:
            self.logger.error(f"Error getting recommendations for orders: {e}", exc_info=True)
        
        self.logger.debug(f"Found {len(orders_with_recommendations)} orders with valid recommendations")
        return orders_with_recommendations
    
    def _prioritize_orders_by_profit(self, orders_with_recommendations: List[Tuple[TradingOrder, ExpertRecommendation]]) -> List[Tuple[TradingOrder, ExpertRecommendation]]:
        """Sort orders by expected profit percentage (descending)."""
        try:
            # Sort by expected profit percentage, highest first
            prioritized = sorted(
                orders_with_recommendations,
                key=lambda x: x[1].expected_profit_percent or 0.0,
                reverse=True
            )
            
            self.logger.debug(f"Prioritized {len(prioritized)} orders by profit potential")
            for i, (order, rec) in enumerate(prioritized[:5]):  # Log top 5
                self.logger.debug(f"#{i+1}: {order.symbol} - {rec.expected_profit_percent:.2f}% profit")
            
            return prioritized
            
        except Exception as e:
            self.logger.error(f"Error prioritizing orders by profit: {e}", exc_info=True)
            return orders_with_recommendations
    
    def _get_existing_allocations(self, expert_instance_id: int) -> Dict[str, float]:
        """Get existing allocations per instrument for the expert."""
        allocations = {}
        
        try:
            with get_db() as session:
                # Get existing transactions for this expert that are still open
                statement = select(Transaction).where(
                    Transaction.expert_id == expert_instance_id,
                    Transaction.status.in_([TransactionStatus.WAITING, TransactionStatus.OPENED])
                )
                
                transactions = session.exec(statement).all()
                
                for transaction in transactions:
                    symbol = transaction.symbol
                    # Use open_price for allocation calculation since current_price is not stored
                    # This represents the capital allocated when the position was opened
                    if transaction.open_price and transaction.quantity:
                        current_value = abs(transaction.quantity * transaction.open_price)
                    else:
                        # For WAITING transactions, use a nominal value
                        current_value = 0
                    
                    if symbol in allocations:
                        allocations[symbol] += current_value
                    else:
                        allocations[symbol] = current_value
                
                self.logger.debug(f"Found existing allocations for expert {expert_instance_id}: {allocations}")
                
        except Exception as e:
            self.logger.error(f"Error getting existing allocations for expert {expert_instance_id}: {e}", exc_info=True)
            raise  # Re-raise to propagate error to caller
        
        return allocations
    
    def _calculate_order_quantities(
        self,
        prioritized_orders: List[Tuple[TradingOrder, ExpertRecommendation]],
        total_virtual_balance: float,
        max_equity_per_instrument: float,
        existing_allocations: Dict[str, float],
        account: AccountInterface,
        expert: 'MarketExpertInterface'
    ) -> List[TradingOrder]:
        """Calculate appropriate quantities for each order.
        
        Args:
            prioritized_orders: List of (order, recommendation) tuples sorted by profit
            total_virtual_balance: Total available balance for trading
            max_equity_per_instrument: Maximum allocation per instrument
            existing_allocations: Dict of symbol -> allocated amount
            account: Account instance for fetching current prices
            expert: Expert instance for accessing instrument weight configurations
        """
        updated_orders = []
        remaining_balance = total_virtual_balance
        instrument_allocations = existing_allocations.copy()
        
        # Get instrument weight configurations from expert settings
        instrument_configs = expert._get_enabled_instruments_config()
        self.logger.debug(f"Retrieved instrument weight configurations: {len(instrument_configs)} instruments")
        
        # Group orders by symbol for diversity calculation
        orders_by_symbol = {}
        for order, recommendation in prioritized_orders:
            symbol = order.symbol
            if symbol not in orders_by_symbol:
                orders_by_symbol[symbol] = []
            orders_by_symbol[symbol].append((order, recommendation))
        
        # Identify top 3 ROI recommendations for special handling
        top_3_roi = prioritized_orders[:3]
        top_3_symbols = {order.symbol for order, _ in top_3_roi}
        
        self.logger.info(f"Processing {len(prioritized_orders)} orders across {len(orders_by_symbol)} instruments")
        self.logger.info(f"Top 3 ROI symbols: {top_3_symbols}")
        self.logger.debug(f"Available balance: ${remaining_balance:.2f}, Max per instrument: ${max_equity_per_instrument:.2f}")
        
        # Fetch all prices at once (bulk fetching)
        all_symbols = list(set(order.symbol for order, _ in prioritized_orders))
        self.logger.debug(f"Fetching prices for {len(all_symbols)} symbols in bulk")
        symbol_prices = account.get_instrument_current_price(all_symbols)
        self.logger.info(f"Bulk fetched {len(symbol_prices)} prices in single API call")
        
        for order, recommendation in prioritized_orders:
            try:
                symbol = order.symbol
                current_allocation = instrument_allocations.get(symbol, 0.0)
                available_for_instrument = max_equity_per_instrument - current_allocation
                
                # Get current market price from bulk-fetched prices
                current_price = symbol_prices.get(symbol) if symbol_prices else None
                if current_price is None:
                    self.logger.error(f"Could not get current price for {symbol}, skipping order {order.id}")
                    order.quantity = 0
                    updated_orders.append(order)
                    continue
                
                # Log calculation inputs
                self.logger.info(f"Order {order.id} ({symbol}) - Calculating quantity:")
                self.logger.info(f"  Inputs: price=${current_price:.2f}, remaining_balance=${remaining_balance:.2f}, "
                               f"max_per_instrument=${max_equity_per_instrument:.2f}, "
                               f"current_allocation=${current_allocation:.2f}, "
                               f"available_for_instrument=${available_for_instrument:.2f}")
                self.logger.debug(f"  ROI={recommendation.expected_profit_percent:.2f}%")
                
                # Calculate maximum affordable quantity based on available equity per instrument
                max_quantity_by_instrument = max(0, available_for_instrument / current_price) if current_price > 0 else 0
                
                # Calculate maximum affordable quantity based on remaining total balance
                max_quantity_by_balance = max(0, remaining_balance / current_price) if current_price > 0 else 0
                
                self.logger.info(f"  Calculated: max_qty_by_instrument={max_quantity_by_instrument:.2f} shares, "
                               f"max_qty_by_balance={max_quantity_by_balance:.2f} shares")
                
                # Special case: Top 3 ROI exception
                if (symbol in top_3_symbols and 
                    current_price > max_equity_per_instrument and 
                    current_price <= remaining_balance and
                    current_allocation == 0):
                    
                    # Allow single share purchase for high-value, high-ROI instruments
                    quantity = 1
                    total_cost = quantity * current_price
                    
                    self.logger.info(f"  TOP 3 ROI EXCEPTION for {symbol}: allocating ${total_cost:.2f} "
                                   f"(exceeds per-instrument limit but within total balance)")
                
                else:
                    # Standard allocation logic
                    if available_for_instrument <= 0:
                        quantity = 0
                        self.logger.info(f"  Result: quantity=0 (no available equity for {symbol}, limit exceeded)")
                    elif remaining_balance <= current_price:
                        quantity = 0
                        self.logger.info(f"  Result: quantity=0 (insufficient remaining balance: ${remaining_balance:.2f} < ${current_price:.2f})")
                    else:
                        # Use the minimum of the two constraints
                        max_quantity = min(max_quantity_by_instrument, max_quantity_by_balance)
                        self.logger.info(f"  Using min of constraints: max_quantity={max_quantity:.2f} shares")
                        
                        # For diversification, prefer smaller positions when multiple instruments available
                        num_remaining_instruments = len([s for s in orders_by_symbol.keys() 
                                                       if instrument_allocations.get(s, 0) < max_equity_per_instrument])
                        
                        if num_remaining_instruments > 1:
                            # Reserve some balance for other instruments
                            diversification_factor = 0.7  # Use 70% of available, save 30% for others
                            original_max = max_quantity
                            max_quantity *= diversification_factor
                            self.logger.info(f"  Diversification ({num_remaining_instruments} remaining instruments): "
                                          f"applied factor 0.7: {original_max:.2f} -> {max_quantity:.2f} shares")
                        
                        # First rounding: float to int
                        quantity = max(0, int(max_quantity))
                        self.logger.info(f"  First rounding: {max_quantity:.2f} -> {quantity} shares (int conversion)")
                        
                        # Ensure we don't allocate less than 1 share unless we can't afford it
                        if quantity == 0 and max_quantity_by_balance >= 1 and max_quantity_by_instrument >= 1:
                            quantity = 1
                            self.logger.info(f"  Minimum allocation enforced: setting quantity to 1 share "
                                          f"(had funds: max_by_balance={max_quantity_by_balance:.2f}, "
                                          f"max_by_instrument={max_quantity_by_instrument:.2f})")
                
                # Apply instrument weight (formula: result * (weight/100))
                if quantity > 0 and symbol in instrument_configs:
                    instrument_weight = instrument_configs[symbol].get('weight', 100.0)
                    if instrument_weight != 100.0:  # Only apply if weight is non-default
                        original_quantity = quantity
                        weighted_quantity = quantity * (instrument_weight / 100.0)
                        self.logger.info(f"  Instrument weight {instrument_weight}%: "
                                       f"{original_quantity} shares * {instrument_weight/100:.2f} = {weighted_quantity:.2f} shares")
                        
                        # Second rounding: weighted quantity to int
                        quantity = max(0, int(weighted_quantity))
                        self.logger.info(f"  Second rounding: {weighted_quantity:.2f} -> {quantity} shares (int conversion)")
                        
                        # CRITICAL: Ensure minimum quantity of 1 if we have funds for at least 1 share
                        # This covers the case where weighting reduces quantity below 1 after rounding
                        if quantity == 0 and max_quantity_by_balance >= 1:
                            quantity = 1
                            self.logger.info(f"  Minimum allocation enforced after weighting: setting quantity to 1 share "
                                          f"(weighted calc gave 0 but max_by_balance={max_quantity_by_balance:.2f})")
                        
                        # Check if we can afford the weighted quantity
                        weighted_cost = quantity * current_price
                        if weighted_cost > remaining_balance or weighted_cost > available_for_instrument:
                            # Revert to original quantity if weighted amount exceeds limits
                            quantity = original_quantity
                            self.logger.info(f"  Weight {instrument_weight}% would exceed limits "
                                          f"(cost ${weighted_cost:.2f} > remaining ${remaining_balance:.2f} or available ${available_for_instrument:.2f}), "
                                          f"keeping original quantity {quantity}")
                        else:
                            self.logger.info(f"  Final after weighting: {quantity} shares (cost ${weighted_cost:.2f})")
                
                # Update order with calculated quantity
                order.quantity = quantity
                
                if quantity > 0:
                    total_cost = quantity * current_price
                    remaining_balance -= total_cost
                    instrument_allocations[symbol] = current_allocation + total_cost
                    
                    self.logger.info(f"  ✓ FINAL: Allocated {quantity} shares of {symbol} at ${current_price:.2f} "
                                   f"(cost: ${total_cost:.2f}, ROI: {recommendation.expected_profit_percent:.2f}%)")
                    self.logger.info(f"  Updated balances: remaining=${remaining_balance:.2f}, "
                                   f"{symbol}_allocation=${instrument_allocations[symbol]:.2f}")
                else:
                    self.logger.warning(f"  ✗ FINAL: Set quantity to 0 for {symbol} - insufficient funds or limits reached")
                
                updated_orders.append(order)
                
            except Exception as e:
                self.logger.error(f"Error calculating quantity for order {order.id}: {e}", exc_info=True)
                order.quantity = 0
                updated_orders.append(order)
        
        total_allocated = total_virtual_balance - remaining_balance
        self.logger.info(f"Risk management allocation complete: ${total_allocated:.2f} allocated, "
                        f"${remaining_balance:.2f} remaining")
        
        # Separate orders into those to update (qty > 0) and those to delete (qty = 0)
        orders_to_update = [o for o in updated_orders if o.quantity > 0]
        orders_to_delete = [o for o in updated_orders if o.quantity == 0]
        
        if orders_to_delete:
            self.logger.info(f"Found {len(orders_to_delete)} orders with quantity=0 that will be deleted")
        
        return orders_to_update, orders_to_delete
    
    def _update_orders_in_database(self, orders: List[TradingOrder]) -> None:
        """Update orders in the database with calculated quantities."""
        try:
            updated_count = 0
            
            for order in orders:
                if update_instance(order):
                    updated_count += 1
                    if order.quantity > 0:
                        self.logger.debug(f"Updated order {order.id} with quantity {order.quantity}")
                else:
                    self.logger.error(f"Failed to update order {order.id} in database")
            
            self.logger.info(f"Successfully updated {updated_count} orders in database")
            
        except Exception as e:
            self.logger.error(f"Error updating orders in database: {e}", exc_info=True)
    
    def _delete_unfunded_orders(self, orders: List[TradingOrder]) -> None:
        """
        Delete orders with quantity=0 (insufficient funds) and their linked orders/transactions.
        
        This is called when automated trade opening is enabled and risk management
        determines some orders cannot be funded.
        
        Args:
            orders: List of TradingOrder objects with quantity=0 to delete
        """
        try:
            from .db import delete_instance
            
            deleted_order_count = 0
            deleted_linked_order_count = 0
            deleted_transaction_count = 0
            
            with get_db() as session:
                for order in orders:
                    try:
                        # Log the deletion
                        self.logger.info(f"Deleting unfunded order {order.id} ({order.symbol}, {order.side}) - insufficient funds")
                        
                        # Find and delete any linked orders (orders that depend on this order)
                        if order.id:
                            linked_orders_statement = select(TradingOrder).where(
                                TradingOrder.depends_on_order == order.id
                            )
                            linked_orders = session.exec(linked_orders_statement).all()
                            
                            for linked_order in linked_orders:
                                self.logger.debug(f"  Deleting linked order {linked_order.id} (waiting on trigger order {order.id})")
                                delete_instance(linked_order, session)
                                deleted_linked_order_count += 1
                        
                        # Find and delete any transaction linked to this order
                        if order.transaction_id:
                            transaction = session.get(Transaction, order.transaction_id)
                            if transaction:
                                self.logger.debug(f"  Deleting transaction {transaction.id} linked to order {order.id}")
                                delete_instance(transaction, session)
                                deleted_transaction_count += 1
                        
                        # Delete the main order
                        delete_instance(order, session)
                        deleted_order_count += 1
                        
                    except Exception as e:
                        self.logger.error(f"Error deleting unfunded order {order.id}: {e}", exc_info=True)
                        # Continue with other orders even if one fails
                        continue
                
                # Commit all deletions
                session.commit()
            
            self.logger.info(f"Deleted {deleted_order_count} unfunded orders, "
                           f"{deleted_linked_order_count} linked orders, "
                           f"{deleted_transaction_count} transactions")
            
        except Exception as e:
            self.logger.error(f"Error deleting unfunded orders: {e}", exc_info=True)


# Global risk management instance
_risk_management = None

def get_risk_management() -> TradeRiskManagement:
    """Get the global risk management instance."""
    global _risk_management
    if _risk_management is None:
        _risk_management = TradeRiskManagement()
    return _risk_management