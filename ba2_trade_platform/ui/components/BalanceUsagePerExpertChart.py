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
        Calculate balance usage for each expert from transactions.
        
        Returns:
            Dict mapping expert names to their balance usage breakdown:
            {
                'expert_name': {
                    'pending': float (total value of pending orders),
                    'filled': float (total value of filled orders)
                }
            }
        """
        session = get_db()
        balance_usage = {}
        
        try:
            # Get all transactions with expert_id (both OPENED and WAITING status)
            from ...core.types import TransactionStatus
            
            # First, check all transactions to debug
            # all_transactions = session.exec(select(Transaction)).all()
            # logger.info(f"Total transactions in database: {len(all_transactions)}")
            # 
            # # Count by status
            # status_counts = {}
            # expert_counts = {'with_expert': 0, 'without_expert': 0}
            # for t in all_transactions:
            #     status_str = str(t.status)
            #     status_counts[status_str] = status_counts.get(status_str, 0) + 1
            #     if t.expert_id:
            #         expert_counts['with_expert'] += 1
            #     else:
            #         expert_counts['without_expert'] += 1
            # 
            # logger.info(f"Transaction status breakdown: {status_counts}")
            # logger.info(f"Expert attribution: {expert_counts}")
            
            # Now get the filtered transactions
            transactions = session.exec(
                select(Transaction)
                .where(Transaction.expert_id.isnot(None))
                .where(Transaction.status.in_([TransactionStatus.OPENED, TransactionStatus.WAITING]))
            ).all()
            
            # logger.info(f"Found {len(transactions)} active transactions with expert attribution (OPENED or WAITING status)")
            
            # if len(transactions) == 0:
            #     # Try without status filter to see if there are ANY transactions with experts
            #     transactions_any_status = session.exec(
            #         select(Transaction)
            #         .where(Transaction.expert_id.isnot(None))
            #     ).all()
            #     logger.info(f"Transactions with expert_id (any status): {len(transactions_any_status)}")
            #     if transactions_any_status:
            #         for t in transactions_any_status:
            #             logger.info(f"  - Transaction {t.id}: {t.symbol}, status={t.status}, expert_id={t.expert_id}")
            
            # Calculate balance usage for each transaction and group by expert
            for transaction in transactions:
                try:
                    # Get expert instance
                    expert = session.get(ExpertInstance, transaction.expert_id)
                    if not expert:
                        logger.warning(f"Expert instance {transaction.expert_id} not found for transaction {transaction.id}")
                        continue
                    
                    # Create unique expert name
                    expert_name = f"{expert.expert} (ID: {expert.id})"
                    
                    # Initialize expert entry if not exists
                    if expert_name not in balance_usage:
                        balance_usage[expert_name] = {
                            'pending': 0.0,
                            'filled': 0.0
                        }
                    
                    # Get account interface for market price (needed for pending equity)
                    account_interface = None
                    try:
                        from ...modules.accounts import get_account_class
                        from ...core.models import AccountDefinition
                        
                        # Get account from first order of this transaction
                        first_order = session.exec(
                            select(TradingOrder)
                            .where(TradingOrder.transaction_id == transaction.id)
                            .limit(1)
                        ).first()
                        
                        if first_order and first_order.account_id:
                            acc_def = session.get(AccountDefinition, first_order.account_id)
                            if acc_def:
                                account_class = get_account_class(acc_def.provider)
                                if account_class:
                                    account_interface = account_class(acc_def.id)
                    except Exception as e:
                        logger.debug(f"Could not get account interface for transaction {transaction.id}: {e}")
                    
                    # Calculate filled equity using the new method
                    filled_equity = transaction.get_current_open_equity(account_interface)
                    balance_usage[expert_name]['filled'] += filled_equity
                    
                    # Calculate pending equity using the new method
                    pending_equity = transaction.get_pending_open_equity(account_interface)
                    balance_usage[expert_name]['pending'] += pending_equity
                    
                    # logger.info(f"Transaction {transaction.id} ({transaction.symbol}): Expert {expert_name}, Filled: ${filled_equity:.2f}, Pending: ${pending_equity:.2f}")
                    
                    # Debug: Check if there are any orders for this transaction
                    # order_count = session.exec(
                    #     select(TradingOrder)
                    #     .where(TradingOrder.transaction_id == transaction.id)
                    # ).all()
                    # logger.debug(f"  - Transaction {transaction.id} has {len(order_count)} orders")
                    
                except Exception as e:
                    logger.error(f"Error calculating balance usage for transaction {transaction.id}: {e}")
                    continue
            
            # Remove experts with zero balance usage
            # before_filter_count = len(balance_usage)
            balance_usage = {k: v for k, v in balance_usage.items() if v['pending'] > 0 or v['filled'] > 0}
            # filtered_count = before_filter_count - len(balance_usage)
            # 
            # if filtered_count > 0:
            #     logger.info(f"Filtered out {filtered_count} experts with zero balance usage")
            
            # Sort by total balance usage (highest to lowest)
            balance_usage = dict(sorted(
                balance_usage.items(),
                key=lambda x: x[1]['pending'] + x[1]['filled'],
                reverse=True
            ))
            
            # logger.info(f"Final result: {len(balance_usage)} experts with active balance usage")
            
        except Exception as e:
            logger.error(f"Error calculating expert balance usage: {e}", exc_info=True)
        finally:
            session.close()
        
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
                    },
                    'formatter': 'function(params) { var total = params[0].value + params[1].value; return params[0].name + "<br/>" + params[0].marker + " " + params[0].seriesName + ": $" + params[0].value.toFixed(2) + "<br/>" + params[1].marker + " " + params[1].seriesName + ": $" + params[1].value.toFixed(2) + "<br/>" + "<b>Total: $" + total.toFixed(2) + "</b>"; }'
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
                                'formatter': 'function(params) { return "$" + params.value.toFixed(2); }'
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
