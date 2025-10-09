"""
Test script to verify datetime formatting in provider outputs.
Tests both dict and markdown formats for various intervals.
"""

from datetime import datetime, timedelta
from ba2_trade_platform.modules.dataproviders.ohlcv import YFinanceDataProvider

def test_ohlcv_formatting():
    """Test OHLCV provider datetime formatting."""
    print("=" * 80)
    print("Testing OHLCV Provider Datetime Formatting")
    print("=" * 80)
    
    provider = YFinanceDataProvider()
    symbol = "AAPL"
    end_date = datetime.now()
    start_date = end_date - timedelta(days=5)
    
    # Test daily interval (should show date only in markdown)
    print("\n1. Testing 1d interval (daily):")
    print("-" * 40)
    
    result = provider.get_ohlcv_data_formatted(
        symbol=symbol,
        start_date=start_date,
        end_date=end_date,
        interval="1d",
        format_type="both"
    )
    
    # Check dict format
    print("Dict format (should have ISO strings):")
    if result['data']['data']:
        sample = result['data']['data'][0]
        print(f"  Date format: {sample['date']}")
        print(f"  Is ISO format: {len(sample['date']) > 10}")  # ISO includes time
    
    # Check markdown format
    print("\nMarkdown format (should show date only for 1d):")
    lines = result['text'].split('\n')
    for line in lines[:15]:  # Show first 15 lines
        if '|' in line and 'AAPL' not in line:
            print(f"  {line}")
    
    print("\n" + "=" * 80)
    print("✅ Test completed. Verify:")
    print("  1. Dict dates are ISO strings (YYYY-MM-DDTHH:MM:SS)")
    print("  2. Markdown dates are date-only for 1d interval (YYYY-MM-DD)")
    print("=" * 80)

def test_intraday_formatting():
    """Test intraday interval formatting."""
    print("\n" + "=" * 80)
    print("Testing Intraday Interval Formatting (1h)")
    print("=" * 80)
    
    provider = YFinanceDataProvider()
    
    # Note: YFinance 1h data requires recent dates
    end_date = datetime.now()
    start_date = end_date - timedelta(days=2)
    
    try:
        result = provider.get_ohlcv_data_formatted(
            symbol="AAPL",
            start_date=start_date,
            end_date=end_date,
            interval="1h",
            format_type="both"
        )
        
        print("\nDict format:")
        if result['data']['data']:
            sample = result['data']['data'][0]
            print(f"  Date format: {sample['date']}")
        
        print("\nMarkdown format (should show date AND time for 1h):")
        lines = result['text'].split('\n')
        for line in lines[5:12]:  # Show some data rows
            if '|' in line and 'DateTime' in line:
                print(f"  {line}")
            elif '|' in line and '$' in line:
                print(f"  {line}")
                break
        
    except Exception as e:
        print(f"⚠️  Could not fetch 1h data (expected for older dates): {e}")
    
    print("\n" + "=" * 80)

if __name__ == "__main__":
    test_ohlcv_formatting()
    test_intraday_formatting()
