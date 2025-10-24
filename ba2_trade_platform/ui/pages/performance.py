"""
Trade Performance Analytics Page

This page provides comprehensive analytics and visualizations for trading performance,
including metrics per expert, time-based analysis, and statistical measures.
"""

from nicegui import ui
from ba2_trade_platform.core.db import get_db
from ba2_trade_platform.core.models import Transaction, TradingOrder, ExpertInstance, AccountDefinition
from ba2_trade_platform.core.types import TransactionStatus
from ba2_trade_platform.ui.components.performance_charts import (
    MetricCard, PerformanceBarChart, TimeSeriesChart, PieChartComponent,
    PerformanceTable, MultiMetricDashboard, ComboChart,
    calculate_sharpe_ratio, calculate_win_loss_ratio, calculate_max_drawdown, calculate_profit_factor
)
from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional, Tuple
import pandas as pd
import numpy as np
from collections import defaultdict


class PerformanceTab:
    """Trade performance analytics and visualization tab."""
    
    def __init__(self, account_id: int):
        """
        Initialize performance tab for an account.
        
        Args:
            account_id: Account ID to analyze
        """
        self.account_id = account_id
        self.date_range_days = 30  # Default to last 30 days
        self.selected_experts = []  # Empty means all experts
        self.data_loaded = False
        
    def _get_closed_transactions(self) -> List[Transaction]:
        """Get all closed transactions for the account within date range."""
        session = get_db()
        try:
            cutoff_date = datetime.now() - timedelta(days=self.date_range_days)
            
            query = session.query(Transaction).filter(
                Transaction.status == TransactionStatus.CLOSED,
                Transaction.close_date.isnot(None),
                Transaction.close_date >= cutoff_date
            )
            
            # Filter by selected experts if any
            if self.selected_experts:
                query = query.filter(Transaction.expert_id.in_(self.selected_experts))
            
            return query.all()
        finally:
            session.close()
    
    def _calculate_transaction_metrics(self, transactions: List[Transaction]) -> Dict[str, Any]:
        """Calculate comprehensive metrics from transactions."""
        if not transactions:
            return {}
        
        # Group by expert instance ID
        expert_transactions = defaultdict(list)
        for txn in transactions:
            expert_transactions[txn.expert_id].append(txn)
        
        # Calculate metrics per expert instance
        expert_metrics = {}
        for expert_id, txns in expert_transactions.items():
            # Get expert instance display name (use alias if available, otherwise class-id format)
            session = get_db()
            try:
                expert = session.get(ExpertInstance, expert_id)
                if expert:
                    # Use alias if available, otherwise "ClassName-ID"
                    if expert.alias:
                        expert_name = expert.alias
                    else:
                        expert_name = f"{expert.expert}-{expert.id}"
                else:
                    expert_name = f"Expert-{expert_id}"
            finally:
                session.close()
            
            # Calculate transaction duration
            durations = []
            for txn in txns:
                if txn.open_date and txn.close_date:
                    duration = (txn.close_date - txn.open_date).total_seconds() / 3600  # hours
                    durations.append(duration)
            
            # Calculate P&L
            # P&L = (close_price - open_price) * quantity (for longs)
            pnls = []
            for txn in txns:
                if txn.open_price and txn.close_price and txn.quantity:
                    pnl = (txn.close_price - txn.open_price) * txn.quantity
                    pnls.append(pnl)
            
            winning_pnls = [p for p in pnls if p > 0]
            losing_pnls = [p for p in pnls if p < 0]
            
            # Win/loss ratio
            win_rate, wins, losses = calculate_win_loss_ratio(
                [{'pnl': pnl} for pnl in pnls]
            )
            
            # Calculate returns for Sharpe ratio
            returns = []
            for txn in txns:
                if txn.open_price and txn.close_price and txn.quantity:
                    position_value = txn.open_price * txn.quantity
                    if position_value != 0:
                        pnl = (txn.close_price - txn.open_price) * txn.quantity
                        returns.append(pnl / position_value)
            
            expert_metrics[expert_name] = {
                'total_transactions': len(txns),
                'avg_duration_hours': np.mean(durations) if durations else 0,
                'total_pnl': sum(pnls) if pnls else 0,
                'avg_pnl': np.mean(pnls) if pnls else 0,
                'win_rate': win_rate,
                'wins': wins,
                'losses': losses,
                'profit_factor': calculate_profit_factor(winning_pnls, losing_pnls),
                'largest_win': max(pnls) if pnls else 0,
                'largest_loss': min(pnls) if pnls else 0,
                'sharpe_ratio': calculate_sharpe_ratio(returns) if len(returns) >= 30 else None,
                'transactions': txns,
                'returns': returns
            }
        
        return expert_metrics
    
    def _calculate_monthly_metrics(self, transactions: List[Transaction]) -> Dict[str, Dict[str, float]]:
        """Calculate monthly metrics per expert instance."""
        monthly_data = defaultdict(lambda: defaultdict(lambda: {'pnl': 0, 'count': 0}))
        
        for txn in transactions:
            if txn.close_date and txn.open_price and txn.close_price and txn.quantity:
                month_key = txn.close_date.strftime('%Y-%m')
                pnl = (txn.close_price - txn.open_price) * txn.quantity
                
                session = get_db()
                try:
                    expert = session.get(ExpertInstance, txn.expert_id)
                    if expert:
                        # Use alias if available, otherwise "ClassName-ID"
                        if expert.alias:
                            expert_name = expert.alias
                        else:
                            expert_name = f"{expert.expert}-{expert.id}"
                    else:
                        expert_name = f"Expert-{txn.expert_id}"
                finally:
                    session.close()
                
                monthly_data[month_key][expert_name]['pnl'] += pnl
                monthly_data[month_key][expert_name]['count'] += 1
        
        return monthly_data
    
    def _render_summary_metrics(self, expert_metrics: Dict[str, Any]):
        """Render top-level summary metric cards."""
        if not expert_metrics:
            ui.label("No transaction data available for the selected period").classes('text-gray-500 text-center p-4')
            return
        
        # Calculate overall metrics
        total_transactions = sum(m['total_transactions'] for m in expert_metrics.values())
        total_pnl = sum(m['total_pnl'] for m in expert_metrics.values())
        all_wins = sum(m['wins'] for m in expert_metrics.values())
        all_losses = sum(m['losses'] for m in expert_metrics.values())
        overall_win_rate = (all_wins / (all_wins + all_losses) * 100) if (all_wins + all_losses) > 0 else 0
        
        # All returns for Sharpe
        all_returns = []
        for metrics in expert_metrics.values():
            all_returns.extend(metrics['returns'])
        
        overall_sharpe = calculate_sharpe_ratio(all_returns) if len(all_returns) >= 30 else None
        
        # Create metric cards
        metrics_list = [
            {
                'title': 'Total Transactions',
                'value': str(total_transactions),
                'subtitle': f'Last {self.date_range_days} days',
                'color': 'primary'
            },
            {
                'title': 'Total P&L',
                'value': f'${total_pnl:,.2f}',
                'subtitle': 'Net profit/loss',
                'color': 'positive' if total_pnl >= 0 else 'negative'
            },
            {
                'title': 'Win Rate',
                'value': f'{overall_win_rate:.1f}%',
                'subtitle': f'{all_wins}W / {all_losses}L',
                'color': 'positive' if overall_win_rate >= 50 else 'neutral'
            },
            {
                'title': 'Sharpe Ratio',
                'value': f'{overall_sharpe:.2f}' if overall_sharpe is not None else 'N/A',
                'subtitle': 'Risk-adjusted return' if overall_sharpe is not None else 'Need 30+ transactions',
                'color': 'primary' if overall_sharpe and overall_sharpe > 1 else 'neutral'
            }
        ]
        
        dashboard = MultiMetricDashboard(metrics_list, columns=4)
        dashboard.render()
    
    def _render_expert_comparison_charts(self, expert_metrics: Dict[str, Any]):
        """Render charts comparing expert instances."""
        if not expert_metrics:
            return
        
        with ui.grid(columns=2).classes('w-full gap-4 mt-6'):
            # Chart 1: Average transaction duration
            duration_data = {name: metrics['avg_duration_hours'] 
                           for name, metrics in expert_metrics.items()}
            chart1 = PerformanceBarChart(
                title="Average Transaction Duration by Expert Instance",
                data=duration_data,
                xlabel="Expert Instance",
                ylabel="Hours",
                height=350
            )
            chart1.render()
            
            # Chart 2: Total P&L by expert instance
            pnl_data = {name: metrics['total_pnl'] 
                       for name, metrics in expert_metrics.items()}
            chart2 = PerformanceBarChart(
                title="Total P&L by Expert Instance",
                data=pnl_data,
                xlabel="Expert Instance",
                ylabel="Profit/Loss ($)",
                height=350
            )
            chart2.render()
            
            # Chart 3: Win/Loss distribution
            win_loss_data = {}
            for name, metrics in expert_metrics.items():
                win_loss_data[f'{name} - Wins'] = metrics['wins']
                win_loss_data[f'{name} - Losses'] = metrics['losses']
            
            chart3 = PieChartComponent(
                title="Win/Loss Distribution by Expert Instance",
                data=win_loss_data,
                donut=True,
                height=350
            )
            chart3.render()
            
            # Chart 4: Average P&L per transaction
            avg_pnl_data = {name: metrics['avg_pnl'] 
                          for name, metrics in expert_metrics.items()}
            chart4 = PerformanceBarChart(
                title="Average P&L per Transaction by Expert Instance",
                data=avg_pnl_data,
                xlabel="Expert Instance",
                ylabel="Average P&L ($)",
                height=350
            )
            chart4.render()
    
    def _render_monthly_trends(self, transactions: List[TradingOrder]):
        """Render monthly trend charts by expert instance."""
        if not transactions:
            return
        
        monthly_data = self._calculate_monthly_metrics(transactions)
        
        if not monthly_data:
            return
        
        ui.label("Monthly Performance Trends").classes('text-xl font-bold mt-8 mb-4')
        
        # Prepare time series data
        months = sorted(monthly_data.keys())
        expert_names = set()
        for month_experts in monthly_data.values():
            expert_names.update(month_experts.keys())
        
        # Profit series
        profit_series = {}
        transaction_series = {}
        
        for expert_name in expert_names:
            profit_points = []
            txn_points = []
            
            for month in months:
                month_date = datetime.strptime(month, '%Y-%m')
                expert_data = monthly_data[month].get(expert_name, {'pnl': 0, 'count': 0})
                profit_points.append((month_date, expert_data['pnl']))
                txn_points.append((month_date, expert_data['count']))
            
            profit_series[expert_name] = profit_points
            transaction_series[expert_name] = txn_points
        
        with ui.grid(columns=2).classes('w-full gap-4'):
            # Monthly profit chart
            chart1 = TimeSeriesChart(
                title="Monthly P&L by Expert Instance",
                series_data=profit_series,
                ylabel="P&L ($)",
                height=400
            )
            chart1.render()
            
            # Monthly transaction count chart
            chart2 = TimeSeriesChart(
                title="Monthly Transaction Count by Expert Instance",
                series_data=transaction_series,
                ylabel="Transactions",
                height=400
            )
            chart2.render()
    
    def _render_detailed_table(self, expert_metrics: Dict[str, Any]):
        """Render detailed performance table."""
        if not expert_metrics:
            return
        
        ui.label("Detailed Performance Metrics").classes('text-xl font-bold mt-8 mb-4')
        
        # Prepare table data
        columns = [
            'Expert Instance', 'Transactions', 'Avg Duration (hrs)', 'Total P&L', 
            'Avg P&L', 'Win Rate', 'Profit Factor', 'Largest Win', 
            'Largest Loss', 'Sharpe Ratio'
        ]
        
        rows = []
        for expert_name, metrics in expert_metrics.items():
            rows.append({
                'Expert Instance': expert_name,
                'Transactions': metrics['total_transactions'],
                'Avg Duration (hrs)': f"{metrics['avg_duration_hours']:.1f}",
                'Total P&L': f"${metrics['total_pnl']:,.2f}",
                'Avg P&L': f"${metrics['avg_pnl']:,.2f}",
                'Win Rate': f"{metrics['win_rate']:.1f}%",
                'Profit Factor': f"{metrics['profit_factor']:.2f}" if metrics['profit_factor'] is not None else 'N/A',
                'Largest Win': f"${metrics['largest_win']:,.2f}",
                'Largest Loss': f"${metrics['largest_loss']:,.2f}",
                'Sharpe Ratio': f"{metrics['sharpe_ratio']:.2f}" if metrics['sharpe_ratio'] is not None else 'N/A'
            })
        
        table = PerformanceTable(
            title="",
            columns=columns,
            rows=rows
        )
        table.render()
    
    def _render_filters(self):
        """Render filter controls."""
        with ui.card().classes('w-full mb-4'):
            ui.label("Filters").classes('text-lg font-bold mb-2')
            
            with ui.row().classes('w-full gap-4'):
                # Date range selector
                ui.label("Time Period:").classes('self-center')
                
                def update_date_range(days: int):
                    self.date_range_days = days
                    self._refresh_data()
                
                with ui.button_group():
                    ui.button('7 Days', on_click=lambda: update_date_range(7))
                    ui.button('30 Days', on_click=lambda: update_date_range(30))
                    ui.button('90 Days', on_click=lambda: update_date_range(90))
                    ui.button('1 Year', on_click=lambda: update_date_range(365))
                    ui.button('All Time', on_click=lambda: update_date_range(365*10))
                
                # Expert filter
                session = get_db()
                try:
                    experts = session.query(ExpertInstance).filter(
                        ExpertInstance.account_id == self.account_id
                    ).all()
                    
                    if experts:
                        ui.label("Expert Instances:").classes('self-center ml-8')
                        # Use alias if available, otherwise use "ClassName-ID" format
                        expert_options = {}
                        for expert in experts:
                            if expert.alias:
                                display_name = expert.alias
                            else:
                                display_name = f"{expert.expert}-{expert.id}"
                            expert_options[expert.id] = display_name
                        
                        def update_expert_filter(selected):
                            self.selected_experts = selected
                            self._refresh_data()
                        
                        ui.select(
                            options=expert_options,
                            multiple=True,
                            label="Filter by expert instance (empty = all)"
                        ).bind_value_to(self, 'selected_experts').on_value_change(
                            lambda e: update_expert_filter(e.value)
                        )
                finally:
                    session.close()
    
    def _refresh_data(self):
        """Refresh all data and re-render."""
        self.content_container.clear()
        with self.content_container:
            self._load_and_render_content()
    
    def _load_and_render_content(self):
        """Load transaction data and render all charts."""
        # Get transactions
        transactions = self._get_closed_transactions()
        
        if not transactions:
            ui.label("No closed transactions found for the selected period").classes(
                'text-gray-500 text-center p-8 text-lg'
            )
            return
        
        # Calculate metrics
        expert_metrics = self._calculate_transaction_metrics(transactions)
        
        # Render components
        self._render_summary_metrics(expert_metrics)
        self._render_expert_comparison_charts(expert_metrics)
        self._render_monthly_trends(transactions)
        self._render_detailed_table(expert_metrics)
        
        self.data_loaded = True
    
    def render(self):
        """Render the complete performance tab."""
        with ui.column().classes('w-full gap-4'):
            ui.label("Trade Performance Analytics").classes('text-2xl font-bold mb-2')
            
            # Filters
            self._render_filters()
            
            # Content container for refresh
            self.content_container = ui.column().classes('w-full gap-4')
            
            with self.content_container:
                # Initial load
                self._load_and_render_content()
