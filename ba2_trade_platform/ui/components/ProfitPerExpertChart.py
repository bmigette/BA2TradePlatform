"""
Profit Per Expert Chart Component

A histogram chart showing profit/loss for each expert instance based on completed transactions.
"""

from nicegui import ui
from sqlmodel import select, func
from typing import Dict, List, Optional
from ...core.db import get_db
from ...core.models import Transaction, ExpertInstance
from ...core.types import TransactionStatus
from ...core.utils import calculate_transaction_pnl
from ...logger import logger
from ..account_filter_context import get_selected_account_id, get_expert_ids_for_account


class ProfitPerExpertChart:
    """Component that displays a histogram of profit per expert instance."""
    
    def __init__(self):
        self.chart = None
        self.render()
    
    def calculate_expert_profits(self) -> Dict[str, float]:
        """
        Calculate profit/loss for each expert from completed transactions.
        
        Returns:
            Dict mapping expert names to their total profit/loss
        """
        profits = {}
        
        # Get global account filter
        selected_account_id = get_selected_account_id()
        account_expert_ids = get_expert_ids_for_account(selected_account_id)
        
        with get_db() as session:
            # Build query for closed transactions
            query = (
                select(Transaction)
                .where(Transaction.status == TransactionStatus.CLOSED)
                .where(Transaction.expert_id.isnot(None))
                .where(Transaction.open_price.isnot(None))
                .where(Transaction.close_price.isnot(None))
            )
            
            # Apply account filter if selected
            if account_expert_ids is not None:
                if account_expert_ids:
                    query = query.where(Transaction.expert_id.in_(account_expert_ids))
                else:
                    # No experts for selected account - return empty
                    return {}
            
            closed_transactions = session.exec(query).all()
            
            logger.debug(f"Found {len(closed_transactions)} closed transactions with experts")
            
            # Calculate profit for each transaction and group by expert
            for transaction in closed_transactions:
                try:
                    # Get expert instance
                    expert = session.get(ExpertInstance, transaction.expert_id)
                    if not expert:
                        logger.warning(f"Expert instance {transaction.expert_id} not found for transaction {transaction.id}")
                        continue
                    
                    # Create unique expert name using alias or expert type with ID
                    expert_name = f"{expert.alias or expert.expert}-{expert.id}"
                    
                    # Calculate profit/loss (handles both long and short positions)
                    profit = calculate_transaction_pnl(transaction)
                    if profit is None:
                        continue
                    
                    # Add to expert's total
                    if expert_name not in profits:
                        profits[expert_name] = 0.0
                    profits[expert_name] += profit
                    
                    logger.debug(f"Transaction {transaction.id}: Expert {expert_name}, Profit: ${profit:.2f}")
                    
                except Exception as e:
                    logger.error(f"Error calculating profit for transaction {transaction.id}: {e}", exc_info=True)
                    continue
            
            # Sort by profit (highest to lowest)
            profits = dict(sorted(profits.items(), key=lambda x: x[1], reverse=True))
            
            logger.info(f"Calculated profits for {len(profits)} experts")
        
        return profits
    
    def render(self):
        """Render the profit per expert chart."""
        with ui.card().classes('p-4'):
            ui.label('📈 Profit Per Expert').classes('text-h6 mb-4')
            
            # Get profit data
            profit_data = self.calculate_expert_profits()
            
            if not profit_data:
                ui.label('No completed transactions found with expert attribution.').classes('text-sm text-gray-500')
                return
            
            # Prepare data for chart
            expert_names = list(profit_data.keys())
            profit_values = list(profit_data.values())
            
            # Create data array with individual colors for each bar
            # ECharts expects data as objects with value and itemStyle properties
            chart_data = []
            for profit in profit_values:
                # For positive bars: round top corners [topLeft, topRight, bottomRight, bottomLeft]
                # For negative bars: round bottom corners
                if profit >= 0:
                    border_radius = [4, 4, 0, 0]  # Round top
                else:
                    border_radius = [0, 0, 4, 4]  # Round bottom
                chart_data.append({
                    'value': round(profit, 2),
                    'itemStyle': {
                        'color': '#00d4aa' if profit >= 0 else '#ff6b6b',
                        'borderRadius': border_radius
                    }
                })
            
            # Create echart options
            options = {
                'backgroundColor': 'transparent',
                'tooltip': {
                    'trigger': 'axis',
                    'axisPointer': {
                        'type': 'shadow'
                    },
                    'formatter': '{b}<br/>Profit: ${c}',
                    'backgroundColor': 'rgba(37, 43, 59, 0.95)',
                    'borderColor': 'rgba(255, 255, 255, 0.1)',
                    'textStyle': {
                        'color': '#ffffff'
                    }
                },
                'grid': {
                    'left': '3%',
                    'right': '4%',
                    'bottom': '20%',
                    'top': '15%',
                    'containLabel': True
                },
                'xAxis': {
                    'type': 'category',
                    'data': expert_names,
                    'axisLabel': {
                        'rotate': 45,
                        'interval': 0,
                        'fontSize': 9,
                        'color': '#a0aec0',
                        'width': 80,
                        'overflow': 'truncate'
                    },
                    'axisLine': {
                        'lineStyle': {
                            'color': 'rgba(255, 255, 255, 0.1)'
                        }
                    }
                },
                'yAxis': {
                    'type': 'value',
                    'name': 'Profit ($)',
                    'nameTextStyle': {
                        'color': '#a0aec0'
                    },
                    'axisLabel': {
                        'formatter': '${value}',
                        'color': '#a0aec0'
                    },
                    'axisLine': {
                        'lineStyle': {
                            'color': 'rgba(255, 255, 255, 0.1)'
                        }
                    },
                    'splitLine': {
                        'lineStyle': {
                            'color': 'rgba(255, 255, 255, 0.05)'
                        }
                    }
                },
                'series': [{
                    'name': 'Profit',
                    'type': 'bar',
                    'data': chart_data,
                    'barMaxWidth': 40,
                    'label': {
                        'show': True,
                        'position': 'top',
                        'formatter': '${c}',
                        'fontSize': 9,
                        'color': '#a0aec0'
                    }
                }]
            }
            
            # Create the chart
            self.chart = ui.echart(options).classes('w-full h-64')
            
            # Add summary statistics
            total_profit = sum(profit_values)
            profitable_experts = sum(1 for p in profit_values if p > 0)
            
            with ui.row().classes('w-full justify-between mt-4 text-sm'):
                ui.label(f'Total Experts: {len(profit_data)}').classes('text-gray-600')
                ui.label(f'Profitable: {profitable_experts}').classes('text-green-600')
                ui.label(f'Total Profit: ${total_profit:.2f}').classes(
                    'font-bold text-green-600' if total_profit >= 0 else 'font-bold text-red-600'
                )
    
    def refresh(self):
        """Refresh the chart with updated data."""
        if self.chart:
            profit_data = self.calculate_expert_profits()
            
            if profit_data:
                expert_names = list(profit_data.keys())
                profit_values = list(profit_data.values())
                
                # Create data array with individual colors for each bar
                chart_data = []
                for profit in profit_values:
                    if profit >= 0:
                        border_radius = [4, 4, 0, 0]  # Round top
                    else:
                        border_radius = [0, 0, 4, 4]  # Round bottom
                    chart_data.append({
                        'value': round(profit, 2),
                        'itemStyle': {
                            'color': '#00d4aa' if profit >= 0 else '#ff6b6b',
                            'borderRadius': border_radius
                        }
                    })
                
                self.chart.options['xAxis']['data'] = expert_names
                self.chart.options['series'][0]['data'] = chart_data
                self.chart.update()
