"""
Balance Usage Per Expert Chart Component

A stacked bar chart showing used vs available balance for each expert instance.
"""

from nicegui import ui
from sqlmodel import select
from typing import Dict, List, Optional
import asyncio
from ...core.db import get_db
from ...core.models import TradingOrder, ExpertInstance, Transaction
from ...core.types import OrderStatus
from ...logger import logger
from ..account_filter_context import get_selected_account_id, get_expert_ids_for_account
from .echart_theme import make_chart_options, MUTED_TEXT


class BalanceUsagePerExpertChart:
    """Component that displays a stacked bar chart of used vs available balance per expert."""

    def __init__(self):
        self.chart = None
        self.container = None
        self.render()

    def calculate_expert_balance_data(self) -> Dict[str, Dict[str, float]]:
        """
        Calculate balance breakdown for each expert: used (pending + filled) and available.

        Returns:
            Dict mapping expert names to their balance breakdown:
            {
                'expert_name': {
                    'pending': float,
                    'filled': float,
                    'available': float,
                    'total': float (virtual balance)
                }
            }
        """
        balance_data = {}

        # Get global account filter
        selected_account_id = get_selected_account_id()
        account_expert_ids = get_expert_ids_for_account(selected_account_id)

        with get_db() as session:
            from ...core.types import TransactionStatus, OrderType
            from ...core.utils import get_account_instance_from_id, get_expert_instance_from_id

            # Get all active expert instances (with account filter)
            expert_query = select(ExpertInstance).where(ExpertInstance.enabled == True)
            if account_expert_ids is not None:
                if account_expert_ids:
                    expert_query = expert_query.where(ExpertInstance.id.in_(account_expert_ids))
                else:
                    return {}

            experts = session.exec(expert_query).all()

            for expert in experts:
                expert_name = f"{expert.alias or expert.expert}-{expert.id}"

                # Get virtual balance for this expert
                expert_interface = get_expert_instance_from_id(expert.id)
                if not expert_interface:
                    continue

                virtual_balance = expert_interface.get_virtual_balance()
                if virtual_balance is None:
                    continue

                balance_data[expert_name] = {
                    'pending': 0.0,
                    'filled': 0.0,
                    'available': 0.0,  # Will be computed as total - filled - pending
                    'total': virtual_balance
                }

            # Now calculate used balance from transactions
            query = (
                select(Transaction)
                .where(Transaction.expert_id.isnot(None))
                .where(Transaction.status.in_([TransactionStatus.OPENED, TransactionStatus.WAITING, TransactionStatus.CLOSING]))
            )

            if account_expert_ids is not None:
                if account_expert_ids:
                    query = query.where(Transaction.expert_id.in_(account_expert_ids))
                else:
                    return balance_data

            transactions = session.exec(query).all()

            # Prefetch prices for all symbols in bulk
            if transactions:
                unique_symbols = list(set(t.symbol for t in transactions))

                try:
                    first_order = session.exec(
                        select(TradingOrder)
                        .where(TradingOrder.transaction_id == transactions[0].id)
                        .limit(1)
                    ).first()

                    if first_order and first_order.account_id:
                        account_interface = get_account_instance_from_id(first_order.account_id, session=session)
                        account_interface.get_instrument_current_price(unique_symbols, price_type='bid')
                        account_interface.get_instrument_current_price(unique_symbols, price_type='ask')
                except Exception as e:
                    logger.warning(f"Failed to proactively prefetch prices: {e}")

            for transaction in transactions:
                try:
                    expert = session.get(ExpertInstance, transaction.expert_id)
                    if not expert:
                        continue

                    expert_name = f"{expert.alias or expert.expert}-{expert.id}"

                    if expert_name not in balance_data:
                        continue

                    # Get account interface for market price
                    account_interface = None
                    try:
                        first_order = session.exec(
                            select(TradingOrder)
                            .where(TradingOrder.transaction_id == transaction.id)
                            .limit(1)
                        ).first()

                        if first_order and first_order.account_id:
                            account_interface = get_account_instance_from_id(first_order.account_id, session=session)
                    except Exception as e:
                        logger.debug(f"Could not get account interface for transaction {transaction.id}: {e}")

                    market_price = None
                    if account_interface:
                        try:
                            market_price = account_interface.get_instrument_current_price(transaction.symbol)
                        except Exception as e:
                            logger.debug(f"Could not get market price for {transaction.symbol}: {e}")

                    if not market_price:
                        continue

                    if transaction.status in [TransactionStatus.OPENED, TransactionStatus.CLOSING]:
                        equity = abs(transaction.quantity) * market_price
                        balance_data[expert_name]['filled'] += equity

                    elif transaction.status == TransactionStatus.WAITING:
                        market_orders = session.exec(
                            select(TradingOrder)
                            .where(TradingOrder.transaction_id == transaction.id)
                            .where(TradingOrder.order_type == OrderType.MARKET)
                        ).all()

                        for order in market_orders:
                            if order.status in OrderStatus.get_unfilled_statuses():
                                remaining_qty = order.quantity
                                if order.filled_qty:
                                    remaining_qty -= order.filled_qty

                                if remaining_qty > 0:
                                    equity = abs(remaining_qty) * market_price
                                    balance_data[expert_name]['pending'] += equity

                except Exception as e:
                    logger.error(f"Error calculating balance usage for transaction {transaction.id}: {e}")
                    continue

            # Compute available = total - filled - pending (ensures stack adds up)
            for name, data in balance_data.items():
                data['available'] = max(0, data['total'] - data['filled'] - data['pending'])

            # Sort by total balance (highest to lowest)
            balance_data = dict(sorted(
                balance_data.items(),
                key=lambda x: x[1]['total'],
                reverse=True
            ))

        return balance_data

    def render(self):
        """Render the balance usage per expert chart."""
        with ui.card().classes('p-4') as card:
            ui.label('💼 Balance Usage Per Expert').classes('text-h6 mb-4')

            # Create container for the chart content
            self.container = ui.column().classes('w-full')

            # Load data asynchronously
            asyncio.create_task(self._load_chart_async())

    async def _load_chart_async(self):
        """Asynchronously load and render the chart data."""
        with self.container:
            spinner = ui.spinner(size='lg')
            loading_label = ui.label('Loading balance usage data...').classes('text-sm text-gray-500 ml-2')

        balance_data = await asyncio.to_thread(self.calculate_expert_balance_data)

        self.container.clear()

        with self.container:
            if not balance_data:
                ui.label('No active experts found.').classes('text-sm text-gray-500')
                return

            expert_names = list(balance_data.keys())
            filled_values = [round(balance_data[name]['filled'], 2) for name in expert_names]
            pending_values = [round(balance_data[name]['pending'], 2) for name in expert_names]
            available_values = [round(balance_data[name]['available'], 2) for name in expert_names]
            total_values = [round(balance_data[name]['total'], 2) for name in expert_names]

            total_filled = sum(filled_values)
            total_pending = sum(pending_values)
            total_available = sum(available_values)
            total_all = sum(total_values)

            options = make_chart_options(
                tooltip={
                    'trigger': 'axis',
                    'axisPointer': {
                        'type': 'shadow'
                    },
                },
                legend={
                    'data': ['Filled Positions', 'Pending Orders', 'Available'],
                    'bottom': 0,
                },
                grid={
                    'bottom': '18%',
                    'top': '18%',
                },
                xAxis={
                    'type': 'category',
                    'data': expert_names,
                    'axisLabel': {
                        'rotate': 45,
                        'interval': 0,
                        'fontSize': 9,
                        'width': 80,
                        'overflow': 'truncate'
                    },
                },
                yAxis={
                    'type': 'value',
                    'axisLabel': {
                        'formatter': '${value}',
                    },
                },
                series=[
                    {
                        'name': 'Filled Positions',
                        'type': 'bar',
                        'stack': 'total',
                        'data': [
                            {
                                'value': filled_values[i],
                                'itemStyle': {
                                    'borderRadius': [0, 0, 0, 0]
                                }
                            } for i in range(len(expert_names))
                        ],
                        'barMaxWidth': 40,
                        'itemStyle': {
                            'color': '#00d4aa'
                        }
                    },
                    {
                        'name': 'Pending Orders',
                        'type': 'bar',
                        'stack': 'total',
                        'data': [
                            {
                                'value': pending_values[i],
                                'itemStyle': {
                                    'borderRadius': [0, 0, 0, 0]
                                }
                            } for i in range(len(expert_names))
                        ],
                        'barMaxWidth': 40,
                        'itemStyle': {
                            'color': '#ffa94d'
                        }
                    },
                    {
                        'name': 'Available',
                        'type': 'bar',
                        'stack': 'total',
                        'data': [
                            {
                                'value': available_values[i],
                                'itemStyle': {
                                    'borderRadius': [4, 4, 0, 0]
                                },
                                'label': {
                                    'show': True,
                                    'position': 'top',
                                    'fontSize': 9,
                                    'color': MUTED_TEXT,
                                    'formatter': f'${total_values[i]:,.0f}'
                                }
                            } for i in range(len(expert_names))
                        ],
                        'barMaxWidth': 40,
                        'itemStyle': {
                            'color': 'rgba(100, 120, 160, 0.3)'
                        }
                    }
                ]
            )

            self.chart = ui.echart(options).classes('w-full h-64')

            with ui.row().classes('w-full justify-between mt-4 text-sm'):
                ui.label(f'Total Experts: {len(balance_data)}').classes('text-gray-600')
                ui.label(f'Filled: ${total_filled:,.2f}').classes('text-green-600 font-bold')
                ui.label(f'Pending: ${total_pending:,.2f}').classes('text-orange-600 font-bold')
                ui.label(f'Available: ${total_available:,.2f}').classes('text-gray-500')
                ui.label(f'Total: ${total_all:,.2f}').classes('font-bold text-blue-600')

    def refresh(self):
        """Refresh the chart with updated data."""
        if self.container:
            self.container.clear()
            asyncio.create_task(self._load_chart_async())
