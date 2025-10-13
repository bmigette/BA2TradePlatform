"""
Check datetime storage in tool outputs to verify timeframe support
"""
import sys
sys.path.insert(0, '.')

from ba2_trade_platform.core.db import get_db
from ba2_trade_platform.core.models import AnalysisOutput
from sqlmodel import select

def check_tool_outputs():
    """Check if datetime information is properly stored in tool outputs."""
    print("=" * 80)
    print("CHECKING TOOL OUTPUT DATETIME STORAGE")
    print("=" * 80)
    
    session = get_db()
    try:
        # Check YFin data output
        print("\n1. Checking YFin Price Data Outputs:")
        print("-" * 80)
        statement = select(AnalysisOutput).where(AnalysisOutput.name.contains('YFin_data')).limit(3)
        outputs = session.exec(statement).all()
        
        if not outputs:
            print("❌ No YFin data outputs found in database")
        else:
            for i, output in enumerate(outputs, 1):
                output_obj = output[0] if isinstance(output, tuple) else output
                print(f"\nOutput #{i}:")
                print(f"  Name: {output_obj.name}")
                print(f"  Created: {output_obj.created_at}")
                print(f"  Length: {len(output_obj.text or '')} chars")
                
                # Check first 500 characters for datetime format
                sample = output_obj.text[:500] if output_obj.text else ""
                print(f"\n  Sample (first 500 chars):")
                print(f"  {sample}")
                
                # Check if it contains time information
                if any(time_marker in sample for time_marker in [' 09:', ' 10:', ' 11:', ' 12:', ' 13:', ' 14:', ' 15:', ' 16:']):
                    print(f"  ✅ Contains intraday time information!")
                else:
                    print(f"  ℹ️  Appears to be daily data (no intraday times found)")
        
        # Check stockstats indicator outputs
        print("\n\n2. Checking Stockstats Indicator Outputs:")
        print("-" * 80)
        statement = select(AnalysisOutput).where(AnalysisOutput.name.contains('stockstats_indicators')).limit(3)
        outputs = session.exec(statement).all()
        
        if not outputs:
            print("❌ No stockstats indicator outputs found in database")
        else:
            for i, output in enumerate(outputs, 1):
                output_obj = output[0] if isinstance(output, tuple) else output
                print(f"\nOutput #{i}:")
                print(f"  Name: {output_obj.name}")
                print(f"  Created: {output_obj.created_at}")
                print(f"  Length: {len(output_obj.text or '')} chars")
                
                # Check first 500 characters
                sample = output_obj.text[:500] if output_obj.text else ""
                print(f"\n  Sample (first 500 chars):")
                print(f"  {sample}")
        
        print("\n\n" + "=" * 80)
        print("ANALYSIS COMPLETE")
        print("=" * 80)
        
        # Summary
        print("\nSummary:")
        print("- If you see time information (HH:MM:SS) in YFin data, timeframe support is working!")
        print("- If you only see dates (YYYY-MM-DD), data is daily (1d interval)")
        print("- For intraday strategies, make sure expert timeframe setting is configured")
        
    finally:
        session.close()

if __name__ == "__main__":
    check_tool_outputs()
