from nicegui import ui
from datetime import date, datetime, timedelta, timezone
from sqlmodel import select, func, Session
from typing import Dict, Any, List, Tuple, Optional
import asyncio

from ...core.db import get_all_instances, get_db, get_instance, update_instance
from ...core.models import AccountDefinition, MarketAnalysis, ExpertRecommendation, ExpertInstance, AppSetting, TradingOrder, Transaction
from ...core.option_positions import opening_legs
from ...core.types import MarketAnalysisStatus, OrderRecommendation, OrderStatus, OrderOpenType, OrderType, OrderDirection
from ...core.types import AssetClass
from ...core.utils import get_expert_instance_from_id, get_market_analysis_id_from_order_id, get_account_instance_from_id, get_order_status_color, get_expert_options_for_ui
from ...core.TransactionHelper import TransactionHelper
from ...modules.accounts import providers
from ...logger import logger
from ..components import LiveTradesTable, LiveTradesTableConfig
from ..components.LiveTradesTable import transaction_status_color
from ..components.MarketAnalysisDetailDialog import MarketAnalysisDetailDialog
from ..components.option_structure_chart import (
    fetch_underlying_bars, render_option_structure_chart,
)
from .option_trades import OptionTradesTab
from ..account_filter_context import get_selected_account_id
from ..components.account_scope import scope_transactions_to_account
from ..utils.perf_logger import PerfLogger
from ..utils.margin_view import capital_requirement, factors_by_account, value_capreq_text
from ..components.refresh_button import refresh_button

#: The Live Trades tabs, IN ORDER. Stocks first and DEFAULT, so the page's existing view is
#: what you get before touching anything.
#:
#: The split is made on ``Transaction.asset_class`` -- the explicit, indexed field. The
#: pre-``asset_class`` tell (``multiplier == 100``) is deliberately NOT used: it is a
#: heuristic, and a mis-filed row would be listed under the wrong tab instead of showing
#: as unclassified.
#:
#: Module-level so the tab contract is unit-testable without rendering a page, the same
#: reason ``ui/menus.py`` keeps ``MENU_ITEMS`` at module level.
ASSET_CLASS_TABS: tuple = (
    ('Stocks', AssetClass.EQUITY, 'show_chart'),
    ('Options', AssetClass.OPTION, 'donut_large'),
)

#: How close to a bracket leg counts as "about to hit it", as a fraction of the leg's price.
PRICE_NEAR_LEG_FRACTION = 0.05


def price_proximity_zone(current_price, take_profit, stop_loss, side,
                         fraction: float = PRICE_NEAR_LEG_FRACTION) -> str:
    """``'sl'``, ``'tp'`` or ``''`` -- which bracket leg the price is within ``fraction`` of.

    Lets the Current column say at a glance which positions are about to resolve, instead of
    the reader eyeballing three numbers per row against each other.

    WHICH SIDE IS "NEAR" DEPENDS ON THE DIRECTION. A long's stop sits BELOW the price and its
    target ABOVE, so it approaches the stop by falling; a short is the mirror image. Reading a
    short with a long's arithmetic would paint it green exactly when it is in trouble.

    Long   : near SL when current <= sl x (1 + f);  near TP when current >= tp x (1 - f)
    Short  : near SL when current >= sl x (1 - f);  near TP when current <= tp x (1 + f)

    SL WINS A TIE. Inside a bracket tighter than 2 x fraction both can be true at once, and
    "you are about to be stopped out" is the half of that the reader needs.

    A missing or non-positive leg is simply not a leg -- many rows carry a stop and no target,
    and those must be judged on the stop alone rather than dropped.
    """
    try:
        price = float(current_price)
    except (TypeError, ValueError):
        return ''
    if price <= 0:
        return ''

    def _leg(value):
        try:
            v = float(value)
        except (TypeError, ValueError):
            return None
        return v if v > 0 else None

    tp, sl = _leg(take_profit), _leg(stop_loss)
    is_long = getattr(side, 'value', side) == OrderDirection.BUY.value

    if sl is not None:
        if (price <= sl * (1 + fraction)) if is_long else (price >= sl * (1 - fraction)):
            return 'sl'
    if tp is not None:
        if (price >= tp * (1 - fraction)) if is_long else (price <= tp * (1 + fraction)):
            return 'tp'
    return ''


class LiveTradesTab:
    """Comprehensive transactions management tab with full control over positions."""

    def __init__(self):
        self.transactions_container = None
        self.live_trades_table: LiveTradesTable = None
        # Totals strip under the table. Held on the instance because the data loader (which
        # knows the filtered set) and the renderer (which owns the labels) are different calls.
        self._totals_row = None
        self._totals: Dict[str, float] = {}
        self.selected_transaction = None
        self.batch_operations_container = None
        # Note: render() is now async and should be awaited after construction

    def _get_order_status_color(self, status):
        """Get color for order status badge."""
        return get_order_status_color(status)

    async def render(self):
        """Render the transactions tab with filtering and control options."""
        render_timer = PerfLogger.start(PerfLogger.PAGE, PerfLogger.RENDER, "LiveTradesTab")
        logger.debug("[RENDER] LiveTradesTab.render() - START")

        # Pre-populate expert options before creating the UI
        expert_options, expert_map = self._get_expert_options()
        self.expert_id_map = expert_map

        with ui.card().classes('w-full'):
            with ui.row().classes('w-full items-center justify-between mb-4'):
                ui.label('💼 Live Trades').classes('text-h6')

            with ui.tabs().classes('w-full') as _tabs:
                _tab_refs = [ui.tab(label, icon=icon) for label, _asset, icon in ASSET_CLASS_TABS]

            with ui.tab_panels(_tabs, value=_tab_refs[0]).classes('w-full'):
                # STOCKS (first and default): the existing view, byte for byte.
                with ui.tab_panel(_tab_refs[0]):
                    self._render_equity_filter_row(expert_options)
                    self.transactions_container = ui.column().classes('w-full')
                    with self.transactions_container:
                        await self._render_transactions_table_async()

                # OPTIONS: its own columns, filters, totals and PRICING -- see the module
                # docstring of ui/pages/option_trades.py. A separate class on purpose: the
                # equity path above is never reached from it.
                with ui.tab_panel(_tab_refs[1]):
                    self.option_tab = OptionTradesTab(
                        on_view_details=self._handle_view_transaction_details,
                        on_close_transaction=self._handle_close_transaction,
                        on_edit_transaction=self._handle_edit_transaction,
                        on_retry_close=self._handle_retry_close_transaction,
                        on_recreate_tpsl=self._handle_recreate_tpsl,
                        on_view_recommendation=self._handle_view_recommendation,
                    )
                    await self.option_tab.render()
        
        render_timer.stop("filters_rendered")

    def _render_equity_filter_row(self, expert_options) -> None:
        """The STOCKS tab's filter row and batch controls (unchanged from the single-tab page).

        Extracted verbatim so tabs could be added AROUND it: the batch buttons act on the
        STOCKS table, which is why they belong in this row and not above both tabs.
        """
        # Filter controls
        with ui.row().classes('gap-2 items-center'):
            # Multi-select status filter with all except CLOSED selected by default
            self.status_filter = ui.select(
                label='Status Filter',
                options=['Waiting', 'Open', 'Closing', 'Closed'],
                value=['Waiting', 'Open', 'Closing'],  # Default: all except Closed
                multiple=True,
                on_change=lambda: self._refresh_transactions()
            ).classes('w-56')

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
            ).props('stack-label').classes('w-40')

            self.broker_order_id_filter = ui.input(
                label='Broker Order ID',
                placeholder='Search by broker order ID...',
                on_change=lambda: self._refresh_transactions()
            ).props('stack-label').classes('w-48')

            refresh_button(lambda: self._refresh_transactions())

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
        """Refresh the transactions table."""
        logger.debug("[REFRESH] _refresh_transactions() - Updating table rows")

        # Refresh expert filter options (in case new experts were added)
        self._populate_expert_filter()

        # Use the LiveTradesTable's built-in refresh
        if self.live_trades_table:
            asyncio.create_task(self.live_trades_table.refresh())
        else:
            # Table doesn't exist yet, create it
            logger.debug("[REFRESH] Table doesn't exist, creating new table")
            self.transactions_container.clear()
            async def _create_table_in_context():
                with self.transactions_container:
                    await self._render_transactions_table_async()
            asyncio.create_task(_create_table_in_context())
    
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

    async def _transactions_data_loader(
        self,
        page: int,
        page_size: int,
        filters: Dict[str, Any],
        sort_by: str,
        descending: bool
    ) -> Tuple[List[Dict], int]:
        """
        Async data loader callback for LiveTradesTable.
        
        Supports server-side pagination, sorting and filtering.
        
        Args:
            page: Current page number (1-indexed)
            page_size: Number of items per page
            filters: Dict with '_global' for global filter and column names for column filters
            sort_by: Column name to sort by
            descending: Sort direction
        
        Returns:
            Tuple of (rows, total_count)
        """
        from ...core.models import Transaction, ExpertInstance, AccountDefinition
        from ...core.types import TransactionStatus
        from sqlmodel import col
        from sqlalchemy import case

        # Extract global filter from filters dict
        global_filter = filters.get('_global', '')

        fetch_timer = PerfLogger.start(PerfLogger.DATA, PerfLogger.FETCH, "LiveTradesData")
        logger.debug(f"[TRANSACTIONS] _transactions_data_loader() page={page}, page_size={page_size}")

        session = get_db()
        try:
            # Build base query - join with ExpertInstance and AccountDefinition for sorting
            base_query = select(Transaction, ExpertInstance).outerjoin(
                ExpertInstance, Transaction.expert_id == ExpertInstance.id
            ).outerjoin(
                AccountDefinition, ExpertInstance.account_id == AccountDefinition.id
            )
            
            # Apply global account filter from header dropdown.
            #
            # A transaction belongs to the account ITS ORDERS WERE PLACED ON -- which is
            # the very rule this page displays: the account column below is read from the
            # transaction's first order. This used to ask a different question instead,
            # mapping the account to its ExpertInstance ids and keeping
            # Transaction.expert_id IN (...), with "no experts -> return empty". Both
            # halves were wrong, because a transaction's expert is not its account:
            #   * a manual account (TastyTrade) has NO experts, so the page went blank
            #     for it while "All" showed its trades;
            #   * allocator- and hand-created transactions have expert_id IS NULL even on
            #     expert-driven accounts, so they vanished when their own account was
            #     selected.
            # scope_transactions_to_account is the same helper the Overview widgets use,
            # so the whole UI attributes a transaction one way. None means "All".
            selected_account_id = get_selected_account_id()
            base_query = scope_transactions_to_account(base_query, selected_account_id)

            # Apply status filter (from page filter controls)
            status_values = self.status_filter.value if hasattr(self, 'status_filter') else ['Waiting', 'Open', 'Closing']
            if status_values and len(status_values) > 0:
                status_map = {
                    'Open': TransactionStatus.OPENED,
                    'Closed': TransactionStatus.CLOSED,
                    'Closing': TransactionStatus.CLOSING,
                    'Waiting': TransactionStatus.WAITING
                }
                selected_statuses = [status_map[s] for s in status_values if s in status_map]
                if selected_statuses:
                    base_query = base_query.where(Transaction.status.in_(selected_statuses))

            # Apply expert filter (from page filter controls)
            if hasattr(self, 'expert_filter') and self.expert_filter.value != 'All':
                if hasattr(self, 'expert_id_map'):
                    expert_id = self.expert_id_map.get(self.expert_filter.value)
                    if expert_id and expert_id != 'All':
                        base_query = base_query.where(Transaction.expert_id == expert_id)

            # Apply symbol filter (from page filter controls)
            if hasattr(self, 'symbol_filter') and self.symbol_filter.value:
                base_query = base_query.where(Transaction.symbol.contains(self.symbol_filter.value.upper()))

            # Apply broker order ID filter (from page filter controls)
            if hasattr(self, 'broker_order_id_filter') and self.broker_order_id_filter.value and self.broker_order_id_filter.value.strip():
                from ...core.models import TradingOrder
                base_query = base_query.join(
                    TradingOrder,
                    Transaction.id == TradingOrder.transaction_id
                ).where(
                    TradingOrder.broker_order_id.contains(self.broker_order_id_filter.value.strip())
                ).distinct()

            # Apply global filter from table (symbol search)
            if global_filter:
                base_query = base_query.where(Transaction.symbol.contains(global_filter.upper()))

            # Get total count for pagination
            count_query = select(func.count()).select_from(base_query.subquery())
            total_count = session.exec(count_query).one()

            # Totals over the WHOLE FILTERED SET, not this page -- a total that changed when you
            # turned the page would be worse than none. Computed off `base_query` before the
            # offset/limit below is applied, and covering ONLY these transactions: manual broker
            # positions are not Transactions, so they are excluded by construction (which is the
            # difference from the Overview page, whose total is every position the broker holds).
            self._compute_filtered_totals(session, base_query)

            # Apply sorting
            # Keys are column NAMES (Quasar sends column name as sortBy, not field)
            # Computed fields (current_pnl) require in-memory sorting after price fetch
            IN_MEMORY_SORT_FIELDS = {'current_pnl': 'current_pnl_numeric'}  # column name -> row data key
            SORT_MAP = {
                'direction': Transaction.side,
                'closed_at': Transaction.close_date,
                'account': AccountDefinition.name,
                'expert': func.coalesce(ExpertInstance.alias, ExpertInstance.expert),
                'closed_pnl': case(
                    (Transaction.open_price == 0, 0),
                    (Transaction.side == 'BUY',
                     (Transaction.close_price - Transaction.open_price) / Transaction.open_price * 100),
                    else_=(Transaction.open_price - Transaction.close_price) / Transaction.open_price * 100
                ),
            }

            needs_in_memory_sort = sort_by in IN_MEMORY_SORT_FIELDS

            if sort_by and not needs_in_memory_sort:
                sort_expr = SORT_MAP.get(sort_by)
                if sort_expr is None:
                    sort_expr = getattr(Transaction, sort_by, None)
                if sort_expr is not None:
                    if descending:
                        base_query = base_query.order_by(sort_expr.desc())
                    else:
                        base_query = base_query.order_by(sort_expr.asc())
                else:
                    # Unknown column - fall back to default sort
                    base_query = base_query.order_by(Transaction.created_at.desc())
            elif not needs_in_memory_sort:
                base_query = base_query.order_by(Transaction.created_at.desc())

            if needs_in_memory_sort:
                # For computed fields: fetch ALL matching rows, build rows, sort in-memory, then paginate
                results = list(session.exec(base_query).all())
                transactions = []
                transaction_experts = {}
                for txn, expert in results:
                    transactions.append(txn)
                    transaction_experts[txn.id] = expert

                if not transactions:
                    fetch_timer.stop(f"count=0, total={total_count}")
                    return [], total_count

                rows = self._build_transaction_rows(transactions, transaction_experts, session)
                row_key = IN_MEMORY_SORT_FIELDS[sort_by]  # Map column name to row data key
                rows.sort(key=lambda r: r.get(row_key, 0), reverse=descending)

                # Apply pagination in-memory
                start = (page - 1) * page_size
                end = start + page_size
                rows = rows[start:end]

                fetch_timer.stop(f"count={len(rows)}, total={total_count} (in-memory sort by {sort_by})")
                return rows, total_count

            # Apply pagination - LazyTable uses 1-indexed pages
            offset = (page - 1) * page_size
            paginated_query = base_query.offset(offset).limit(page_size)

            # Execute query
            results = list(session.exec(paginated_query).all())
            transactions = []
            transaction_experts = {}
            for txn, expert in results:
                transactions.append(txn)
                transaction_experts[txn.id] = expert

            if not transactions:
                fetch_timer.stop(f"count=0, total={total_count}")
                return [], total_count

            # Build rows using existing logic
            rows = self._build_transaction_rows(transactions, transaction_experts, session)

            fetch_timer.stop(f"count={len(rows)}, total={total_count}")
            return rows, total_count

        except Exception as e:
            logger.error(f"Error loading transactions data: {e}", exc_info=True)
            return [], 0
        finally:
            session.close()

    def _build_transaction_rows(
        self,
        transactions: List[Transaction],
        transaction_experts: Dict[int, ExpertInstance],
        session
    ) -> List[Dict]:
        """Build row data from transactions for the table."""
        from ...core.types import TransactionStatus
        from ...core.models import TradingOrder, AccountDefinition
        from collections import defaultdict

        # BATCH PRICE FETCHING: Collect all symbols from open transactions grouped by account
        symbols_by_account = defaultdict(set)
        txn_to_account = {}

        # Pre-fetch account_id for ALL transactions (needed for account name column)
        for txn in transactions:
            order_stmt = select(TradingOrder).where(TradingOrder.transaction_id == txn.id).limit(1)
            first_order = session.exec(order_stmt).first()
            if first_order:
                txn_to_account[txn.id] = first_order.account_id
                # Also collect symbols for open transactions
                if txn.status in (TransactionStatus.OPENED, TransactionStatus.CLOSING) and txn.open_price and txn.quantity:
                    symbols_by_account[first_order.account_id].add(txn.symbol)

        # Build account ID to name mapping
        account_names = {}
        unique_account_ids = set(txn_to_account.values())
        if unique_account_ids:
            accounts = session.exec(
                select(AccountDefinition).where(AccountDefinition.id.in_(unique_account_ids))
            ).all()
            for acc in accounts:
                account_names[acc.id] = acc.name

        # The EFFECTIVE margin factor per account (1.0 with margin off, no broker read),
        # once per account per render rather than per row. Scoped to the accounts that
        # will actually RENDER a requirement -- ``symbols_by_account`` holds only the
        # accounts with an open position -- so a page of nothing but closed trades costs
        # no broker call at all. With margin on the read does touch the broker, which is
        # why it lives here on the async loader path beside the price fetch rather than
        # in the paint. Deriving the factor from the header's hourly snapshot instead of
        # reading it per render is a recorded follow-up (see the design doc).
        factor_by_account: Dict[int, float] = factors_by_account(
            symbols_by_account.keys(),
            resolve=lambda acc_id: get_account_instance_from_id(acc_id, session=session))

        # Fetch prices in batch for each account
        current_prices = {}
        logger.debug(f"Fetching prices for {len(symbols_by_account)} accounts: {dict(symbols_by_account)}")
        
        for account_id, symbols in symbols_by_account.items():
            try:
                account_inst = get_account_instance_from_id(account_id)
                if account_inst and symbols:
                    symbols_list = list(symbols)
                    logger.debug(f"Fetching prices for account {account_id}, symbols: {symbols_list}")
                    prices_dict = account_inst.get_instrument_current_price(symbols_list)
                    logger.debug(f"Got prices: {prices_dict}")
                    if prices_dict:
                        current_prices.update(prices_dict)
                else:
                    logger.warning(f"Account {account_id} not found or no symbols to fetch")
            except Exception as e:
                logger.warning(f"Batch price fetch failed for account {account_id}: {e}", exc_info=True)

        # Build rows
        rows = []
        for txn in transactions:
            # Calculate current P/L for open positions
            current_pnl = ''
            current_pnl_numeric = 0
            current_price_str = ''
            current_price_zone = ''

            if txn.status in (TransactionStatus.OPENED, TransactionStatus.CLOSING) and txn.open_price and txn.quantity:
                try:
                    current_price = current_prices.get(txn.symbol)
                    if current_price:
                        current_price_str = f"${current_price:.2f}"
                        current_price_zone = price_proximity_zone(
                            current_price, txn.take_profit, txn.stop_loss, txn.side)
                        pnl = TransactionHelper.calculate_pnl(txn, current_price)
                        if pnl:
                            current_pnl = f"${pnl['amount']:+.2f} ({pnl['percent']:+.1f}%)"
                            current_pnl_numeric = pnl['percent']
                except Exception as e:
                    logger.debug(f"Could not calculate P/L for {txn.symbol}: {e}")

            # Closed P/L
            closed_pnl = ''
            closed_pnl_numeric = 0
            if txn.close_price and txn.open_price and txn.quantity:
                # Use side field: BUY=LONG, SELL=SHORT
                if txn.side == OrderDirection.BUY:
                    pnl_closed = (txn.close_price - txn.open_price) * txn.quantity
                else:
                    pnl_closed = (txn.open_price - txn.close_price) * txn.quantity
                cost_basis = txn.open_price * abs(txn.quantity)
                pnl_closed_pct = (pnl_closed / cost_basis * 100) if cost_basis > 0 else 0
                closed_pnl = f"${pnl_closed:+.2f} ({pnl_closed_pct:+.1f}%)"
                closed_pnl_numeric = pnl_closed_pct

            # Status styling
            status_color = transaction_status_color(txn.status)

            # Expert shortname
            expert = transaction_experts.get(txn.id)
            expert_shortname = ''
            if expert:
                expert_shortname = f"{expert.alias}-{expert.id}" if expert.alias else f"Expert-{expert.id}"

            # Get orders for expansion
            orders_data = []
            txn_orders = []
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

                        order_category = 'Entry'
                        if TransactionHelper.is_tpsl_order(order):
                            if TransactionHelper.is_tp_order(order):
                                order_category = 'Take Profit'
                            elif TransactionHelper.is_sl_order(order):
                                order_category = 'Stop Loss'
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

            # Check for missing TP/SL orders
            has_missing_tpsl_orders = self._check_missing_tpsl_orders(txn, txn_orders)

            # Calculate value (qty * current_price)
            value_str = ''
            if txn.quantity and current_price_str:
                try:
                    current_price = current_prices.get(txn.symbol)
                    if current_price:
                        value = txn.quantity * current_price
                        # What the position is WORTH, and beside it what it costs the
                        # account: on margin those differ, and only the second competes
                        # with every other position for the same balance.
                        acc_id = txn_to_account.get(txn.id)
                        factor = factor_by_account.get(acc_id)
                        capreq = (capital_requirement(value, effective_factor=factor)
                                  if factor is not None else None)
                        value_str = value_capreq_text(value, capreq)
                except Exception as e:
                    logger.debug(f"Could not calculate value for {txn.symbol}: {e}")

            # Get account name for this transaction
            account_id = txn_to_account.get(txn.id)
            account_name = account_names.get(account_id, '') if account_id else ''

            row = {
                'id': txn.id,
                '_selected': False,  # Selection is managed by LiveTradesTable
                'account_name': account_name,
                'symbol': txn.symbol,
                'direction': txn.side.value,
                'expert': expert_shortname,
                'quantity': f"{txn.quantity:.2f}",
                'open_price': f"${txn.open_price:.2f}" if txn.open_price else '',
                'current_price': current_price_str,
                # '' | 'sl' | 'tp' -- the Current cell paints from this, see LiveTradesTable.
                'current_price_zone': current_price_zone,
                'value': value_str,
                'close_price': f"${txn.close_price:.2f}" if txn.close_price else '',
                'take_profit': f"${txn.take_profit:.2f}" if txn.take_profit else '',
                'stop_loss': f"${txn.stop_loss:.2f}" if txn.stop_loss else '',
                'current_pnl': current_pnl,
                'current_pnl_numeric': current_pnl_numeric,
                'closed_pnl': closed_pnl,
                'closed_pnl_numeric': closed_pnl_numeric,
                'status': txn.status.value,
                'status_color': status_color,
                'created_at': txn.created_at.strftime('%Y-%m-%d %H:%M') if txn.created_at else '',
                'closed_at': txn.close_date.strftime('%Y-%m-%d %H:%M') if txn.close_date else '',
                'is_open': txn.status == TransactionStatus.OPENED,
                'is_waiting': txn.status == TransactionStatus.WAITING,
                'is_closing': txn.status == TransactionStatus.CLOSING,
                'has_missing_tpsl_orders': has_missing_tpsl_orders,
                'orders': orders_data,
                'order_count': len(orders_data),
                'actions': 'actions'
            }
            rows.append(row)

        return rows

    def _check_missing_tpsl_orders(self, txn, txn_orders) -> bool:
        """Check if TP/SL are defined but have no valid orders.

        Uses TransactionHelper.is_tp_order/is_sl_order for reliable order type detection.
        """
        from ...core.types import TransactionStatus

        if txn.status != TransactionStatus.OPENED:
            return False

        has_tp_defined = txn.take_profit is not None and txn.take_profit > 0
        has_sl_defined = txn.stop_loss is not None and txn.stop_loss > 0

        if not (has_tp_defined or has_sl_defined):
            return False

        has_valid_tp_order = False
        has_valid_sl_order = False
        invalid_statuses = {'canceled', 'rejected', 'error', 'expired'}

        for order in txn_orders:
            order_status = order.status.value.lower() if hasattr(order.status, 'value') else str(order.status).lower()
            if order_status in invalid_statuses:
                continue

            if has_tp_defined and not has_valid_tp_order:
                if TransactionHelper.is_tp_order(order):
                    has_valid_tp_order = True

            if has_sl_defined and not has_valid_sl_order:
                if TransactionHelper.is_sl_order(order):
                    has_valid_sl_order = True

            if (not has_tp_defined or has_valid_tp_order) and (not has_sl_defined or has_valid_sl_order):
                break

        return (has_tp_defined and not has_valid_tp_order) or (has_sl_defined and not has_valid_sl_order)

    def _compute_filtered_totals(self, session, base_query) -> None:
        """Sum cost basis, market value and unrealised P/L over every transaction the current
        filters match, and stash them on ``self._totals`` for the strip under the table.

        COST BASIS is exact and free -- ``quantity * open_price`` straight off the rows, no
        prices needed. MARKET VALUE needs a live price per symbol, so it reuses the same bulk
        per-account fetch the row builder uses; symbols whose price is unavailable contribute
        their cost basis instead of nothing, so the value total can never read as a loss that
        is really a missing quote. ``priced``/``total`` is recorded so the strip can say when
        the picture is incomplete rather than quietly showing a wrong number.

        Never raises: a totals strip is worth less than the table it sits under.
        """
        from ...core.types import TransactionStatus

        totals = {'cost': 0.0, 'value': 0.0, 'pnl': 0.0, 'count': 0, 'priced': 0, 'unfilled': 0}
        try:
            rows = list(session.exec(base_query).all())
            txn_ids = [t.id for t, _e in rows]
            acc_by_txn = self._account_ids_for_transactions(session, txn_ids)
            by_account: Dict[Any, set] = {}
            entries = []
            for txn, _expert in rows:
                # Only trades that actually HOLD something. The default status filter includes
                # WAITING, and a waiting order has not been filled -- it owns no shares and has
                # committed no money, so adding qty*open_price for it would overstate the cost
                # basis with capital that has not left the account. Counted separately and
                # named on the strip instead of being silently folded in or silently dropped.
                if txn.status not in (TransactionStatus.OPENED, TransactionStatus.CLOSING):
                    totals['unfilled'] += 1
                    continue
                qty = float(txn.quantity or 0)
                open_price = float(txn.open_price or 0)
                if not qty or not open_price:
                    continue
                acc_id = acc_by_txn.get(txn.id)
                entries.append((txn.symbol, qty, open_price, acc_id))
                by_account.setdefault(acc_id, set()).add(txn.symbol)

            prices: Dict[str, float] = {}
            for acc_id, symbols in by_account.items():
                if acc_id is None or not symbols:
                    continue
                try:
                    inst = get_account_instance_from_id(acc_id)
                    if inst:
                        got = inst.get_instrument_current_price(list(symbols))
                        if got:
                            prices.update(got)
                except Exception as e:  # noqa: BLE001 -- one bad account must not void the total
                    logger.debug(f"totals: price fetch failed for account {acc_id}: {e}")

            for symbol, qty, open_price, _acc in entries:
                cost = qty * open_price
                totals['cost'] += cost
                totals['count'] += 1
                px = prices.get(symbol)
                if px:
                    totals['value'] += qty * float(px)
                    totals['priced'] += 1
                else:
                    totals['value'] += cost      # unpriced -> flat, never a phantom loss
            totals['pnl'] = totals['value'] - totals['cost']
        except Exception as e:  # noqa: BLE001
            logger.debug(f"Could not compute live-trade totals: {e}")
        self._totals = totals
        # INSIDE the guard, because the docstring's promise covers the repaint too. It sat
        # outside, so a failure here escaped into the loader's own except and blanked the whole
        # TABLE -- the thing the strip is explicitly worth less than. Demonstrated by the
        # account-filter tests: a missing `_totals_row` attribute took out all 7 of them, none
        # of which is about totals.
        try:
            self._refresh_totals_row()
        except Exception as e:  # noqa: BLE001
            logger.debug(f"Could not repaint the live-trade totals strip: {e}")

    def _account_ids_for_transactions(self, session, txn_ids: List[int]) -> Dict[int, int]:
        """``{transaction_id: account_id}`` for all *txn_ids* in ONE query.

        A transaction belongs to the account ITS ORDERS WERE PLACED ON -- the same rule the
        ACCOUNT column uses -- so the price fetch asks the broker that actually holds the
        position. Batched deliberately: this runs inside a loader that auto-refreshes every
        30 seconds, and a query per transaction would make the strip cost more than the table.
        """
        if not txn_ids:
            return {}
        from sqlmodel import col   # local, matching this module's existing style
        try:
            # ``TradingOrder.account_id`` directly -- the same field the ACCOUNT column reads
            # off the transaction's first order. (There is no TradingOrder.expert_id; the
            # expert is reached through Transaction or ExpertRecommendation, see
            # TradingOrder.get_expert_id -- but the account is right here, so do not detour.)
            pairs = session.exec(
                select(TradingOrder.transaction_id, TradingOrder.account_id)
                .where(col(TradingOrder.transaction_id).in_(txn_ids))
                .order_by(col(TradingOrder.id))
            ).all()
        except Exception as e:  # noqa: BLE001
            logger.debug(f"totals: account mapping failed: {e}")
            return {}
        out: Dict[int, int] = {}
        for txn_id, account_id in pairs:
            if txn_id is not None and account_id is not None:
                out.setdefault(txn_id, account_id)   # first order wins, as the column does
        return out

    def _refresh_totals_row(self) -> None:
        """Repaint the totals strip from ``self._totals`` (no-op before it is rendered)."""
        if self._totals_row is None:
            return
        t = self._totals
        self._totals_row.clear()
        with self._totals_row:
            ui.label('TOTAL (open trades):').classes('text-sm font-bold text-secondary-custom')
            ui.label(f"Cost: ${t.get('cost', 0.0):,.2f}").classes('text-sm font-semibold')
            pnl = t.get('pnl', 0.0)
            pl_color = 'number-positive' if pnl >= 0 else 'number-negative'
            ui.label(f"Unrealized P/L: ${pnl:,.2f}").classes(f'text-sm font-bold {pl_color}')
            ui.label(f"Market Value: ${t.get('value', 0.0):,.2f}").classes('text-sm font-semibold')
            notes = []
            priced, count = t.get('priced', 0), t.get('count', 0)
            if count and priced < count:
                # Say it out loud rather than letting the unpriced rows read as flat.
                notes.append(f'{count - priced} of {count} unpriced, shown at cost')
            if t.get('unfilled'):
                notes.append(f"{t['unfilled']} waiting, not counted")
            if notes:
                ui.label('(' + '; '.join(notes) + ')').classes(
                    'text-xs text-secondary-custom italic')

    async def _render_transactions_table_async(self):
        """Render the main transactions table using LiveTradesTable component."""
        logger.debug("[RENDER] _render_transactions_table_async() - START")

        # Create LiveTradesTable with data loader and event handlers
        self.live_trades_table = LiveTradesTable(
            data_loader=self._transactions_data_loader,
            config=LiveTradesTableConfig(
                page_size=20,
                table_name="LiveTradesTable",
                show_global_filter=True,
                # The filter row's Refresh does this AND repopulates the expert filter.
                show_refresh=False,
                show_selection=True,
                dense=True,
                auto_refresh_interval=30,
            ),
            on_edit=self._handle_edit_transaction,
            on_close=self._handle_close_transaction,
            on_retry_close=self._handle_retry_close_transaction,
            on_recreate_tpsl=self._handle_recreate_tpsl,
            on_view_recommendation=self._handle_view_recommendation,
            on_view_transaction_details=self._handle_view_transaction_details,
            on_selection_change=self._handle_selection_change
        )

        # Render the table
        await self.live_trades_table.render()

        # Totals strip, matching the Overview page's shape (cost -> unrealised P/L -> market
        # value), in the table's footer: right under the rows, above the pagination controls.
        # The data loader has usually already run by now, so paint it immediately from
        # whatever it stored and let later loads repaint it.
        with self.live_trades_table.footer:
            self._totals_row = ui.row().classes(
                'w-full justify-end items-center gap-6 px-4 py-3 bg-white/5 border-b border-white/10')
        self._refresh_totals_row()

        logger.debug("[RENDER] _render_transactions_table_async() - END")

    def _handle_edit_transaction(self, transaction_id: int):
        """Handle edit button click from LiveTradesTable."""
        # Create mock event_data for compatibility
        class EventData:
            args = transaction_id
        self._show_edit_dialog(EventData())

    def _handle_close_transaction(self, transaction_id: int):
        """Handle close button click from LiveTradesTable."""
        class EventData:
            args = transaction_id
        self._show_close_dialog(EventData())

    def _handle_retry_close_transaction(self, transaction_id: int):
        """Handle retry close button click from LiveTradesTable."""
        class EventData:
            args = transaction_id
        self._show_retry_close_dialog(EventData())

    def _handle_recreate_tpsl(self, transaction_id: int):
        """Handle recreate TP/SL button click from LiveTradesTable."""
        class EventData:
            args = transaction_id
        self._recreate_tpsl_orders(EventData())

    def _handle_view_recommendation(self, rec_id: int):
        """Handle view recommendation button click from LiveTradesTable."""
        class EventData:
            args = rec_id
        self._show_recommendation_dialog(EventData())

    def _handle_view_transaction_details(self, transaction_id: int):
        """Handle view transaction details button click from LiveTradesTable."""
        self._show_transaction_details_dialog(transaction_id)

    def _handle_selection_change(self, selected_ids: List[int]):
        """Handle selection change from LiveTradesTable."""
        self._update_batch_buttons_for_selection(len(selected_ids))

    def _update_batch_buttons_for_selection(self, count: int):
        """Update batch operation buttons based on selection count."""
        has_selection = count > 0

        if hasattr(self, 'batch_select_all_btn'):
            self.batch_select_all_btn.set_visibility(True)
        if hasattr(self, 'batch_clear_btn'):
            self.batch_clear_btn.set_visibility(has_selection)
        if hasattr(self, 'batch_close_btn'):
            self.batch_close_btn.set_visibility(has_selection)
        if hasattr(self, 'batch_adjust_tp_btn'):
            self.batch_adjust_tp_btn.set_visibility(has_selection)

    def _render_transactions_table(self):
        """Render the main transactions table (sync wrapper for compatibility)."""
        async def _render_in_context():
            with self.transactions_container:
                await self._render_transactions_table_async()
        asyncio.create_task(_render_in_context())

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

            # Get the entry order to find account_id using TransactionHelper
            entry_order = TransactionHelper.get_entry_order(txn)
            if not entry_order:
                ui.notify('Could not find entry order', type='negative')
                return

            account_id = entry_order.account_id

            # Get account instance
            account_inst = get_account_instance_from_id(account_id)
            if not account_inst:
                ui.notify('Could not load account instance', type='negative')
                return

            # Cancel any existing TP/SL orders that are still active using TransactionHelper
            existing_tpsl_orders = TransactionHelper.get_active_tpsl_orders(txn)

            for order in existing_tpsl_orders:
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
                    success = account_inst.adjust_tp_sl(txn, txn.take_profit, txn.stop_loss, source="manual")
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
                        success = account_inst.adjust_tp(txn, txn.take_profit, source="manual")
                        if success:
                            orders_created.append('TP')
                            logger.info(f"Created TP order at ${txn.take_profit:.2f} for transaction {transaction_id}")
                        else:
                            logger.warning(f"Failed to create TP order for transaction {transaction_id}")
                    except Exception as e:
                        logger.error(f"Failed to create TP order: {e}", exc_info=True)

                if has_sl:
                    try:
                        success = account_inst.adjust_sl(txn, txn.stop_loss, source="manual")
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
            ui.label(f'Adjust Position for {txn.symbol}').classes('text-h6 mb-4')

            with ui.column().classes('w-full gap-4'):
                ui.label(f'Current Position: {txn.quantity:+.2f} @ ${txn.open_price:.2f}').classes('text-sm text-gray-600')

                qty_input = ui.number(
                    label='Position Quantity',
                    value=txn.quantity,
                    format='%.2f',
                    step=1
                ).classes('w-full').props('hint="Increase or decrease position size"')

                tp_label = 'Take Profit Price' + (' 🔒 (manual override)' if txn.tp_manual_override else '')
                tp_input = ui.number(
                    label=tp_label,
                    value=txn.take_profit if txn.take_profit else None,
                    format='%.2f',
                    prefix='$'
                ).classes('w-full')

                sl_label = 'Stop Loss Price' + (' 🔒 (manual override)' if txn.sl_manual_override else '')
                sl_input = ui.number(
                    label=sl_label,
                    value=txn.stop_loss if txn.stop_loss else None,
                    format='%.2f',
                    prefix='$'
                ).classes('w-full')

                if txn.tp_manual_override or txn.sl_manual_override:
                    locked = []
                    if txn.tp_manual_override: locked.append('TP')
                    if txn.sl_manual_override: locked.append('SL')
                    ui.label(
                        f'⚠️ {" and ".join(locked)} locked — automation will not modify until reverted.'
                    ).classes('text-xs text-orange-600')

                with ui.row().classes('w-full justify-between items-center gap-2'):
                    ui.button(
                        'Revert (re-enable automation)',
                        icon='restore',
                        on_click=lambda: self._revert_tpsl_manual_override(transaction_id, dialog),
                    ).props('flat color=warning')
                    with ui.row().classes('gap-2'):
                        ui.button('Cancel', on_click=dialog.close).props('flat')
                        ui.button('Update', on_click=lambda: self._update_position(
                            transaction_id, qty_input.value, tp_input.value, sl_input.value, dialog
                        )).props('color=primary')

        dialog.open()

    def _revert_tpsl_manual_override(self, transaction_id: int, dialog):
        """Clear tp_manual_override / sl_manual_override flags so the next
        automation cycle (ruleset / smart risk manager / expert) is free to
        adjust TP and SL again."""
        from ...core.models import Transaction
        from ...core.db import update_instance

        try:
            txn = get_instance(Transaction, transaction_id)
            if not txn:
                ui.notify('Transaction not found', type='negative')
                return
            had_tp_lock = txn.tp_manual_override
            had_sl_lock = txn.sl_manual_override
            if not (had_tp_lock or had_sl_lock):
                ui.notify('TP/SL are not currently manually overridden', type='info')
                return
            txn.tp_manual_override = False
            txn.sl_manual_override = False
            update_instance(txn)

            cleared = []
            if had_tp_lock: cleared.append('TP')
            if had_sl_lock: cleared.append('SL')
            ui.notify(
                f'{" and ".join(cleared)} lock cleared — automation will manage from next cycle.',
                type='positive',
            )
            logger.info(
                f"Cleared manual override on transaction {transaction_id} for {txn.symbol}: "
                f"tp={had_tp_lock}, sl={had_sl_lock}"
            )
            dialog.close()
            self._refresh_transactions()
        except Exception as e:
            ui.notify(f'Error reverting overrides: {str(e)}', type='negative')
            logger.error(f"Error reverting overrides for transaction {transaction_id}: {e}", exc_info=True)

    def _update_position(self, transaction_id, new_quantity, tp_price, sl_price, dialog):
        """Update position quantity and/or TP/SL for a transaction."""
        from ...core.models import Transaction, TradingOrder
        from ...core.db import update_instance, get_db
        from sqlmodel import select, Session
        from ...core.TransactionHelper import TransactionHelper

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

            # Get account to use adjust methods
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

            # Direction-aware TP/SL sanity check BEFORE touching anything: catches
            # swapped values (e.g. TP below entry / SL above it on a long), which
            # the broker would reject anyway (Alpaca 42210000) or fill instantly.
            if tp_price or sl_price:
                try:
                    ref_price = account.get_instrument_current_price(txn.symbol)
                except Exception:
                    ref_price = None
                ok, reason = TransactionHelper.validate_tp_sl_prices(
                    txn.side, tp_price or None, sl_price or None,
                    reference_price=ref_price or txn.open_price)
                if not ok:
                    ui.notify(reason, type='negative', timeout=10000)
                    return

            # Check for quantity change
            old_quantity = txn.quantity
            qty_changed = new_quantity != old_quantity
            
            if qty_changed:
                # Validate new quantity
                if new_quantity <= 0:
                    ui.notify('Quantity must be greater than 0', type='negative')
                    return
                
                # Calculate quantity change
                qty_change = new_quantity - old_quantity
                
                # Use TransactionHelper to adjust quantity with TP/SL handling
                result = TransactionHelper.adjust_quantity_with_tpsl(
                    account=account,
                    transaction=txn,
                    qty_change=qty_change,
                    tp_price=tp_price if tp_price else None,
                    sl_price=sl_price if sl_price else None
                )
                
                if result["success"]:
                    msg = f'Position adjusted from {old_quantity:.2f} to {new_quantity:.2f}'
                    if tp_price or sl_price:
                        msg += f' with TP/SL'
                    ui.notify(msg, type='positive')
                    dialog.close()
                    self._refresh_transactions()
                    return
                else:
                    ui.notify(f'Failed to adjust position: {result["message"]}', type='negative')
                    return
            
            # No quantity change - just handle TP/SL updates
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
                        success = account.adjust_tp_sl(txn, tp_value, sl_value, source="manual")
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
                        success = account.adjust_tp(txn, tp_value, source="manual")
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
                        success = account.adjust_sl(txn, sl_value, source="manual")
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
                        success = account.adjust_tp(txn, tp_value, source="manual")
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
                            success = account.adjust_sl(txn, txn.stop_loss, source="manual")
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
                        success = account.adjust_sl(txn, sl_value, source="manual")
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
                            success = account.adjust_tp(txn, txn.take_profit, source="manual")
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
                ui.button('Reset & Retry', on_click=lambda: self._retry_close_position(transaction_id, dialog)).props('color=warning')

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
                    def open_analysis(aid=rec.market_analysis_id):
                        dialog.close()
                        if not hasattr(self, '_analysis_dialog'):
                            self._analysis_dialog = MarketAnalysisDetailDialog()
                        self._analysis_dialog.open(aid)
                    ui.button('View Market Analysis',
                             on_click=open_analysis,
                             icon='analytics').props('color=secondary')
                else:
                    ui.space()  # Empty space if no analysis link

                # Close button
                ui.button('Close', on_click=dialog.close).props('flat color=primary')

        dialog.open()

    def _show_transaction_details_dialog(self, transaction_id: int):
        """Show comprehensive transaction details in a dialog."""
        from ...core.models import ExpertRecommendation, ExpertInstance
        from ...core.types import TransactionStatus

        if not transaction_id:
            ui.notify('No transaction ID provided', type='warning')
            return

        # Get the transaction
        txn = get_instance(Transaction, transaction_id)
        if not txn:
            ui.notify('Transaction not found', type='negative')
            return

        # Get expert instance
        expert = get_instance(ExpertInstance, txn.expert_id) if txn.expert_id else None
        expert_name = f"{expert.expert} (ID: {expert.id})" if expert else "No Expert"

        # Get all orders for this transaction
        with Session(get_db().bind) as session:
            stmt = select(TradingOrder).where(TradingOrder.transaction_id == transaction_id).order_by(TradingOrder.created_at)
            orders = session.exec(stmt).all()

        with ui.dialog().props('maximized') as dialog, ui.card().classes('w-full h-full overflow-auto'):
            # Header
            with ui.row().classes('w-full items-center justify-between mb-4 bg-primary/10 p-4 rounded'):
                ui.label(f'🔍 Transaction Details - ID {transaction_id}').classes('text-h5')
                ui.button(icon='close', on_click=dialog.close).props('flat round')

            with ui.scroll_area().classes('w-full h-full'):
                # OPTION STRUCTURE (spec 2026-09-20, step 8). Rendered BEFORE the generic
                # cards because a structure's terms are what make the numbers below
                # readable at all: without them "Open Price $8.00" reads like a share
                # price, and four legs of one condor read as four separate trades.
                if getattr(txn, 'asset_class', None) == AssetClass.OPTION:
                    self._render_option_structure_section(txn, orders)

                # Transaction Overview
                with ui.card().classes('w-full mb-4'):
                    ui.label('📊 Transaction Overview').classes('text-h6 mb-3')
                    
                    with ui.grid(columns=4).classes('w-full gap-4 metric-grid'):
                        # Symbol
                        with ui.card().classes('bg-primary/5'):
                            ui.label('Symbol').classes('text-caption text-grey-7')
                            ui.label(txn.symbol).classes('text-h6 font-bold')
                        
                        # Direction
                        with ui.card().classes('bg-primary/5'):
                            ui.label('Direction').classes('text-caption text-grey-7')
                            # Use side field: BUY=LONG, SELL=SHORT
                            direction = 'BUY (LONG)' if txn.side == OrderDirection.BUY else 'SELL (SHORT)'
                            dir_color = 'green' if txn.side == OrderDirection.BUY else 'red'
                            ui.badge(direction, color=dir_color).classes('text-body1')
                        
                        # Quantity
                        with ui.card().classes('bg-primary/5'):
                            ui.label('Quantity').classes('text-caption text-grey-7')
                            ui.label(f'{txn.quantity:.2f}').classes('text-body1 font-bold')
                        
                        # Status
                        with ui.card().classes('bg-primary/5'):
                            ui.label('Status').classes('text-caption text-grey-7')
                            status_color = self._get_transaction_status_color(txn.status)
                            ui.badge(txn.status.value, color=status_color).classes('text-body1')

                    # Prices and P/L
                    with ui.grid(columns=4).classes('w-full gap-4 mt-4'):
                        # Open Price
                        with ui.card().classes('bg-primary/5'):
                            ui.label('Open Price').classes('text-caption text-grey-7')
                            open_price_str = f'${txn.open_price:.2f}' if txn.open_price else 'N/A'
                            ui.label(open_price_str).classes('text-body1')
                        
                        # Take Profit
                        with ui.card().classes('bg-primary/5'):
                            ui.label('Take Profit').classes('text-caption text-grey-7')
                            tp_str = f'${txn.take_profit:.2f}' if txn.take_profit else 'Not Set'
                            ui.label(tp_str).classes('text-body1')
                        
                        # Stop Loss
                        with ui.card().classes('bg-primary/5'):
                            ui.label('Stop Loss').classes('text-caption text-grey-7')
                            sl_str = f'${txn.stop_loss:.2f}' if txn.stop_loss else 'Not Set'
                            ui.label(sl_str).classes('text-body1')
                        
                        # Close Price
                        with ui.card().classes('bg-primary/5'):
                            ui.label('Close Price').classes('text-caption text-grey-7')
                            close_price_str = f'${txn.close_price:.2f}' if txn.close_price else 'Not Closed'
                            ui.label(close_price_str).classes('text-body1')

                    # Dates
                    with ui.grid(columns=3).classes('w-full gap-4 mt-4'):
                        # Created
                        with ui.card().classes('bg-primary/5'):
                            ui.label('Created').classes('text-caption text-grey-7')
                            created_str = txn.created_at.strftime('%Y-%m-%d %H:%M:%S') if txn.created_at else 'N/A'
                            ui.label(created_str).classes('text-body2')
                        
                        # Opened
                        with ui.card().classes('bg-primary/5'):
                            ui.label('Opened').classes('text-caption text-grey-7')
                            opened_str = txn.open_date.strftime('%Y-%m-%d %H:%M:%S') if txn.open_date else 'Not Opened'
                            ui.label(opened_str).classes('text-body2')
                        
                        # Closed
                        with ui.card().classes('bg-primary/5'):
                            ui.label('Closed').classes('text-caption text-grey-7')
                            closed_str = txn.close_date.strftime('%Y-%m-%d %H:%M:%S') if txn.close_date else 'Not Closed'
                            ui.label(closed_str).classes('text-body2')
                    
                    # Expert
                    with ui.card().classes('bg-primary/5 mt-4'):
                        ui.label('Expert').classes('text-caption text-grey-7')
                        ui.label(expert_name).classes('text-body1 font-bold')

                # WHAT WILL CLOSE THIS POSITION, directly under the numbers describing
                # it. The dialog could say which expert opened a trade and not one word
                # about the rules that will exit it -- the question an open position
                # actually raises -- so the answer was a trip to Settings, into the
                # expert, into its ruleset, with the transaction no longer on screen.
                self._render_expert_strategy_section(expert)

                # Transaction Meta Data
                if txn.meta_data and txn.meta_data:
                    with ui.card().classes('w-full mb-4'):
                        ui.label('🗃️ Transaction Meta Data').classes('text-h6 mb-3')
                        
                        # Show TradeConditionsData if available
                        if 'TradeConditionsData' in txn.meta_data:
                            trade_conditions = txn.meta_data['TradeConditionsData']
                            with ui.card().classes('bg-yellow/10'):
                                ui.label('Trade Conditions Data').classes('text-subtitle2 font-bold mb-2')
                                if 'current_target_price' in trade_conditions:
                                    with ui.row().classes('gap-2'):
                                        ui.label('Current Target Price:').classes('text-body2 font-bold')
                                        ui.label(f"${trade_conditions['current_target_price']:.2f}").classes('text-body2 text-green')
                        
                        # Show all meta_data in JSON format
                        with ui.expansion('Raw Meta Data (JSON)', icon='code').classes('w-full mt-2'):
                            ui.json_editor({'content': {'json': txn.meta_data}}).classes('w-full').props('read-only')

                # Related Orders
                with ui.card().classes('w-full mb-4'):
                    ui.label(f'📋 Related Orders ({len(orders)})').classes('text-h6 mb-3')
                    
                    if orders:
                        for idx, order in enumerate(orders):
                            with ui.expansion(f'Order #{order.id} - {order.order_type.value if order.order_type else "N/A"} {order.side.value if order.side else "N/A"}', 
                                            icon='receipt').classes('w-full').props('dense'):
                                
                                with ui.grid(columns=3).classes('w-full gap-4 mt-2'):
                                    # Basic Info
                                    with ui.card():
                                        ui.label('Order ID').classes('text-caption text-grey-7')
                                        ui.label(str(order.id)).classes('text-body2 font-bold')
                                    
                                    with ui.card():
                                        ui.label('Type / Side').classes('text-caption text-grey-7')
                                        type_str = f"{order.order_type.value if order.order_type else 'N/A'} / {order.side.value if order.side else 'N/A'}"
                                        ui.label(type_str).classes('text-body2')
                                    
                                    with ui.card():
                                        ui.label('Status').classes('text-caption text-grey-7')
                                        status_color = self._get_order_status_color(order.status)
                                        ui.badge(order.status.value if order.status else 'UNKNOWN', color=status_color)
                                    
                                    # Quantities
                                    with ui.card():
                                        ui.label('Quantity').classes('text-caption text-grey-7')
                                        qty_str = f'{order.quantity:.2f}' if order.quantity else 'N/A'
                                        ui.label(qty_str).classes('text-body2')
                                    
                                    with ui.card():
                                        ui.label('Filled').classes('text-caption text-grey-7')
                                        filled_str = f'{order.filled_qty:.2f}' if order.filled_qty else '0.00'
                                        ui.label(filled_str).classes('text-body2')
                                    
                                    with ui.card():
                                        ui.label('Open Price').classes('text-caption text-grey-7')
                                        open_str = f'${order.open_price:.2f}' if order.open_price else 'N/A'
                                        ui.label(open_str).classes('text-body2')
                                    
                                    # Prices
                                    with ui.card():
                                        ui.label('Limit Price').classes('text-caption text-grey-7')
                                        limit_str = f'${order.limit_price:.2f}' if order.limit_price else 'N/A'
                                        ui.label(limit_str).classes('text-body2')
                                    
                                    with ui.card():
                                        ui.label('Stop Price').classes('text-caption text-grey-7')
                                        stop_str = f'${order.stop_price:.2f}' if order.stop_price else 'N/A'
                                        ui.label(stop_str).classes('text-body2')
                                    
                                    with ui.card():
                                        ui.label('Account ID').classes('text-caption text-grey-7')
                                        ui.label(str(order.account_id)).classes('text-body2')
                                
                                # Broker Info
                                if order.broker_order_id:
                                    with ui.card().classes('mt-2 bg-blue/10'):
                                        ui.label('Broker Order ID').classes('text-caption text-grey-7')
                                        ui.label(order.broker_order_id).classes('text-body2 font-mono')
                                
                                # Comment
                                if order.comment:
                                    with ui.card().classes('mt-2'):
                                        ui.label('Comment').classes('text-caption text-grey-7')
                                        ui.label(order.comment).classes('text-body2')
                                
                                # Created date
                                with ui.card().classes('mt-2'):
                                    ui.label('Created').classes('text-caption text-grey-7')
                                    created_str = order.created_at.strftime('%Y-%m-%d %H:%M:%S') if order.created_at else 'N/A'
                                    ui.label(created_str).classes('text-body2')
                                
                                # Expert Recommendation
                                if order.expert_recommendation_id:
                                    with ui.card().classes('mt-2 bg-green/10'):
                                        with ui.row().classes('items-center justify-between w-full'):
                                            ui.label(f'Expert Recommendation ID: {order.expert_recommendation_id}').classes('text-body2 font-bold')
                                            
                                            def show_rec_details(rec_id=order.expert_recommendation_id):
                                                dialog.close()
                                                class EventData:
                                                    args = rec_id
                                                self._show_recommendation_dialog(EventData())
                                            
                                            ui.button('View Details', icon='info', on_click=show_rec_details).props('size=sm color=primary')
                    else:
                        ui.label('No orders found for this transaction').classes('text-grey-6 text-center q-pa-md')

        dialog.open()

    # --------------------------------------------------------------- strategy ---

    #: Entry blue, exit amber -- the test platform's two tones, so the same rule in the
    #: two UIs is the same colour. Both are ``/10`` over the dark card beneath them.
    _RULE_TONES = {'entry': 'bg-blue/10', 'exit': 'bg-orange/10'}

    def _render_expert_strategy_section(self, expert) -> None:
        """The expert's entry rules, exit conditions and screener filter. Read-only.

        Drawn as the test platform's Strategy tab draws them -- WHEN <gate> THEN
        <action>, one line per clause, the joining word in the gutter -- through the
        same formatter (``ui.utils.ruleset_view``). An operator reading a live position
        is almost always holding it against the backtest it was deployed from, and two
        wordings for one rule engine is a translation step in their head.

        Nothing here edits: the rules belong to a RULESET, which several experts may
        share, so an edit made from a single transaction would silently reach every
        position the ruleset governs. The section names the rulesets so the Settings
        page can be found.

        A transaction with no expert (allocator- or hand-created) draws nothing at all
        rather than an empty card promising rules that do not exist.
        """
        from ..utils.ruleset_view import ruleset_rule_views, screener_criteria

        if expert is None:
            return

        # SMART MODE BYPASSES THE RULESETS. ``WorkerQueue._process_expert_recommendations``
        # hands a smart-mode expert to the SmartRiskManager instead of the TradeManager,
        # and it is the TradeManager that evaluates these rules -- so printing them
        # without saying so would describe a mechanism that is not running.
        settings, risk_mode = {}, 'classic'
        try:
            from ...core.utils import get_risk_manager_mode
            interface = get_expert_instance_from_id(expert.id)
            if interface:
                settings = interface.settings or {}
                risk_mode = get_risk_manager_mode(settings)
        except Exception as e:
            # The rules still render: they are read from the DB, not from the interface.
            logger.warning(f'Could not load settings for expert {expert.id}: {e}')

        entry = ruleset_rule_views(expert.enter_market_ruleset_id)
        exits = ruleset_rule_views(expert.open_positions_ruleset_id)
        criteria = (screener_criteria(settings, self._screener_definitions())
                    if settings.get('instrument_selection_method') == 'screener' else [])
        if not entry and not exits and not criteria:
            return

        with ui.card().classes('w-full mb-4'):
            with ui.row().classes('w-full items-center gap-2 mb-3'):
                ui.label('🎯 Expert Strategy').classes('text-h6')
                if risk_mode == 'smart':
                    with ui.badge('Smart risk manager', color='purple'):
                        ui.tooltip('This expert is in Smart mode: the Smart Risk Manager '
                                   'decides entries and exits, and these rules are not '
                                   'what closes the position.')
            self._render_rule_group('Entry Rules', entry, tone='entry', color='blue',
                                    ruleset_id=expert.enter_market_ruleset_id)
            self._render_rule_group('Exit Conditions', exits, tone='exit', color='orange',
                                    ruleset_id=expert.open_positions_ruleset_id)
            if criteria:
                self._render_screener_criteria(criteria)

    def _render_rule_group(self, title: str, views, *, tone: str, color: str,
                           ruleset_id) -> None:
        """One side of the strategy: a heading, the ruleset's name, and its rules.

        The clause markup is ``ruleset_view.render_clause``, shared with the RULESET
        EDITOR: two copies of it is how the read-only view and the editable one start
        describing one rule in two different shapes.
        """
        from ..utils.ruleset_view import render_clause

        if not views:
            return
        with ui.row().classes('w-full items-baseline gap-2 mt-2'):
            ui.label(f'{title} ({len(views)})').classes(f'text-subtitle2 text-{color}')
            name = self._ruleset_name(ruleset_id)
            if name:
                ui.label(name).classes('text-caption text-grey-7')
            if len(views) > 1:
                # THE PRECEDENCE, said out loud. ``TradeActionEvaluator`` breaks after
                # the first rule whose conditions pass unless that rule is marked
                # ``continue_processing``, so a reader who takes this list as "all of
                # these apply" has the mechanism backwards.
                ui.label('— in order; the first match wins') \
                    .classes('text-caption text-grey-7')
        for view in views:
            with ui.card().classes(f'w-full q-pa-sm q-mb-xs {self._RULE_TONES[tone]}'):
                with ui.row().classes('items-baseline gap-2'):
                    ui.label(view.name).classes('text-body2 text-weight-bold')
                    if view.continues:
                        with ui.badge('continues', color='orange'):
                            ui.tooltip('Evaluation carries on to the next rule even '
                                       'after this one matches.')
                render_clause('WHEN', 'AND', view.when)
                render_clause('THEN', 'AND', view.then)

    @staticmethod
    def _screener_definitions():
        """``MarketExpertInterface``'s built-in setting metadata, or ``{}``.

        The source of truth for what a screener setting is called and what it falls
        back to -- the same dict the Settings dialog builds its editors from, so the
        read-only view and the editable one can never disagree about a label.
        """
        try:
            from ...core.interfaces.MarketExpertInterface import MarketExpertInterface
            MarketExpertInterface._ensure_builtin_settings()
            return MarketExpertInterface._builtin_settings or {}
        except Exception as e:
            logger.warning(f'Could not load built-in expert settings: {e}')
            return {}

    def _render_screener_criteria(self, criteria) -> None:
        """The filter that decides which instruments this expert may even look at.

        Each row carries the raw setting KEY beside its description. That is not
        clutter: ``screener_market_cap_max`` at 0 means no ceiling, and the deploy that
        read it as "admits nothing" is why the key is on screen (see the deploy-parity
        note in the settings export tooling).
        """
        with ui.row().classes('w-full items-baseline gap-2 mt-3'):
            ui.label(f'Screener ({len(criteria)})').classes('text-subtitle2 text-teal')
            ui.label('— the universe this expert selects from') \
                .classes('text-caption text-grey-7')
        with ui.grid(columns=2).classes('w-full gap-x-6 gap-y-1'):
            for criterion in criteria:
                with ui.row().classes('w-full items-baseline justify-between no-wrap gap-2'):
                    with ui.column().classes('gap-0 min-w-0'):
                        ui.label(criterion.label).classes('text-body2 truncate')
                        ui.label(criterion.key).classes('text-caption text-grey-7 font-mono')
                    # A value the user CHOSE and one that merely defaulted are different
                    # facts about a filter, and only the first was a decision.
                    value = ui.label(criterion.value).classes('text-body2 font-mono')
                    if criterion.is_default:
                        value.classes('text-grey-7')
                        value.tooltip('Not set on this expert — the platform default applies.')

    def _ruleset_name(self, ruleset_id) -> str:
        """The ruleset's name, or a stated absence. Never raises into the dialog.

        A dangling id -- the ruleset was deleted while an expert still pointed at it --
        is a real state, and it must show as one rather than collapsing the section
        that was about to explain what closes this position.
        """
        if not ruleset_id:
            return ''
        try:
            from ...core.models import Ruleset
            ruleset = get_instance(Ruleset, ruleset_id)
            return ruleset.name if ruleset else '(not found)'
        except Exception:
            logger.warning(f'Transaction details reference missing ruleset {ruleset_id}')
            return '(not found)'

    # ------------------------------------------------------------------ options ---
    #: Fewer stored ATM-IV samples than this and a "rank" would be a percentile of noise.
    IV_RANK_MIN_SAMPLES = 20

    def _render_option_structure_section(self, txn, orders) -> None:
        """The option intent, its legs, and the legs' live contract detail.

        Reads only what the transaction and its orders already hold, so it cannot fail on
        the network. The contract detail (quote, IV, greeks, moneyness) is a BROKER call and
        is filled in afterwards on a worker thread -- see ``_fill_option_contract_detail``.
        """
        from ...core.types import OrderDirection, OrderStatus

        option_orders = [o for o in orders if getattr(o, 'contract_symbol', None)]
        multiplier = getattr(txn, 'multiplier', None)
        expiry = getattr(txn, 'expiry', None)
        today = datetime.now(timezone.utc).date()
        dte = (expiry - today).days if isinstance(expiry, date) else None
        is_debit = getattr(txn, 'side', None) == OrderDirection.BUY

        with ui.card().classes('w-full mb-4'):
            with ui.row().classes('items-center gap-2 mb-2'):
                ui.label('🧩 Option Structure').classes('text-h6')
                ui.badge('OPTION', color='purple')
                if getattr(txn, 'option_strategy', None):
                    ui.badge(str(txn.option_strategy), color='indigo')

            with ui.grid(columns=4).classes('w-full gap-4'):
                with ui.card().classes('bg-primary/5'):
                    ui.label('Strategy').classes('text-caption text-grey-7')
                    ui.label(str(getattr(txn, 'option_strategy', None) or '—')).classes('text-body1 font-bold')
                with ui.card().classes('bg-primary/5'):
                    ui.label('Expiry').classes('text-caption text-grey-7')
                    ui.label(
                        '—' if expiry is None
                        else f'{expiry.isoformat()}' + (f' · {dte} DTE' if dte is not None else '')
                    ).classes('text-body1 font-bold')
                with ui.card().classes('bg-primary/5'):
                    ui.label('Legs / Multiplier').classes('text-caption text-grey-7')
                    ui.label(
                        f'{len(option_orders) or len(orders)} legs · '
                        + (f'x{multiplier}' if multiplier else 'multiplier NOT recorded')
                    ).classes('text-body1 font-bold')
                with ui.card().classes('bg-primary/5'):
                    ui.label('Net Premium / share').classes('text-caption text-grey-7')
                    ui.label(
                        '—' if txn.open_price is None
                        else f"${abs(float(txn.open_price)):.2f} {'debit' if is_debit else 'credit'}"
                    ).classes('text-body1 font-bold')

            if not multiplier:
                # Say it here rather than letting a 100x-undersized P&L read as a fact.
                ui.label(
                    'Contract multiplier not recorded: dollar P&L for this transaction cannot be '
                    'derived (it is NOT assumed to be 100).'
                ).classes('text-xs text-orange-400 mt-2')

            ui.label(
                'TP / SL on this transaction are PREMIUM levels (per share), not underlying prices.'
            ).classes('text-xs text-secondary-custom mt-2')

            if option_orders:
                columns = ['Leg', 'Contract', 'Strike', 'Expiry', 'Qty × mult', 'Intent', 'Status', 'Fill prem.']
                rows = []
                for order in option_orders:
                    rows.append({
                        'Leg': f"{getattr(order.side, 'value', '?')} {getattr(order.option_type, 'value', getattr(order, 'option_type', '') or '?')}",
                        'Contract': order.contract_symbol,
                        'Strike': '—' if order.strike is None else f'${float(order.strike):.2f}',
                        'Expiry': order.expiry.isoformat() if getattr(order, 'expiry', None) else '—',
                        'Qty × mult': f"{order.quantity:g} × {getattr(order, 'multiplier', None) or '?'}",
                        'Intent': getattr(order, 'position_intent', None) or '—',
                        'Status': getattr(order.status, 'value', '') or '—',
                        'Fill prem.': '—' if order.open_price is None else f'${float(order.open_price):.2f}/share',
                    })
                ui.table(columns=[{'name': c, 'label': c, 'field': c, 'align': 'left'} for c in columns],
                         rows=rows, row_key='Contract').classes('w-full mt-3').props('dense flat')

            # THE CHART (spec steps 9-10): cached-style daily candles for the underlying,
            # one dashed line per strike, both marker sets, and the expiration payoff drawn
            # rotated onto the price axis. Built from a worker thread because it fetches
            # bars; the payoff itself is derived from the recorded terms alone.
            self._option_chart_container = ui.column().classes('w-full mt-3')
            asyncio.create_task(self._fill_option_chart(self._option_chart_container, txn, orders))

            # BROKER data lands here, off the render path.
            self._option_detail_container = ui.column().classes('w-full mt-3')
            if option_orders:
                account_id = next((o.account_id for o in orders if getattr(o, 'account_id', None)), None)
                if account_id:
                    asyncio.create_task(self._fill_option_contract_detail(
                        self._option_detail_container, account_id,
                        [o.contract_symbol for o in option_orders], txn.symbol,
                    ))

    def _payoff_for(self, txn, orders):
        """The expiration payoff of this transaction, from its recorded order terms.

        A live ORDER's multiplier is a recorded contract term (the order carries the real
        one, and it is copied onto the transaction at creation), so it is trusted when
        present -- and a missing one still refuses to price rather than assuming 100.
        """
        from ba2_common.core.option_payoff_chart import (
            PayoffUnavailable, build_payoff_chart, chart_legs_from_rows,
        )

        # The ENTRY structure, never the order history (review R2). Concatenating every order
        # with a contract symbol drew the net cash of already-closed fills as if it were the
        # structure's expiration outcomes: a spread's -600/+400 curve flattened to +370 at
        # every price, and a cancelled order could add exposure that was never held.
        leg_set = opening_legs(txn, orders)

        # An executed OPENING leg dropped for a missing size or premium leaves a DIFFERENT
        # position, so the curve is refused rather than drawn for the remainder (second
        # review, N4): a 95/105 spread missing its short leg's premium would otherwise plot as
        # a lone long call with UNLIMITED maximum profit.
        if leg_set.incomplete:
            return PayoffUnavailable(leg_set.unavailable_reason or 'structure incompletely recorded')

        return build_payoff_chart(chart_legs_from_rows(leg_set.chart_rows()))

    async def _fill_option_chart(self, container, txn, orders) -> None:
        """Fetch the underlying's bars off the render path, then paint the figure."""
        from ba2_common.core.option_payoff_chart import PayoffUnavailable

        payoff = self._payoff_for(txn, orders)

        stamps = [getattr(order, 'created_at', None) for order in orders]
        stamps += [getattr(txn, 'open_date', None), getattr(txn, 'close_date', None)]
        stamps = [stamp for stamp in stamps if stamp is not None]
        if stamps:
            start = min(stamps) - timedelta(days=20)
            end = max(stamps) + timedelta(days=20)
        else:
            end = datetime.now(timezone.utc)
            start = end - timedelta(days=60)

        bars = None
        try:
            bars = await asyncio.to_thread(fetch_underlying_bars, txn.symbol, start, end)
        except Exception as exc:
            # A missing bar series is not a broken popup: the strikes, the leg table and
            # the payoff figures are all still there.
            logger.warning(f"[OPTION CHART] no bars for {txn.symbol}: {exc}")

        try:
            render_option_structure_chart(
                container, txn=txn, orders=orders, payoff=payoff, bars=bars,
                underlying=txn.symbol)
            if isinstance(payoff, PayoffUnavailable):
                with container:
                    ui.label(f'Expiration payoff unavailable — {payoff.reason}').classes(
                        'text-xs text-secondary-custom')
        except Exception as exc:
            logger.warning(f"[OPTION CHART] could not render for txn {txn.id}: {exc}")

    def _collect_option_contract_detail(self, account_id, contracts, underlying):
        """BLOCKING broker reads for the leg table's detail block.

        Only ever called inside ``asyncio.to_thread`` (the repo's convention for broker
        round trips). Every failure is per-leg and reported as unknown: a missing greek is
        never rendered as zero, and an account that does not implement the options
        interface simply has no detail to show.
        """
        from ba2_common.core.interfaces.OptionsAccountInterface import OptionsAccountInterface

        detail = {'account_supports_options': False, 'spot': None, 'atm_iv': None,
                  'iv_rank': None, 'quotes': {}, 'errors': {}}
        try:
            account = get_account_instance_from_id(account_id)
        except Exception as exc:
            detail['errors']['account'] = str(exc)
            return detail

        if not isinstance(account, OptionsAccountInterface):
            detail['errors']['account'] = 'account does not implement the options interface'
            return detail
        detail['account_supports_options'] = True

        try:
            detail['spot'] = account.get_instrument_current_price(underlying, 'mid')
        except Exception as exc:
            detail['errors']['spot'] = str(exc)

        for contract in contracts:
            try:
                quote = account.get_option_quote(contract)
            except Exception as exc:
                detail['errors'][contract] = str(exc)
                continue
            if quote is None:
                detail['errors'][contract] = 'no snapshot'
                continue
            detail['quotes'][contract] = {
                'bid': getattr(quote, 'bid', None), 'ask': getattr(quote, 'ask', None),
                'last': getattr(quote, 'last', None), 'mid': getattr(quote, 'mid', None),
                'iv': getattr(quote, 'implied_volatility', None),
                'delta': getattr(quote, 'delta', None), 'gamma': getattr(quote, 'gamma', None),
                'theta': getattr(quote, 'theta', None), 'vega': getattr(quote, 'vega', None),
                'timestamp': getattr(quote, 'timestamp', None),
            }

        try:
            detail['atm_iv'] = account.get_atm_implied_volatility(underlying)
        except Exception as exc:
            detail['errors']['atm_iv'] = str(exc)

        return detail

    def _iv_rank(self, account_id, underlying, current_iv):
        """IV rank from OUR OWN stored ATM-IV series (brokers publish no IV history).

        Returns ``(rank_percent, sample_count)`` or ``(None, count)`` when the window is too
        short to mean anything -- a percentile of four samples is not a rank.
        """
        from ...core.models import OptionIVSnapshot

        # The session is acquired INSIDE the guard: a DB handle that cannot be obtained is
        # the same outcome as a read that fails -- no rank -- and must not escape into the
        # task that is painting the dialog.
        session = None
        try:
            session = get_db()
            rows = session.exec(
                select(OptionIVSnapshot)
                .where(OptionIVSnapshot.account_id == account_id,
                       OptionIVSnapshot.underlying == underlying)
                .order_by(OptionIVSnapshot.recorded_at)
            ).all()
        except Exception as exc:
            logger.debug(f"[OPTION DETAIL] IV history read failed: {exc}")
            return None, 0
        finally:
            if session is not None:
                session.close()

        samples = [float(r.atm_iv) for r in rows if getattr(r, 'atm_iv', None) is not None]
        if len(samples) < self.IV_RANK_MIN_SAMPLES or current_iv is None:
            return None, len(samples)
        at_or_below = sum(1 for value in samples if value <= float(current_iv))
        return (at_or_below / len(samples)) * 100.0, len(samples)

    async def _fill_option_contract_detail(self, container, account_id, contracts, underlying) -> None:
        """Fill the contract-detail block from a worker thread, then paint it."""
        try:
            detail = await asyncio.to_thread(
                self._collect_option_contract_detail, account_id, contracts, underlying)
        except Exception as exc:
            logger.warning(f"[OPTION DETAIL] contract detail unavailable: {exc}")
            return

        iv_rank, samples = self._iv_rank(account_id, underlying, detail.get('atm_iv'))

        with container:
            if not detail.get('account_supports_options'):
                ui.label(
                    'Contract detail unavailable: ' + detail['errors'].get('account', 'no options interface')
                ).classes('text-xs text-secondary-custom')
                return

            with ui.row().classes('items-center gap-3'):
                ui.label('📈 Contract detail (CURRENT — not the position\'s P&L)').classes('text-subtitle1 font-bold')
                if detail.get('spot') is not None:
                    ui.label(f"underlying now ${float(detail['spot']):.2f}").classes('text-xs text-secondary-custom')
                if detail.get('atm_iv') is not None:
                    ui.label(f"ATM IV now {float(detail['atm_iv']) * 100:.1f}%").classes('text-xs text-secondary-custom')
                if iv_rank is not None:
                    ui.label(f"IV rank {iv_rank:.0f}% ({samples} samples)").classes('text-xs text-secondary-custom')
                elif detail.get('atm_iv') is not None:
                    ui.label(
                        f'IV rank not available yet ({samples} of {self.IV_RANK_MIN_SAMPLES} stored samples)'
                    ).classes('text-xs text-secondary-custom')

            def cell(value, fmt='{:.2f}'):
                return '—' if value is None else fmt.format(float(value))

            table_rows = []
            for contract in contracts:
                quote = detail['quotes'].get(contract)
                if quote is None:
                    table_rows.append({
                        'Contract': contract, 'Bid': '—', 'Ask': '—', 'Mid': '—', 'Last': '—',
                        'IV': '—', 'Delta': '—', 'Gamma': '—', 'Theta': '—', 'Vega': '—',
                        'Note': detail['errors'].get(contract, 'unavailable'),
                    })
                    continue
                table_rows.append({
                    'Contract': contract,
                    'Bid': cell(quote['bid']), 'Ask': cell(quote['ask']),
                    'Mid': cell(quote['mid']), 'Last': cell(quote['last']),
                    'IV': '—' if quote['iv'] is None else f"{float(quote['iv']) * 100:.1f}%",
                    'Delta': cell(quote['delta'], '{:+.3f}'), 'Gamma': cell(quote['gamma'], '{:.4f}'),
                    'Theta': cell(quote['theta'], '{:+.3f}'), 'Vega': cell(quote['vega'], '{:.3f}'),
                    'Note': '',
                })

            columns = ['Contract', 'Bid', 'Ask', 'Mid', 'Last', 'IV', 'Delta', 'Gamma', 'Theta', 'Vega', 'Note']
            ui.table(columns=[{'name': c, 'label': c, 'field': c, 'align': 'left'} for c in columns],
                     rows=table_rows, row_key='Contract').classes('w-full').props('dense flat')
            ui.label(
                'Broker-sourced and current: greeks and IV describe the contract RIGHT NOW, '
                'and are not part of any expiration payoff.'
            ).classes('text-xs text-secondary-custom')

    def _get_transaction_status_color(self, status):
        """Get color for transaction status badge (the one map the tables use too)."""
        return transaction_status_color(status)

    def _select_all_transactions(self):
        """Select all visible transactions."""
        if self.live_trades_table:
            self.live_trades_table.select_all_visible()

    def _clear_selected_transactions(self):
        """Clear all selected transactions."""
        if self.live_trades_table:
            self.live_trades_table.clear_selection()

    def _update_batch_buttons(self):
        """Show/hide batch operation buttons based on selection."""
        if not hasattr(self, 'batch_close_btn'):
            return

        count = len(self.live_trades_table.get_selected_ids()) if self.live_trades_table else 0
        has_selection = count > 0

        self.batch_select_all_btn.set_visibility(True)
        self.batch_clear_btn.set_visibility(has_selection)
        self.batch_close_btn.set_visibility(has_selection)
        self.batch_adjust_tp_btn.set_visibility(has_selection)

    def _batch_close_transactions(self):
        """Show confirmation dialog and close all selected transactions."""
        if not self.live_trades_table:
            return
            
        selected_ids = self.live_trades_table.get_selected_ids()
        if not selected_ids:
            ui.notify('No transactions selected', type='warning')
            return

        count = len(selected_ids)

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

        if not self.live_trades_table:
            return
            
        transaction_ids = self.live_trades_table.get_selected_ids()
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
        if not self.live_trades_table:
            return
            
        selected_ids = self.live_trades_table.get_selected_ids()
        if not selected_ids:
            ui.notify('No transactions selected', type='warning')
            return

        count = len(selected_ids)

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

        if not self.live_trades_table:
            return
            
        transaction_ids = self.live_trades_table.get_selected_ids()
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
                            success = account.adjust_tp(txn, new_tp_price, source="manual")
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


async def content():
    """Render the live trades page content."""
    tab = LiveTradesTab()
    await tab.render()