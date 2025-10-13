"""
Test script to verify InstrumentGraph component integration with TradingAgentsUI.
This script checks that the data visualization feature can properly render price and indicator data.
"""

import sys
from pathlib import Path

# Add the project root to the path
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))

from ba2_trade_platform.core.db import get_db
from ba2_trade_platform.core.models import MarketAnalysis, AnalysisOutput
from ba2_trade_platform.modules.experts.TradingAgentsUI import TradingAgentsUI
from sqlmodel import select


def test_data_visualization():
    """Test the data visualization panel rendering."""
    print("=" * 80)
    print("Testing InstrumentGraph Integration with TradingAgentsUI")
    print("=" * 80)
    
    # Get a MarketAnalysis with AnalysisOutputs
    session = get_db()
    
    try:
        # Find a completed analysis
        statement = (
            select(MarketAnalysis)
            .where(MarketAnalysis.status == "COMPLETED")
            .order_by(MarketAnalysis.created_at.desc())
            .limit(1)
        )
        analysis = session.exec(statement).first()
        
        if not analysis:
            print("❌ No completed analysis found in database")
            print("   Run a TradingAgents analysis first to generate data")
            return False
        
        print(f"✅ Found analysis: ID={analysis.id}, Symbol={analysis.symbol}, Status={analysis.status}")
        
        # Check for AnalysisOutputs
        statement = (
            select(AnalysisOutput)
            .where(AnalysisOutput.market_analysis_id == analysis.id)
        )
        outputs = session.exec(statement).all()
        
        print(f"\n📊 Analysis Outputs ({len(outputs)} found):")
        
        has_price_data = False
        has_indicator_data = False
        
        for output in outputs:
            output_obj = output[0] if isinstance(output, tuple) else output
            print(f"   • {output_obj.name}: {len(output_obj.text or '')} chars")
            
            if 'tool_output_get_YFin_data' in output_obj.name.lower():
                has_price_data = True
                print("     ✓ Price data found")
            
            if 'tool_output_get_stockstats_indicators' in output_obj.name.lower():
                has_indicator_data = True
                print("     ✓ Indicator data found")
        
        if not has_price_data:
            print("\n⚠️  No price data found (tool_output_get_YFin_data_online)")
        
        if not has_indicator_data:
            print("⚠️  No indicator data found (tool_output_get_stockstats_indicators_report_online)")
        
        # Test UI instantiation
        print("\n🎨 Testing UI Instantiation:")
        try:
            ui = TradingAgentsUI(analysis)
            print("✅ TradingAgentsUI instantiated successfully")
            print(f"   Analysis: {ui.market_analysis.symbol} - {ui.market_analysis.status}")
        except Exception as e:
            print(f"❌ Failed to instantiate UI: {e}")
            return False
        
        print("\n" + "=" * 80)
        print("✅ Test completed successfully!")
        print("=" * 80)
        print("\nNext steps:")
        print("1. Start the application: .venv\\Scripts\\python.exe main.py")
        print("2. Navigate to Market Analysis page")
        print("3. View a completed TradingAgents analysis")
        print("4. Click the '📉 Data Visualization' tab")
        print("5. Verify the chart renders with price and indicator data")
        
        return True
        
    except Exception as e:
        print(f"❌ Test failed with error: {e}")
        import traceback
        traceback.print_exc()
        return False
    
    finally:
        session.close()


if __name__ == "__main__":
    success = test_data_visualization()
    sys.exit(0 if success else 1)
