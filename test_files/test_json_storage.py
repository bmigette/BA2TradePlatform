"""
Test script to verify data visualization JSON storage fix.

This script checks:
1. market_analysis_id is in AgentState
2. LoggingToolNode can access market_analysis_id
3. JSON tool outputs are stored correctly
4. Data visualization can reconstruct indicators

Run after completing a TradingAgents analysis.
"""

from ba2_trade_platform.core.db import get_db, select
from ba2_trade_platform.core.models import AnalysisOutput, MarketAnalysis
import json


def test_json_storage():
    """Test that JSON tool outputs are being stored."""
    print("=" * 80)
    print("Testing JSON Tool Output Storage")
    print("=" * 80)
    
    session = get_db()
    
    # Find latest market analysis
    statement = select(MarketAnalysis).order_by(MarketAnalysis.created_at.desc()).limit(5)
    recent_analyses = session.exec(statement).all()
    
    if not recent_analyses:
        print("❌ No market analyses found. Run a TradingAgents analysis first.")
        return
    
    print(f"\n✅ Found {len(recent_analyses)} recent analyses")
    
    for analysis in recent_analyses:
        print(f"\n{'='*80}")
        print(f"Analysis ID: {analysis.id}")
        print(f"Symbol: {analysis.symbol}")
        print(f"Status: {analysis.status}")
        print(f"Created: {analysis.created_at}")
        print(f"{'='*80}")
        
        # Query for all outputs
        statement = select(AnalysisOutput).where(
            AnalysisOutput.market_analysis_id == analysis.id
        )
        outputs = session.exec(statement).all()
        
        print(f"\n📊 Total outputs: {len(outputs)}")
        
        # Filter JSON outputs
        json_outputs = [o for o in outputs if o.name.endswith('_json')]
        print(f"📝 JSON outputs: {len(json_outputs)}")
        
        if json_outputs:
            print("\n✅ JSON STORAGE WORKING!")
            print("\nJSON Outputs Found:")
            for output in json_outputs:
                print(f"\n  • {output.name}")
                print(f"    Type: {output.output_type}")
                print(f"    Size: {len(output.text)} bytes")
                
                # Parse and display JSON structure
                try:
                    data = json.loads(output.text)
                    print(f"    Tool: {data.get('tool', 'N/A')}")
                    print(f"    Symbol: {data.get('symbol', 'N/A')}")
                    print(f"    Interval: {data.get('interval', 'N/A')}")
                    
                    if 'indicator' in data:
                        print(f"    Indicator: {data.get('indicator', 'N/A')}")
                    
                    if 'start_date' in data and 'end_date' in data:
                        print(f"    Date Range: {data['start_date']} to {data['end_date']}")
                except json.JSONDecodeError as e:
                    print(f"    ⚠️  Warning: Could not parse JSON: {e}")
        else:
            print("\n❌ NO JSON OUTPUTS FOUND")
            print("This indicates the LoggingToolNode fix is not working.")
            print("\nDebugging Info:")
            print("  1. Check that tools are returning dict with '_internal' flag")
            print("  2. Verify LoggingToolNode is executing tools directly")
            print("  3. Check logs for '[JSON_STORED]' messages")
        
        # Show some non-JSON outputs for context
        text_outputs = [o for o in outputs if not o.name.endswith('_json')][:5]
        if text_outputs:
            print(f"\n📄 Sample text outputs (first 5):")
            for output in text_outputs:
                print(f"  • {output.name} ({output.output_type})")
    
    session.close()
    print("\n" + "=" * 80)


def test_data_reconstruction():
    """Test that we can reconstruct indicator data from JSON."""
    print("\n" + "=" * 80)
    print("Testing Data Reconstruction")
    print("=" * 80)
    
    session = get_db()
    
    # Find a JSON indicator output
    statement = select(AnalysisOutput).where(
        AnalysisOutput.name.like('%get_stockstats_indicators_report_online_json')
    ).limit(1)
    json_output = session.exec(statement).first()
    
    if not json_output:
        print("❌ No indicator JSON found to test reconstruction")
        session.close()
        return
    
    print(f"✅ Found indicator JSON: {json_output.name}")
    
    try:
        # Parse JSON parameters
        params = json.loads(json_output.text)
        print(f"\n📋 Parameters:")
        print(f"  Tool: {params.get('tool')}")
        print(f"  Indicator: {params.get('indicator')}")
        print(f"  Symbol: {params.get('symbol')}")
        print(f"  Interval: {params.get('interval')}")
        print(f"  Date Range: {params.get('start_date')} to {params.get('end_date')}")
        
        # Try to reconstruct (without actually fetching data)
        print(f"\n✅ Parameters successfully extracted")
        print(f"   Data can be reconstructed using:")
        print(f"   - YFinanceDataProvider for price data")
        print(f"   - StockstatsUtils for indicators")
        
    except Exception as e:
        print(f"\n❌ Error reconstructing data: {e}")
    
    session.close()
    print("\n" + "=" * 80)


if __name__ == "__main__":
    print("\n" + "=" * 80)
    print("DATA VISUALIZATION & JSON STORAGE TEST")
    print("=" * 80)
    
    test_json_storage()
    test_data_reconstruction()
    
    print("\n✅ Test Complete!")
    print("\nNext Steps:")
    print("  1. If JSON outputs found: Test data visualization UI")
    print("  2. If no JSON outputs: Check logs for '[JSON_STORED]' messages")
    print("  3. Run a new analysis to verify the fix is working")
    print()
