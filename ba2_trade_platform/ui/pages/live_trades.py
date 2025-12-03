from nicegui import ui
from datetime import datetime, timedelta, timezone
from sqlmodel import select, func, Session
from typing import Dict, Any
import requests
import aiohttp
import asyncio
import json

from ...core.db import get_all_instances, get_db, get_instance, update_instance
from ...core.models import AccountDefinition, MarketAnalysis, ExpertRecommendation, ExpertInstance, AppSetting, TradingOrder, Transaction
from ...core.types import MarketAnalysisStatus, OrderRecommendation, OrderStatus, OrderOpenType, OrderType
from ...core.utils import get_expert_instance_from_id, get_market_analysis_id_from_order_id, get_account_instance_from_id, get_order_status_color, get_expert_options_for_ui
from ...modules.accounts import providers
from ...logger import logger
from ..utils.TableCacheManager import TableCacheManager, AsyncTableLoader

class LiveTradesTab:
    """Comprehensive transactions management tab with full control over positions."""

    def __init__(self):
        self.transactions_container = None
        self.transactions_table = None
        self.selected_transaction = None
        self.selected_transactions = {}  # Dictionary to track selected transaction IDs
        self.batch_operations_container = None
        # Initialize cache manager for lazy loading
        self.cache_manager = TableCacheManager("LiveTrades")
        self.async_loader = None
        self.render()

    def _get_order_status_color(self, status):
        """Get color for order status badge."""
        return get_order_status_color(status)

    def render(self):
        """Render the transactions tab with filtering and control options."""
        logger.debug("[RENDER] LiveTradesTab.render() - START")

        # Pre-populate expert options before creating the UI
        expert_options, expert_map = self._get_expert_options()
        self.expert_id_map = expert_map

        with ui.card().classes('w-full'):
            with ui.row().classes('w-full items-center justify-between mb-4'):
                ui.label('💼 Live Trades').classes('text-h6')

                # Filter controls
                with ui.row().classes('gap-2'):
                    # Multi-select status filter with all except CLOSED selected by default
                    self.status_filter = ui.select(
                        label='Status Filter',
                        options=['Waiting', 'Open', 'Closing', 'Closed'],
                        value=['Waiting', 'Open', 'Closing'],  # Default: all except Closed
                        multiple=True,
                        on_change=lambda: self._refresh_transactions()
                    ).classes('w-48')

                    # Expert filter - populated with all experts
                    self.expert_filter = ui.select(
                        label='Expert',
                        options=expert_options,
                        value='All',
                        on_change=lambda: self._refresh_transactions()
                    ).classes('w-48')

                    self.symbol_filter = ui.input(
                        label='Symbol',
                        placeholder='Filter by symbol...',
                        on_change=lambda: self._refresh_transactions()
                    ).classes('w-40')

                    self.broker_order_id_filter = ui.input(
                        label='Broker Order ID',
                        placeholder='Search by broker order ID...',
                        on_change=lambda: self._refresh_transactions()
                    ).classes('w-48')

                    ui.button('Refresh', icon='refresh', on_click=lambda: self._refresh_transactions()).props('outline')

                    ui.button('Force Refresh Account', icon='cloud_download', on_click=self._force_refresh_account_now).props('outline')

                    # Batch operation buttons
                    self.batch_operations_container = ui.row().classes('gap-2 ml-4')
                    self.batch_select_all_btn = ui.button(
                        'Select All',
                        icon='done_all',
                        on_click=self._select_all_transactions
                    ).props('outline size=md').classes('hidden')
                    self.batch_select_all_btn.set_visibility(False)

                    self.batch_clear_btn = ui.button(
                        'Clear',
                        icon='clear',
                        on_click=self._clear_selected_transactions
                    ).props('outline size=md').classes('hidden')
                    self.batch_clear_btn.set_visibility(False)

                    self.batch_close_btn = ui.button(
                        'Batch Close',
                        icon='close',
                        on_click=self._batch_close_transactions
                    ).props('outline color=negative size=md').classes('hidden')
                    self.batch_close_btn.set_visibility(False)

                    self.batch_adjust_tp_btn = ui.button(
                        'Batch Adjust TP',
                        icon='trending_up',
                        on_click=self._batch_adjust_tp_dialog
                    ).props('outline color=info size=md').classes('hidden')
                    self.batch_adjust_tp_btn.set_visibility(False)

            # Transactions table container
            self.transactions_container = ui.column().classes('w-full')
            self._render_transactions_table()

    def _get_expert_options(self):
        """Get list of expert options and ID mapping."""
        return get_expert_options_for_ui()

    def _populate_expert_filter(self):
        """Populate the expert filter dropdown with all available experts."""
        # Get fresh expert options
        expert_options, expert_map = self._get_expert_options()
        self.expert_id_map = expert_map

        # Update expert filter options
        if hasattr(self, 'expert_filter'):
            current_value = self.expert_filter.value
            self.expert_filter.options = expert_options
            # Reset to 'All' if current value is not in the new options
            if current_value not in expert_options:
                self.expert_filter.value = 'All'
            # Force update of the select component
            self.expert_filter.update()

        logger.debug(f"[POPULATE] Populated expert filter with {len(expert_options)} options")

    def _refresh_transactions(self):
        """Refresh the transactions table with async loading and caching."""
        logger.debug("[REFRESH] _refresh_transactions() - Updating table rows")

        # Refresh expert filter options (in case new experts were added)
        self._populate_expert_filter()

        # Invalidate cache if filters changed
        filter_state = self._get_filter_state()
        if self.cache_manager.should_invalidate_cache(filter_state):
            logger.debug("[REFRESH] Filters changed, invalidating cache")
            self.cache_manager.invalidate_cache()

        # If table doesn't exist yet, create it
        if not self.transactions_table:
            logger.debug("[REFRESH] Table doesn't exist, creating new table")
            self.transactions_container.clear()
            with self.transactions_container:
                self._render_transactions_table()
            return

        # Otherwise, async load and update the rows data
        try:
            # Run async load in background
            asyncio.create_task(self._async_refresh_transactions())
            logger.debug("[REFRESH] Async refresh task started")
        except Exception as e:
            logger.error(f"Error starting async refresh: {e}", exc_info=True)
            # Fallback to sync refresh
            try:
                new_rows = self._get_transactions_data()
                logger.debug(f"[REFRESH] Updating table with {len(new_rows)} rows")
                self.transactions_table.rows.clear()
                self.transactions_table.rows.extend(new_rows)
                logger.debug("[REFRESH] _refresh_transactions() - Complete")
            except Exception as e2:
                logger.error(f"Error in fallback refresh: {e2}", exc_info=True)
                logger.debug("[REFRESH] Fallback failed, recreating table")
                self.transactions_container.clear()
                with self.transactions_container:
                    self._render_transactions_table()
    
    async def _async_refresh_transactions(self):
        """Async transaction refresh with cache and progressive loading."""
        try:
            filter_state = self._get_filter_state()
            
            # Use cache manager to get data (fetches only if filters changed)
            new_rows, _ = await self.cache_manager.get_data(
                fetch_func=self._get_transactions_data,
                filter_state=filter_state
            )
            
            if not new_rows:
                self.transactions_table.rows = []
                return
            
            # Use async loader for progressive rendering on large datasets
            if not self.async_loader and self.transactions_table:
                self.async_loader = AsyncTableLoader(self.transactions_table, batch_size=50)
            
            if self.async_loader:
                await self.async_loader.load_rows_async(new_rows)
            else:
                self.transactions_table.rows = new_rows
            
            logger.debug(f"[ASYNC] Transaction table refreshed with {len(new_rows)} rows")
        except Exception as e:
            logger.error(f"Error in async refresh: {e}", exc_info=True)
    
    def _get_filter_state(self) -> tuple:
        """Get current filter state as hashable tuple for cache invalidation."""
        status_values = tuple(sorted(self.status_filter.value if hasattr(self, 'status_filter') and self.status_filter.value else []))
        expert_value = self.expert_filter.value if hasattr(self, 'expert_filter') else 'All'
        symbol_value = self.symbol_filter.value if hasattr(self, 'symbol_filter') else ''
        broker_order_value = self.broker_order_id_filter.value if hasattr(self, 'broker_order_id_filter') else ''
        return (status_values, expert_value, symbol_value, broker_order_value)

    def _force_refresh_account_now(self):
        """Force an immediate account refresh (non-blocking)."""
        logger.info("[ACCOUNT_REFRESH] User clicked 'Force Refresh Account' button")
        try:
            from ...core.JobManager import get_job_manager
            job_manager = get_job_manager()

            # Execute account refresh immediately as background task
            job_manager.execute_account_refresh_immediately()

            ui.notify('Account refresh started in background', type='info')
            logger.info("[ACCOUNT_REFRESH] Account refresh task queued successfully")
        except Exception as e:
            ui.notify(f'Error starting account refresh: {str(e)}', type='negative')
            logger.error(f"Error executing account refresh: {e}", exc_info=True)

    def _clean_pending_orders_dialog(self):
        """Show confirmation dialog for cleaning pending orders."""
        logger.debug("User clicked 'Clean Pending Orders' button")

        def confirm_clean():
            """Execute the cleanup after confirmation."""
            logger.info("User confirmed pending orders cleanup")
            try:
                from ...core.TradeManager import get_trade_manager
                trade_manager = get_trade_manager()

                # Execute cleanup
                stats = trade_manager.clean_pending_orders()

                # Show results
                if stats['errors']:
                    error_msg = '\n'.join(stats['errors'][:5])  # Show first 5 errors
                    ui.notify(
                        f"Cleanup completed with issues:\n{error_msg}",
                        type='warning'
                    )
                else:
                    ui.notify(
                        f"✓ Cleaned {stats['orders_deleted']} pending orders\n"
                        f"✓ Deleted {stats['dependents_deleted']} dependent orders\n"
                        f"✓ Closed {stats['transactions_closed']} transactions",
                        type='positive'
                    )

                logger.info(
                    f"Pending orders cleanup complete: "
                    f"orders={stats['orders_deleted']}, "
                    f"dependents={stats['dependents_deleted']}, "
                    f"transactions_closed={stats['transactions_closed']}, "
                    f"errors={len(stats['errors'])}"
                )

                # Refresh the table
                self._refresh_transactions()

            except Exception as e:
                ui.notify(f'Error cleaning pending orders: {str(e)}', type='negative')
                logger.error(f"Error during pending orders cleanup: {e}", exc_info=True)

        # Show confirmation dialog
        with ui.dialog() as dialog:
            with ui.card():
                ui.label('Clean Pending Orders?').classes('text-lg font-bold')
                ui.label(
                    'This will delete all PENDING, WAITING_TRIGGER, and ERROR orders, '
                    'along with their dependent orders. Any associated transactions will be marked as CLOSED.'
                ).classes('text-sm text-gray-600')
                ui.label(
                    '⚠️ This action cannot be undone!'
                ).classes('text-sm text-red-600 font-bold mt-2')

                with ui.row().classes('gap-2 mt-4'):
                    ui.button('Cancel', on_click=dialog.close).props('outline')
                    ui.button('Clean', on_click=lambda: [confirm_clean(), dialog.close()]).props('color=negative')

        dialog.open()

    def _get_transactions_data(self):
        """Get transactions data for the table.

        Returns:
            List of row dictionaries for the table
        """
        from ...core.models import Transaction, ExpertInstance
        from ...core.types import TransactionStatus
        from sqlmodel import col

        logger.debug("[TRANSACTIONS] _get_transactions_data() - START")

        session = get_db()
        try:
            # Build query based on filters - join with ExpertInstance for expert info
            statement = select(Transaction, ExpertInstance).outerjoin(
                ExpertInstance, Transaction.expert_id == ExpertInstance.id
            ).order_by(Transaction.created_at.desc())

            logger.debug(f"[TRANSACTIONS] Initial query built")

            # Apply status filter (multi-select)
            status_values = self.status_filter.value if hasattr(self, 'status_filter') else ['Waiting', 'Open', 'Closing']
            logger.debug(f"[TRANSACTIONS] Status filter: {status_values}")
            if status_values and len(status_values) > 0:
                status_map = {
                    'Open': TransactionStatus.OPENED,
                    'Closed': TransactionStatus.CLOSED,
                    'Closing': TransactionStatus.CLOSING,
                    'Waiting': TransactionStatus.WAITING
                }
                # Filter by selected statuses
                selected_statuses = [status_map[s] for s in status_values if s in status_map]
                if selected_statuses:
                    statement = statement.where(Transaction.status.in_(selected_statuses))

            # Apply expert filter
            if hasattr(self, 'expert_filter') and self.expert_filter.value != 'All':
                # Ensure expert_id_map exists
                if hasattr(self, 'expert_id_map'):
                    expert_id = self.expert_id_map.get(self.expert_filter.value)
                    if expert_id and expert_id != 'All':
                        statement = statement.where(Transaction.expert_id == expert_id)

            # Apply symbol filter
            if hasattr(self, 'symbol_filter') and self.symbol_filter.value:
                logger.debug(f"[TRANSACTIONS] Symbol filter: {self.symbol_filter.value}")
                statement = statement.where(Transaction.symbol.contains(self.symbol_filter.value.upper()))

            # Apply broker order ID filter - requires joining with TradingOrder
            if hasattr(self, 'broker_order_id_filter') and self.broker_order_id_filter.value and self.broker_order_id_filter.value.strip():
                logger.debug(f"[TRANSACTIONS] Broker order ID filter: {self.broker_order_id_filter.value}")
                from ...core.models import TradingOrder
                # Join with TradingOrder and filter by broker_order_id
                statement = statement.join(
                    TradingOrder,
                    Transaction.id == TradingOrder.transaction_id
                ).where(
                    TradingOrder.broker_order_id.contains(self.broker_order_id_filter.value.strip())
                ).distinct()

            # Execute query and separate transaction and expert
            logger.debug("[TRANSACTIONS] Executing query...")
            results = list(session.exec(statement).all())
            logger.debug(f"[TRANSACTIONS] Query returned {len(results)} results")
            transactions = []
            transaction_experts = {}
            for txn, expert in results:
                transactions.append(txn)
                transaction_experts[txn.id] = expert

            logger.debug(f"[TRANSACTIONS] Processed {len(transactions)} transactions")

            if not transactions:
                # Return empty list instead of creating UI here - let the caller handle it
                logger.debug("[TRANSACTIONS] No transactions found, returning empty list")
                return []

            # BATCH PRICE FETCHING: Collect all symbols from open transactions grouped by account
            # This prevents individual API calls and uses batch fetching instead
            from ...core.types import TransactionStatus
            from ...core.models import TradingOrder
            from collections import defaultdict

            symbols_by_account = defaultdict(set)  # account_id -> set of symbols
            txn_to_account = {}  # transaction_id -> account_id mapping

            for txn in transactions:
                if txn.status == TransactionStatus.OPENED and txn.open_price and txn.quantity:
                    # Get account_id for this transaction
                    order_stmt = select(TradingOrder).where(TradingOrder.transaction_id == txn.id).limit(1)
                    first_order = session.exec(order_stmt).first()
                    if first_order:
                        symbols_by_account[first_order.account_id].add(txn.symbol)
                        txn_to_account[txn.id] = first_order.account_id

            # Fetch prices in batch for each account
            current_prices = {}  # symbol -> price mapping
            for account_id, symbols in symbols_by_account.items():
                try:
                    account_inst = get_account_instance_from_id(account_id)
                    if account_inst and symbols:
                        symbols_list = list(symbols)
                        logger.debug(f"[BATCH] Fetching {len(symbols_list)} symbols for account {account_id}: {symbols_list}")
                        # Batch fetch - returns dict {symbol: price}
                        prices_dict = account_inst.get_instrument_current_price(symbols_list)
                        if prices_dict:
                            current_prices.update(prices_dict)
                except Exception as e:
                    logger.warning(f"Batch price fetch failed for account {account_id}: {e}")

            # Prepare table data
            logger.debug(f"[RENDER] _render_transactions_table() - Building rows for {len(transactions)} transactions")
            rows = []
            for txn in transactions:
                # Calculate current P/L for open positions using cached/batch-fetched price
                current_pnl = ''
                current_pnl_numeric = 0  # Numeric value for sorting (percentage)
                current_price_str = ''

                # Use batch-fetched prices for open transactions
                if txn.status == TransactionStatus.OPENED and txn.open_price and txn.quantity:
                    try:
                        current_price = current_prices.get(txn.symbol)
                        if current_price:
                            current_price_str = f"${current_price:.2f}"
                            # Calculate P/L: (current_price - open_price) * quantity
                            if txn.quantity > 0:  # Long position
                                pnl_current = (current_price - txn.open_price) * abs(txn.quantity)
                            else:  # Short position
                                pnl_current = (txn.open_price - current_price) * abs(txn.quantity)
                            # Calculate P/L percentage based on cost basis
                            cost_basis = txn.open_price * abs(txn.quantity)
                            pnl_pct = (pnl_current / cost_basis * 100) if cost_basis > 0 else 0
                            current_pnl = f"${pnl_current:+.2f} ({pnl_pct:+.1f}%)"
                            current_pnl_numeric = pnl_pct  # Store percentage for sorting
                    except Exception as e:
                        logger.debug(f"Could not calculate P/L for {txn.symbol}: {e}")

                # Closed P/L - calculate from open/close prices
                closed_pnl = ''
                closed_pnl_numeric = 0  # Numeric value for sorting
                if txn.close_price and txn.open_price and txn.quantity:
                    if txn.quantity > 0:  # Long position
                        pnl_closed = (txn.close_price - txn.open_price) * abs(txn.quantity)
                    else:  # Short position
                        pnl_closed = (txn.open_price - txn.close_price) * abs(txn.quantity)
                    closed_pnl = f"${pnl_closed:+.2f}"
                    closed_pnl_numeric = pnl_closed  # Store numeric value for sorting

                # Status styling
                status_color = {
                    TransactionStatus.OPENED: 'green',
                    TransactionStatus.CLOSING: 'orange',
                    TransactionStatus.CLOSED: 'gray',
                    TransactionStatus.WAITING: 'orange'
                }.get(txn.status, 'gray')

                # Get expert shortname (alias + ID)
                expert = transaction_experts.get(txn.id)
                expert_shortname = ''
                if expert:
                    expert_shortname = f"{expert.alias}-{expert.id}" if expert.alias else f"Expert-{expert.id}"

                # Get all orders for this transaction
                orders_data = []
                if txn.id:
                    try:
                        orders_statement = select(TradingOrder).where(
                            TradingOrder.transaction_id == txn.id
                        ).order_by(TradingOrder.created_at)
                        txn_orders = list(session.exec(orders_statement).all())

                        for order in txn_orders:
                            order_type_display = order.order_type.value if hasattr(order.order_type, 'value') else str(order.order_type)
                            order_side_display = order.side.value if hasattr(order.side, 'value') else str(order.side)
                            order_status_display = order.status.value if hasattr(order.status, 'value') else str(order.status)

                            # Determine if this is a TP/SL order
                            order_category = 'Entry'
                            if order.depends_on_order:
                                # Dependent orders are TP/SL orders
                                # Determine if it's TP or SL by checking order type and comment
                                is_stop_order = 'stop' in order_type_display.lower()
                                is_limit_order = 'limit' in order_type_display.lower()
                                comment_lower = (order.comment or '').lower()

                                # Check comment first for explicit indicators
                                if 'tp' in comment_lower or 'take_profit' in comment_lower or 'take profit' in comment_lower:
                                    order_category = 'Take Profit'
                                elif 'sl' in comment_lower or 'stop_loss' in comment_lower or 'stop loss' in comment_lower:
                                    order_category = 'Stop Loss'
                                # Fallback to order type heuristic
                                elif is_stop_order:
                                    order_category = 'Stop Loss'
                                elif is_limit_order:
                                    order_category = 'Take Profit'
                                else:
                                    order_category = 'Dependent'

                            orders_data.append({
                                'id': order.id,
                                'type': order_type_display,
                                'side': order_side_display,
                                'category': order_category,
                                'quantity': f"{order.quantity:.2f}" if order.quantity else '0.00',
                                'filled_qty': f"{order.filled_qty:.2f}" if order.filled_qty else '0.00',
                                'limit_price': f"${order.limit_price:.2f}" if order.limit_price else '',
                                'stop_price': f"${order.stop_price:.2f}" if order.stop_price else '',
                                'status': order_status_display,
                                'status_color': self._get_order_status_color(order.status),
                                'broker_order_id': order.broker_order_id or '',
                                'created_at': order.created_at.strftime('%Y-%m-%d %H:%M') if order.created_at else '',
                                'comment': order.comment or '',
                                'expert_recommendation_id': order.expert_recommendation_id,
                                'has_recommendation': order.expert_recommendation_id is not None
                            })
                    except Exception as e:
                        logger.error(f"Error loading orders for transaction {txn.id}: {e}")

                # Check if TP/SL are defined but have no valid orders
                # Valid means: order exists with correct type/price and is not CANCELED/REJECTED/ERROR
                has_missing_tpsl_orders = False
                if txn.status == TransactionStatus.OPENED:  # Only for open transactions
                    has_tp_defined = txn.take_profit is not None and txn.take_profit > 0
                    has_sl_defined = txn.stop_loss is not None and txn.stop_loss > 0

                    if has_tp_defined or has_sl_defined:
                        # Check if we have valid TP/SL orders
                        from ...core.types import OrderStatus

                        has_valid_tp_order = False
                        has_valid_sl_order = False
                        has_valid_bracket_order = False  # STOP_LIMIT order that covers both TP and SL

                        # Invalid order statuses (terminal failed states)
                        invalid_statuses = {'canceled', 'rejected', 'error', 'expired'}

                        # Entry order types to skip (not TP/SL orders)
                        entry_order_types = {'market', 'limit'}

                        for order in txn_orders:
                            # Get order type first to determine if this is an entry order
                            order_type = order.order_type.value.lower() if hasattr(order.order_type, 'value') else str(order.order_type).lower()

                            # Skip entry orders (MARKET/LIMIT without depends_on_order)
                            if order_type in entry_order_types and not order.depends_on_order:
                                continue

                            # Check if order is in valid state
                            order_status = order.status.value.lower() if hasattr(order.status, 'value') else str(order.status).lower()
                            is_valid_order = order_status not in invalid_statuses

                            if not is_valid_order:
                                continue

                            # Round prices to 1 decimal for comparison
                            order_limit_price = round(order.limit_price, 1) if order.limit_price else None
                            order_stop_price = round(order.stop_price, 1) if order.stop_price else None
                            txn_tp = round(txn.take_profit, 1) if txn.take_profit else None
                            txn_sl = round(txn.stop_loss, 1) if txn.stop_loss else None

                            # Check for bracket order (STOP_LIMIT that covers both TP and SL)
                            if has_tp_defined and has_sl_defined:
                                # OCO order: has both TP (limit_price) and SL (stop_price)
                                if order_type == 'oco':
                                    if order_limit_price == txn_tp and order_stop_price == txn_sl:
                                        has_valid_bracket_order = True
                                        has_valid_tp_order = True
                                        has_valid_sl_order = True
                                # Legacy STOP_LIMIT bracket orders
                                elif 'stop_limit' in order_type:
                                    # For bracket orders, check if both TP (limit) and SL (stop) match
                                    if order_limit_price == txn_tp and order_stop_price == txn_sl:
                                        has_valid_bracket_order = True
                                        has_valid_tp_order = True
                                        has_valid_sl_order = True

                            # Check for individual TP order (LIMIT or OCO with only TP)
                            if has_tp_defined and not has_valid_tp_order:
                                # OCO order with both TP and SL
                                if order_type == 'oco' and has_sl_defined:
                                    if order_limit_price == txn_tp and order_stop_price == txn_sl:
                                        has_valid_tp_order = True
                                # Individual TP-only orders (SELL_LIMIT / BUY_LIMIT)
                                elif ('limit' in order_type and 'stop' not in order_type) or order_type in ['sell_limit', 'buy_limit']:
                                    if order_limit_price == txn_tp:
                                        has_valid_tp_order = True

                            # Check for individual SL order (STOP or OCO with only SL)
                            if has_sl_defined and not has_valid_sl_order:
                                # OCO order with both TP and SL
                                if order_type == 'oco' and has_tp_defined:
                                    if order_limit_price == txn_tp and order_stop_price == txn_sl:
                                        has_valid_sl_order = True
                                # Individual SL-only orders (SELL_STOP / BUY_STOP)
                                elif ('stop' in order_type and 'limit' not in order_type) or order_type in ['sell_stop', 'buy_stop']:
                                    if order_stop_price == txn_sl:
                                        has_valid_sl_order = True

                        # Missing if TP/SL is defined but no valid order exists
                        has_missing_tpsl_orders = (has_tp_defined and not has_valid_tp_order) or (has_sl_defined and not has_valid_sl_order)

                row = {
                    'id': txn.id,
                    '_selected': txn.id in self.selected_transactions,  # Track selection state for checkbox
                    'symbol': txn.symbol,
                    'expert': expert_shortname,
                    'quantity': f"{txn.quantity:+.2f}",
                    'open_price': f"${txn.open_price:.2f}" if txn.open_price else '',
                    'current_price': current_price_str,
                    'close_price': f"${txn.close_price:.2f}" if txn.close_price else '',
                    'take_profit': f"${txn.take_profit:.2f}" if txn.take_profit else '',
                    'stop_loss': f"${txn.stop_loss:.2f}" if txn.stop_loss else '',
                    'current_pnl': current_pnl,
                    'current_pnl_numeric': current_pnl_numeric,  # Numeric value for sorting
                    'closed_pnl': closed_pnl,
                    'closed_pnl_numeric': closed_pnl_numeric,  # Numeric value for sorting
                    'status': txn.status.value,
                    'status_color': status_color,
                    'created_at': txn.created_at.strftime('%Y-%m-%d %H:%M') if txn.created_at else '',
                    'closed_at': txn.close_date.strftime('%Y-%m-%d %H:%M') if txn.close_date else '',
                    'is_open': txn.status == TransactionStatus.OPENED,
                    'is_waiting': txn.status == TransactionStatus.WAITING,  # Track WAITING status
                    'is_closing': txn.status == TransactionStatus.CLOSING,  # Track CLOSING status
                    'has_missing_tpsl_orders': has_missing_tpsl_orders,  # Track if TP/SL defined but no valid orders
                    'orders': orders_data,  # Add orders for expansion
                    'order_count': len(orders_data),  # Show order count
                    'actions': 'actions'
                }
                rows.append(row)

            logger.debug(f"[TRANSACTIONS] Returning {len(rows)} rows")
            return rows

        except Exception as e:
            logger.error(f"Error getting transactions data: {e}", exc_info=True)
            return []
        finally:
            session.close()

    def _render_transactions_table(self):
        """Render the main transactions table."""
        logger.debug("[RENDER] _render_transactions_table() - START")

        # Get transactions data
        logger.debug("[RENDER] Calling _get_transactions_data()...")
        rows = self._get_transactions_data()
        logger.debug(f"[RENDER] Got {len(rows) if rows else 0} rows")

        if not rows:
            logger.debug("[RENDER] No rows, showing 'No transactions found' message")
            ui.label('No transactions found.').classes('text-gray-500')
            return

        # Table columns
        columns = [
            {'name': 'select', 'label': '', 'field': 'select', 'align': 'left', 'sortable': False},  # Selection checkbox column
            {'name': 'expand', 'label': '', 'field': 'expand', 'align': 'left'},  # Expand column
            {'name': 'id', 'label': 'ID', 'field': 'id', 'align': 'center', 'sortable': True},
            {'name': 'symbol', 'label': 'Symbol', 'field': 'symbol', 'align': 'left', 'sortable': True},
            {'name': 'expert', 'label': 'Expert', 'field': 'expert', 'align': 'left', 'sortable': True},
            {'name': 'quantity', 'label': 'Qty', 'field': 'quantity', 'align': 'right', 'sortable': True},
            {'name': 'open_price', 'label': 'Open Price', 'field': 'open_price', 'align': 'right', 'sortable': True},
            {'name': 'current_price', 'label': 'Current', 'field': 'current_price', 'align': 'right'},
            {'name': 'close_price', 'label': 'Close Price', 'field': 'close_price', 'align': 'right'},
            {'name': 'take_profit', 'label': 'TP', 'field': 'take_profit', 'align': 'right'},
            {'name': 'stop_loss', 'label': 'SL', 'field': 'stop_loss', 'align': 'right'},
            {'name': 'current_pnl', 'label': 'Current P/L', 'field': 'current_pnl_numeric', 'align': 'right', 'sortable': True},
            {'name': 'closed_pnl', 'label': 'Closed P/L', 'field': 'closed_pnl_numeric', 'align': 'right', 'sortable': True},
            {'name': 'status', 'label': 'Status', 'field': 'status', 'align': 'center', 'sortable': True},
            {'name': 'order_count', 'label': 'Orders', 'field': 'order_count', 'align': 'center'},
            {'name': 'created_at', 'label': 'Created', 'field': 'created_at', 'align': 'left', 'sortable': True},
            {'name': 'closed_at', 'label': 'Closed', 'field': 'closed_at', 'align': 'left', 'sortable': True},
            {'name': 'actions', 'label': 'Actions', 'field': 'actions', 'align': 'center'}
        ]

        # Create table with expansion support
        logger.debug(f"[RENDER] _render_transactions_table() - Creating table with {len(rows)} rows")
        self.transactions_table = ui.table(
            columns=columns,
            rows=rows,
            row_key='id',
            pagination={'rowsPerPage': 20}
        ).classes('w-full').props('flat bordered')

        # Add row selection via click handler
        # Store reference to rows for selection tracking
        def on_row_click(row_data):
            """Handle row clicks to toggle selection."""
            row_id = row_data['id']
            if row_id in self.selected_transactions:
                del self.selected_transactions[row_id]
            else:
                self.selected_transactions[row_id] = True
            self._update_batch_buttons()
            # Refresh table to show updated styling
            self.transactions_table.update()

        # Attach click handler to table rows via Quasar's row-click event
        self.transactions_table.props('row-key="id" @row-click="(evt, row) => {}"')

        # We'll manually handle selection in the Vue template slot instead
        # Store the click handler for reference
        self._on_row_click = on_row_click

        # Add expand button in second column
        self.transactions_table.add_slot('body-cell-expand', '''
            <q-td :props="props">
                <q-btn
                    size="sm"
                    color="primary"
                    round
                    dense
                    @click="props.expand = !props.expand"
                    :icon="props.expand ? 'expand_less' : 'expand_more'"
                />
            </q-td>
        ''')

        # Add expansion details showing orders
        self.transactions_table.add_slot('body', '''
                <q-tr :props="props">
                    <q-td v-for="col in props.cols" :key="col.name" :props="props">
                        <template v-if="col.name === 'select'">
                            <q-checkbox
                                :model-value="props.row._selected || false"
                                @update:model-value="(val) => $parent.$emit('toggle_row_selection', props.row.id)"
                            />
                        </template>
                        <template v-else-if="col.name === 'expand'">
                            <q-btn
                                size="sm"
                                color="primary"
                                round
                                dense
                                @click="props.expand = !props.expand"
                                :icon="props.expand ? 'expand_less' : 'expand_more'"
                            />
                        </template>
                        <template v-else-if="col.name === 'status'">
                            <q-badge :color="props.row.status_color" :label="col.value" />
                        </template>
                        <template v-else-if="col.name === 'current_pnl'">
                            <span :class="props.row.current_pnl_numeric > 0 ? 'text-green-600 font-bold' : props.row.current_pnl_numeric < 0 ? 'text-red-600 font-bold' : ''">
                                {{ props.row.current_pnl }}
                            </span>
                        </template>
                        <template v-else-if="col.name === 'closed_pnl'">
                            <span :class="props.row.closed_pnl_numeric > 0 ? 'text-green-600 font-bold' : props.row.closed_pnl_numeric < 0 ? 'text-red-600 font-bold' : ''">
                                {{ props.row.closed_pnl }}
                            </span>
                        </template>
                        <template v-else-if="col.name === 'actions'">
                            <q-btn v-if="props.row.has_missing_tpsl_orders"
                                   icon="warning"
                                   size="sm"
                                   flat
                                   round
                                   color="warning"
                                   @click="$parent.$emit('recreate_tpsl_orders', props.row.id)"
                                   title="TP/SL defined but no valid orders - Click to recreate"
                            >
                                <q-tooltip>TP/SL defined but no valid orders - Click to recreate</q-tooltip>
                            </q-btn>
                            <q-btn v-if="props.row.is_open"
                                   icon="edit"
                                   size="sm"
                                   flat
                                   round
                                   color="primary"
                                   @click="$parent.$emit('edit_transaction', props.row.id)"
                                   title="Adjust TP/SL"
                            />
                            <q-btn v-if="(props.row.is_open || props.row.is_waiting) && !props.row.is_closing"
                                   icon="close"
                                   size="sm"
                                   flat
                                   round
                                   color="negative"
                                   @click="$parent.$emit('close_transaction', props.row.id)"
                                   :title="props.row.is_waiting ? 'Cancel Orders' : 'Close Position'"
                            />
                            <q-btn v-else-if="props.row.is_closing"
                                   icon="refresh"
                                   size="sm"
                                   flat
                                   round
                                   color="orange"
                                   @click="$parent.$emit('retry_close_transaction', props.row.id)"
                                   title="Retry Close (reset status and try again)"
                            />
                            <span v-else class="text-grey-5">—</span>
                        </template>
                        <template v-else>
                            {{ col.value }}
                        </template>
                    </q-td>
                </q-tr>
                <q-tr v-show="props.expand" :props="props" class="bg-blue-50">
                    <q-td colspan="100%">
                        <div class="q-pa-md">
                            <div class="text-subtitle2 q-mb-sm">📋 Related Orders ({{ props.row.order_count }})</div>
                            <q-markup-table flat bordered dense v-if="props.row.orders.length > 0">
                                <thead>
                                    <tr class="bg-grey-3">
                                        <th class="text-left">ID</th>
                                        <th class="text-left">Category</th>
                                        <th class="text-left">Type</th>
                                        <th class="text-left">Side</th>
                                        <th class="text-right">Quantity</th>
                                        <th class="text-right">Filled</th>
                                        <th class="text-right">Limit Price</th>
                                        <th class="text-right">Stop Price</th>
                                        <th class="text-center">Status</th>
                                        <th class="text-left">Broker ID</th>
                                        <th class="text-left">Created</th>
                                        <th class="text-left">Comment</th>
                                        <th class="text-center">Actions</th>
                                    </tr>
                                </thead>
                                <tbody>
                                    <tr v-for="order in props.row.orders" :key="order.id">
                                        <td class="text-left">{{ order.id }}</td>
                                        <td class="text-left">
                                            <q-badge :color="order.category === 'Entry' ? 'blue' : order.category === 'Take Profit' ? 'green' : order.category === 'Stop Loss' ? 'red' : 'grey'"
                                                     :label="order.category" />
                                        </td>
                                        <td class="text-left">{{ order.type }}</td>
                                        <td class="text-left">
                                            <q-badge :color="order.side === 'BUY' ? 'positive' : 'negative'" :label="order.side" />
                                        </td>
                                        <td class="text-right">{{ order.quantity }}</td>
                                        <td class="text-right">{{ order.filled_qty }}</td>
                                        <td class="text-right">{{ order.limit_price }}</td>
                                        <td class="text-right">{{ order.stop_price }}</td>
                                        <td class="text-center">
                                            <q-badge :color="order.status_color" :label="order.status" />
                                        </td>
                                        <td class="text-left text-caption">{{ order.broker_order_id }}</td>
                                        <td class="text-left">{{ order.created_at }}</td>
                                        <td class="text-left text-caption">{{ order.comment }}</td>
                                        <td class="text-center">
                                            <q-btn v-if="order.has_recommendation"
                                                   icon="info"
                                                   size="sm"
                                                   flat
                                                   round
                                                   color="primary"
                                                   @click="$parent.$emit('view_recommendation', order.expert_recommendation_id)"
                                                   title="View Expert Recommendation"
                                            />
                                            <span v-else class="text-grey-5">—</span>
                                        </td>
                                    </tr>
                                </tbody>
                            </q-markup-table>
                            <div v-else class="text-grey-6 text-center q-pa-md">No orders found for this transaction</div>
                        </div>
                    </q-td>
                </q-tr>
        ''')

        # Handle events
        logger.debug("[RENDER] _render_transactions_table() - Setting up event handlers")
        self.transactions_table.on('toggle_row_selection', self._toggle_row_selection)
        self.transactions_table.on('edit_transaction', self._show_edit_dialog)
        self.transactions_table.on('close_transaction', self._show_close_dialog)
        self.transactions_table.on('retry_close_transaction', self._show_retry_close_dialog)
        self.transactions_table.on('recreate_tpsl_orders', self._recreate_tpsl_orders)
        self.transactions_table.on('view_recommendation', self._show_recommendation_dialog)
        logger.debug("[RENDER] _render_transactions_table() - END (success)")

    def _toggle_row_selection(self, event_data):
        """Toggle selection state for a transaction row."""
        # Extract transaction_id from event_data
        transaction_id = event_data.args if hasattr(event_data, 'args') else event_data

        if transaction_id in self.selected_transactions:
            del self.selected_transactions[transaction_id]
        else:
            self.selected_transactions[transaction_id] = True

        # Update the specific row's _selected flag in the table data
        for row in self.transactions_table.rows:
            if row['id'] == transaction_id:
                row['_selected'] = transaction_id in self.selected_transactions
                break

        self._update_batch_buttons()
        # Refresh table to show updated checkbox state
        self.transactions_table.update()

    def _recreate_tpsl_orders(self, event_data):
        """Recreate TP/SL orders for a transaction that has TP/SL defined but no valid orders."""
        from ...core.models import Transaction, TradingOrder
        from ...core.types import OrderStatus

        # Extract transaction_id from event_data
        transaction_id = event_data.args if hasattr(event_data, 'args') else event_data

        try:
            txn = get_instance(Transaction, transaction_id)
            if not txn:
                ui.notify('Transaction not found', type='negative')
                return

            # Verify transaction is open
            from ...core.types import TransactionStatus
            if txn.status != TransactionStatus.OPENED:
                ui.notify('Transaction is not open', type='negative')
                return

            # Verify TP or SL is defined
            if not ((txn.take_profit and txn.take_profit > 0) or (txn.stop_loss and txn.stop_loss > 0)):
                ui.notify('No TP/SL defined for this transaction', type='negative')
                return

            # Get the entry order to find account_id
            with get_db() as session:
                entry_order_stmt = select(TradingOrder).where(
                    TradingOrder.transaction_id == transaction_id,
                    TradingOrder.depends_on_order == None  # Entry order has no dependencies
                ).limit(1)
                entry_order = session.exec(entry_order_stmt).first()

                if not entry_order:
                    ui.notify('Could not find entry order', type='negative')
                    return

                account_id = entry_order.account_id

            # Cancel any existing TP/SL orders that are still active
            with get_db() as session:
                existing_tpsl_stmt = select(TradingOrder).where(
                    TradingOrder.transaction_id == transaction_id,
                    TradingOrder.depends_on_order != None  # TP/SL orders depend on entry
                )
                existing_tpsl_orders = list(session.exec(existing_tpsl_stmt).all())

                account_inst = get_account_instance_from_id(account_id)
                if not account_inst:
                    ui.notify('Could not load account instance', type='negative')
                    return

                for order in existing_tpsl_orders:
                    # Only cancel orders that are not already in terminal state
                    terminal_statuses = OrderStatus.get_terminal_statuses()
                    if order.status not in terminal_statuses:
                        try:
                            account_inst.cancel_order(order.id)
                            logger.info(f"Canceled existing TP/SL order {order.id} for transaction {transaction_id}")
                        except Exception as e:
                            logger.warning(f"Failed to cancel order {order.id}: {e}")

            # Recreate TP/SL orders using the new adjust methods (creates OCO/OTO orders)
            orders_created = []

            # Check if both TP and SL are defined - if so, use adjust_tp_sl for OCO order
            has_tp = txn.take_profit and txn.take_profit > 0
            has_sl = txn.stop_loss and txn.stop_loss > 0

            if has_tp and has_sl:
                # Both TP and SL defined - create as OCO order
                try:
                    success = account_inst.adjust_tp_sl(txn, txn.take_profit, txn.stop_loss)
                    if success:
                        orders_created.extend(['TP', 'SL'])
                        logger.info(
                            f"Created OCO order with TP at ${txn.take_profit:.2f} "
                            f"and SL at ${txn.stop_loss:.2f} "
                            f"for transaction {transaction_id}"
                        )
                    else:
                        logger.warning(f"Failed to create OCO order for transaction {transaction_id}")
                except Exception as e:
                    logger.error(f"Failed to create TP/SL OCO order: {e}", exc_info=True)
            else:
                # Only one of TP or SL defined - create separately
                if has_tp:
                    try:
                        success = account_inst.adjust_tp(txn, txn.take_profit)
                        if success:
                            orders_created.append('TP')
                            logger.info(f"Created TP order at ${txn.take_profit:.2f} for transaction {transaction_id}")
                        else:
                            logger.warning(f"Failed to create TP order for transaction {transaction_id}")
                    except Exception as e:
                        logger.error(f"Failed to create TP order: {e}", exc_info=True)

                if has_sl:
                    try:
                        success = account_inst.adjust_sl(txn, txn.stop_loss)
                        if success:
                            orders_created.append('SL')
                            logger.info(f"Created SL order at ${txn.stop_loss:.2f} for transaction {transaction_id}")
                        else:
                            logger.warning(f"Failed to create SL order for transaction {transaction_id}")
                    except Exception as e:
                        logger.error(f"Failed to create SL order: {e}", exc_info=True)

            if orders_created:
                orders_str = ' and '.join(orders_created)
                ui.notify(f'✓ {orders_str} order(s) recreated for {txn.symbol}', type='positive')
                logger.info(f"Successfully recreated {orders_str} orders for transaction {transaction_id}")
            else:
                ui.notify('No TP/SL orders were created', type='warning')
                logger.warning(f"No TP/SL orders were created for transaction {transaction_id}")

            # Refresh the table
            self._refresh_transactions()

        except Exception as e:
            logger.error(f"Error recreating TP/SL orders for transaction {transaction_id}: {e}", exc_info=True)
            ui.notify(f'Error: {str(e)}', type='negative')

    def _show_edit_dialog(self, event_data):
        """Show dialog to edit TP/SL for a transaction."""
        from ...core.models import Transaction

        # Extract transaction_id from event_data
        transaction_id = event_data.args if hasattr(event_data, 'args') else event_data

        txn = get_instance(Transaction, transaction_id)
        if not txn:
            ui.notify('Transaction not found', type='negative')
            return

        with ui.dialog() as dialog, ui.card().classes('w-96'):
            ui.label(f'Adjust TP/SL for {txn.symbol}').classes('text-h6 mb-4')

            with ui.column().classes('w-full gap-4'):
                ui.label(f'Position: {txn.quantity:+.2f} @ ${txn.open_price:.2f}').classes('text-sm text-gray-600')

                tp_input = ui.number(
                    label='Take Profit Price',
                    value=txn.take_profit if txn.take_profit else None,
                    format='%.2f',
                    prefix='$'
                ).classes('w-full')

                sl_input = ui.number(
                    label='Stop Loss Price',
                    value=txn.stop_loss if txn.stop_loss else None,
                    format='%.2f',
                    prefix='$'
                ).classes('w-full')

                with ui.row().classes('w-full justify-end gap-2'):
                    ui.button('Cancel', on_click=dialog.close).props('flat')
                    ui.button('Update', on_click=lambda: self._update_tp_sl(
                        transaction_id, tp_input.value, sl_input.value, dialog
                    )).props('color=primary')

        dialog.open()

    def _update_tp_sl(self, transaction_id, tp_price, sl_price, dialog):
        """Update TP/SL for a transaction."""
        from ...core.models import Transaction, TradingOrder
        from ...core.db import update_instance, get_db
        from sqlmodel import select, Session

        try:
            txn = get_instance(Transaction, transaction_id)
            if not txn:
                ui.notify('Transaction not found', type='negative')
                return

            # Query for the first (opening) order associated with this transaction
            session = get_db()
            order_statement = select(TradingOrder).where(
                TradingOrder.transaction_id == transaction_id
            ).order_by(TradingOrder.created_at).limit(1)
            order = session.exec(order_statement).first()

            if not order or not order.account_id:
                ui.notify('No orders linked to this transaction or order account not found', type='negative')
                return

            # Get account to use adjust_tp/adjust_sl methods
            from ...modules.accounts import get_account_class
            from ...core.models import AccountDefinition

            acc_def = get_instance(AccountDefinition, order.account_id)
            if not acc_def:
                ui.notify('Account definition not found', type='negative')
                return

            account_class = get_account_class(acc_def.provider)
            if not account_class:
                ui.notify(f'Account provider {acc_def.provider} not found', type='negative')
                return

            account = account_class(acc_def.id)

            # Detect changes including deletions (None/0 is a valid change)
            # Convert empty strings to None for proper comparison
            tp_value = tp_price if tp_price else None
            sl_value = sl_price if sl_price else None
            tp_changed = tp_value != txn.take_profit
            sl_changed = sl_value != txn.stop_loss

            # Update transaction values first (source of truth)
            if tp_changed:
                txn.take_profit = tp_value
            if sl_changed:
                txn.stop_loss = sl_value

            if tp_changed or sl_changed:
                from ...core.db import update_instance
                update_instance(txn)

            # Now call account methods to sync orders with transaction state
            # The account methods will determine correct order types (OCO vs individual)
            if tp_changed and sl_changed:
                # Both changed
                if tp_value and sl_value:
                    # Both have values - account will create/update OCO
                    try:
                        success = account.adjust_tp_sl(txn, tp_value, sl_value)
                        if success:
                            ui.notify(f'TP/SL updated to ${tp_value:.2f}/${sl_value:.2f}', type='positive')
                        else:
                            ui.notify('Failed to update TP/SL', type='negative')
                    except Exception as e:
                        ui.notify(f'Error updating TP/SL: {str(e)}', type='negative')
                        logger.error(f"Error updating TP/SL: {e}", exc_info=True)
                elif tp_value:
                    # Only TP remains - account will create individual LIMIT order
                    try:
                        success = account.adjust_tp(txn, tp_value)
                        if success:
                            ui.notify(f'TP updated to ${tp_value:.2f}, SL removed', type='positive')
                        else:
                            ui.notify('Failed to update TP', type='negative')
                    except Exception as e:
                        ui.notify(f'Error updating TP: {str(e)}', type='negative')
                        logger.error(f"Error updating TP: {e}", exc_info=True)
                elif sl_value:
                    # Only SL remains - account will create individual STOP order
                    try:
                        success = account.adjust_sl(txn, sl_value)
                        if success:
                            ui.notify(f'SL updated to ${sl_value:.2f}, TP removed', type='positive')
                        else:
                            ui.notify('Failed to update SL', type='negative')
                    except Exception as e:
                        ui.notify(f'Error updating SL: {str(e)}', type='negative')
                        logger.error(f"Error updating SL: {e}", exc_info=True)
                else:
                    # Both deleted - cancel all TP/SL orders
                    try:
                        with Session(get_db().bind) as session:
                            orders = session.exec(
                                select(TradingOrder).where(
                                    TradingOrder.transaction_id == txn.id,
                                    TradingOrder.status.notin_(OrderStatus.get_terminal_statuses()),
                                    TradingOrder.order_type.notin_([OrderType.MARKET, "limit"])
                                )
                            ).all()
                            for order in orders:
                                if order.broker_order_id:
                                    try:
                                        account.cancel_order(order.id)
                                        logger.info(f"Cancelled order {order.id}")
                                    except Exception as e:
                                        logger.warning(f"Failed to cancel order {order.id}: {e}")
                        ui.notify('TP/SL removed', type='positive')
                    except Exception as e:
                        ui.notify(f'Error removing TP/SL: {str(e)}', type='negative')
                        logger.error(f"Error removing TP/SL: {e}", exc_info=True)
            elif tp_changed:
                # Only TP changed - account method will handle OCO ↔ individual transitions
                if tp_value:
                    try:
                        success = account.adjust_tp(txn, tp_value)
                        if success:
                            ui.notify(f'Take Profit updated to ${tp_value:.2f}', type='positive')
                        else:
                            ui.notify('Failed to update Take Profit', type='negative')
                    except Exception as e:
                        ui.notify(f'Error updating TP: {str(e)}', type='negative')
                        logger.error(f"Error updating TP: {e}", exc_info=True)
                else:
                    # TP deleted - if SL exists, account will create SL-only order
                    if txn.stop_loss:
                        try:
                            success = account.adjust_sl(txn, txn.stop_loss)
                            if success:
                                ui.notify('Take Profit removed, Stop Loss kept', type='positive')
                            else:
                                ui.notify('Failed to remove Take Profit', type='negative')
                        except Exception as e:
                            ui.notify(f'Error removing TP: {str(e)}', type='negative')
                            logger.error(f"Error removing TP: {e}", exc_info=True)
                    else:
                        # No SL either - cancel all TP/SL orders
                        try:
                            with Session(get_db().bind) as session:
                                orders = session.exec(
                                    select(TradingOrder).where(
                                        TradingOrder.transaction_id == txn.id,
                                        TradingOrder.status.notin_(OrderStatus.get_terminal_statuses()),
                                        TradingOrder.order_type.notin_([OrderType.MARKET, "limit"])
                                    )
                                ).all()
                                for order in orders:
                                    if order.broker_order_id:
                                        try:
                                            account.cancel_order(order.id)
                                            logger.info(f"Cancelled order {order.id}")
                                        except Exception as e:
                                            logger.warning(f"Failed to cancel order {order.id}: {e}")
                            ui.notify('Take Profit removed', type='positive')
                        except Exception as e:
                            ui.notify(f'Error removing TP: {str(e)}', type='negative')
                            logger.error(f"Error removing TP: {e}", exc_info=True)
            elif sl_changed:
                # Only SL changed - account method will handle OCO ↔ individual transitions
                if sl_value:
                    try:
                        success = account.adjust_sl(txn, sl_value)
                        if success:
                            ui.notify(f'Stop Loss updated to ${sl_value:.2f}', type='positive')
                        else:
                            ui.notify('Failed to update Stop Loss', type='negative')
                    except Exception as e:
                        ui.notify(f'Error updating SL: {str(e)}', type='negative')
                        logger.error(f"Error updating SL: {e}", exc_info=True)
                else:
                    # SL deleted - if TP exists, account will create TP-only order
                    if txn.take_profit:
                        try:
                            success = account.adjust_tp(txn, txn.take_profit)
                            if success:
                                ui.notify('Stop Loss removed, Take Profit kept', type='positive')
                            else:
                                ui.notify('Failed to remove Stop Loss', type='negative')
                        except Exception as e:
                            ui.notify(f'Error removing SL: {str(e)}', type='negative')
                            logger.error(f"Error removing SL: {e}", exc_info=True)
                    else:
                        # No TP either - cancel all TP/SL orders
                        try:
                            with Session(get_db().bind) as session:
                                orders = session.exec(
                                    select(TradingOrder).where(
                                        TradingOrder.transaction_id == txn.id,
                                        TradingOrder.status.notin_(OrderStatus.get_terminal_statuses()),
                                        TradingOrder.order_type.notin_([OrderType.MARKET, "limit"])
                                    )
                                ).all()
                                for order in orders:
                                    if order.broker_order_id:
                                        try:
                                            account.cancel_order(order.id)
                                            logger.info(f"Cancelled order {order.id}")
                                        except Exception as e:
                                            logger.warning(f"Failed to cancel order {order.id}: {e}")
                            ui.notify('Stop Loss removed', type='positive')
                        except Exception as e:
                            ui.notify(f'Error removing SL: {str(e)}', type='negative')
                            logger.error(f"Error removing SL: {e}", exc_info=True)

            dialog.close()
            self._refresh_transactions()

        except Exception as e:
            ui.notify(f'Error: {str(e)}', type='negative')
            logger.error(f"Error updating TP/SL: {e}", exc_info=True)

    def _show_retry_close_dialog(self, event_data):
        """Show dialog to retry closing a transaction stuck in CLOSING status."""
        from ...core.models import Transaction
        from ...core.types import TransactionStatus

        # Extract transaction_id from event_data
        transaction_id = event_data.args if hasattr(event_data, 'args') else event_data

        txn = get_instance(Transaction, transaction_id)
        if not txn:
            ui.notify('Transaction not found', type='negative')
            return

        if txn.status != TransactionStatus.CLOSING:
            ui.notify('Transaction is not in CLOSING status', type='warning')
            return

        with ui.dialog() as dialog, ui.card().classes('w-96'):
            ui.label('⚠️ Retry Close Transaction').classes('text-h6 mb-4')

            ui.label('This transaction is stuck in CLOSING status.').classes('mb-2')
            ui.label(f'{txn.symbol}: {txn.quantity:+.2f} @ ${txn.open_price:.2f}').classes('text-sm font-bold mb-2')

            ui.separator().classes('my-4')

            ui.label('This will:').classes('font-bold mb-2')
            with ui.column().classes('ml-4 mb-4'):
                ui.label('1. Reset status back to OPENED/WAITING').classes('text-sm')
                ui.label('2. Allow you to retry closing the position').classes('text-sm')
                ui.label('3. You can then click Close again').classes('text-sm')

            ui.label('⚠️ Use this if the close operation failed or got stuck.').classes('text-sm text-orange mb-4')

            with ui.row().classes('w-full justify-end gap-2'):
                ui.button('Cancel', on_click=dialog.close).props('flat')
                ui.button('Reset & Retry', on_click=lambda: self._retry_close_position(transaction_id, dialog)).props('color=orange')

        dialog.open()

    def _retry_close_position(self, transaction_id, dialog):
        """Reset transaction status from CLOSING to allow retry, then close using AccountInterface."""
        from ...core.models import Transaction, TradingOrder
        from ...core.types import TransactionStatus, OrderStatus
        from ...core.db import update_instance

        try:
            txn = get_instance(Transaction, transaction_id)
            if not txn:
                ui.notify('Transaction not found', type='negative')
                return

            if txn.status != TransactionStatus.CLOSING:
                ui.notify('Transaction is not in CLOSING status', type='warning')
                dialog.close()
                return

            # Get account interface using centralized helper
            from ...core.utils import get_account_instance_from_transaction, close_transaction_with_logging

            account = get_account_instance_from_transaction(transaction_id)
            if not account:
                if txn.status.value == 'FAILED':
                    ui.notify('Cannot process FAILED transaction - transaction was previously marked as failed', type='negative')
                    dialog.close()
                    return
                else:
                    # Transaction is orphaned (no orders found) - mark as closed with logging
                    logger.warning(f"Transaction {transaction_id} has no orders - marking as closed")
                    close_transaction_with_logging(
                        txn,
                        account_id=1,  # Use default account ID for orphaned transactions
                        close_reason="orphaned_no_orders",
                        additional_data={"note": "Transaction had no associated orders"}
                    )
                    update_instance(txn)
                    ui.notify('Transaction closed (was orphaned with no orders)', type='positive')
                    dialog.close()
                    self._refresh_transactions()
                    return

            # Capture client for background task
            from nicegui import context
            client = context.client

            # Use async AccountInterface close_transaction method (handles retry logic and refresh)
            logger.info(f"Retrying close for transaction {transaction_id}")

            async def retry_close_async():
                try:
                    result = await account.close_transaction_async(transaction_id)

                    # Use client.safe_invoke for UI updates from background task
                    def show_result():
                        if result['success']:
                            ui.notify(result['message'], type='positive')
                            logger.info(f"Retry close transaction {transaction_id}: {result['message']}")
                        else:
                            ui.notify(result['message'], type='negative')
                            logger.error(f"Retry close transaction {transaction_id} failed: {result['message']}")
                        self._refresh_transactions()

                    client.safe_invoke(show_result)

                except Exception as e:
                    # Schedule error notification via client
                    def show_error():
                        ui.notify(f'Error during retry close: {str(e)}', type='negative')
                    client.safe_invoke(show_error)
                    logger.error(f"Error in retry_close_async: {e}", exc_info=True)

            # Run async operation using background_tasks
            from nicegui import background_tasks
            background_tasks.create(retry_close_async(), name=f'retry_close_{transaction_id}')

            dialog.close()
            # Show immediate feedback
            ui.notify('Closing transaction...', type='info')

        except Exception as e:
            ui.notify(f'Error: {str(e)}', type='negative')
            logger.error(f"Error retrying close position: {e}", exc_info=True)

    def _show_close_dialog(self, event_data):
        """Show confirmation dialog before closing a position."""
        from ...core.models import Transaction

        # Extract transaction_id from event_data
        transaction_id = event_data.args if hasattr(event_data, 'args') else event_data

        txn = get_instance(Transaction, transaction_id)
        if not txn:
            ui.notify('Transaction not found', type='negative')
            return

        with ui.dialog() as dialog, ui.card().classes('w-96'):
            ui.label(f'Close Position').classes('text-h6 mb-4')

            ui.label(f'Are you sure you want to close this position?').classes('mb-2')
            ui.label(f'{txn.symbol}: {txn.quantity:+.2f} @ ${txn.open_price:.2f}').classes('text-sm font-bold mb-4')

            with ui.row().classes('w-full justify-end gap-2'):
                ui.button('Cancel', on_click=dialog.close).props('flat')
                ui.button('Close Position', on_click=lambda: self._close_position(transaction_id, dialog)).props('color=negative')

        dialog.open()

    def _close_position(self, transaction_id, dialog):
        """
        Close a position using AccountInterface.close_transaction method.
        This handles all closing logic including:
        - Canceling unfilled orders
        - Deleting WAITING_TRIGGER orders
        - Checking for existing close orders
        - Creating new closing orders if needed
        """
        from ...core.models import Transaction, TradingOrder
        from ...core.types import TransactionStatus

        try:
            txn = get_instance(Transaction, transaction_id)
            if not txn:
                ui.notify('Transaction not found', type='negative')
                return

            # Check if transaction is already being closed
            if txn.status == TransactionStatus.CLOSING:
                ui.notify('Transaction is already being closed', type='warning')
                dialog.close()
                return

            # Get account interface using centralized helper
            from ...core.utils import get_account_instance_from_transaction, close_transaction_with_logging

            account = get_account_instance_from_transaction(transaction_id)
            if not account:
                if txn.status.value == 'FAILED':
                    ui.notify('Cannot process FAILED transaction - transaction was previously marked as failed', type='negative')
                    dialog.close()
                    return
                else:
                    # Transaction is orphaned (no orders found) - mark as closed with logging
                    logger.warning(f"Transaction {transaction_id} has no orders - marking as closed")
                    close_transaction_with_logging(
                        txn,
                        account_id=1,  # Use default account ID for orphaned transactions
                        close_reason="orphaned_no_orders",
                        additional_data={"note": "Transaction had no associated orders"}
                    )
                    update_instance(txn)
                    ui.notify('Transaction closed (was orphaned with no orders)', type='positive')
                    dialog.close()
                    self._refresh_transactions()
                    return

            # Capture client for background task
            from nicegui import context
            client = context.client

            # Use async AccountInterface close_transaction method (includes refresh)
            logger.info(f"Closing transaction {transaction_id}")

            async def close_async():
                try:
                    result = await account.close_transaction_async(transaction_id)

                    # Use client.safe_invoke for UI updates from background task
                    def show_result():
                        if result['success']:
                            ui.notify(result['message'], type='positive')
                            logger.info(f"Close transaction {transaction_id}: {result['message']}")
                        else:
                            ui.notify(result['message'], type='negative')
                            logger.error(f"Close transaction {transaction_id} failed: {result['message']}")
                        self._refresh_transactions()

                    client.safe_invoke(show_result)

                except Exception as e:
                    # Schedule error notification via client
                    def show_error():
                        ui.notify(f'Error during close: {str(e)}', type='negative')
                    client.safe_invoke(show_error)
                    logger.error(f"Error in close_async: {e}", exc_info=True)

            # Run async operation using background_tasks
            from nicegui import background_tasks
            background_tasks.create(close_async(), name=f'close_{transaction_id}')

            dialog.close()
            # Show immediate feedback
            ui.notify('Closing transaction...', type='info')

        except Exception as e:
            ui.notify(f'Error: {str(e)}', type='negative')
            logger.error(f"Error closing position: {e}", exc_info=True)

    def _show_recommendation_dialog(self, event_data):
        """Show expert recommendation details in a dialog."""
        from ...core.models import ExpertRecommendation, ExpertInstance

        # Extract recommendation_id from event_data
        recommendation_id = event_data.args if hasattr(event_data, 'args') else event_data

        if not recommendation_id:
            ui.notify('No recommendation ID provided', type='warning')
            return

        # Get the recommendation
        rec = get_instance(ExpertRecommendation, recommendation_id)
        if not rec:
            ui.notify('Recommendation not found', type='negative')
            return

        # Get expert instance
        expert = get_instance(ExpertInstance, rec.instance_id) if rec.instance_id else None
        expert_name = f"{expert.expert} (ID: {expert.id})" if expert else "Unknown Expert"

        with ui.dialog() as dialog, ui.card().classes('w-full max-w-2xl'):
            ui.label('📊 Expert Recommendation Details').classes('text-h6 mb-4')

            # Expert and symbol info
            with ui.row().classes('w-full mb-4'):
                with ui.card().classes('flex-1'):
                    ui.label('Expert').classes('text-caption text-grey-7')
                    ui.label(expert_name).classes('text-body1 font-bold')
                with ui.card().classes('flex-1'):
                    ui.label('Symbol').classes('text-caption text-grey-7')
                    ui.label(rec.symbol).classes('text-body1 font-bold')

            # Recommendation details
            with ui.grid(columns=2).classes('w-full gap-4 mb-4'):
                # Trade recommendation
                with ui.card():
                    ui.label('Recommendation').classes('text-caption text-grey-7')
                    rec_color = 'green' if rec.recommended_action.value == 'BUY' else 'red' if rec.recommended_action.value == 'SELL' else 'grey'
                    ui.badge(rec.recommended_action.value, color=rec_color).classes('text-body1')

                # Confidence
                with ui.card():
                    ui.label('Confidence').classes('text-caption text-grey-7')
                    confidence_pct = rec.confidence if rec.confidence else 0.0
                    ui.label(f'{confidence_pct:.1f}%').classes('text-body1 font-bold')

                # Expected profit
                with ui.card():
                    ui.label('Expected Profit').classes('text-caption text-grey-7')
                    profit_str = f'{rec.expected_profit_percent:+.2f}%' if rec.expected_profit_percent else 'N/A'
                    ui.label(profit_str).classes('text-body1 font-bold')

                # Price at date
                with ui.card():
                    ui.label('Price at Recommendation').classes('text-caption text-grey-7')
                    price_str = f'${rec.price_at_date:.2f}' if rec.price_at_date else 'N/A'
                    ui.label(price_str).classes('text-body1')

                # Time horizon
                if rec.time_horizon:
                    with ui.card():
                        ui.label('Time Horizon').classes('text-caption text-grey-7')
                        ui.label(rec.time_horizon.value).classes('text-body1')

                # Risk level
                if rec.risk_level:
                    with ui.card():
                        ui.label('Risk Level').classes('text-caption text-grey-7')
                        risk_color = 'red' if 'HIGH' in rec.risk_level.value else 'orange' if 'MEDIUM' in rec.risk_level.value else 'green'
                        ui.badge(rec.risk_level.value, color=risk_color).classes('text-body1')

            # Analysis/Reasoning
            if rec.details:
                with ui.card().classes('w-full mb-4'):
                    ui.label('Analysis').classes('text-caption text-grey-7 mb-2')
                    ui.label(rec.details).classes('text-body2 whitespace-pre-wrap')

            # Metadata
            with ui.expansion('Metadata', icon='info').classes('w-full'):
                with ui.grid(columns=2).classes('gap-2'):
                    ui.label('Recommendation ID:').classes('text-caption font-bold')
                    ui.label(str(rec.id)).classes('text-caption')

                    ui.label('Created:').classes('text-caption font-bold')
                    created_str = rec.created_at.strftime('%Y-%m-%d %H:%M:%S') if rec.created_at else 'N/A'
                    ui.label(created_str).classes('text-caption')

                    ui.label('Market Analysis ID:').classes('text-caption font-bold')
                    ui.label(str(rec.market_analysis_id) if rec.market_analysis_id else 'N/A').classes('text-caption')

            # Action buttons
            with ui.row().classes('w-full justify-between mt-4'):
                # Navigate to analysis button (only show if market_analysis_id exists)
                if rec.market_analysis_id:
                    ui.button('View Market Analysis',
                             on_click=lambda: ui.navigate.to(f'/market_analysis/{rec.market_analysis_id}'),
                             icon='analytics').props('color=secondary')
                else:
                    ui.space()  # Empty space if no analysis link

                # Close button
                ui.button('Close', on_click=dialog.close).props('flat color=primary')

        dialog.open()

    def _select_all_transactions(self):
        """Select all visible transactions."""
        if not self.transactions_table:
            return

        # Select all current rows
        for row in self.transactions_table.rows:
            self.selected_transactions[row['id']] = True
            row['_selected'] = True

        self._update_batch_buttons()
        # Update table to show checkboxes
        self.transactions_table.update()

    def _clear_selected_transactions(self):
        """Clear all selected transactions."""
        self.selected_transactions.clear()
        # Update all rows to reflect cleared selection
        if self.transactions_table:
            for row in self.transactions_table.rows:
                row['_selected'] = False
        self._update_batch_buttons()
        # Update table to hide checkboxes
        if self.transactions_table:
            self.transactions_table.update()

    def _update_batch_buttons(self):
        """Show/hide batch operation buttons based on selection."""
        if not hasattr(self, 'batch_close_btn'):
            return

        has_selection = len(self.selected_transactions) > 0

        self.batch_select_all_btn.set_visibility(True)
        self.batch_clear_btn.set_visibility(has_selection)
        self.batch_close_btn.set_visibility(has_selection)
        self.batch_adjust_tp_btn.set_visibility(has_selection)

    def _batch_close_transactions(self):
        """Show confirmation dialog and close all selected transactions."""
        if not self.selected_transactions:
            ui.notify('No transactions selected', type='warning')
            return

        count = len(self.selected_transactions)

        with ui.dialog() as dialog, ui.card().classes('w-full max-w-sm'):
            ui.label('Confirm Batch Close').classes('text-h6 mb-4')

            ui.label(f'Are you sure you want to close {count} transaction{"s" if count != 1 else ""}?').classes('text-body1 mb-4')
            ui.label('This action cannot be undone.').classes('text-caption text-red-700 mb-4')

            with ui.row().classes('w-full justify-end gap-2'):
                ui.button('Cancel', on_click=dialog.close).props('flat')
                ui.button('Confirm Close', on_click=lambda: self._execute_batch_close(dialog)).props('color=negative')

        dialog.open()

    def _execute_batch_close(self, dialog):
        """Execute batch close operation (async, non-blocking)."""
        dialog.close()

        transaction_ids = list(self.selected_transactions.keys())
        if not transaction_ids:
            ui.notify('No transactions selected', type='warning')
            return

        # Capture context for background task
        from nicegui import context, background_tasks
        client = context.client

        async def batch_close_async():
            """Async batch close operation."""
            try:
                from ...core.db import get_instance
                from ...core.models import Transaction, ExpertInstance
                from ...core.utils import get_account_instance_from_id

                success_count = 0
                failed = []

                for txn_id in transaction_ids:
                    try:
                        txn = get_instance(Transaction, txn_id)
                        if not txn:
                            failed.append(txn_id)
                            continue

                        # Get account from expert instance
                        account = None
                        if txn.expert_id:
                            expert_instance = get_instance(ExpertInstance, txn.expert_id)
                            if expert_instance and expert_instance.account_id:
                                account = get_account_instance_from_id(expert_instance.account_id)

                        if account and hasattr(account, 'close_transaction_async'):
                            result = await account.close_transaction_async(txn_id)
                            if result.get('success'):
                                success_count += 1
                                logger.info(f"Batch close transaction {txn_id}: {result.get('message')}")
                            else:
                                failed.append(txn_id)
                                logger.warning(f"Batch close transaction {txn_id} failed: {result.get('message')}")
                        else:
                            failed.append(txn_id)
                            logger.warning(f"Cannot close transaction {txn_id}: no account found")
                    except Exception as e:
                        logger.error(f"Error closing transaction {txn_id}: {e}", exc_info=True)
                        failed.append(txn_id)

                # Schedule UI update
                def show_result():
                    if failed:
                        ui.notify(
                            f'Closed {success_count}/{len(transaction_ids)} transactions. {len(failed)} failed.',
                            type='warning'
                        )
                    else:
                        ui.notify(
                            f'Successfully closed {success_count} transaction{"s" if success_count != 1 else ""}',
                            type='positive'
                        )
                    self.selected_transactions.clear()
                    self._update_batch_buttons()
                    self._refresh_transactions()

                client.safe_invoke(show_result)

            except Exception as e:
                def show_error():
                    ui.notify(f'Error during batch close: {str(e)}', type='negative')
                client.safe_invoke(show_error)
                logger.error(f"Error in batch_close_async: {e}", exc_info=True)

        # Run async operation in background
        background_tasks.create(batch_close_async(), name=f'batch_close_{len(transaction_ids)}_txns')
        ui.notify(f'Closing {len(transaction_ids)} transaction{"s" if len(transaction_ids) != 1 else ""}...', type='info')

    def _batch_adjust_tp_dialog(self):
        """Show dialog to set TP percentage for batch of transactions."""
        if not self.selected_transactions:
            ui.notify('No transactions selected', type='warning')
            return

        count = len(self.selected_transactions)

        with ui.dialog() as dialog, ui.card().classes('w-full max-w-sm'):
            ui.label('Batch Adjust Take Profit').classes('text-h6 mb-4')

            ui.label(f'Set TP for {count} transaction{"s" if count != 1 else ""}').classes('text-body2 mb-4')

            tp_percent_input = ui.number(
                label='TP % from Open Price',
                value=5.0,
                min=0.1,
                max=100.0,
                step=0.1,
                format='%.1f'
            ).classes('w-full mb-4')

            ui.label('Example: 5.0% means TP = Open Price × 1.05').classes('text-caption text-grey-7')

            with ui.row().classes('w-full justify-end gap-2 mt-4'):
                ui.button('Cancel', on_click=dialog.close).props('flat')
                ui.button('Apply', on_click=lambda: self._execute_batch_adjust_tp(tp_percent_input.value, dialog)).props('color=info')

        dialog.open()

    def _execute_batch_adjust_tp(self, tp_percent: float, dialog):
        """Execute batch TP adjustment (async, non-blocking)."""
        dialog.close()

        transaction_ids = list(self.selected_transactions.keys())
        if not transaction_ids:
            ui.notify('No transactions selected', type='warning')
            return

        # Capture context for background task
        from nicegui import context, background_tasks
        client = context.client

        async def batch_adjust_tp_async():
            """Async batch TP adjustment."""
            try:
                from ...core.db import get_instance, update_instance, get_db
                from ...core.models import Transaction, TradingOrder, ExpertInstance
                from ...core.types import OrderType, OrderDirection
                from ...core.utils import get_account_instance_from_id
                from sqlmodel import Session, select

                success_count = 0
                failed = []
                existing_tp_modified = []
                new_tp_created = []

                for txn_id in transaction_ids:
                    try:
                        logger.info(f"[Batch TP] Processing transaction {txn_id}")
                        txn = get_instance(Transaction, txn_id)
                        if not txn:
                            logger.error(f"[Batch TP] Transaction {txn_id} not found")
                            failed.append(txn_id)
                            continue

                        if not txn.open_price or txn.open_price <= 0:
                            logger.error(f"[Batch TP] Transaction {txn_id} has invalid open_price: {txn.open_price}")
                            failed.append(txn_id)
                            continue

                        logger.info(f"[Batch TP] Transaction {txn_id}: symbol={txn.symbol}, open_price={txn.open_price}")

                        # Get current open qty early (while txn is in session context)
                        try:
                            current_open_qty = txn.get_current_open_qty()
                            logger.info(f"[Batch TP] Transaction {txn_id}: current_open_qty={current_open_qty}")
                        except Exception as e:
                            logger.error(f"[Batch TP] Could not get current open qty for transaction {txn_id}: {e}", exc_info=True)
                            current_open_qty = 0

                        # Calculate new TP price
                        new_tp_price = txn.open_price * (1 + tp_percent / 100)
                        logger.info(f"[Batch TP] Transaction {txn_id}: calculated new_tp_price={new_tp_price:.2f}")

                        # Get account for order operations
                        # Transaction has expert_id, get account from expert instance
                        account = None
                        if txn.expert_id:
                            try:
                                expert_instance = get_instance(ExpertInstance, txn.expert_id)
                                if expert_instance and expert_instance.account_id:
                                    logger.info(f"[Batch TP] Transaction {txn_id}: found expert_id={txn.expert_id}, account_id={expert_instance.account_id}")
                                    account = get_account_instance_from_id(expert_instance.account_id)
                                else:
                                    logger.warning(f"[Batch TP] Transaction {txn_id}: expert {txn.expert_id} not found or has no account")
                            except Exception as e:
                                logger.error(f"[Batch TP] Transaction {txn_id}: error getting expert instance: {e}", exc_info=True)
                        else:
                            logger.warning(f"[Batch TP] Transaction {txn_id}: no expert_id")

                        if not account:
                            logger.error(f"[Batch TP] Cannot adjust TP for transaction {txn_id}: no account found")
                            failed.append(txn_id)
                            continue

                        logger.info(f"[Batch TP] Transaction {txn_id}: found account")

                        # Use adjust_tp() to handle TP adjustment properly (creates OCO/OTO orders)
                        logger.info(f"[Batch TP] Transaction {txn_id}: calling adjust_tp with price ${new_tp_price:.2f}")
                        try:
                            success = account.adjust_tp(txn, new_tp_price)
                            if success:
                                success_count += 1
                                existing_tp_modified.append(txn_id)
                                logger.info(f"[Batch TP] Transaction {txn_id}: ✓ Successfully adjusted TP to ${new_tp_price:.2f}")
                            else:
                                failed.append(txn_id)
                                logger.error(f"[Batch TP] Transaction {txn_id}: ✗ Failed to adjust TP (adjust_tp returned False)")
                        except Exception as e:
                            logger.error(f"[Batch TP] Transaction {txn_id}: ✗ Error adjusting TP: {e}", exc_info=True)
                            failed.append(txn_id)

                    except Exception as e:
                        logger.error(f"Error processing transaction {txn_id}: {e}", exc_info=True)
                        failed.append(txn_id)

                # Schedule UI update with detailed results
                def show_result():
                    msg_parts = [f'Updated {success_count}/{len(transaction_ids)} transactions']
                    if existing_tp_modified:
                        msg_parts.append(f'{len(existing_tp_modified)} modified existing orders')
                    if new_tp_created:
                        msg_parts.append(f'{len(new_tp_created)} created new TPs')

                    message = ' • '.join(msg_parts)

                    if failed:
                        message += f' • {len(failed)} failed'
                        ui.notify(message, type='warning')
                    else:
                        ui.notify(message + f' (+{tp_percent:.1f}%)', type='positive')

                    self.selected_transactions.clear()
                    self._update_batch_buttons()
                    self._refresh_transactions()

                client.safe_invoke(show_result)

            except Exception as e:
                def show_error():
                    ui.notify(f'Error during batch TP adjustment: {str(e)}', type='negative')
                client.safe_invoke(show_error)
                logger.error(f"Error in batch_adjust_tp_async: {e}", exc_info=True)

        # Run async operation in background
        background_tasks.create(batch_adjust_tp_async(), name=f'batch_adjust_tp_{len(transaction_ids)}_txns')
        ui.notify(f'Adjusting TP for {len(transaction_ids)} transaction{"s" if len(transaction_ids) != 1 else ""}...', type='info')


def content():
    """Render the live trades page content."""
    LiveTradesTab()