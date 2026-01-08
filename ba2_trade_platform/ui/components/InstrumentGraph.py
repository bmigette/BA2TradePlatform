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
                 indicators_data: Optional[Dict[str, pd.DataFrame]] = None,
                 recommendation_date: Optional[Any] = None,
                 recommendation_action: Optional[str] = None):
        """
        Initialize the InstrumentGraph component.
        
        Args:
            symbol: The instrument symbol (e.g., "AAPL", "MSFT")
            price_data: DataFrame with OHLC data (columns: Date, Open, High, Low, Close, Volume)
            indicators_data: Dict mapping indicator name to DataFrame with indicator values
            recommendation_date: Date when the trading recommendation was made (for marker display)
            recommendation_action: The recommended action (BUY, SELL, HOLD) for the marker label
        """
        self.symbol = symbol
        self.price_data = price_data if price_data is not None else pd.DataFrame()
        self.indicators_data = indicators_data if indicators_data is not None else {}
        self.recommendation_date = recommendation_date
        self.recommendation_action = recommendation_action
        
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
                
                # Render using Plotly with responsive sizing
                self.chart = ui.plotly(chart_config).classes('w-full').style('height: 700px;')
                
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
            
            # Align indicator timezones with price data for proper plotting
            price_index = self.price_data.index if isinstance(self.price_data.index, pd.DatetimeIndex) else None
            
            # Debug: Log price index info
            if price_index is not None:
                logger.debug(f"Price index: {len(price_index)} rows, tz={price_index.tz}, "
                           f"first={price_index[0] if len(price_index) > 0 else 'N/A'}, "
                           f"last={price_index[-1] if len(price_index) > 0 else 'N/A'}")
            
            # Only align timezones, don't force reindexing to avoid creating NaN gaps
            if price_index is not None:
                for indicator_name in self.indicators_data.keys():
                    indicator_df = self.indicators_data[indicator_name]
                    if not indicator_df.empty and isinstance(indicator_df.index, pd.DatetimeIndex):
                        # Debug: Log indicator index before timezone alignment
                        logger.debug(f"Indicator '{indicator_name}' before timezone alignment: {len(indicator_df)} rows, tz={indicator_df.index.tz}, "
                                   f"first={indicator_df.index[0]}, last={indicator_df.index[-1]}")
                        
                        # Only align timezones if they differ, preserve original dates to avoid gaps
                        if price_index.tz is not None and indicator_df.index.tz != price_index.tz:
                            if indicator_df.index.tz is None:
                                # Indicator is naive, localize to match price data timezone
                                indicator_df.index = indicator_df.index.tz_localize(price_index.tz)
                                logger.debug(f"Localized naive indicator '{indicator_name}' to {price_index.tz}")
                            else:
                                # Convert indicator timezone to match price data
                                indicator_df.index = indicator_df.index.tz_convert(price_index.tz)
                                logger.debug(f"Converted indicator '{indicator_name}' from {indicator_df.index.tz} to {price_index.tz}")
                            
                            self.indicators_data[indicator_name] = indicator_df
                        
                        # Debug: Log final indicator stats (no gaps created)
                        final_df = self.indicators_data[indicator_name]
                        non_nan_count = final_df.notna().sum().sum()
                        total_values = final_df.shape[0] * final_df.shape[1]
                        logger.debug(f"Timezone-aligned indicator '{indicator_name}': "
                                   f"{len(final_df)} rows preserved, "
                                   f"non-NaN values: {non_nan_count}/{total_values} (no gaps created)")
            
            # Categorize indicators by type
            # Static mapping based on indicator definitions from MarketIndicatorsInterface
            INDICATOR_CATEGORIES = {
                # Moving Averages - display on price chart
                'close 50 sma': 'price',
                'close 200 sma': 'price',
                'close 10 ema': 'price',
                
                # MACD - display on momentum subplot
                'macd': 'momentum',
                'macds': 'momentum',
                'macdh': 'momentum',
                
                # Oscillators - display on oscillator subplot
                'rsi': 'oscillator',
                'mfi': 'oscillator',
                
                # Volatility - ATR on momentum subplot
                'atr': 'momentum',
                'boll': 'price',
                'boll ub': 'price',
                'boll lb': 'price',
                
                # Volume - volume-weighted MA on price chart
                'vwma': 'price',
            }
            
            price_indicators = []  # Same scale as price
            oscillators = []  # 0-100 scale (RSI, Stochastic, Williams %R, etc.)
            momentum_indicators = []  # MACD, momentum, etc.
            other_indicators = []  # Unknown indicators
            
            for indicator_name in self.indicators_data.keys():
                if not self.visible_indicators.get(indicator_name, False):
                    continue
                    
                indicator_lower = indicator_name.lower()
                
                # Try to find in static mapping first
                category = INDICATOR_CATEGORIES.get(indicator_lower)
                
                if category == 'price':
                    price_indicators.append(indicator_name)
                elif category == 'oscillator':
                    oscillators.append(indicator_name)
                elif category == 'momentum':
                    momentum_indicators.append(indicator_name)
                else:
                    # Fallback: try pattern matching for unmapped indicators
                    if any(x in indicator_lower for x in ['ma', 'ema', 'sma', 'wma', 'bollinger', 'vwap', 'keltner', 'envelope', 'pivot']):
                        price_indicators.append(indicator_name)
                        logger.debug(f"Indicator '{indicator_name}' mapped to price (pattern match)")
                    elif any(x in indicator_lower for x in ['rsi', 'stoch', 'williams', 'cci', 'roc', 'mfi']):
                        oscillators.append(indicator_name)
                        logger.debug(f"Indicator '{indicator_name}' mapped to oscillator (pattern match)")
                    elif any(x in indicator_lower for x in ['macd', 'momentum', 'trix', 'ppo', 'atr']):
                        momentum_indicators.append(indicator_name)
                        logger.debug(f"Indicator '{indicator_name}' mapped to momentum (pattern match)")
                    else:
                        # Try to auto-detect by checking value ranges as last resort
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
                                            logger.debug(f"Indicator '{indicator_name}' mapped to oscillator (auto-detect: 0-100 range)")
                                        else:
                                            other_indicators.append(indicator_name)
                                            logger.debug(f"Indicator '{indicator_name}' mapped to other (auto-detect: range {min_val}-{max_val})")
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
            
            # Use INTEGER indices for x-axis instead of datetime objects
            # This creates a continuous axis regardless of time gaps (weekends, overnight, etc.)
            x_data = list(range(len(self.price_data)))
            
            # Store datetime labels for axis display and hover text
            if isinstance(self.price_data.index, pd.DatetimeIndex):
                datetime_labels = [dt.strftime('%Y-%m-%d %H:%M') for dt in self.price_data.index]
            else:
                datetime_labels = [str(i) for i in range(len(self.price_data))]
            
            logger.debug(f"Price data: {len(self.price_data)} rows, using integer x-axis with {len(datetime_labels)} datetime labels")
            
            # Add candlestick chart for price with datetime labels
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
                        showlegend=True,
                        text=datetime_labels,
                        hovertext=datetime_labels
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
                    # Use SAME integer x-axis as price data for perfect alignment
                    indicator_x = x_data
                    
                    logger.debug(f"Indicator '{indicator_name}': {len(indicator_df)} rows, "
                                f"first: {indicator_df.index[0] if len(indicator_df) > 0 else 'N/A'}")
                    
                    fig.add_trace(
                        go.Scatter(
                            x=indicator_x,
                            y=indicator_df[value_col],
                            mode='lines',
                            name=indicator_name,
                            line=dict(width=1.5, color=colors[color_idx % len(colors)]),
                            opacity=0.8,
                            connectgaps=True,  # Ensure continuous lines across date gaps
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
                        opacity=0.15,  # Reduced from 0.3 to make volume less prominent
                        showlegend=True,
                        yaxis='y2'
                    ),
                    row=1, col=1,
                    secondary_y=True
                )
            
            # Add oscillators to second subplot
            current_row = 2
            if oscillators:
                logger.debug(f"Adding {len(oscillators)} oscillator indicators to subplot {current_row}")
                color_idx = 0
                for indicator_name in oscillators:
                    indicator_df = self.indicators_data[indicator_name]
                    if indicator_df.empty:
                        continue
                    
                    logger.debug(f"Oscillator '{indicator_name}': columns={list(indicator_df.columns)}, shape={indicator_df.shape}")
                    
                    value_col = None
                    for col in indicator_df.columns:
                        if indicator_df[col].dtype in ['float64', 'int64']:
                            value_col = col
                            break
                    
                    if value_col:
                        # Debug: Check for NaN values
                        non_nan_count = indicator_df[value_col].notna().sum()
                        logger.debug(f"Oscillator '{indicator_name}': value_col='{value_col}', non-NaN values: {non_nan_count}/{len(indicator_df)}, "
                                   f"min={indicator_df[value_col].min()}, max={indicator_df[value_col].max()}")
                        
                        # Use SAME integer x-axis as price data for perfect alignment
                        indicator_x = x_data
                        
                        fig.add_trace(
                            go.Scatter(
                                x=indicator_x,
                                y=indicator_df[value_col],
                                mode='lines',
                                name=indicator_name,
                                line=dict(width=2, color=colors[color_idx % len(colors)]),
                                connectgaps=True,  # Ensure continuous lines across date gaps
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
                logger.debug(f"Adding {len(momentum_indicators)} momentum indicators to subplot {current_row}")
                color_idx = 0
                for indicator_name in momentum_indicators:
                    indicator_df = self.indicators_data[indicator_name]
                    if indicator_df.empty:
                        logger.warning(f"Momentum indicator '{indicator_name}' is empty")
                        continue
                    
                    logger.debug(f"Momentum '{indicator_name}': columns={list(indicator_df.columns)}, shape={indicator_df.shape}")
                    
                    # MACD often has multiple columns (MACD, Signal, Histogram)
                    for col in indicator_df.columns:
                        if indicator_df[col].dtype in ['float64', 'int64']:
                            non_nan_count = indicator_df[col].notna().sum()
                            logger.debug(f"Momentum '{indicator_name}' column '{col}': non-NaN values: {non_nan_count}/{len(indicator_df)}, "
                                       f"min={indicator_df[col].min()}, max={indicator_df[col].max()}")
                            
                            # Use SAME integer x-axis as price data for perfect alignment
                            indicator_x = x_data
                            
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
                                        connectgaps=True,  # Ensure continuous lines across date gaps
                                        showlegend=True
                                    ),
                                    row=current_row, col=1
                                )
                            color_idx += 1
                
                # Add zero line for momentum
                fig.add_hline(y=0, line_dash="dash", line_color="gray", opacity=0.5, row=current_row, col=1)
            
            # Add recommendation date marker (vertical line across all subplots)
            if self.recommendation_date:
                try:
                    # Find the index position of the recommendation date in the price data
                    rec_date = pd.Timestamp(self.recommendation_date)
                    if rec_date.tz is None and self.price_data.index.tz is not None:
                        rec_date = rec_date.tz_localize('UTC')
                    elif rec_date.tz is not None and self.price_data.index.tz is None:
                        rec_date = rec_date.tz_localize(None)
                    
                    # Find closest matching timestamp in index
                    try:
                        rec_idx = self.price_data.index.get_loc(rec_date, method='nearest')
                    except:
                        # Fallback: find closest timestamp manually
                        time_diffs = abs(self.price_data.index - rec_date)
                        rec_idx = time_diffs.argmin()
                    
                    logger.info(f"Adding recommendation date marker at index {rec_idx} (date: {self.recommendation_date})")
                    
                    # Use shapes to add vertical line at integer x position
                    for row_idx in range(1, num_rows + 1):
                        fig.add_shape(
                            type="line",
                            x0=rec_idx,
                            x1=rec_idx,
                            y0=0,
                            y1=1,
                            yref=f"y{'' if row_idx == 1 else row_idx} domain",
                            xref="x",
                            line=dict(
                                color="#f59e0b",
                                width=2,
                                dash="dash"
                            ),
                            opacity=0.7
                        )
                    
                    # Add annotation on the first subplot only
                    # Determine the label and styling based on recommendation action
                    if self.recommendation_action:
                        # Extract string value if it's an enum
                        action_str = str(self.recommendation_action.value if hasattr(self.recommendation_action, 'value') else self.recommendation_action)
                        # Remove 'OrderRecommendation.' prefix if present
                        action_str = action_str.replace('OrderRecommendation.', '').upper()
                        
                        logger.debug(f"Recommendation action: {self.recommendation_action} -> {action_str}")
                        
                        action_icons = {'BUY': '📈', 'SELL': '📉', 'HOLD': '➖'}
                        action_colors = {'BUY': '#10b981', 'SELL': '#ef4444', 'HOLD': '#f59e0b'}
                        icon = action_icons.get(action_str, '📊')
                        color = action_colors.get(action_str, '#f59e0b')
                        label = f"{icon} {action_str.title()} Recommendation"
                    else:
                        color = "#f59e0b"
                        label = "📊 Recommendation"
                    
                    fig.add_annotation(
                        x=rec_idx,
                        y=1.0,
                        yref="y domain",
                        xref="x",
                        text=label,
                        showarrow=False,
                        font=dict(size=12, color=color, weight='bold'),
                        bgcolor="rgba(26, 31, 46, 0.95)",
                        bordercolor=color,
                        borderwidth=2,
                        borderpad=4,
                        yanchor="top"
                    )
                    
                    logger.info(f"Successfully added recommendation date marker")
                    
                except Exception as e:
                    logger.error(f"Could not add recommendation date marker: {e}", exc_info=True)
            
            # Update layout with dark theme styling
            fig.update_layout(
                title={
                    'text': f'{self.symbol} - Price Action & Technical Indicators',
                    'x': 0.5,
                    'xanchor': 'center',
                    'font': {'size': 20, 'color': '#e2e8f0'}
                },
                height=700,
                autosize=True,  # Auto-size to container width
                hovermode='x unified',
                showlegend=True,
                legend=dict(
                    orientation="v",
                    yanchor="top",
                    y=0.99,
                    xanchor="left",
                    x=1.01,
                    bgcolor="rgba(26, 31, 46, 0.9)",
                    bordercolor="#3d4a5c",
                    borderwidth=1,
                    font=dict(color='#a0aec0')
                ),
                template='plotly_dark',
                margin=dict(l=60, r=180, t=80, b=60),
                paper_bgcolor='rgba(0,0,0,0)',
                plot_bgcolor='rgba(0,0,0,0)',
                # Enable drag mode for panning
                dragmode='pan',
                hoverlabel=dict(
                    bgcolor='#1a1f2e',
                    font_size=12,
                    font_color='#e2e8f0',
                    bordercolor='#3d4a5c'
                )
            )
            
            # Disable range slider on all x-axes (especially the main price chart)
            fig.update_xaxes(rangeslider_visible=False)
            
            # Configure x-axis to display datetime labels at appropriate intervals
            # Show every 5th label for readability
            tick_interval = max(1, len(datetime_labels) // 20)  # Show ~20 labels
            tickvals = list(range(0, len(datetime_labels), tick_interval))
            ticktext = [datetime_labels[i] for i in tickvals]
            
            fig.update_xaxes(
                title_text="Date & Time",
                title_font=dict(color='#a0aec0'),
                showgrid=True,
                gridwidth=1,
                gridcolor='rgba(160,174,192,0.15)',
                tickfont=dict(color='#a0aec0'),
                row=num_rows,
                col=1,
                tickangle=-45,  # Angle the labels for better readability with datetime
                tickmode='array',
                tickvals=tickvals,
                ticktext=ticktext
            )
            
            # Primary y-axis (price)
            fig.update_yaxes(
                title_text="Price ($)",
                title_font=dict(color='#a0aec0'),
                showgrid=True,
                gridwidth=1,
                gridcolor='rgba(160,174,192,0.15)',
                tickfont=dict(color='#a0aec0'),
                row=1,
                col=1,
                secondary_y=False
            )
            
            # Secondary y-axis (volume)
            if 'Volume' in self.price_data.columns:
                fig.update_yaxes(
                    title_text="Volume",
                    title_font=dict(color='#a0aec0'),
                    tickfont=dict(color='#a0aec0'),
                    showgrid=False,
                    row=1,
                    col=1,
                    secondary_y=True
                )
            
            # Oscillator y-axis
            if oscillators:
                fig.update_yaxes(
                    title_text="Value",
                    title_font=dict(color='#a0aec0'),
                    tickfont=dict(color='#a0aec0'),
                    range=[-5, 105],  # Slightly wider than 0-100 for visibility
                    showgrid=True,
                    gridwidth=1,
                    gridcolor='rgba(160,174,192,0.15)',
                    row=2,
                    col=1
                )
            
            # Momentum y-axis
            if momentum_indicators:
                row_num = 3 if oscillators else 2
                fig.update_yaxes(
                    title_text="Value",
                    title_font=dict(color='#a0aec0'),
                    tickfont=dict(color='#a0aec0'),
                    showgrid=True,
                    gridwidth=1,
                    gridcolor='rgba(160,174,192,0.15)',
                    row=row_num,
                    col=1
                )
            
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
