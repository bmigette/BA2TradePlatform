"""
TradeRiskManagement - Risk management system for automated trading

This module implements comprehensive risk management for pending orders,
including profit-based prioritization, position sizing, and diversification.
"""

from typing import Dict, List, Optional, Tuple, Any, TYPE_CHECKING
from datetime import datetime, timezone

from .interfaces import AccountInterface
from ..logger import logger
from .models import TradingOrder, ExpertRecommendation, ExpertInstance, Transaction
from .types import OrderStatus, OrderDirection, TransactionStatus
from .db import get_instance, get_all_instances, update_instance, get_db
from sqlmodel import select, Session

if TYPE_CHECKING:
    from .interfaces import MarketExpertInterface


def compute_order_priority_score(expected_profit_percent: float | None,
                                 confidence: float | None) -> float:
    """Confidence-aware priority score for ranking pending orders (higher = funded first).

    Pure function (no DB/IO) so it can be unit-tested.

    - When the expert provides an expected profit estimate (non-zero), rank by
      profit weighted by conviction: ``expected_profit_percent * (confidence/100)``.
      A 40%-confidence / 30%-profit moonshot should not automatically outrank an
      85%-confidence / 10%-profit setup.
    - When the expert does NOT estimate profit (e.g. FinnHubRating stores 0.0, RM-3/
      EX-5), profit-only sorting would push it permanently last. Such orders fall
      into a reserved band ``[-1, 0)`` ordered by confidence: always below any order
      with genuine positive expected profit, but still ranked by conviction amongst
      themselves (so they can get funded rather than being starved indefinitely).
    """
    profit = expected_profit_percent or 0.0
    conf_frac = (confidence or 0.0) / 100.0
    if profit != 0.0:
        return profit * conf_frac
    # No profit estimate available: reserved confidence-ordered band below positive profit.
    return -1.0 + conf_frac


def estimate_transaction_allocation(quantity: float | None,
                                    open_price: float | None,
                                    fallback_price: float | None) -> float:
    """Estimate the capital committed by a transaction (RM-4). Pure function.

    Prefer ``open_price``; when it is absent (e.g. a WAITING transaction created
    without an estimated fill price), fall back to ``fallback_price`` — typically
    the linked pending order's limit/stop price — so committed capital is not
    undercounted (which would open a per-instrument over-allocation window across
    risk-manager runs). Returns 0.0 only when no price is available at all.
    """
    if not quantity:
        return 0.0
    price = open_price if open_price else fallback_price
    if not price:
        return 0.0
    return abs(quantity * price)


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
                self.logger.error(error_msg)
                raise ValueError(error_msg)
            
            # Get the expert instance model
            expert_instance = get_instance(ExpertInstance, expert_instance_id)
            if not expert_instance:
                error_msg = f"Expert instance model {expert_instance_id} not found"
                self.logger.error(error_msg)
                raise ValueError(error_msg)
            
            # Check if automated trade opening is enabled
            allow_automated_trade_opening = expert.get_setting_with_interface_default(
                'allow_automated_trade_opening', log_warning=False
            )
            if not allow_automated_trade_opening:
                self.logger.debug(f"Automated trade opening disabled for expert {expert_instance_id}, skipping risk management")
                return updated_orders
            
            # Get expert trading permissions using interface defaults
            enable_buy = expert.get_setting_with_interface_default(
                'enable_buy', log_warning=False
            )
            enable_sell = expert.get_setting_with_interface_default(
                'enable_sell', log_warning=False
            )
            
            # Get virtual equity per instrument limit using interface default
            max_virtual_equity_per_instrument_percent = expert.get_setting_with_interface_default(
                'max_virtual_equity_per_instrument_percent', log_warning=False
            )
            if max_virtual_equity_per_instrument_percent is None:
                # No hidden money defaults (CLAUDE.md). get_setting_with_interface_default
                # already supplies the interface default; a None here means a genuine
                # misconfiguration — fail loudly rather than silently using 10%.
                raise ValueError(
                    f"Expert {expert_instance_id} has no max_virtual_equity_per_instrument_percent "
                    f"setting and the interface provides no default; cannot size orders safely"
                )
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
            # Orders dropped by the permission filter (e.g. a SELL entry created for a
            # buy-only expert) must be cleaned up — otherwise they leak as permanent
            # PENDING qty=0 orders with dangling TP/SL legs and a stuck WAITING txn.
            filtered_ids = {o.id for o in filtered_orders}
            dropped_by_permission = [o for o in pending_orders if o.id not in filtered_ids]
            if dropped_by_permission and allow_automated_trade_opening:
                self.logger.info(
                    f"Cleaning up {len(dropped_by_permission)} pending order(s) dropped by "
                    f"buy/sell permission filter for expert {expert_instance_id}"
                )
                self._delete_unfunded_orders(dropped_by_permission)
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
                self.logger.error(error_msg)
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
                self.logger.error(error_msg)
                raise ValueError(error_msg)
            
            # Step 7: Get existing positions to account for current allocations
            existing_allocations = self._get_existing_allocations(expert_instance_id)
            self.logger.debug(f"Existing allocations for expert {expert_instance_id}: {existing_allocations}")
            
            # Step 8: Calculate quantities for prioritized orders
            orders_to_update, orders_to_delete, symbol_prices = self._calculate_order_quantities(
                prioritized_orders,
                total_virtual_balance,
                max_equity_per_instrument,
                existing_allocations,
                account,
                expert
            )

            # Step 9: Update orders in database
            updated_in_db, failed_in_db = self._update_orders_in_database(orders_to_update)

            # Step 10: Delete orders with quantity=0 if automated trade opening is enabled
            if allow_automated_trade_opening and orders_to_delete:
                self._delete_unfunded_orders(orders_to_delete, symbol_prices, max_equity_per_instrument)
            
            updated_orders = orders_to_update  # For return value compatibility
            
            self.logger.info(f"Risk management completed for expert {expert_instance_id}: "
                           f"updated {len(updated_orders)} orders, deleted {len(orders_to_delete) if orders_to_delete else 0} unfunded orders")
            
            # Log activity for risk manager execution
            try:
                from .db import log_activity
                from .types import ActivityLogSeverity, ActivityLogType
                
                # Reflect partial DB-update failures in the activity log (RM-5):
                # don't report SUCCESS if some orders failed to persist.
                rm_severity = ActivityLogSeverity.WARNING if failed_in_db else ActivityLogSeverity.SUCCESS
                failure_note = f", {failed_in_db} FAILED to persist" if failed_in_db else ""
                log_activity(
                    severity=rm_severity,
                    activity_type=ActivityLogType.RISK_MANAGER_RAN,
                    description=f"Classic risk manager: reviewed {len(pending_orders)} pending orders, updated {len(updated_orders)}, deleted {len(orders_to_delete) if orders_to_delete else 0} unfunded orders{failure_note}",
                    data={
                        "mode": "classic",
                        "orders_reviewed": len(pending_orders),
                        "orders_filtered": len(filtered_orders),
                        "orders_with_recommendations": len(orders_with_recommendations),
                        "orders_updated": len(updated_orders),
                        "orders_failed_to_persist": failed_in_db,
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
                from .db import log_activity
                from .types import ActivityLogSeverity, ActivityLogType
                
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
        """Get all pending orders for an expert (RM-2: single JOIN, no N+1)."""
        try:
            with get_db() as session:
                # Single JOIN filtered by the expert instance, instead of loading every
                # pending order in the system and issuing one ExpertRecommendation lookup
                # per order (the previous O(N) pattern). select(TradingOrder) with
                # sqlmodel returns TradingOrder instances (not Row tuples).
                statement = (
                    select(TradingOrder)
                    .join(
                        ExpertRecommendation,
                        TradingOrder.expert_recommendation_id == ExpertRecommendation.id,
                    )
                    .where(
                        TradingOrder.status == OrderStatus.PENDING,
                        TradingOrder.expert_recommendation_id.is_not(None),
                        ExpertRecommendation.instance_id == expert_instance_id,
                    )
                )

                expert_orders = []
                seen_order_ids = set()  # Guard against any duplicate rows from the join
                for order in session.exec(statement).all():
                    if order.id in seen_order_ids:
                        continue
                    seen_order_ids.add(order.id)
                    expert_orders.append(order)
                    self.logger.debug(
                        f"Found pending order {order.id} for {order.symbol} (side: {order.side.value})"
                    )

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
        """Get orders with their linked recommendations.

        RM-2: batch-fetch all recommendations in a single query instead of one
        ``session.get`` per order (the previous N+1).
        """
        orders_with_recommendations = []

        try:
            rec_ids = {o.expert_recommendation_id for o in orders if o.expert_recommendation_id}
            if not rec_ids:
                return []

            with get_db() as session:
                rows = session.exec(
                    select(ExpertRecommendation).where(ExpertRecommendation.id.in_(rec_ids))
                ).all()
                recs_by_id = {rec.id: rec for rec in rows}

            for order in orders:
                recommendation = recs_by_id.get(order.expert_recommendation_id)
                if recommendation:
                    # Include even when expected_profit_percent is None/0.0 —
                    # prioritization is confidence-aware (RM-3/EX-5), so experts
                    # that don't estimate profit are ranked by conviction, not dropped.
                    orders_with_recommendations.append((order, recommendation))
                else:
                    self.logger.debug(f"Order {order.id} has no linked recommendation")

        except Exception as e:
            self.logger.error(f"Error getting recommendations for orders: {e}", exc_info=True)

        self.logger.debug(f"Found {len(orders_with_recommendations)} orders with valid recommendations")
        return orders_with_recommendations
    
    def _prioritize_orders_by_profit(self, orders_with_recommendations: List[Tuple[TradingOrder, ExpertRecommendation]]) -> List[Tuple[TradingOrder, ExpertRecommendation]]:
        """Sort orders by a confidence-aware priority score (descending).

        See compute_order_priority_score: profit weighted by confidence, with a
        confidence-ordered fallback band for experts that don't estimate profit so
        they are not permanently starved (RM-3/EX-5).
        """
        try:
            prioritized = sorted(
                orders_with_recommendations,
                key=lambda x: compute_order_priority_score(
                    x[1].expected_profit_percent, x[1].confidence
                ),
                reverse=True
            )

            self.logger.debug(f"Prioritized {len(prioritized)} orders by confidence-aware score")
            for i, (order, rec) in enumerate(prioritized[:5]):  # Log top 5
                score = compute_order_priority_score(rec.expected_profit_percent, rec.confidence)
                self.logger.debug(
                    f"#{i+1}: {order.symbol} - score={score:.3f} "
                    f"(profit={rec.expected_profit_percent or 0.0:.2f}%, conf={rec.confidence or 0.0:.0f})"
                )

            return prioritized

        except Exception as e:
            self.logger.error(f"Error prioritizing orders by score: {e}", exc_info=True)
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
                    # Prefer open_price (capital allocated when the position opened).
                    # WAITING transactions may lack open_price; in that case estimate
                    # committed capital from the linked pending order's limit/stop price
                    # so the per-instrument cap isn't undercounted (RM-4).
                    fallback_price = None
                    if not transaction.open_price and transaction.quantity:
                        for o in transaction.trading_orders:
                            fallback_price = o.limit_price or o.open_price or o.stop_price
                            if fallback_price:
                                break
                        if not fallback_price:
                            self.logger.warning(
                                f"WAITING transaction {transaction.id} ({symbol}) has no open_price "
                                f"and no order price; counting allocation as 0 "
                                f"(may understate committed capital)"
                            )
                    current_value = estimate_transaction_allocation(
                        transaction.quantity, transaction.open_price, fallback_price
                    )

                    allocations[symbol] = allocations.get(symbol, 0.0) + current_value
                
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
        early_skipped_count = 0  # Track orders skipped early due to unaffordability
        instrument_allocations = existing_allocations.copy()
        
        # Get instrument weight configurations from expert settings
        instrument_configs = expert._get_enabled_instruments_config()
        self.logger.debug(f"Retrieved instrument weight configurations: {len(instrument_configs)} instruments")

        # Diversification factor (RM-5): configurable per expert. Defaults to 1.0 (off)
        # so diversification is governed by the per-instrument cap; when set < 1.0 and
        # multiple instruments still have headroom, only this fraction of the available
        # equity is used for a single instrument, reserving the rest for others.
        diversification_factor = expert.get_setting_with_interface_default(
            'diversification_factor', log_warning=False
        )
        if diversification_factor is None:
            diversification_factor = 1.0
        
        # Group orders by symbol for diversity calculation
        orders_by_symbol = {}
        for order, recommendation in prioritized_orders:
            symbol = order.symbol
            if symbol not in orders_by_symbol:
                orders_by_symbol[symbol] = []
            orders_by_symbol[symbol].append((order, recommendation))
        
        self.logger.info(f"Processing {len(prioritized_orders)} orders across {len(orders_by_symbol)} instruments")
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
                
                # EARLY AFFORDABILITY CHECK: Skip if can't afford even 1 share within the
                # per-instrument limit. The order is marked qty=0 -> deleted (transaction
                # cancelled) rather than left stuck PENDING. Respects the user's limit with
                # no special-case bypass.
                if available_for_instrument < current_price:
                    self.logger.warning(f"  ⚡ EARLY SKIP: {symbol} price ${current_price:.2f} exceeds available per-instrument "
                                      f"${available_for_instrument:.2f} (can't afford 1 share within limit) - marking for deletion")
                    order.quantity = 0
                    early_skipped_count += 1
                    updated_orders.append(order)
                    continue
                
                if remaining_balance < current_price:
                    self.logger.warning(f"  ⚡ EARLY SKIP: {symbol} price ${current_price:.2f} exceeds remaining balance "
                                      f"${remaining_balance:.2f} (can't afford 1 share) - marking for deletion")
                    order.quantity = 0
                    early_skipped_count += 1
                    updated_orders.append(order)
                    continue
                
                # Calculate maximum affordable quantity based on available equity per instrument
                max_quantity_by_instrument = max(0, available_for_instrument / current_price) if current_price > 0 else 0
                
                # Calculate maximum affordable quantity based on remaining total balance
                max_quantity_by_balance = max(0, remaining_balance / current_price) if current_price > 0 else 0
                
                self.logger.info(f"  Calculated: max_qty_by_instrument={max_quantity_by_instrument:.2f} shares, "
                               f"max_qty_by_balance={max_quantity_by_balance:.2f} shares")
                
                # Standard allocation logic — respect the user's per-instrument limit.
                # No special-case bypass: a symbol whose 1-share price exceeds the cap is
                # simply not bought (early-skipped above), never forced through at qty=1.
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

                    if num_remaining_instruments > 1 and diversification_factor < 1.0:
                        # Reserve some balance for other instruments (configurable, RM-5;
                        # default 1.0 = off, so this only runs when explicitly lowered)
                        original_max = max_quantity
                        max_quantity *= diversification_factor
                        self.logger.info(f"  Diversification ({num_remaining_instruments} remaining instruments): "
                                      f"applied factor {diversification_factor}: {original_max:.2f} -> {max_quantity:.2f} shares")

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
        
        if early_skipped_count > 0:
            self.logger.info(f"⚡ Early skip optimization: {early_skipped_count} orders skipped due to unaffordability "
                           f"(preventing unnecessary TP/SL creation attempts)")
        
        # Separate orders into those to update (qty > 0) and those to delete (qty = 0)
        orders_to_update = [o for o in updated_orders if o.quantity > 0]
        orders_to_delete = [o for o in updated_orders if o.quantity == 0]

        if orders_to_delete:
            self.logger.info(f"Found {len(orders_to_delete)} orders with quantity=0 that will be deleted")

        return orders_to_update, orders_to_delete, symbol_prices
    
    def _update_orders_in_database(self, orders: List[TradingOrder]) -> Tuple[int, int]:
        """
        Update orders in the database with calculated quantities using TransactionHelper.

        For each order, updates its transaction and all related orders (entry + TP/SL) in sync
        to prevent qty mismatches and invalid order states.

        Returns:
            (updated_count, failed_count). Per-transaction failures are counted and
            returned (RM-5) so the caller can reflect partial failure in the activity
            log instead of always reporting SUCCESS. A hard/unexpected error is
            re-raised so it is not silently swallowed.
        """
        from .TransactionHelper import TransactionHelper
        from .db import get_db
        from sqlmodel import Session, select

        try:
            updated_count = 0
            failed_count = 0
            grouped_by_transaction = {}
            
            # Group orders by transaction
            with Session(get_db().bind) as session:
                for order in orders:
                    if order.transaction_id:
                        if order.transaction_id not in grouped_by_transaction:
                            # Load transaction from DB
                            transaction = session.get(Transaction, order.transaction_id)
                            if transaction:
                                grouped_by_transaction[order.transaction_id] = {
                                    'transaction': transaction,
                                    'orders': [order]
                                }
                        else:
                            grouped_by_transaction[order.transaction_id]['orders'].append(order)
                    else:
                        # Order without transaction - update directly (legacy support)
                        from .db import update_instance
                        if update_instance(order):
                            updated_count += 1
                            self.logger.debug(f"Updated orphan order {order.id} with quantity {order.quantity}")
            
            # Update each transaction and its related orders
            for txn_id, data in grouped_by_transaction.items():
                transaction = data['transaction']
                orders_in_txn = data['orders']
                
                # Use the first order's new quantity (all should be the same)
                if orders_in_txn:
                    new_quantity = orders_in_txn[0].quantity
                    
                    # Use TransactionHelper to update transaction and all related orders in sync
                    if TransactionHelper.adjust_qty(transaction, new_quantity):
                        updated_count += len(orders_in_txn)
                        self.logger.debug(
                            f"Updated transaction {txn_id} with quantity {new_quantity} "
                            f"and {len(orders_in_txn)} related orders"
                        )
                    else:
                        failed_count += len(orders_in_txn)
                        self.logger.error(f"Failed to update transaction {txn_id}")

            if failed_count:
                self.logger.warning(
                    f"Updated {updated_count} orders in database via TransactionHelper; "
                    f"{failed_count} order(s) FAILED to update"
                )
            else:
                self.logger.info(f"Successfully updated {updated_count} orders in database via TransactionHelper")

            return updated_count, failed_count

        except Exception as e:
            # Don't swallow: surface the failure to the caller so the activity log
            # reflects it rather than reporting SUCCESS over a broken update (RM-5).
            self.logger.error(f"Error updating orders in database: {e}", exc_info=True)
            raise
    
    def _delete_unfunded_orders(self, orders: List[TradingOrder], symbol_prices: Dict[str, float] = None, max_equity_per_instrument: float = None) -> None:
        """
        Delete orders with quantity=0 (insufficient funds) and their linked orders/transactions.

        This is called when automated trade opening is enabled and risk management
        determines some orders cannot be funded.

        Args:
            orders: List of TradingOrder objects with quantity=0 to delete
            symbol_prices: Optional dict of symbol -> current price for logging
            max_equity_per_instrument: Optional max allocation per instrument for logging
        """
        try:
            from .db import delete_instance

            deleted_order_count = 0
            deleted_linked_order_count = 0
            deleted_transaction_count = 0

            with get_db() as session:
                for order in orders:
                    try:
                        # Log the deletion with price details
                        price = symbol_prices.get(order.symbol) if symbol_prices else None
                        price_info = f"price=${price:.2f}" if price else "price=unknown"
                        limit_info = f", limit=${max_equity_per_instrument:.2f}" if max_equity_per_instrument else ""
                        self.logger.info(f"Deleting unfunded order {order.id} ({order.symbol}, {order.side}) - "
                                       f"{price_info}{limit_info} (can't afford 1 share)")
                        
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