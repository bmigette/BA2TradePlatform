"""
Balance Usage Per Expert Chart Component

A stacked histogram chart showing balance usage for each expert instance,
split into pending orders and filled orders.
"""

from nicegui import ui
from sqlmodel import select
from typing import Dict, List
import asyncio
from ...core.db import get_db
from ...core.models import TradingOrder, ExpertInstance, Transaction
from ...core.types import OrderStatus
from ...logger import logger


class BalanceUsagePerExpertChart:
    """Component that displays a stacked histogram of balance usage per expert instance."""
    
    def __init__(self):
        self.chart = None
        self.container = None
        self.render()
    
    def calculate_expert_balance_usage(self) -> Dict[str, Dict[str, float]]:
        """
        Calculate balance usage for each expert from MARKET orders only.
        Only MARKET orders use buying power - TP/SL orders are exit orders and should be ignored.
        
        Returns:
            Dict mapping expert names to their balance usage breakdown:
            {
                'expert_name': {
                    'pending': float (total value of pending MARKET orders),
                    'filled': float (total value of filled MARKET orders)
                }
            }
        """
        balance_usage = {}
        
        with get_db() as session:
            from ...core.types import TransactionStatus, OrderType
            from ...core.utils import get_account_instance_from_id
            
            # Get all transactions with expert_id (both OPENED and WAITING status)
            transactions = session.exec(
                select(Transaction)
                .where(Transaction.expert_id.isnot(None))
                .where(Transaction.status.in_([TransactionStatus.OPENED, TransactionStatus.WAITING]))
            ).all()
            
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
                        
                        # Prefetch bid and ask prices
                        account_interface.get_instrument_current_price(unique_symbols, price_type='bid')
                        account_interface.get_instrument_current_price(unique_symbols, price_type='ask')
                except Exception as e:
                    logger.warning(f"Failed to proactively prefetch prices: {e}")
            
            # Calculate balance usage for each transaction by directly querying MARKET orders
            for transaction in transactions:
                try:
                    # Get expert instance
                    expert = session.get(ExpertInstance, transaction.expert_id)
                    if not expert:
                        logger.warning(f"Expert instance {transaction.expert_id} not found for transaction {transaction.id}")
                        continue
                    
                    # Create unique expert name using alias or expert type with ID
                    expert_name = f"{expert.alias or expert.expert}-{expert.id}"
                    
                    # Initialize expert entry if not exists
                    if expert_name not in balance_usage:
                        balance_usage[expert_name] = {
                            'pending': 0.0,
                            'filled': 0.0
                        }
                    
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
                    
                    # Get market price for value calculation
                    market_price = None
                    if account_interface:
                        try:
                            market_price = account_interface.get_instrument_current_price(transaction.symbol)
                        except Exception as e:
                            logger.debug(f"Could not get market price for {transaction.symbol}: {e}")
                    
                    if not market_price:
                        continue
                    
                    # Get all MARKET orders for this transaction
                    market_orders = session.exec(
                        select(TradingOrder)
                        .where(TradingOrder.transaction_id == transaction.id)
                        .where(TradingOrder.order_type == OrderType.MARKET)
                    ).all()
                    
                    # Calculate pending and filled equity from MARKET orders only
                    for order in market_orders:
                        if order.status in OrderStatus.get_unfilled_statuses():
                            # Unfilled MARKET order - count as pending
                            remaining_qty = order.quantity
                            if order.filled_qty:
                                remaining_qty -= order.filled_qty
                            
                            if remaining_qty > 0:
                                equity = abs(remaining_qty) * market_price
                                balance_usage[expert_name]['pending'] += equity
                                
                        elif order.status in OrderStatus.get_executed_statuses():
                            # Executed MARKET order - count as filled/used balance
                            filled_qty = order.filled_qty if order.filled_qty else order.quantity
                            
                            if filled_qty > 0:
                                equity = abs(filled_qty) * market_price
                                balance_usage[expert_name]['filled'] += equity
                        # Other statuses (terminal, error, etc.) are not counted
                    
                except Exception as e:
                    logger.error(f"Error calculating balance usage for transaction {transaction.id}: {e}")
                    continue
            
            # Remove experts with zero balance usage
            balance_usage = {k: v for k, v in balance_usage.items() if v['pending'] > 0 or v['filled'] > 0}
            
            # Sort by total balance usage (highest to lowest)
            balance_usage = dict(sorted(
                balance_usage.items(),
                key=lambda x: x[1]['pending'] + x[1]['filled'],
                reverse=True
            ))
        
        return balance_usage
    
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
            # Show loading spinner
            spinner = ui.spinner(size='lg')
            loading_label = ui.label('Loading balance usage data...').classes('text-sm text-gray-500 ml-2')
            
        # Run the data calculation in a thread to avoid blocking
        balance_data = await asyncio.to_thread(self.calculate_expert_balance_usage)
        
        # Clear the container
        self.container.clear()
        
        with self.container:
            if not balance_data:
                ui.label('No active balance usage found (all orders closed/canceled).').classes('text-sm text-gray-500')
                return
            
            # Prepare data for stacked bar chart
            expert_names = list(balance_data.keys())
            pending_values = [balance_data[name]['pending'] for name in expert_names]
            filled_values = [balance_data[name]['filled'] for name in expert_names]
            
            # Calculate totals for display
            total_pending = sum(pending_values)
            total_filled = sum(filled_values)
            total_usage = total_pending + total_filled
            
            # Calculate total values per expert for top label display
            total_per_expert = [round(pending_values[i] + filled_values[i], 2) for i in range(len(expert_names))]
            
            # Create echart options for stacked bar chart
            options = {
                'tooltip': {
                    'trigger': 'axis',
                    'axisPointer': {
                        'type': 'shadow'
                    }
                    # Note: Complex tooltip with total calculation would require JavaScript
                    # Using default tooltip which shows series values separately
                },
                'legend': {
                    'data': ['Filled Orders', 'Pending Orders'],
                    'bottom': 0
                },
                'grid': {
                    'left': '3%',
                    'right': '4%',
                    'bottom': '15%',
                    'top': '15%',  # Increased to make room for labels
                    'containLabel': True
                },
                'xAxis': {
                    'type': 'category',
                    'data': expert_names,
                    'axisLabel': {
                        'rotate': 45,
                        'interval': 0,
                        'fontSize': 10
                    }
                },
                'yAxis': {
                    'type': 'value',
                    'name': 'Balance ($)',
                    'axisLabel': {
                        'formatter': '${value}'
                    }
                },
                'series': [
                    {
                        'name': 'Filled Orders',
                        'type': 'bar',
                        'stack': 'total',
                        'data': [round(v, 2) for v in filled_values],
                        'itemStyle': {
                            'color': '#4CAF50'  # Green for filled
                        },
                        'label': {
                            'show': False
                        }
                    },
                    {
                        'name': 'Pending Orders',
                        'type': 'bar',
                        'stack': 'total',
                        'data': [round(v, 2) for v in pending_values],
                        'itemStyle': {
                            'color': '#FF9800'  # Orange for pending
                        },
                        'label': {
                            'show': False  # Disable default label, use markPoint instead
                        },
                        'markPoint': {
                            'symbol': 'rect',
                            'symbolSize': [60, 20],
                            'symbolOffset': [0, -10],
                            'itemStyle': {
                                'color': 'rgba(255, 255, 255, 0.9)',
                                'borderColor': '#666',
                                'borderWidth': 1
                            },
                            'label': {
                                'show': True,
                                'fontSize': 10,
                                'fontWeight': 'bold',
                                'color': '#333',
                                'formatter': '${c}'  # ECharts template string: {c} = value
                            },
                            'data': [{'coord': [i, total_per_expert[i]], 'value': total_per_expert[i]} for i in range(len(expert_names))]
                        }
                    }
                ]
            }
            
            # Create the chart
            self.chart = ui.echart(options).classes('w-full h-64')
            
            # Add summary statistics
            with ui.row().classes('w-full justify-between mt-4 text-sm'):
                ui.label(f'Total Experts: {len(balance_data)}').classes('text-gray-600')
                ui.label(f'Filled: ${total_filled:,.2f}').classes('text-green-600 font-bold')
                ui.label(f'Pending: ${total_pending:,.2f}').classes('text-orange-600 font-bold')
                ui.label(f'Total Usage: ${total_usage:,.2f}').classes('font-bold text-blue-600')
    
    def refresh(self):
        """Refresh the chart with updated data."""
        if self.chart:
            balance_data = self.calculate_expert_balance_usage()
            
            if balance_data:
                expert_names = list(balance_data.keys())
                pending_values = [balance_data[name]['pending'] for name in expert_names]
                filled_values = [balance_data[name]['filled'] for name in expert_names]
                
                # Calculate total values per expert for top label display
                total_per_expert = [round(pending_values[i] + filled_values[i], 2) for i in range(len(expert_names))]
                
                self.chart.options['xAxis']['data'] = expert_names
                self.chart.options['series'][0]['data'] = [round(v, 2) for v in filled_values]
                self.chart.options['series'][1]['data'] = [round(v, 2) for v in pending_values]
                self.chart.options['series'][1]['markPoint']['data'] = [{'coord': [i, total_per_expert[i]], 'value': total_per_expert[i]} for i in range(len(expert_names))]
                self.chart.update()
