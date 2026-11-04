"""
Profit Per Expert Chart Component

A histogram chart showing profit/loss for each expert instance based on completed transactions.
"""

from nicegui import ui
from sqlmodel import select, func
from typing import Dict, List, Optional
import asyncio
from ...core.db import get_db
from ...core.models import Transaction, ExpertInstance
from ...core.types import TransactionStatus
from ...core.utils import calculate_transaction_pnl
from ...logger import logger
from ..account_filter_context import get_selected_account_id
from ...services import stats as stats_service
from .echart_theme import make_chart_options, MUTED_TEXT


class ProfitPerExpertChart:
    """Component that displays a histogram of profit per expert instance."""

    def __init__(self):
        self.chart = None
        self.container = None
        self.summary_container = None
        self.render()
    
    def calculate_expert_profits(self) -> Dict[str, float]:
        """
        Calculate profit/loss for each expert from completed transactions.
        
        Returns:
            Dict mapping expert names to their total profit/loss
        """
        # The computation lives in services.stats (the management API calls the same function).
        return stats_service.profit_per_expert(get_selected_account_id())

    def render(self):
        """Render the profit per expert chart."""
        with ui.card().classes('p-4') as card:
            ui.label('📈 Profit Per Expert').classes('text-h6 mb-4')

            # Create container for the chart content
            self.container = ui.column().classes('w-full')

            # Load data asynchronously
            asyncio.create_task(self._load_chart_async())

    async def _load_chart_async(self):
        """Asynchronously load and render the chart data."""
        with self.container:
            # Show loading spinner
            spinner = ui.spinner(size='lg')
            loading_label = ui.label('Loading profit data...').classes('text-sm text-gray-500 ml-2')

        # Run the data calculation in a thread to avoid blocking
        profit_data = await asyncio.to_thread(self.calculate_expert_profits)

        # Clear the container
        self.container.clear()

        with self.container:
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
            options = make_chart_options(
                tooltip={
                    'trigger': 'axis',
                    'axisPointer': {
                        'type': 'shadow'
                    },
                    'formatter': '{b}<br/>Profit: ${c}',
                },
                grid={
                    'bottom': '20%',
                    'top': '15%',
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
                    'name': 'Profit ($)',
                    'nameTextStyle': {
                        'color': MUTED_TEXT
                    },
                    'axisLabel': {
                        'formatter': '${value}',
                    },
                },
                series=[{
                    'name': 'Profit',
                    'type': 'bar',
                    'data': chart_data,
                    'barMaxWidth': 40,
                    'label': {
                        'show': True,
                        'position': 'top',
                        'formatter': '${c}',
                        'fontSize': 9,
                        'color': MUTED_TEXT
                    }
                }]
            )

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
        if self.container:
            self.container.clear()
            asyncio.create_task(self._load_chart_async())
