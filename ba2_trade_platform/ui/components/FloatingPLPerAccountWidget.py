"""
Floating P/L Widget Base and Per-Account Widget
Displays unrealized profit/loss for open positions grouped by account.
"""
from nicegui import ui
import asyncio
from typing import Dict, Optional, List, Tuple
from sqlmodel import select, Session
from ...logger import logger
from ...core.db import get_db
from ...core.models import Transaction, AccountDefinition, TradingOrder
from ...core.types import TransactionStatus, OrderStatus, OrderDirection, OrderType
from ...core.utils import get_account_instance_from_id
from ..account_filter_context import get_selected_account_id, get_expert_ids_for_account


class _FloatingPLWidgetBase:
    """Base class for floating P/L widgets.

    Subclasses must define:
        _title: str             - card header text
        _get_extra_filters()    - additional SQLAlchemy where-clauses for the transaction query
        _group_transactions()   - group raw transactions into {account_id: [(trans, display_name), ...]}
    """

    _title: str = ""

    def __init__(self):
        """Initialize and render the widget."""
        self.render()

    def render(self):
        """Render the widget with loading state."""
        with ui.card().classes('p-4'):
            ui.label(self._title).classes('text-h6 mb-4')

            # Create loading placeholder
            loading_label = ui.label('🔄 Calculating floating P/L...').classes('text-sm text-gray-500')
            content_container = ui.column().classes('w-full')

            # Load data asynchronously (non-blocking)
            asyncio.create_task(self._load_data_async(loading_label, content_container))

    # ------------------------------------------------------------------
    # Hooks for subclasses
    # ------------------------------------------------------------------

    def _get_extra_filters(self) -> list:
        """Return additional where-clause expressions to apply to the transaction query.

        Default: no extra filters.
        """
        return []

    def _group_transactions(
        self, transactions: List[Transaction], session: Session
    ) -> Dict[int, List[Tuple[Transaction, str]]]:
        """Group *transactions* by account_id for bulk price fetching.

        Must return ``{account_id: [(transaction, display_name), ...]}``.
        *display_name* is the label shown in the UI (e.g. account name or expert alias).

        Subclasses override this to implement their own grouping logic.
        """
        raise NotImplementedError

    # ------------------------------------------------------------------
    # Core P/L calculation (shared)
    # ------------------------------------------------------------------

    def _calculate_pl_sync(
        self,
        selected_account_id: Optional[int],
        account_expert_ids: Optional[List[int]],
    ) -> Dict[str, float]:
        """Synchronous P/L calculation (runs in thread pool to avoid blocking).

        Uses bulk price fetching per broker account.

        Args:
            selected_account_id: The selected account ID from filter, or None for all.
            account_expert_ids: List of expert IDs belonging to selected account, or None for all.
        """
        pl_by_name: Dict[str, float] = {}

        session = get_db()
        try:
            # Build query for open transactions
            query = (
                select(Transaction)
                .where(Transaction.status.in_([TransactionStatus.OPENED, TransactionStatus.WAITING]))
            )

            # Subclass-specific filters (e.g. expert_id IS NOT NULL)
            for clause in self._get_extra_filters():
                query = query.where(clause)

            # Apply account filter if selected (filter by expert_id which belongs to account)
            if account_expert_ids is not None:
                if account_expert_ids:
                    query = query.where(Transaction.expert_id.in_(account_expert_ids))
                else:
                    # No experts for selected account - return empty
                    return {}

            transactions = session.exec(query).all()

            # Group transactions by account for bulk price fetching
            account_transactions = self._group_transactions(transactions, session)

            # Fetch prices in bulk per account and calculate P/L
            for account_id, trans_list in account_transactions.items():
                try:
                    # Get account interface once
                    account = get_account_instance_from_id(account_id, session=session)
                    if not account:
                        continue

                    # Get broker positions to use their current_price
                    broker_positions = account.get_positions()
                    prices: Dict[str, float] = {}
                    if broker_positions:
                        for pos in broker_positions:
                            pos_dict = pos if isinstance(pos, dict) else dict(pos)
                            prices[pos_dict['symbol']] = float(pos_dict['current_price'])

                    # Calculate P/L for each transaction using broker prices
                    for trans, display_name in trans_list:
                        current_price = prices.get(trans.symbol)

                        if not current_price:
                            continue

                        # Get all orders for this transaction
                        all_orders = session.exec(
                            select(TradingOrder)
                            .where(TradingOrder.transaction_id == trans.id)
                        ).all()

                        if not all_orders:
                            continue

                        # Get position side from transaction.side field
                        # BUY = LONG position, SELL = SHORT position
                        position_direction = trans.side

                        # Get all FILLED orders (any type that affects position)
                        # Exclude: OCO, OTO orders (they are TP/SL brackets, not position-affecting until triggered)
                        filled_orders = [
                            o for o in all_orders
                            if o.status in OrderStatus.get_executed_statuses()
                            and o.order_type not in [OrderType.OCO, OrderType.OTO]
                            and o.filled_qty and o.filled_qty > 0
                        ]

                        # Calculate net position and weighted average cost
                        total_buy_cost = 0.0
                        total_buy_qty = 0.0
                        total_sell_cost = 0.0
                        total_sell_qty = 0.0

                        for order in filled_orders:
                            if not order.open_price or not order.filled_qty:
                                continue

                            if order.side == OrderDirection.BUY:
                                total_buy_cost += order.filled_qty * order.open_price
                                total_buy_qty += order.filled_qty
                            elif order.side == OrderDirection.SELL:
                                total_sell_cost += order.filled_qty * order.open_price
                                total_sell_qty += order.filled_qty

                        # Net filled quantity = buys - sells
                        net_filled_qty = total_buy_qty - total_sell_qty

                        if abs(net_filled_qty) < 0.01:  # No net position
                            continue

                        # Calculate weighted average entry price based on position direction
                        if position_direction == OrderDirection.BUY:
                            # Long position: entry price is avg BUY price
                            if total_buy_qty < 0.01:
                                continue
                            avg_price = total_buy_cost / total_buy_qty
                        else:
                            # Short position: entry price is avg SELL price
                            if total_sell_qty < 0.01:
                                continue
                            avg_price = total_sell_cost / total_sell_qty

                        # Use transaction.quantity as source of truth for current position
                        position_qty = trans.quantity

                        # Calculate P/L: (current_price - avg_price) * position_quantity
                        pl = (current_price - avg_price) * position_qty
                        if position_direction == OrderDirection.SELL:
                            pl = -pl  # Invert for short positions

                        if display_name not in pl_by_name:
                            pl_by_name[display_name] = 0.0
                        pl_by_name[display_name] += pl

                        # Debug: Log when net filled qty differs from transaction qty
                        if abs(net_filled_qty - abs(trans.quantity)) > 0.01:
                            logger.debug(
                                f"Transaction {trans.id}: net_filled_qty={net_filled_qty:.2f} "
                                f"(buys={total_buy_qty:.2f} - sells={total_sell_qty:.2f}), "
                                f"transaction.quantity={trans.quantity}"
                            )

                except Exception as e:
                    logger.error(f"Error calculating P/L for account {account_id}: {e}", exc_info=True)
                    continue

        finally:
            session.close()

        return pl_by_name

    # ------------------------------------------------------------------
    # Async UI rendering (shared)
    # ------------------------------------------------------------------

    async def _load_data_async(self, loading_label, content_container):
        """Calculate and display floating P/L (async wrapper for thread pool execution)."""
        try:
            # Capture account filter values BEFORE running in thread pool
            # (app.storage.user is request-context bound and not available in thread pool)
            selected_account_id = get_selected_account_id()
            account_expert_ids = get_expert_ids_for_account(selected_account_id)

            # Run database queries in thread pool to avoid blocking UI
            loop = asyncio.get_event_loop()
            pl_data = await loop.run_in_executor(
                None,
                lambda: self._calculate_pl_sync(selected_account_id, account_expert_ids)
            )

            # Clear loading message
            try:
                loading_label.delete()
            except RuntimeError:
                return

            # Display results
            try:
                with content_container:
                    if not pl_data:
                        ui.label('No open positions').classes('text-sm text-gray-500')
                        return

                    # Sort by P/L (highest to lowest)
                    sorted_pl = sorted(pl_data.items(), key=lambda x: x[1], reverse=True)

                    # Calculate total
                    total_pl = sum(pl_data.values())

                    # Display each entry's P/L
                    for name, pl in sorted_pl:
                        with ui.row().classes('w-full justify-between items-center mb-1'):
                            ui.label(name).classes('text-sm truncate max-w-[200px]')
                            pl_color = 'text-green-600' if pl >= 0 else 'text-red-600'
                            ui.label(f'${pl:,.2f}').classes(f'text-sm font-bold {pl_color}')

                    # Separator
                    ui.separator().classes('my-2')

                    # Total P/L
                    with ui.row().classes('w-full justify-between items-center'):
                        ui.label('Total P/L:').classes('text-sm font-bold')
                        total_color = 'text-green-600' if total_pl >= 0 else 'text-red-600'
                        ui.label(f'${total_pl:,.2f}').classes(f'text-lg font-bold {total_color}')

            except RuntimeError:
                return

        except Exception as e:
            logger.error(f"Error loading floating P/L ({self._title}): {e}", exc_info=True)
            try:
                loading_label.delete()
            except RuntimeError:
                return
            try:
                with content_container:
                    ui.label('❌ Error calculating P/L').classes('text-sm text-red-600')
            except RuntimeError:
                pass


class FloatingPLPerAccountWidget(_FloatingPLWidgetBase):
    """Widget component showing floating profit/loss per account."""

    _title = '📊 Floating P/L Per Account'

    def _group_transactions(
        self, transactions: List[Transaction], session: Session
    ) -> Dict[int, List[Tuple[Transaction, str]]]:
        selected_account_id = get_selected_account_id()
        account_transactions: Dict[int, List[Tuple[Transaction, str]]] = {}

        for trans in transactions:
            try:
                first_order = session.exec(
                    select(TradingOrder)
                    .where(TradingOrder.transaction_id == trans.id)
                    .limit(1)
                ).first()

                if not first_order or not first_order.account_id:
                    continue

                # If account filter is active, skip transactions from other accounts
                if selected_account_id is not None and first_order.account_id != selected_account_id:
                    continue

                account_def = session.get(AccountDefinition, first_order.account_id)
                if not account_def:
                    continue

                account_id = first_order.account_id
                if account_id not in account_transactions:
                    account_transactions[account_id] = []
                account_transactions[account_id].append((trans, account_def.name))

            except Exception as e:
                logger.error(f"Error grouping transaction {trans.id}: {e}", exc_info=True)
                continue

        return account_transactions
