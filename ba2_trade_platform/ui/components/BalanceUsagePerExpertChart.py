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
from ..account_filter_context import get_selected_account_id
from ...services import balance_usage
from .echart_theme import make_chart_options, MUTED_TEXT


class BalanceUsagePerExpertChart:
    """Component that displays a stacked bar chart of used vs available balance per expert."""

    def __init__(self):
        self.chart = None
        self.container = None
        self.render()

    def calculate_expert_balance_data(self) -> Dict[str, Dict[str, float]]:
        """Balance breakdown per expert (pending, filled, available, total). The computation lives in
        services.balance_usage (the management API calls the same function)."""
        return balance_usage.balance_usage_per_expert(get_selected_account_id())

    @staticmethod
    def summarize_capital(balance_data: Dict[str, Dict[str, float]]) -> Dict[str, Optional[float]]:
        """Account-level totals for the footer (see services.balance_usage.summarize_capital)."""
        return balance_usage.summarize_capital(balance_data)


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

            summary = self.summarize_capital(balance_data)

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

            pct = summary['allocated_pct']
            # Over 100% the sleeves promise more than the account holds. That is a valid
            # setup, not an error, so it is coloured as a caution rather than hidden.
            allocated_classes = ('font-bold text-orange-500' if pct is not None and pct > 100.0
                                 else 'font-bold text-blue-600')
            allocated_text = f"Allocated: ${summary['allocated']:,.2f}"
            if pct is not None:
                # "tradable", not a bare dollar figure: with margin on this denominator
                # is levered (balance x the factor), and a levered number must name what
                # it is or it reads as the account's cash.
                allocated_text += f" ({pct:,.0f}% of ${summary['capital']:,.2f} tradable)"

            with ui.row().classes('w-full justify-between mt-4 text-sm'):
                ui.label(f'Total Experts: {len(balance_data)}').classes('text-gray-600')
                ui.label(f"Filled: ${summary['filled']:,.2f}").classes('text-green-600 font-bold')
                ui.label(f"Pending: ${summary['pending']:,.2f}").classes('text-orange-600 font-bold')
                available_text = f"Available: ${summary['available']:,.2f}"
                if summary['broker_bp'] is None:
                    available_text += " (broker buying power unknown)"
                elif summary['available'] < summary['available_uncapped']:
                    available_text += f" (capped at broker buying power; ${summary['available_uncapped']:,.2f} before the cap)"
                ui.label(available_text).classes('text-gray-500')
                ui.label(allocated_text).classes(allocated_classes)

    def refresh(self):
        """Refresh the chart with updated data."""
        if self.container:
            self.container.clear()
            asyncio.create_task(self._load_chart_async())
