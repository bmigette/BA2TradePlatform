"""
TradeRiskManagement - Risk management system for automated trading

This module implements comprehensive risk management for pending orders,
including profit-based prioritization, position sizing, and diversification.
"""

from typing import Dict, List, Optional, Tuple, Any
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
import logging
from ..logger import logger
from .models import TradingOrder, ExpertRecommendation, ExpertInstance, Transaction
from .types import OrderStatus, OrderDirection, TransactionStatus
from .db import get_instance, get_all_instances, update_instance, get_db
from sqlmodel import select, Session


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
        self.logger = logger.getChild("TradeRiskManagement")
    
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
            from ..modules.accounts import get_account_class
            from .models import AccountDefinition
            
            # Get the expert instance with settings
            expert = get_expert_instance_from_id(expert_instance_id)
            if not expert:
                self.logger.error(f"Expert instance {expert_instance_id} not found", exc_info=True)
                return updated_orders
            
            # Get the expert instance model
            expert_instance = get_instance(ExpertInstance, expert_instance_id)
            if not expert_instance:
                self.logger.error(f"Expert instance model {expert_instance_id} not found", exc_info=True)
                return updated_orders
            
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
            max_equity_per_instrument_ratio = max_virtual_equity_per_instrument_percent / 100.0
            
            self.logger.info(f"Starting risk management for expert {expert_instance_id}: "
                           f"buy={enable_buy}, sell={enable_sell}, "
                           f"max_per_instrument={max_virtual_equity_per_instrument_percent}%")
            
            # Step 1: Collect pending orders with quantity=0
            pending_orders = self._get_pending_orders_for_review(expert_instance_id)
            if not pending_orders:
                self.logger.debug(f"No pending orders found for expert {expert_instance_id}")
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
            
            # Step 5: Get account and virtual balance
            account_def = get_instance(AccountDefinition, expert_instance.account_id)
            if not account_def:
                self.logger.error(f"Account definition {expert_instance.account_id} not found", exc_info=True)
                return updated_orders
            
            account_class = get_account_class(account_def.provider)
            if not account_class:
                self.logger.error(f"Account provider {account_def.provider} not found", exc_info=True)
                return updated_orders
            
            account = account_class(account_def.id)
            
            # Get virtual trading balance
            account_info = account.get_account_info()
            if not account_info or 'virtual_trading_balance' not in account_info:
                self.logger.error(f"Could not get virtual trading balance for account {account_def.id}", exc_info=True)
                return updated_orders
            
            total_virtual_balance = float(account_info['virtual_trading_balance'])
            max_equity_per_instrument = total_virtual_balance * max_equity_per_instrument_ratio
            
            self.logger.info(f"Virtual balance: ${total_virtual_balance:.2f}, "
                           f"max per instrument: ${max_equity_per_instrument:.2f}")
            
            # Step 6: Get existing positions to account for current allocations
            existing_allocations = self._get_existing_allocations(expert_instance_id)
            
            # Step 7: Calculate quantities for prioritized orders
            updated_orders = self._calculate_order_quantities(
                prioritized_orders,
                total_virtual_balance,
                max_equity_per_instrument,
                existing_allocations
            )
            
            # Step 8: Update orders in database
            self._update_orders_in_database(updated_orders)
            
            self.logger.info(f"Risk management completed for expert {expert_instance_id}: "
                           f"updated {len(updated_orders)} orders")
            
        except Exception as e:
            self.logger.error(f"Error in risk management for expert {expert_instance_id}: {e}", exc_info=True)
        
        return updated_orders
    
    def _get_pending_orders_for_review(self, expert_instance_id: int) -> List[TradingOrder]:
        """Get all pending orders with quantity=0 for an expert."""
        try:
            with get_db() as session:
                # First get all pending orders with quantity=0 and order_recommendation_id
                statement = select(TradingOrder).where(
                    TradingOrder.status == OrderStatus.PENDING,
                    TradingOrder.quantity == 0,
                    TradingOrder.order_recommendation_id.is_not(None)
                )
                
                all_pending_orders = session.exec(statement).all()
                
                # Filter orders that belong to this expert by checking their recommendations
                expert_orders = []
                for order in all_pending_orders:
                    if order.order_recommendation_id:
                        recommendation = session.get(ExpertRecommendation, order.order_recommendation_id)
                        if recommendation and recommendation.instance_id == expert_instance_id:
                            expert_orders.append(order)
                
                self.logger.debug(f"Found {len(expert_orders)} pending orders for review for expert {expert_instance_id}")
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
            elif order.side == OrderDirection.SELL and enable_sell:
                filtered_orders.append(order)
            else:
                self.logger.debug(f"Skipping order {order.id} - {order.side.value} not enabled")
        
        self.logger.debug(f"Filtered {len(filtered_orders)} orders from {len(orders)} based on permissions")
        return filtered_orders
    
    def _get_orders_with_recommendations(self, orders: List[TradingOrder]) -> List[Tuple[TradingOrder, ExpertRecommendation]]:
        """Get orders with their linked recommendations."""
        orders_with_recommendations = []
        
        try:
            with get_db() as session:
                for order in orders:
                    if order.order_recommendation_id:
                        recommendation = session.get(ExpertRecommendation, order.order_recommendation_id)
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
                    Transaction.expert_instance_id == expert_instance_id,
                    Transaction.status.in_([TransactionStatus.OPEN, TransactionStatus.PARTIAL])
                )
                
                transactions = session.exec(statement).all()
                
                for transaction in transactions:
                    symbol = transaction.symbol
                    # Calculate current value of the position
                    current_value = abs(transaction.quantity * transaction.current_price) if transaction.current_price else 0
                    
                    if symbol in allocations:
                        allocations[symbol] += current_value
                    else:
                        allocations[symbol] = current_value
                
                self.logger.debug(f"Found existing allocations for expert {expert_instance_id}: {allocations}")
                
        except Exception as e:
            self.logger.error(f"Error getting existing allocations for expert {expert_instance_id}: {e}", exc_info=True)
        
        return allocations
    
    def _calculate_order_quantities(
        self,
        prioritized_orders: List[Tuple[TradingOrder, ExpertRecommendation]],
        total_virtual_balance: float,
        max_equity_per_instrument: float,
        existing_allocations: Dict[str, float]
    ) -> List[TradingOrder]:
        """Calculate appropriate quantities for each order."""
        updated_orders = []
        remaining_balance = total_virtual_balance
        instrument_allocations = existing_allocations.copy()
        
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
        
        self.logger.debug(f"Processing {len(prioritized_orders)} orders across {len(orders_by_symbol)} instruments")
        self.logger.debug(f"Top 3 ROI symbols: {top_3_symbols}")
        
        for order, recommendation in prioritized_orders:
            try:
                symbol = order.symbol
                current_allocation = instrument_allocations.get(symbol, 0.0)
                available_for_instrument = max_equity_per_instrument - current_allocation
                
                # Get current market price (simplified - in practice you'd use real-time data)
                current_price = recommendation.current_price or 1.0
                
                self.logger.debug(f"Processing {symbol}: price=${current_price:.2f}, "
                                f"current_allocation=${current_allocation:.2f}, "
                                f"available=${available_for_instrument:.2f}")
                
                # Calculate maximum affordable quantity based on available equity per instrument
                max_quantity_by_instrument = max(0, available_for_instrument / current_price) if current_price > 0 else 0
                
                # Calculate maximum affordable quantity based on remaining total balance
                max_quantity_by_balance = max(0, remaining_balance / current_price) if current_price > 0 else 0
                
                # Special case: Top 3 ROI exception
                if (symbol in top_3_symbols and 
                    current_price > max_equity_per_instrument and 
                    current_price <= remaining_balance and
                    current_allocation == 0):
                    
                    # Allow single share purchase for high-value, high-ROI instruments
                    quantity = 1
                    total_cost = quantity * current_price
                    
                    self.logger.info(f"Top 3 ROI exception for {symbol}: allocating ${total_cost:.2f} "
                                   f"(exceeds per-instrument limit but within total balance)")
                
                else:
                    # Standard allocation logic
                    if available_for_instrument <= 0:
                        quantity = 0
                        self.logger.debug(f"No available equity for {symbol} (limit exceeded)")
                    elif remaining_balance <= current_price:
                        quantity = 0
                        self.logger.debug(f"Insufficient remaining balance for {symbol}")
                    else:
                        # Use the minimum of the two constraints
                        max_quantity = min(max_quantity_by_instrument, max_quantity_by_balance)
                        
                        # For diversification, prefer smaller positions when multiple instruments available
                        num_remaining_instruments = len([s for s in orders_by_symbol.keys() 
                                                       if instrument_allocations.get(s, 0) < max_equity_per_instrument])
                        
                        if num_remaining_instruments > 1:
                            # Reserve some balance for other instruments
                            diversification_factor = 0.7  # Use 70% of available, save 30% for others
                            max_quantity *= diversification_factor
                        
                        quantity = max(0, int(max_quantity))
                        
                        # Ensure we don't allocate less than 1 share unless we can't afford it
                        if quantity == 0 and max_quantity_by_balance >= 1 and max_quantity_by_instrument >= 1:
                            quantity = 1
                
                # Update order with calculated quantity
                order.quantity = quantity
                
                if quantity > 0:
                    total_cost = quantity * current_price
                    remaining_balance -= total_cost
                    instrument_allocations[symbol] = current_allocation + total_cost
                    
                    self.logger.info(f"Allocated {quantity} shares of {symbol} at ${current_price:.2f} "
                                   f"(total: ${total_cost:.2f}, ROI: {recommendation.expected_profit_percent:.2f}%)")
                else:
                    self.logger.debug(f"Set quantity to 0 for {symbol} - insufficient funds or limits")
                
                updated_orders.append(order)
                
            except Exception as e:
                self.logger.error(f"Error calculating quantity for order {order.id}: {e}", exc_info=True)
                order.quantity = 0
                updated_orders.append(order)
        
        total_allocated = total_virtual_balance - remaining_balance
        self.logger.info(f"Risk management allocation complete: ${total_allocated:.2f} allocated, "
                        f"${remaining_balance:.2f} remaining")
        
        return updated_orders
    
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


# Global risk management instance
_risk_management = None

def get_risk_management() -> TradeRiskManagement:
    """Get the global risk management instance."""
    global _risk_management
    if _risk_management is None:
        _risk_management = TradeRiskManagement()
    return _risk_management