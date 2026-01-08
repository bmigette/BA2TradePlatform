"""
Test to exactly replicate what the UI does when loading and rendering indicators.
This simulates the TradingAgentsUI.py _render_data_visualization_panel() flow.
"""
import sys
import os
import pandas as pd
import json
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from ba2_trade_platform.core.db import get_db
from ba2_trade_platform.core.models import MarketAnalysis, AnalysisOutput
from ba2_trade_platform.logger import logger
from sqlmodel import select

# Load analysis 9710
analysis_id = 9710
session = get_db()

try:
    analysis = session.get(MarketAnalysis, analysis_id)
    
    statement = select(AnalysisOutput).where(
        AnalysisOutput.market_analysis_id == analysis_id
    )
    outputs = list(session.exec(statement).all())
    
    print(f"Loaded {len(outputs)} outputs for analysis {analysis_id}")
    
    # SIMULATING UI'S EXACT FLOW FROM LINE 871-956 of TradingAgentsUI.py
    use_stored = True  # Checkbox is checked
    
    indicators_data = {}
    
    if use_stored:
        print("\n✅ Using stored indicators from database (checkbox checked)")
        
        for output in outputs:
            output_obj = output
            is_indicator_output = (
                'tool_output_get_indicator_data' in output_obj.name.lower() or
                'tool_output_get_stockstats_indicators' in output_obj.name.lower()
            )
            
            if is_indicator_output:
                try:
                    if output_obj.name.endswith('_json') and output_obj.text:
                        params = json.loads(output_obj.text)
                        
                        print(f"\n📊 Processing: {output_obj.name}")
                        print(f"   Tool: {params.get('tool', 'N/A')}")
                        
                        if params.get('tool') == 'get_indicator_data':
                            indicator_name = params.get('indicator', 'Unknown')
                            indicator_name_display = indicator_name.replace('_', ' ').title()
                            
                            indicator_data = params.get('data', {})
                            
                            if isinstance(indicator_data, dict):
                                if 'dates' in indicator_data and 'values' in indicator_data:
                                    dates = pd.to_datetime(indicator_data['dates'])
                                    values = indicator_data['values']
                                    
                                    print(f"   Dates: {len(dates)} entries")
                                    print(f"   Values: {len(values)} entries")
                                    print(f"   First date: {dates[0]}")
                                    print(f"   Last date: {dates[-1]}")
                                    print(f"   Date TZ before: {dates.tz}")
                                    
                                    indicator_df = pd.DataFrame({
                                        indicator_name_display: values
                                    }, index=dates)
                                    indicator_df.index.name = 'Date'
                                    
                                    # THIS IS WHAT THE UI DOES - Make timezone-aware to match price data (UTC)
                                    if indicator_df.index.tz is None:
                                        indicator_df.index = indicator_df.index.tz_localize('UTC')
                                        print(f"   ⚠️  Localized to UTC")
                                    
                                    print(f"   Final index TZ: {indicator_df.index.tz}")
                                    print(f"   Final index first: {indicator_df.index[0]}")
                                    print(f"   Final index last: {indicator_df.index[-1]}")
                                    
                                    # Check for NaN values
                                    nan_count = indicator_df[indicator_name_display].isna().sum()
                                    print(f"   NaN values: {nan_count}/{len(indicator_df)}")
                                    
                                    # Check date continuity
                                    date_diffs = indicator_df.index.to_series().diff()
                                    large_gaps = date_diffs[date_diffs > pd.Timedelta(days=4)]
                                    if len(large_gaps) > 0:
                                        print(f"   ⚠️  {len(large_gaps)} large date gaps detected!")
                                        for gap_date, gap_size in list(large_gaps.items())[:3]:
                                            print(f"      - Gap at {gap_date}: {gap_size}")
                                    else:
                                        print(f"   ✅ No large date gaps")
                                    
                                    indicators_data[indicator_name_display] = indicator_df
                                    print(f"   ✅ Loaded indicator '{indicator_name_display}': {len(indicator_df)} rows")
                                    
                except Exception as e:
                    print(f"   ❌ Error: {e}")
                    logger.error(f"Error parsing indicator: {e}", exc_info=True)
    
    print(f"\n\n{'='*80}")
    print(f"SUMMARY")
    print(f"{'='*80}")
    print(f"Total indicators loaded: {len(indicators_data)}")
    for name, df in indicators_data.items():
        print(f"\n{name}:")
        print(f"  Rows: {len(df)}")
        print(f"  Index TZ: {df.index.tz}")
        print(f"  NaN count: {df[df.columns[0]].isna().sum()}")
        print(f"  First value: {df[df.columns[0]].iloc[0] if len(df) > 0 else 'N/A'}")
        print(f"  Last value: {df[df.columns[0]].iloc[-1] if len(df) > 0 else 'N/A'}")

finally:
    session.close()
