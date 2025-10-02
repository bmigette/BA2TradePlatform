"""
InstrumentGraph Component
Displays interactive price action charts with technical indicators
"""
from nicegui import ui
from typing import Dict, List, Any, Optional
import pandas as pd
import json
from ...logger import logger


class InstrumentGraph:
    """
    Interactive chart component for displaying instrument price data and technical indicators.
    
    Features:
    - Price action (OHLC) display
    - Multiple technical indicators overlay
    - Checkbox controls to show/hide indicators
    - Responsive design
    """
    
    def __init__(self, symbol: str, price_data: Optional[pd.DataFrame] = None, 
                 indicators_data: Optional[Dict[str, pd.DataFrame]] = None):
        """
        Initialize the InstrumentGraph component.
        
        Args:
            symbol: The instrument symbol (e.g., "AAPL", "MSFT")
            price_data: DataFrame with OHLC data (columns: Date, Open, High, Low, Close, Volume)
            indicators_data: Dict mapping indicator name to DataFrame with indicator values
        """
        self.symbol = symbol
        self.price_data = price_data if price_data is not None else pd.DataFrame()
        self.indicators_data = indicators_data if indicators_data is not None else {}
        
        # Track which indicators are visible
        self.visible_indicators = {name: True for name in self.indicators_data.keys()}
        
        # Chart container reference
        self.chart_container = None
        self.chart = None
        
    def render(self) -> None:
        """Render the complete graph component with controls and chart."""
        try:
            with ui.card().classes('w-full'):
                # Header
                with ui.row().classes('w-full items-center justify-between mb-4'):
                    ui.label(f'📈 {self.symbol} - Price & Indicators').classes('text-h6')
                    
                    # Indicator visibility controls
                    if self.indicators_data:
                        with ui.row().classes('gap-4'):
                            ui.label('Show Indicators:').classes('text-sm font-bold')
                            for indicator_name in self.indicators_data.keys():
                                ui.checkbox(indicator_name, value=self.visible_indicators[indicator_name]) \
                                    .on_value_change(lambda e, name=indicator_name: self._toggle_indicator(name, e.value))
                
                # Chart container
                self.chart_container = ui.column().classes('w-full')
                
                # Initial render
                self._render_chart()
                
        except Exception as e:
            logger.error(f"Error rendering InstrumentGraph: {e}", exc_info=True)
            ui.label(f'Error rendering chart: {e}').classes('text-red-500')
    
    def _toggle_indicator(self, indicator_name: str, is_visible: bool) -> None:
        """Toggle visibility of an indicator and re-render chart."""
        self.visible_indicators[indicator_name] = is_visible
        self._render_chart()
    
    def _render_chart(self) -> None:
        """Render or update the chart with current settings."""
        try:
            # Clear existing chart
            if self.chart_container:
                self.chart_container.clear()
            
            with self.chart_container:
                if self.price_data.empty:
                    ui.label('No price data available').classes('text-gray-500 text-center py-8')
                    return
                
                # Prepare chart data
                chart_config = self._build_chart_config()
                
                # Render using Plotly via NiceGUI
                self.chart = ui.plotly(chart_config).classes('w-full h-96')
                
        except Exception as e:
            logger.error(f"Error rendering chart: {e}", exc_info=True)
            with self.chart_container:
                ui.label(f'Error creating chart: {e}').classes('text-red-500')
    
    def _build_chart_config(self) -> Dict[str, Any]:
        """Build Plotly chart configuration."""
        try:
            import plotly.graph_objects as go
            from plotly.subplots import make_subplots
            
            # Determine number of subplots (price + volume if available + visible indicators)
            num_rows = 1  # Always have price chart
            row_heights = [0.7]  # Price chart gets 70% height
            
            if 'Volume' in self.price_data.columns:
                num_rows += 1
                row_heights.append(0.3)
            
            # Create subplots
            fig = make_subplots(
                rows=num_rows,
                cols=1,
                shared_xaxes=True,
                vertical_spacing=0.03,
                row_heights=row_heights,
                subplot_titles=(['Price'] + (['Volume'] if num_rows > 1 else []))
            )
            
            # Add candlestick chart for price
            if all(col in self.price_data.columns for col in ['Open', 'High', 'Low', 'Close']):
                fig.add_trace(
                    go.Candlestick(
                        x=self.price_data.index if isinstance(self.price_data.index, pd.DatetimeIndex) else self.price_data.get('Date', range(len(self.price_data))),
                        open=self.price_data['Open'],
                        high=self.price_data['High'],
                        low=self.price_data['Low'],
                        close=self.price_data['Close'],
                        name='Price',
                        increasing_line_color='green',
                        decreasing_line_color='red'
                    ),
                    row=1, col=1
                )
            elif 'Close' in self.price_data.columns:
                # Fallback to line chart if OHLC not available
                fig.add_trace(
                    go.Scatter(
                        x=self.price_data.index if isinstance(self.price_data.index, pd.DatetimeIndex) else self.price_data.get('Date', range(len(self.price_data))),
                        y=self.price_data['Close'],
                        mode='lines',
                        name='Close Price',
                        line=dict(color='blue', width=2)
                    ),
                    row=1, col=1
                )
            
            # Add technical indicators to price chart
            for indicator_name, indicator_df in self.indicators_data.items():
                if self.visible_indicators.get(indicator_name, False) and not indicator_df.empty:
                    # Determine which column to plot (usually the first numeric column)
                    value_col = None
                    for col in indicator_df.columns:
                        if indicator_df[col].dtype in ['float64', 'int64']:
                            value_col = col
                            break
                    
                    if value_col:
                        fig.add_trace(
                            go.Scatter(
                                x=indicator_df.index if isinstance(indicator_df.index, pd.DatetimeIndex) else range(len(indicator_df)),
                                y=indicator_df[value_col],
                                mode='lines',
                                name=indicator_name,
                                line=dict(width=1.5),
                                opacity=0.8
                            ),
                            row=1, col=1
                        )
            
            # Add volume chart if available
            if 'Volume' in self.price_data.columns and num_rows > 1:
                colors = ['red' if close < open_ else 'green' 
                         for close, open_ in zip(self.price_data['Close'], self.price_data['Open'])]
                
                fig.add_trace(
                    go.Bar(
                        x=self.price_data.index if isinstance(self.price_data.index, pd.DatetimeIndex) else self.price_data.get('Date', range(len(self.price_data))),
                        y=self.price_data['Volume'],
                        name='Volume',
                        marker_color=colors,
                        opacity=0.5
                    ),
                    row=2, col=1
                )
            
            # Update layout
            fig.update_layout(
                title=f'{self.symbol} Price Action & Technical Indicators',
                xaxis_title='Date',
                yaxis_title='Price',
                height=600,
                hovermode='x unified',
                showlegend=True,
                legend=dict(
                    orientation="h",
                    yanchor="bottom",
                    y=1.02,
                    xanchor="right",
                    x=1
                ),
                template='plotly_white'
            )
            
            # Remove rangeslider for cleaner look
            fig.update_xaxes(rangeslider_visible=False)
            
            return fig
            
        except ImportError:
            logger.warning("Plotly not installed, using fallback chart")
            return self._build_fallback_chart()
        except Exception as e:
            logger.error(f"Error building chart config: {e}", exc_info=True)
            return self._build_fallback_chart()
    
    def _build_fallback_chart(self) -> Dict[str, Any]:
        """Build a simple fallback chart using Highcharts if Plotly is not available."""
        try:
            # Prepare data for Highcharts
            dates = self.price_data.index.strftime('%Y-%m-%d').tolist() if isinstance(self.price_data.index, pd.DatetimeIndex) else list(range(len(self.price_data)))
            
            series = []
            
            # Add price series
            if 'Close' in self.price_data.columns:
                series.append({
                    'name': 'Close Price',
                    'data': self.price_data['Close'].tolist(),
                    'type': 'line',
                    'color': '#2563eb'
                })
            
            # Add visible indicators
            for indicator_name, indicator_df in self.indicators_data.items():
                if self.visible_indicators.get(indicator_name, False) and not indicator_df.empty:
                    value_col = None
                    for col in indicator_df.columns:
                        if indicator_df[col].dtype in ['float64', 'int64']:
                            value_col = col
                            break
                    
                    if value_col:
                        series.append({
                            'name': indicator_name,
                            'data': indicator_df[value_col].tolist(),
                            'type': 'line'
                        })
            
            # Highcharts configuration
            return {
                'chart': {'type': 'line', 'height': 600},
                'title': {'text': f'{self.symbol} Price & Indicators'},
                'xAxis': {'categories': dates, 'title': {'text': 'Date'}},
                'yAxis': {'title': {'text': 'Value'}},
                'series': series,
                'legend': {'enabled': True},
                'tooltip': {'shared': True}
            }
            
        except Exception as e:
            logger.error(f"Error building fallback chart: {e}", exc_info=True)
            return {}
    
    def update_data(self, price_data: Optional[pd.DataFrame] = None,
                    indicators_data: Optional[Dict[str, pd.DataFrame]] = None) -> None:
        """
        Update the chart with new data.
        
        Args:
            price_data: New price data DataFrame
            indicators_data: New indicators data dict
        """
        if price_data is not None:
            self.price_data = price_data
        
        if indicators_data is not None:
            self.indicators_data = indicators_data
            # Initialize visibility for new indicators
            for name in indicators_data.keys():
                if name not in self.visible_indicators:
                    self.visible_indicators[name] = True
        
        # Re-render chart
        self._render_chart()
