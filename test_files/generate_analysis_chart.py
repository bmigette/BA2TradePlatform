"""
Generate Plotly chart for a market analysis and save as HTML.

Usage:
    python test_files/generate_analysis_chart.py <analysis_id>
"""

import sys
import os
import json
import pandas as pd
from datetime import datetime

# Add project root to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from ba2_trade_platform.core.db import get_db
from ba2_trade_platform.core.models import MarketAnalysis, AnalysisOutput
from ba2_trade_platform.logger import logger
from sqlmodel import select

import plotly.graph_objects as go
from plotly.subplots import make_subplots


def load_analysis_data(analysis_id: int):
    """Load market analysis data from database."""
    session = get_db()
    try:
        # Get analysis
        analysis = session.get(MarketAnalysis, analysis_id)
        if not analysis:
            print(f"❌ Analysis {analysis_id} not found")
            return None, None, None
        
        print(f"✅ Loaded Analysis #{analysis_id} - {analysis.symbol}")
        
        # Load all outputs
        statement = select(AnalysisOutput).where(
            AnalysisOutput.market_analysis_id == analysis_id
        )
        outputs = list(session.exec(statement).all())
        
        # Load OHLCV data
        ohlcv_outputs = [o for o in outputs if 'ohlcv' in o.name.lower() and o.name.endswith('_json')]
        if not ohlcv_outputs:
            print("❌ No OHLCV data found")
            return analysis, None, None
        
        ohlcv_data = json.loads(ohlcv_outputs[0].text)
        data_dict = ohlcv_data.get('data', {})
        if 'data' in data_dict:
            data_dict = data_dict['data']
        
        # Parse OHLCV
        if isinstance(data_dict, list) and len(data_dict) > 0:
            price_df = pd.DataFrame(data_dict)
            price_df['date'] = pd.to_datetime(price_df['date'])
            price_df.set_index('date', inplace=True)
            price_df.index.name = 'Date'
            price_df = price_df[['open', 'high', 'low', 'close', 'volume']].copy()
            price_df.columns = ['Open', 'High', 'Low', 'Close', 'Volume']
        elif isinstance(data_dict, dict) and 'dates' in data_dict:
            dates = pd.to_datetime(data_dict['dates'])
            price_df = pd.DataFrame({
                'Open': data_dict['open'],
                'High': data_dict['high'],
                'Low': data_dict['low'],
                'Close': data_dict['close'],
                'Volume': data_dict['volume']
            }, index=dates)
            price_df.index.name = 'Date'
        else:
            print("❌ Invalid OHLCV format")
            return analysis, None, None
        
        print(f"✅ Loaded {len(price_df)} price data points")
        
        # Load indicators
        indicator_outputs = [
            o for o in outputs
            if 'indicator_data' in o.name.lower() and o.name.endswith('_json')
        ]
        
        indicators_data = {}
        for output in indicator_outputs:
            try:
                params = json.loads(output.text)
                if params.get('tool') == 'get_indicator_data':
                    indicator_name = params.get('indicator', 'Unknown')
                    indicator_dict = params.get('data', {})
                    
                    if isinstance(indicator_dict, dict) and 'dates' in indicator_dict and 'values' in indicator_dict:
                        dates = pd.to_datetime(indicator_dict['dates'])
                        if dates.tz is None:
                            dates = dates.tz_localize('UTC')
                        
                        display_name = indicator_name.replace('_', ' ').title()
                        indicator_df = pd.DataFrame({
                            display_name: indicator_dict['values']
                        }, index=dates)
                        indicator_df.index.name = 'Date'
                        
                        indicators_data[display_name] = indicator_df
                        print(f"✅ Loaded indicator: {display_name} ({len(indicator_df)} points)")
            except Exception as e:
                print(f"⚠️  Error loading indicator from {output.name}: {e}")
        
        print(f"✅ Total indicators loaded: {len(indicators_data)}")
        
        return analysis, price_df, indicators_data
        
    finally:
        session.close()


def build_plotly_chart(symbol: str, price_data: pd.DataFrame, indicators_data: dict, with_connectgaps: bool = True):
    """Build Plotly chart with indicators."""
    
    # Categorize indicators
    INDICATOR_CATEGORIES = {
        'close 50 sma': 'price',
        'close 200 sma': 'price',
        'close 10 ema': 'price',
        'macd': 'momentum',
        'macds': 'momentum',
        'macdh': 'momentum',
        'rsi': 'oscillator',
        'mfi': 'oscillator',
        'atr': 'momentum',
        'boll': 'price',
        'boll ub': 'price',
        'boll lb': 'price',
        'vwma': 'price',
    }
    
    price_indicators = []
    oscillators = []
    momentum_indicators = []
    
    for indicator_name in indicators_data.keys():
        indicator_lower = indicator_name.lower()
        category = INDICATOR_CATEGORIES.get(indicator_lower)
        
        if category == 'price':
            price_indicators.append(indicator_name)
        elif category == 'oscillator':
            oscillators.append(indicator_name)
        elif category == 'momentum':
            momentum_indicators.append(indicator_name)
        else:
            # Pattern matching fallback
            if any(x in indicator_lower for x in ['ma', 'ema', 'sma', 'boll', 'vwma']):
                price_indicators.append(indicator_name)
            elif any(x in indicator_lower for x in ['rsi', 'stoch', 'mfi']):
                oscillators.append(indicator_name)
            elif any(x in indicator_lower for x in ['macd', 'atr']):
                momentum_indicators.append(indicator_name)
    
    # Determine subplot structure
    subplot_titles = ['Price & Volume']
    num_rows = 1
    row_heights = [0.5]
    
    if oscillators:
        subplot_titles.append('Oscillators (RSI, etc.)')
        num_rows += 1
        row_heights.append(0.25)
    
    if momentum_indicators:
        subplot_titles.append('Momentum Indicators (MACD, etc.)')
        num_rows += 1
        row_heights.append(0.25)
    
    if num_rows > 1:
        row_heights = [h / sum(row_heights) for h in row_heights]
    
    # Create subplots
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
    
    # Use INTEGER indices for x-axis instead of datetime
    # This creates a continuous axis regardless of time gaps
    x_data = list(range(len(price_data)))
    
    # Store datetime labels for hover text
    if isinstance(price_data.index, pd.DatetimeIndex):
        datetime_labels = [dt.strftime('%Y-%m-%d %H:%M') for dt in price_data.index]
    else:
        datetime_labels = [str(i) for i in range(len(price_data))]
    
    # CRITICAL: Align all indicators to price data index to use SAME x-axis
    # This ensures perfect alignment and no gaps
    for indicator_name in list(indicators_data.keys()):
        indicator_df = indicators_data[indicator_name]
        if not indicator_df.empty and isinstance(indicator_df.index, pd.DatetimeIndex):
            # Reindex indicator to match price data index exactly
            indicator_df_aligned = indicator_df.reindex(price_data.index)
            indicators_data[indicator_name] = indicator_df_aligned
    
    # Add candlestick with custom hover text showing datetime
    fig.add_trace(
        go.Candlestick(
            x=x_data,
            open=price_data['Open'],
            high=price_data['High'],
            low=price_data['Low'],
            close=price_data['Close'],
            name='Price',
            increasing_line_color='#26a69a',
            increasing_fillcolor='#26a69a',
            decreasing_line_color='#ef5350',
            decreasing_fillcolor='#ef5350',
            showlegend=True,
            text=datetime_labels,
            hovertext=datetime_labels
        ),
        row=1, col=1,
        secondary_y=False
    )
    
    # Add volume
    if 'Volume' in price_data.columns:
        colors_vol = ['#ef5350' if close < open_ else '#26a69a' 
                     for close, open_ in zip(price_data['Close'], price_data['Open'])]
        
        fig.add_trace(
            go.Bar(
                x=x_data,
                y=price_data['Volume'],
                name='Volume',
                marker_color=colors_vol,
                opacity=0.15,
                showlegend=True,
                yaxis='y2'
            ),
            row=1, col=1,
            secondary_y=True
        )
    
    # Add price-scale indicators
    colors = ['#ff6f00', '#7c4dff', '#00c853', '#d500f9', '#0091ea']
    color_idx = 0
    
    for indicator_name in price_indicators:
        indicator_df = indicators_data[indicator_name]
        if indicator_df.empty:
            continue
        
        value_col = indicator_df.columns[0]
        
        # Use SAME x-axis as price data (already aligned via reindex)
        indicator_x = x_data
        
        trace_kwargs = {
            'x': indicator_x,
            'y': indicator_df[value_col],
            'mode': 'lines',
            'name': indicator_name,
            'line': dict(width=1.5, color=colors[color_idx % len(colors)]),
            'opacity': 0.8,
            'showlegend': True
        }
        
        if with_connectgaps:
            trace_kwargs['connectgaps'] = True
        
        fig.add_trace(
            go.Scatter(**trace_kwargs),
            row=1, col=1,
            secondary_y=False
        )
        color_idx += 1
    
    # Add oscillators
    current_row = 2
    if oscillators:
        color_idx = 0
        for indicator_name in oscillators:
            indicator_df = indicators_data[indicator_name]
            if indicator_df.empty:
                continue
            
            value_col = indicator_df.columns[0]
            
            # Use SAME x-axis as price data (already aligned via reindex)
            indicator_x = x_data
            
            trace_kwargs = {
                'x': indicator_x,
                'y': indicator_df[value_col],
                'mode': 'lines',
                'name': indicator_name,
                'line': dict(width=2, color=colors[color_idx % len(colors)]),
                'showlegend': True
            }
            
            if with_connectgaps:
                trace_kwargs['connectgaps'] = True
            
            fig.add_trace(
                go.Scatter(**trace_kwargs),
                row=current_row, col=1
            )
            color_idx += 1
        
        # Reference lines
        if len(x_data) > 0:
            fig.add_hline(y=70, line_dash="dash", line_color="red", opacity=0.5, row=current_row, col=1)
            fig.add_hline(y=30, line_dash="dash", line_color="green", opacity=0.5, row=current_row, col=1)
            fig.add_hline(y=50, line_dash="dot", line_color="gray", opacity=0.3, row=current_row, col=1)
        
        current_row += 1
    
    # Add momentum indicators
    if momentum_indicators:
        color_idx = 0
        for indicator_name in momentum_indicators:
            indicator_df = indicators_data[indicator_name]
            if indicator_df.empty:
                continue
            
            for col in indicator_df.columns:
                if indicator_df[col].dtype in ['float64', 'int64']:
                    # Use SAME x-axis as price data (already aligned via reindex)
                    indicator_x = x_data
                    
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
                        trace_kwargs = {
                            'x': indicator_x,
                            'y': indicator_df[col],
                            'mode': 'lines',
                            'name': f"{indicator_name} - {col}",
                            'line': dict(width=2, color=colors[color_idx % len(colors)]),
                            'showlegend': True
                        }
                        
                        if with_connectgaps:
                            trace_kwargs['connectgaps'] = True
                        
                        fig.add_trace(
                            go.Scatter(**trace_kwargs),
                            row=current_row, col=1
                        )
                    color_idx += 1
        
        # Zero line
        fig.add_hline(y=0, line_dash="dash", line_color="gray", opacity=0.5, row=current_row, col=1)
    
    # Update layout
    fig.update_layout(
        title={
            'text': f'{symbol} - Price Action & Technical Indicators',
            'x': 0.5,
            'xanchor': 'center',
            'font': {'size': 20, 'color': '#e2e8f0'}
        },
        height=700,
        autosize=True,
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
        margin=dict(l=60, r=200, t=80, b=60),
        paper_bgcolor='rgba(0,0,0,0)',
        plot_bgcolor='rgba(0,0,0,0)',
        dragmode='pan',
        xaxis_rangeslider_visible=False,
        hoverlabel=dict(
            bgcolor='#1a1f2e',
            font_size=12,
            font_color='#e2e8f0',
            bordercolor='#3d4a5c'
        )
    )
    
    # Update axes - use tickvals and ticktext to show datetime labels
    fig.update_xaxes(
        title_text="Date",
        title_font=dict(color='#a0aec0'),
        showgrid=True,
        gridwidth=1,
        gridcolor='rgba(160,174,192,0.15)',
        tickfont=dict(color='#a0aec0'),
        tickangle=-45,
        # Show datetime labels at intervals
        tickmode='array',
        tickvals=list(range(0, len(datetime_labels), max(1, len(datetime_labels)//20))),
        ticktext=[datetime_labels[i] for i in range(0, len(datetime_labels), max(1, len(datetime_labels)//20))]
    )
    
    fig.update_yaxes(
        title_text="Price ($)",
        title_font=dict(color='#a0aec0'),
        showgrid=True,
        gridwidth=1,
        gridcolor='rgba(160,174,192,0.15)',
        tickfont=dict(color='#a0aec0'),
        secondary_y=False,
        row=1
    )
    
    fig.update_yaxes(
        title_text="Volume",
        title_font=dict(color='#a0aec0'),
        tickfont=dict(color='#a0aec0'),
        showgrid=False,
        secondary_y=True,
        row=1
    )
    
    return fig


def main():
    if len(sys.argv) < 2:
        print("Usage: python test_files/generate_analysis_chart.py <analysis_id>")
        print("\nExample: python test_files/generate_analysis_chart.py 9710")
        sys.exit(1)
    
    try:
        analysis_id = int(sys.argv[1])
    except ValueError:
        print(f"Error: Invalid analysis ID '{sys.argv[1]}'. Must be an integer.")
        sys.exit(1)
    
    print(f"\n{'='*80}")
    print(f"GENERATING CHART FOR ANALYSIS #{analysis_id}")
    print(f"{'='*80}\n")
    
    # Load data
    analysis, price_data, indicators_data = load_analysis_data(analysis_id)
    
    if price_data is None or indicators_data is None:
        print("❌ Failed to load data")
        sys.exit(1)
    
    # Generate chart WITH connectgaps=True
    print("\n📊 Generating chart WITH connectgaps=True...")
    fig_with = build_plotly_chart(analysis.symbol, price_data, indicators_data, with_connectgaps=True)
    output_file_with = f"test_files/analysis_{analysis_id}_with_connectgaps.html"
    fig_with.write_html(output_file_with)
    print(f"✅ Saved: {output_file_with}")
    
    # Generate chart WITHOUT connectgaps (for comparison)
    print("\n📊 Generating chart WITHOUT connectgaps (for comparison)...")
    fig_without = build_plotly_chart(analysis.symbol, price_data, indicators_data, with_connectgaps=False)
    output_file_without = f"test_files/analysis_{analysis_id}_without_connectgaps.html"
    fig_without.write_html(output_file_without)
    print(f"✅ Saved: {output_file_without}")
    
    print(f"\n{'='*80}")
    print("CHART GENERATION COMPLETE")
    print(f"{'='*80}")
    print("\nGenerated 2 files for comparison:")
    print(f"  1. WITH connectgaps=True:    {output_file_with}")
    print(f"  2. WITHOUT connectgaps:       {output_file_without}")
    print("\nOpen these HTML files in your browser to compare the difference.")
    print("The WITH connectgaps version should show continuous indicator lines.\n")


if __name__ == "__main__":
    main()
