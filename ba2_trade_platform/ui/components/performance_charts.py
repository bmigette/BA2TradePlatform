"""
Reusable Performance Chart Components

This module provides reusable chart components for displaying trading performance metrics.
All components use Plotly for consistent, interactive visualizations.
"""

from nicegui import ui
import plotly.graph_objects as go
from datetime import datetime
from typing import List, Dict, Any, Optional, Sequence, Tuple
import pandas as pd


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
                 date_format: str = "auto",
                 bar_series: Optional[Dict[str, List[Tuple[datetime, float]]]] = None,
                 bar_ylabel: str = ""):
        """
        Args:
            title: Chart title
            series_data: Dict of {series_name: [(datetime, value), ...]}
            xlabel: X-axis label
            ylabel: Y-axis label
            height: Chart height in pixels
            date_format: Date format - "auto", "monthly", "daily", or custom strftime format
            bar_series: Optional {series_name: [(datetime, value), ...]} drawn as
                translucent bars BEHIND the lines, on their own right-hand axis. Used for
                a magnitude that shares the x-axis but not the unit -- drawdown %, which
                cannot share a dollar axis without one of the two being meaningless.
            bar_ylabel: Label for that right-hand axis.
        """
        self.title = title
        self.series_data = series_data
        self.xlabel = xlabel
        self.ylabel = ylabel
        self.height = height
        self.date_format = date_format
        self.bar_series = bar_series or {}
        self.bar_ylabel = bar_ylabel
    
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

        # BARS FIRST. Plotly paints traces in the order they are added, so adding these
        # before the lines is what puts them behind. Their colour is looked up by the
        # SERIES NAME, not by their own enumeration index, so an expert's bar is the same
        # hue as its line even when only some experts have bars.
        line_names = list(self.series_data.keys())
        for bar_name, points in self.bar_series.items():
            if not points:
                continue
            try:
                color = self.COLORS[line_names.index(bar_name) % len(self.COLORS)]
            except ValueError:
                color = self.COLORS[len(line_names) % len(self.COLORS)]
            fig.add_trace(go.Bar(
                x=[d[0] for d in points],
                y=[d[1] for d in points],
                name=f'{bar_name} DD',
                yaxis='y2',
                marker=dict(color=color, opacity=0.22),
                hovertemplate=f'{bar_name} drawdown<br>%{{x}}<br>%{{y:.1f}}%<extra></extra>',
            ))

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
            # A SECOND axis, not a shared one: drawdown is a percentage and the lines are
            # dollars. Sharing would either squash the lines flat or scale the bars into
            # nonsense, depending on which unit happened to be larger.
            #
            # ``showgrid=False`` so the two axes do not draw two sets of gridlines over
            # each other, and the bars are grouped so several experts sit side by side in
            # a month instead of hiding one another.
            yaxis2=dict(
                title=dict(text=self.bar_ylabel, font=dict(color='#a0aec0')),
                tickfont=dict(color='#a0aec0', size=10),
                overlaying='y',
                side='right',
                showgrid=False,
                rangemode='tozero',
            ) if self.bar_series else None,
            barmode='group',
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
    
    def __init__(self, title: str, columns: List[str], rows: List[Dict[str, Any]],
                 mobile_hide: Optional[Sequence[str]] = None):
        """
        Args:
            title: Table title
            columns: List of column names
            rows: List of dictionaries with row data
        """
        self.title = title
        self.columns = columns
        #: Column LABELS to drop on a phone. Empty by default, so an existing
        #: caller keeps every column and nothing disappears without being asked for.
        self.mobile_hide = set(mobile_hide or ())
        self.rows = rows
    
    def render(self):
        """Render the performance table."""
        if self.title:
            ui.label(self.title).classes('text-lg font-bold mb-2').style('color: #e2e8f0;')
        
        if not self.rows:
            ui.label("No data available").classes('text-center p-4').style('color: #a0aec0;')
            return
        
        # Create table with sortable columns
        #
        # ``mobile_hide`` tags a column so the responsive layer can drop it on a phone.
        # Quasar adds no per-column class of its own, so ``classes``/``headerClasses``
        # -- its documented hook -- is the only way a stylesheet can reach one. A
        # positional `nth-child` rule would hide the WRONG column the first time one is
        # inserted, on a table of money.
        table_data = {
            'columns': [
                {'name': col, 'label': col, 'field': col, 'align': 'left',
                 'sortable': True,
                 **({'classes': 'mobile-hide', 'headerClasses': 'mobile-hide'}
                    if col in self.mobile_hide else {})}
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
        # ``metric-grid`` is the mobile layer's hook: the blanket 1-column collapse
        # is right for a chart and wrong for a row of small tiles, so this grid asks
        # to stay two-up on a phone rather than becoming four screens of scrolling.
        with ui.grid(columns=self.columns).classes('w-full gap-4 metric-grid'):
            for metric in self.metrics:
                card = MetricCard(
                    title=metric.get('title', ''),
                    value=metric.get('value', ''),
                    subtitle=metric.get('subtitle', ''),
                    trend=metric.get('trend'),
                    color=metric.get('color', 'primary')
                )
                card.render()


# Metric functions moved to ba2_common (site plan P0a); re-bound here for existing importers.
from ba2_common.analytics.performance import (  # noqa: E402,F401
    calculate_max_drawdown, calculate_profit_factor, calculate_sharpe_ratio,
    calculate_win_loss_ratio, max_drawdown_from_pnl,
)
