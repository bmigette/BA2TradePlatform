"""
Profit Per Expert Chart Component

A histogram chart showing profit/loss for each expert instance based on completed transactions.
"""

from nicegui import ui
from sqlmodel import select, func
from typing import Dict, List
from ...core.db import get_db
from ...core.models import Transaction, ExpertInstance
from ...core.types import TransactionStatus
from ...logger import logger


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
        session = get_db()
        profits = {}
        
        try:
            # Get all closed transactions with expert_id
            closed_transactions = session.exec(
                select(Transaction)
                .where(Transaction.status == TransactionStatus.CLOSED)
                .where(Transaction.expert_id.isnot(None))
                .where(Transaction.open_price.isnot(None))
                .where(Transaction.close_price.isnot(None))
            ).all()
            
            logger.debug(f"Found {len(closed_transactions)} closed transactions with experts")
            
            # Calculate profit for each transaction and group by expert
            for transaction in closed_transactions:
                try:
                    # Get expert instance
                    expert = session.get(ExpertInstance, transaction.expert_id)
                    if not expert:
                        logger.warning(f"Expert instance {transaction.expert_id} not found for transaction {transaction.id}")
                        continue
                    
                    # Create unique expert name (Expert Name - Account Name)
                    expert_name = f"{expert.expert} (ID: {expert.id})"
                    
                    # Calculate profit/loss
                    # Profit = (close_price - open_price) * quantity
                    # Note: For short positions, this would be negative if price went up
                    profit = (transaction.close_price - transaction.open_price) * transaction.quantity
                    
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
            
        except Exception as e:
            logger.error(f"Error calculating expert profits: {e}", exc_info=True)
        finally:
            session.close()
        
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
                chart_data.append({
                    'value': profit,
                    'itemStyle': {
                        'color': 'green' if profit >= 0 else 'red'
                    }
                })
            
            # Create echart options
            options = {
                'tooltip': {
                    'trigger': 'axis',
                    'axisPointer': {
                        'type': 'shadow'
                    },
                    'formatter': '{b}<br/>Profit: ${c}'
                },
                'grid': {
                    'left': '3%',
                    'right': '4%',
                    'bottom': '15%',
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
                    'name': 'Profit ($)',
                    'axisLabel': {
                        'formatter': '${value}'
                    }
                },
                'series': [{
                    'name': 'Profit',
                    'type': 'bar',
                    'data': chart_data,
                    'label': {
                        'show': True,
                        'position': 'top',
                        'formatter': '${c}',
                        'fontSize': 10
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
                    chart_data.append({
                        'value': profit,
                        'itemStyle': {
                            'color': 'green' if profit >= 0 else 'red'
                        }
                    })
                
                self.chart.options['xAxis']['data'] = expert_names
                self.chart.options['series'][0]['data'] = chart_data
                self.chart.update()
