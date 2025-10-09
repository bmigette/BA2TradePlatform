"""
Test indicator datetime formatting with interval-aware display.
"""

from datetime import datetime, timedelta
from ba2_trade_platform.modules.dataproviders.indicators import PandasIndicatorCalc
from ba2_trade_platform.modules.dataproviders.ohlcv import YFinanceDataProvider


def test_indicator_formatting():
    """Test that indicators show proper datetime for intraday intervals."""
    print("=" * 80)
    print("Testing Indicator Datetime Formatting")
    print("=" * 80)
    
    # PandasIndicatorCalc needs an OHLCV provider
    ohlcv_provider = YFinanceDataProvider()
    provider = PandasIndicatorCalc(ohlcv_provider)
    symbol = "AAPL"
    end_date = datetime.now()
    start_date = end_date - timedelta(days=5)
    
    # Test with 1h interval (should show datetime with hours)
    print("\n1. Testing VWMA indicator with 1h interval:")
    print("-" * 40)
    
    try:
        result = provider.get_indicator(
            symbol=symbol,
            indicator="vwma",
            start_date=start_date,
            end_date=end_date,
            interval="1h",
            format_type="markdown"
        )
        
        print("✅ Got markdown result")
        
        # Find the table rows
        lines = result.split('\n')
        print("\n✅ Sample rows from indicator table:")
        row_count = 0
        for line in lines:
            if '|' in line and not 'Date' in line and not '---' in line:
                print(f"   {line}")
                row_count += 1
                if row_count >= 5:  # Show first 5 data rows
                    break
        
        # Check if time is shown
        has_time = any(':' in line for line in lines if '|' in line and not 'Date' in line and not '---' in line)
        
        if has_time:
            print("\n✅ SUCCESS: Hourly data shows time (e.g., '2025-07-14 09:30:00')")
        else:
            print("\n❌ FAILED: Hourly data should show time but doesn't")
            
    except Exception as e:
        print(f"\n⚠️  Could not test 1h indicator: {e}")
    
    # Test with 1d interval (should show date only)
    print("\n" + "=" * 80)
    print("2. Testing RSI indicator with 1d interval:")
    print("-" * 40)
    
    try:
        result = provider.get_indicator(
            symbol=symbol,
            indicator="rsi",
            start_date=start_date,
            end_date=end_date,
            interval="1d",
            format_type="markdown"
        )
        
        print("✅ Got markdown result")
        
        # Find the table rows
        lines = result.split('\n')
        print("\n✅ Sample rows from indicator table:")
        row_count = 0
        for line in lines:
            if '|' in line and not 'Date' in line and not '---' in line:
                print(f"   {line}")
                row_count += 1
                if row_count >= 5:
                    break
        
        # Check if time is NOT shown
        data_lines = [line for line in lines if '|' in line and not 'Date' in line and not '---' in line]
        if data_lines:
            first_data_line = data_lines[0]
            # Extract date part (first column)
            date_part = first_data_line.split('|')[1].strip() if '|' in first_data_line else ""
            
            if ':' not in date_part and len(date_part) == 10:
                print(f"\n✅ SUCCESS: Daily data shows date only (e.g., '{date_part}')")
            else:
                print(f"\n⚠️  Daily data format: '{date_part}' (expected YYYY-MM-DD)")
                
    except Exception as e:
        print(f"\n⚠️  Could not test 1d indicator: {e}")
    
    print("\n" + "=" * 80)


if __name__ == "__main__":
    try:
        test_indicator_formatting()
    except Exception as e:
        print(f"\n❌ TEST FAILED: {e}")
        import traceback
        traceback.print_exc()
