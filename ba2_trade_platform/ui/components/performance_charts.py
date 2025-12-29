"""
Reusable Performance Chart Components

This module provides reusable chart components for displaying trading performance metrics.
All components use Plotly for consistent, interactive visualizations.
"""

from nicegui import ui
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from datetime import datetime
from typing import List, Dict, Any, Optional, Tuple
import pandas as pd
import numpy as np


class MetricCard:
    """Display a single metric with optional trend indicator and comparison."""
    
    def __init__(self, title: str, value: str, subtitle: str = "", 
                 trend: Optional[float] = None, color: str = "primary"):
        """
        Args:
            title: Metric name
            value: Main value to display
            subtitle: Additional context
            trend: Percentage change (positive/negative)
            color: Card color (primary, positive, negative, neutral)
        """
        self.title = title
        self.value = value
        self.subtitle = subtitle
        self.trend = trend
        self.color = color
    
    def render(self):
        """Render the metric card."""
        # Dark theme color mapping using inline styles
        styles_map = {
            "primary": ("color: #74c0fc;", "background: rgba(116, 192, 252, 0.1); border: 1px solid rgba(116, 192, 252, 0.2);"),
            "positive": ("color: #00d4aa;", "background: rgba(0, 212, 170, 0.1); border: 1px solid rgba(0, 212, 170, 0.2);"),
            "negative": ("color: #ff6b6b;", "background: rgba(255, 107, 107, 0.1); border: 1px solid rgba(255, 107, 107, 0.2);"),
            "neutral": ("color: #a0aec0;", "background: rgba(160, 174, 192, 0.1); border: 1px solid rgba(160, 174, 192, 0.2);")
        }
        
        text_style, bg_style = styles_map.get(self.color, styles_map["primary"])
        
        with ui.card().classes('w-full').style(f'{bg_style} border-radius: 8px;'):
            ui.label(self.title).classes('text-sm font-medium').style(text_style)
            ui.label(self.value).classes('text-3xl font-bold mt-1').style('color: #e2e8f0;')
            
            if self.trend is not None:
                trend_color = '#00d4aa' if self.trend >= 0 else '#ff6b6b'
                trend_icon = '↑' if self.trend >= 0 else '↓'
                ui.label(f'{trend_icon} {abs(self.trend):.1f}%').classes('text-sm mt-1').style(f'color: {trend_color};')
            
            if self.subtitle:
                ui.label(self.subtitle).classes('text-xs mt-1').style('color: #a0aec0;')


class PerformanceBarChart:
    """Bar chart component for comparing metrics across experts."""
    
    def __init__(self, title: str, data: Dict[str, float], 
                 xlabel: str = "", ylabel: str = "", height: int = 400):
        """
        Args:
            title: Chart title
            data: Dictionary of {label: value}
            xlabel: X-axis label
            ylabel: Y-axis label
            height: Chart height in pixels
        """
        self.title = title
        self.data = data
        self.xlabel = xlabel
        self.ylabel = ylabel
        self.height = height
    
    def render(self):
        """Render the bar chart."""
        if not self.data:
            ui.label("No data available").classes('text-gray-400 text-center p-4')
            return
        
        labels = list(self.data.keys())
        values = list(self.data.values())
        
        # Color bars based on value (teal for positive, red for negative)
        colors = ['#00d4aa' if v >= 0 else '#ff6b6b' for v in values]
        
        fig = go.Figure(data=[
            go.Bar(
                x=labels,
                y=values,
                marker=dict(
                    color=colors,
                    line=dict(width=0),
                    cornerradius=6  # Rounded corners for bars
                ),
                text=[f'{v:.2f}' for v in values],
                textposition='outside',
                textfont=dict(color='#e2e8f0', size=11)
            )
        ])
        
        fig.update_layout(
            title=dict(text=self.title, font=dict(color='#e2e8f0', size=14)),
            xaxis_title=dict(text=self.xlabel, font=dict(color='#a0aec0')),
            yaxis_title=dict(text=self.ylabel, font=dict(color='#a0aec0')),
            height=self.height,
            showlegend=False,
            hovermode='x unified',
            plot_bgcolor='rgba(0,0,0,0)',
            paper_bgcolor='rgba(0,0,0,0)',
            xaxis=dict(
                tickfont=dict(color='#a0aec0', size=10),
                gridcolor='rgba(160,174,192,0.1)',
                tickangle=-45
            ),
            yaxis=dict(
                tickfont=dict(color='#a0aec0', size=10),
                gridcolor='rgba(160,174,192,0.2)',
                zerolinecolor='rgba(160,174,192,0.3)'
            ),
            margin=dict(l=60, r=20, t=50, b=100),
            hoverlabel=dict(
                bgcolor='#1a1f2e',
                font_size=12,
                font_color='#e2e8f0',
                bordercolor='#3d4a5c'
            )
        )
        
        ui.plotly(fig).classes('w-full')


class TimeSeriesChart:
    """Line chart component for time-series data."""
    
    def __init__(self, title: str, series_data: Dict[str, List[Tuple[datetime, float]]],
                 xlabel: str = "Date", ylabel: str = "", height: int = 400,
                 date_format: str = "auto"):
        """
        Args:
            title: Chart title
            series_data: Dict of {series_name: [(datetime, value), ...]}
            xlabel: X-axis label
            ylabel: Y-axis label
            height: Chart height in pixels
            date_format: Date format - "auto", "monthly", "daily", or custom strftime format
        """
        self.title = title
        self.series_data = series_data
        self.xlabel = xlabel
        self.ylabel = ylabel
        self.height = height
        self.date_format = date_format
    
    # Modern color palette for dark theme
    COLORS = [
        '#00d4aa', '#ff6b6b', '#ffa94d', '#74c0fc', '#b197fc',
        '#63e6be', '#ffd43b', '#ff8787', '#69db7c', '#a9e34b',
        '#4dabf7', '#da77f2', '#f783ac', '#38d9a9', '#fab005'
    ]
    
    def render(self):
        """Render the time series chart."""
        if not self.series_data:
            ui.label("No data available").classes('text-gray-400 text-center p-4')
            return
        
        fig = go.Figure()
        
        for idx, (series_name, data_points) in enumerate(self.series_data.items()):
            if not data_points:
                continue
            
            dates = [d[0] for d in data_points]
            values = [d[1] for d in data_points]
            color = self.COLORS[idx % len(self.COLORS)]
            
            fig.add_trace(go.Scatter(
                x=dates,
                y=values,
                mode='lines+markers',
                name=series_name,
                line=dict(color=color, width=2),
                marker=dict(size=6, color=color),
                hovertemplate=f'{series_name}<br>Date: %{{x}}<br>Value: %{{y:.2f}}<extra></extra>'
            ))
        
        fig.update_layout(
            title=dict(text=self.title, font=dict(color='#e2e8f0', size=14)),
            xaxis_title=dict(text=self.xlabel, font=dict(color='#a0aec0')),
            yaxis_title=dict(text=self.ylabel, font=dict(color='#a0aec0')),
            height=self.height,
            hovermode='x unified',
            plot_bgcolor='rgba(0,0,0,0)',
            paper_bgcolor='rgba(0,0,0,0)',
            xaxis=dict(
                tickfont=dict(color='#a0aec0', size=10),
                gridcolor='rgba(160,174,192,0.2)',
                # Configure tick format based on date_format parameter
                tickformat='%b %Y' if self.date_format == 'monthly' else None,
                dtick='M1' if self.date_format == 'monthly' else None
            ),
            yaxis=dict(
                tickfont=dict(color='#a0aec0', size=10),
                gridcolor='rgba(160,174,192,0.2)',
                zerolinecolor='rgba(160,174,192,0.3)'
            ),
            legend=dict(
                orientation="h",
                yanchor="bottom",
                y=1.02,
                xanchor="right",
                x=1,
                font=dict(color='#a0aec0', size=10),
                bgcolor='rgba(0,0,0,0)'
            ),
            margin=dict(l=60, r=20, t=80, b=60),
            hoverlabel=dict(
                bgcolor='#1a1f2e',
                font_size=12,
                font_color='#e2e8f0',
                bordercolor='#3d4a5c'
            )
        )
        
        ui.plotly(fig).classes('w-full')


class PieChartComponent:
    """Pie/donut chart component for showing distributions."""
    
    def __init__(self, title: str, data: Dict[str, float], 
                 donut: bool = True, height: int = 400):
        """
        Args:
            title: Chart title
            data: Dictionary of {label: value}
            donut: If True, create donut chart; if False, create pie chart
            height: Chart height in pixels
        """
        self.title = title
        self.data = data
        self.donut = donut
        self.height = height
    
    # Modern color palette for dark theme
    COLORS = [
        '#00d4aa', '#ff6b6b', '#ffa94d', '#74c0fc', '#b197fc',
        '#63e6be', '#ffd43b', '#ff8787', '#69db7c', '#a9e34b',
        '#4dabf7', '#da77f2', '#f783ac', '#38d9a9', '#fab005'
    ]
    
    def render(self):
        """Render the pie/donut chart."""
        if not self.data:
            ui.label("No data available").classes('text-gray-400 text-center p-4')
            return
        
        labels = list(self.data.keys())
        values = list(self.data.values())
        
        # Color wins green and losses red
        colors = []
        for label in labels:
            if 'Win' in label:
                colors.append('#00d4aa')
            elif 'Loss' in label:
                colors.append('#ff6b6b')
            else:
                colors.append(self.COLORS[len(colors) % len(self.COLORS)])
        
        fig = go.Figure(data=[go.Pie(
            labels=labels,
            values=values,
            hole=0.5 if self.donut else 0,
            textinfo='percent',
            textfont=dict(color='#e2e8f0', size=10),
            marker=dict(colors=colors, line=dict(color='#1a1f2e', width=2)),
            hovertemplate='%{label}<br>Value: %{value:.0f}<br>Percent: %{percent}<extra></extra>'
        )])
        
        fig.update_layout(
            title=dict(text=self.title, font=dict(color='#e2e8f0', size=14)),
            height=self.height,
            showlegend=True,
            plot_bgcolor='rgba(0,0,0,0)',
            paper_bgcolor='rgba(0,0,0,0)',
            legend=dict(
                orientation="v",
                yanchor="middle",
                y=0.5,
                xanchor="left",
                x=1.0,
                font=dict(color='#a0aec0', size=9),
                bgcolor='rgba(0,0,0,0)'
            ),
            margin=dict(l=20, r=150, t=50, b=20),
            hoverlabel=dict(
                bgcolor='#1a1f2e',
                font_size=12,
                font_color='#e2e8f0',
                bordercolor='#3d4a5c'
            )
        )
        
        ui.plotly(fig).classes('w-full')


class PerformanceTable:
    """Table component for displaying detailed performance metrics."""
    
    def __init__(self, title: str, columns: List[str], rows: List[Dict[str, Any]]):
        """
        Args:
            title: Table title
            columns: List of column names
            rows: List of dictionaries with row data
        """
        self.title = title
        self.columns = columns
        self.rows = rows
    
    def render(self):
        """Render the performance table."""
        if self.title:
            ui.label(self.title).classes('text-lg font-bold mb-2').style('color: #e2e8f0;')
        
        if not self.rows:
            ui.label("No data available").classes('text-center p-4').style('color: #a0aec0;')
            return
        
        # Create table with sortable columns
        table_data = {
            'columns': [{'name': col, 'label': col, 'field': col, 'align': 'left', 'sortable': True} 
                       for col in self.columns],
            'rows': self.rows
        }
        
        ui.table(**table_data).classes('w-full dark-table')


class MultiMetricDashboard:
    """Dashboard with multiple metric cards in a grid."""
    
    def __init__(self, metrics: List[Dict[str, Any]], columns: int = 4):
        """
        Args:
            metrics: List of metric dictionaries (title, value, subtitle, trend, color)
            columns: Number of columns in grid
        """
        self.metrics = metrics
        self.columns = columns
    
    def render(self):
        """Render the metrics dashboard."""
        with ui.grid(columns=self.columns).classes('w-full gap-4'):
            for metric in self.metrics:
                card = MetricCard(
                    title=metric.get('title', ''),
                    value=metric.get('value', ''),
                    subtitle=metric.get('subtitle', ''),
                    trend=metric.get('trend'),
                    color=metric.get('color', 'primary')
                )
                card.render()


class ComboChart:
    """Combined bar and line chart for comparing metrics."""
    
    def __init__(self, title: str, categories: List[str],
                 bar_data: Dict[str, List[float]],
                 line_data: Dict[str, List[float]],
                 height: int = 500):
        """
        Args:
            title: Chart title
            categories: X-axis categories (e.g., months)
            bar_data: Dict of {series_name: [values]} for bars
            line_data: Dict of {series_name: [values]} for lines
            height: Chart height in pixels
        """
        self.title = title
        self.categories = categories
        self.bar_data = bar_data
        self.line_data = line_data
        self.height = height
    
    # Modern color palette for dark theme
    BAR_COLORS = ['#00d4aa', '#74c0fc', '#ffa94d', '#b197fc']
    LINE_COLORS = ['#ff6b6b', '#ffd43b', '#63e6be', '#da77f2']
    
    def render(self):
        """Render the combo chart."""
        fig = make_subplots(specs=[[{"secondary_y": True}]])
        
        # Add bar traces
        for idx, (series_name, values) in enumerate(self.bar_data.items()):
            fig.add_trace(
                go.Bar(
                    name=series_name, 
                    x=self.categories, 
                    y=values,
                    marker=dict(color=self.BAR_COLORS[idx % len(self.BAR_COLORS)])
                ),
                secondary_y=False
            )
        
        # Add line traces
        for idx, (series_name, values) in enumerate(self.line_data.items()):
            color = self.LINE_COLORS[idx % len(self.LINE_COLORS)]
            fig.add_trace(
                go.Scatter(
                    name=series_name, 
                    x=self.categories, 
                    y=values, 
                    mode='lines+markers',
                    line=dict(color=color, width=2),
                    marker=dict(size=6, color=color)
                ),
                secondary_y=True
            )
        
        fig.update_layout(
            title=dict(text=self.title, font=dict(color='#e2e8f0', size=14)),
            height=self.height,
            hovermode='x unified',
            barmode='group',
            plot_bgcolor='rgba(0,0,0,0)',
            paper_bgcolor='rgba(0,0,0,0)',
            legend=dict(
                font=dict(color='#a0aec0', size=10),
                bgcolor='rgba(0,0,0,0)'
            ),
            margin=dict(l=60, r=60, t=50, b=60),
            hoverlabel=dict(
                bgcolor='#1a1f2e',
                font_size=12,
                font_color='#e2e8f0',
                bordercolor='#3d4a5c'
            )
        )
        
        fig.update_xaxes(
            title_text="Period",
            title_font=dict(color='#a0aec0'),
            tickfont=dict(color='#a0aec0', size=10),
            gridcolor='rgba(160,174,192,0.2)'
        )
        fig.update_yaxes(
            title_text="Count", 
            secondary_y=False,
            title_font=dict(color='#a0aec0'),
            tickfont=dict(color='#a0aec0', size=10),
            gridcolor='rgba(160,174,192,0.2)'
        )
        fig.update_yaxes(
            title_text="Amount", 
            secondary_y=True,
            title_font=dict(color='#a0aec0'),
            tickfont=dict(color='#a0aec0', size=10)
        )
        
        ui.plotly(fig).classes('w-full')


# Utility functions for performance calculations

def calculate_sharpe_ratio(returns: List[float], risk_free_rate: float = 0.02) -> Optional[float]:
    """
    Calculate annualized Sharpe ratio.
    
    Args:
        returns: List of daily returns
        risk_free_rate: Annual risk-free rate (default 2%)
    
    Returns:
        Sharpe ratio or None if insufficient data
    """
    if len(returns) < 30:
        return None
    
    returns_array = np.array(returns)
    mean_return = np.mean(returns_array)
    std_return = np.std(returns_array, ddof=1)
    
    if std_return == 0:
        return 0.0
    
    # Annualize assuming 252 trading days
    daily_rf = risk_free_rate / 252
    sharpe = (mean_return - daily_rf) / std_return * np.sqrt(252)
    
    return sharpe


def calculate_win_loss_ratio(transactions: List[Dict[str, Any]]) -> Tuple[float, int, int]:
    """
    Calculate win/loss ratio and counts.
    
    Args:
        transactions: List of transaction dictionaries with 'pnl' field
    
    Returns:
        Tuple of (win_rate_percentage, win_count, loss_count)
    """
    wins = sum(1 for t in transactions if t.get('pnl', 0) > 0)
    losses = sum(1 for t in transactions if t.get('pnl', 0) < 0)
    total = wins + losses
    
    win_rate = (wins / total * 100) if total > 0 else 0.0
    
    return win_rate, wins, losses


def calculate_max_drawdown(equity_curve: List[float]) -> float:
    """
    Calculate maximum drawdown from equity curve.
    
    Args:
        equity_curve: List of equity values over time
    
    Returns:
        Maximum drawdown as a percentage
    """
    if not equity_curve or len(equity_curve) < 2:
        return 0.0
    
    equity_array = np.array(equity_curve)
    running_max = np.maximum.accumulate(equity_array)
    drawdown = (equity_array - running_max) / running_max * 100
    
    return abs(np.min(drawdown))


def calculate_profit_factor(winning_trades: List[float], losing_trades: List[float]) -> Optional[float]:
    """
    Calculate profit factor (gross profit / gross loss).
    
    Args:
        winning_trades: List of winning trade P&Ls
        losing_trades: List of losing trade P&Ls
    
    Returns:
        Profit factor or None if no losing trades
    """
    gross_profit = sum(winning_trades) if winning_trades else 0
    gross_loss = abs(sum(losing_trades)) if losing_trades else 0
    
    if gross_loss == 0:
        return None if gross_profit == 0 else float('inf')
    
    return gross_profit / gross_loss
