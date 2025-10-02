"""
InstrumentGraph Component
Displays interactive price action charts with technical indicators using multiple Y-axes
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
                
                # Render using Plotly via NiceGUI with responsive sizing
                self.chart = ui.plotly(chart_config).classes('w-full').style('height: 700px; max-height: 80vh;')
                
        except Exception as e:
            logger.error(f"Error rendering chart: {e}", exc_info=True)
            with self.chart_container:
                ui.label(f'Error creating chart: {e}').classes('text-red-500')
    
    def _build_chart_config(self) -> Dict[str, Any]:
        """
        Build Plotly chart configuration with multiple Y-axes for different indicator scales.
        
        Separates indicators into groups:
        - Price-scale indicators (MA, EMA, Bollinger Bands, VWAP, etc.)
        - Oscillators (RSI, Stochastic, etc.) - separate subplot
        - Momentum indicators (MACD, etc.) - separate subplot  
        - Volume - separate subplot
        """
        try:
            import plotly.graph_objects as go
            from plotly.subplots import make_subplots
            
            # Categorize indicators by type
            price_indicators = []  # Same scale as price
            oscillators = []  # 0-100 scale (RSI, Stochastic, Williams %R, etc.)
            momentum_indicators = []  # MACD, momentum, etc.
            other_indicators = []  # Unknown indicators
            
            for indicator_name in self.indicators_data.keys():
                if not self.visible_indicators.get(indicator_name, False):
                    continue
                    
                indicator_lower = indicator_name.lower()
                
                # Categorize based on indicator name
                if any(x in indicator_lower for x in ['ma_', 'ema_', 'sma_', 'wma', 'bollinger', 'vwap', 'keltner', 'envelope', 'pivot']):
                    price_indicators.append(indicator_name)
                elif any(x in indicator_lower for x in ['rsi', 'stoch', 'williams', 'cci', 'roc', 'mfi']):
                    oscillators.append(indicator_name)
                elif any(x in indicator_lower for x in ['macd', 'momentum', 'trix', 'ppo']):
                    momentum_indicators.append(indicator_name)
                else:
                    # Try to auto-detect by checking value ranges
                    indicator_df = self.indicators_data[indicator_name]
                    if not indicator_df.empty:
                        for col in indicator_df.columns:
                            if indicator_df[col].dtype in ['float64', 'int64']:
                                values = indicator_df[col].dropna()
                                if len(values) > 0:
                                    min_val, max_val = values.min(), values.max()
                                    # If values are in 0-100 range, likely an oscillator
                                    if 0 <= min_val and max_val <= 100:
                                        oscillators.append(indicator_name)
                                    else:
                                        other_indicators.append(indicator_name)
                                break
            
            # Determine subplot structure
            subplot_titles = ['Price & Volume']
            num_rows = 1
            row_heights = [0.5]  # Price chart starts with 50%
            
            if oscillators:
                subplot_titles.append('Oscillators (RSI, Stochastic, etc.)')
                num_rows += 1
                row_heights.append(0.25)
            
            if momentum_indicators:
                subplot_titles.append('Momentum Indicators (MACD, etc.)')
                num_rows += 1
                row_heights.append(0.25)
            
            # Adjust heights to sum to 1.0
            if num_rows > 1:
                row_heights = [h / sum(row_heights) for h in row_heights]
            
            # Create subplots with secondary y-axis for volume
            specs = [[{"secondary_y": True}]] + [[{"secondary_y": False}]] * (num_rows - 1)
            
            fig = make_subplots(
                rows=num_rows,
                cols=1,
                shared_xaxes=True,
                vertical_spacing=0.05,
                row_heights=row_heights,
                subplot_titles=subplot_titles,
                specs=specs
            )
            
            # Prepare x-axis data - use datetime objects directly for proper time series handling
            # Plotly handles pandas DatetimeIndex natively
            if isinstance(self.price_data.index, pd.DatetimeIndex):
                x_data = self.price_data.index
            else:
                x_data = self.price_data.get('Date', list(range(len(self.price_data))))
            
            # Add candlestick chart for price
            if all(col in self.price_data.columns for col in ['Open', 'High', 'Low', 'Close']):
                fig.add_trace(
                    go.Candlestick(
                        x=x_data,
                        open=self.price_data['Open'],
                        high=self.price_data['High'],
                        low=self.price_data['Low'],
                        close=self.price_data['Close'],
                        name='Price',
                        increasing_line_color='#26a69a',  # Teal for up
                        increasing_fillcolor='#26a69a',
                        decreasing_line_color='#ef5350',  # Red for down
                        decreasing_fillcolor='#ef5350',
                        showlegend=True
                    ),
                    row=1, col=1,
                    secondary_y=False
                )
            elif 'Close' in self.price_data.columns:
                # Fallback to line chart
                fig.add_trace(
                    go.Scatter(
                        x=x_data,
                        y=self.price_data['Close'],
                        mode='lines',
                        name='Close Price',
                        line=dict(color='#2563eb', width=2),
                        showlegend=True
                    ),
                    row=1, col=1,
                    secondary_y=False
                )
            
            # Add price-scale indicators to main chart
            colors = ['#ff6f00', '#7c4dff', '#00c853', '#d500f9', '#0091ea']
            color_idx = 0
            
            for indicator_name in price_indicators:
                indicator_df = self.indicators_data[indicator_name]
                if indicator_df.empty:
                    continue
                
                value_col = None
                for col in indicator_df.columns:
                    if indicator_df[col].dtype in ['float64', 'int64']:
                        value_col = col
                        break
                
                if value_col:
                    # Use DatetimeIndex directly - Plotly handles it natively
                    if isinstance(indicator_df.index, pd.DatetimeIndex):
                        indicator_x = indicator_df.index
                    else:
                        indicator_x = list(range(len(indicator_df)))
                    
                    fig.add_trace(
                        go.Scatter(
                            x=indicator_x,
                            y=indicator_df[value_col],
                            mode='lines',
                            name=indicator_name,
                            line=dict(width=1.5, color=colors[color_idx % len(colors)]),
                            opacity=0.8,
                            showlegend=True
                        ),
                        row=1, col=1,
                        secondary_y=False
                    )
                    color_idx += 1
            
            # Add volume bar chart on secondary y-axis
            if 'Volume' in self.price_data.columns:
                colors_vol = ['#ef5350' if close < open_ else '#26a69a' 
                             for close, open_ in zip(self.price_data['Close'], self.price_data['Open'])]
                
                fig.add_trace(
                    go.Bar(
                        x=x_data,
                        y=self.price_data['Volume'],
                        name='Volume',
                        marker_color=colors_vol,
                        opacity=0.3,
                        showlegend=True,
                        yaxis='y2'
                    ),
                    row=1, col=1,
                    secondary_y=True
                )
            
            # Add oscillators to second subplot
            current_row = 2
            if oscillators:
                color_idx = 0
                for indicator_name in oscillators:
                    indicator_df = self.indicators_data[indicator_name]
                    if indicator_df.empty:
                        continue
                    
                    value_col = None
                    for col in indicator_df.columns:
                        if indicator_df[col].dtype in ['float64', 'int64']:
                            value_col = col
                            break
                    
                    if value_col:
                        # Use DatetimeIndex directly - Plotly handles it natively
                        if isinstance(indicator_df.index, pd.DatetimeIndex):
                            indicator_x = indicator_df.index
                        else:
                            indicator_x = list(range(len(indicator_df)))
                        
                        fig.add_trace(
                            go.Scatter(
                                x=indicator_x,
                                y=indicator_df[value_col],
                                mode='lines',
                                name=indicator_name,
                                line=dict(width=2, color=colors[color_idx % len(colors)]),
                                showlegend=True
                            ),
                            row=current_row, col=1
                        )
                        color_idx += 1
                
                # Add reference lines for oscillators (overbought/oversold)
                if len(x_data) > 0:
                    # 70/30 lines for RSI
                    fig.add_hline(y=70, line_dash="dash", line_color="red", opacity=0.5, row=current_row, col=1)
                    fig.add_hline(y=30, line_dash="dash", line_color="green", opacity=0.5, row=current_row, col=1)
                    fig.add_hline(y=50, line_dash="dot", line_color="gray", opacity=0.3, row=current_row, col=1)
                
                current_row += 1
            
            # Add momentum indicators to third subplot
            if momentum_indicators:
                color_idx = 0
                for indicator_name in momentum_indicators:
                    indicator_df = self.indicators_data[indicator_name]
                    if indicator_df.empty:
                        continue
                    
                    # MACD often has multiple columns (MACD, Signal, Histogram)
                    for col in indicator_df.columns:
                        if indicator_df[col].dtype in ['float64', 'int64']:
                            # Use DatetimeIndex directly - Plotly handles it natively
                            if isinstance(indicator_df.index, pd.DatetimeIndex):
                                indicator_x = indicator_df.index
                            else:
                                indicator_x = list(range(len(indicator_df)))
                            
                            # Use bar chart for histogram, line for others
                            is_histogram = 'hist' in col.lower()
                            
                            if is_histogram:
                                colors_macd = ['#ef5350' if v < 0 else '#26a69a' for v in indicator_df[col]]
                                fig.add_trace(
                                    go.Bar(
                                        x=indicator_x,
                                        y=indicator_df[col],
                                        name=f"{indicator_name} - {col}",
                                        marker_color=colors_macd,
                                        opacity=0.6,
                                        showlegend=True
                                    ),
                                    row=current_row, col=1
                                )
                            else:
                                fig.add_trace(
                                    go.Scatter(
                                        x=indicator_x,
                                        y=indicator_df[col],
                                        mode='lines',
                                        name=f"{indicator_name} - {col}",
                                        line=dict(width=2, color=colors[color_idx % len(colors)]),
                                        showlegend=True
                                    ),
                                    row=current_row, col=1
                                )
                            color_idx += 1
                
                # Add zero line for momentum
                fig.add_hline(y=0, line_dash="dash", line_color="gray", opacity=0.5, row=current_row, col=1)
            
            # Update layout with better styling
            fig.update_layout(
                title={
                    'text': f'{self.symbol} - Price Action & Technical Indicators',
                    'x': 0.5,
                    'xanchor': 'center',
                    'font': {'size': 20, 'color': '#1f2937'}
                },
                height=700,
                hovermode='x unified',
                showlegend=True,
                legend=dict(
                    orientation="v",
                    yanchor="top",
                    y=0.99,
                    xanchor="left",
                    x=1.01,
                    bgcolor="rgba(255, 255, 255, 0.9)",
                    bordercolor="#e5e7eb",
                    borderwidth=1
                ),
                template='plotly_white',
                margin=dict(l=60, r=180, t=80, b=60),
                paper_bgcolor='white',
                plot_bgcolor='#fafafa'
            )
            
            # Update axes
            fig.update_xaxes(
                title_text="Date & Time",
                showgrid=True,
                gridwidth=1,
                gridcolor='#e5e7eb',
                row=num_rows,
                col=1,
                tickangle=-45  # Angle the labels for better readability with datetime
            )
            
            # Primary y-axis (price)
            fig.update_yaxes(
                title_text="Price ($)",
                showgrid=True,
                gridwidth=1,
                gridcolor='#e5e7eb',
                row=1,
                col=1,
                secondary_y=False
            )
            
            # Secondary y-axis (volume)
            if 'Volume' in self.price_data.columns:
                fig.update_yaxes(
                    title_text="Volume",
                    showgrid=False,
                    row=1,
                    col=1,
                    secondary_y=True
                )
            
            # Oscillator y-axis
            if oscillators:
                fig.update_yaxes(
                    title_text="Value",
                    range=[-5, 105],  # Slightly wider than 0-100 for visibility
                    showgrid=True,
                    gridwidth=1,
                    gridcolor='#e5e7eb',
                    row=2,
                    col=1
                )
            
            # Momentum y-axis
            if momentum_indicators:
                row_num = 3 if oscillators else 2
                fig.update_yaxes(
                    title_text="Value",
                    showgrid=True,
                    gridwidth=1,
                    gridcolor='#e5e7eb',
                    row=row_num,
                    col=1
                )
            
            # Remove rangeslider for cleaner look
            fig.update_xaxes(rangeslider_visible=False)
            
            return fig
            
        except ImportError as e:
            logger.warning(f"Plotly not installed: {e}, using fallback chart")
            return self._build_fallback_chart()
        except Exception as e:
            logger.error(f"Error building chart config: {e}", exc_info=True)
            return self._build_fallback_chart()
    
    def _build_fallback_chart(self) -> Dict[str, Any]:
        """Build a simple fallback chart using Highcharts if Plotly is not available."""
        try:
            # Prepare data for Highcharts
            if isinstance(self.price_data.index, pd.DatetimeIndex):
                dates = self.price_data.index.strftime('%Y-%m-%d %H:%M').tolist()
            else:
                dates = list(range(len(self.price_data)))
            
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
