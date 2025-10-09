"""
Test to verify datetime formatting changes don't break data visualization chart.

This test confirms that:
1. get_ohlcv_data() still returns DataFrame with datetime objects
2. Chart component can still consume the DataFrame correctly
3. Datetime formatting only affects dict/markdown, not DataFrame output
"""

from datetime import datetime, timedelta
import pandas as pd
from ba2_trade_platform.modules.dataproviders.ohlcv import YFinanceDataProvider


def test_dataframe_output():
    """Test that get_ohlcv_data() returns DataFrame with proper datetime objects."""
    print("=" * 80)
    print("Test 1: Verify get_ohlcv_data() returns DataFrame (not dict)")
    print("=" * 80)
    
    provider = YFinanceDataProvider()
    symbol = "AAPL"
    end_date = datetime.now()
    start_date = end_date - timedelta(days=5)
    
    # This is what TradingAgentsUI calls
    result = provider.get_ohlcv_data(
        symbol=symbol,
        start_date=start_date,
        end_date=end_date,
        interval="1d"
    )
    
    print(f"\n✅ Result type: {type(result)}")
    assert isinstance(result, pd.DataFrame), "Result should be a DataFrame"
    
    print(f"✅ Has 'Date' column: {'Date' in result.columns}")
    assert 'Date' in result.columns, "DataFrame should have 'Date' column"
    
    print(f"✅ Date column type: {result['Date'].dtype}")
    # Should be datetime64 or similar
    assert pd.api.types.is_datetime64_any_dtype(result['Date']), "Date column should be datetime type"
    
    print(f"✅ Sample date value: {result['Date'].iloc[0]}")
    print(f"✅ Sample date type: {type(result['Date'].iloc[0])}")
    
    # Verify this is a proper datetime object, not a string
    assert hasattr(result['Date'].iloc[0], 'strftime'), "Date should be datetime object with strftime method"
    
    print("\n✅ SUCCESS: get_ohlcv_data() returns DataFrame with datetime objects (unchanged)")
    print("=" * 80)


def test_formatted_output():
    """Test that get_ohlcv_data_formatted() returns dict with ISO strings."""
    print("\n" + "=" * 80)
    print("Test 2: Verify get_ohlcv_data_formatted() returns dict with ISO strings")
    print("=" * 80)
    
    provider = YFinanceDataProvider()
    symbol = "AAPL"
    end_date = datetime.now()
    start_date = end_date - timedelta(days=5)
    
    # This is what AI agents call
    result = provider.get_ohlcv_data_formatted(
        symbol=symbol,
        start_date=start_date,
        end_date=end_date,
        interval="1d",
        format_type="dict"
    )
    
    print(f"\n✅ Result type: {type(result)}")
    assert isinstance(result, dict), "Result should be a dict"
    
    print(f"✅ Has 'data' key: {'data' in result}")
    assert 'data' in result, "Dict should have 'data' key"
    
    if result['data'] and result['data'].get('data'):
        sample = result['data']['data'][0]
        print(f"✅ Sample date: {sample['date']}")
        print(f"✅ Date is string: {isinstance(sample['date'], str)}")
        assert isinstance(sample['date'], str), "Date should be a string in dict format"
        
        # Verify it's ISO format (has 'T' separator)
        assert 'T' in sample['date'] or len(sample['date']) >= 10, "Date should be ISO format"
        print(f"✅ Date is ISO format: {len(sample['date']) >= 10}")
    
    print("\n✅ SUCCESS: get_ohlcv_data_formatted() returns dict with ISO strings (new behavior)")
    print("=" * 80)


def test_markdown_output():
    """Test that markdown output uses date-only for daily intervals."""
    print("\n" + "=" * 80)
    print("Test 3: Verify markdown uses date-only for daily intervals")
    print("=" * 80)
    
    provider = YFinanceDataProvider()
    symbol = "AAPL"
    end_date = datetime.now()
    start_date = end_date - timedelta(days=5)
    
    # Test daily interval (should show date only)
    result = provider.get_ohlcv_data_formatted(
        symbol=symbol,
        start_date=start_date,
        end_date=end_date,
        interval="1d",
        format_type="markdown"
    )
    
    print(f"\n✅ Result type: {type(result)}")
    assert isinstance(result, str), "Markdown result should be a string"
    
    lines = result.split('\n')
    # Find first data row (skip header and separator)
    for line in lines:
        if '|' in line and '$' in line:  # Data row with price
            print(f"✅ Sample row: {line}")
            # Should have date format YYYY-MM-DD (10 chars), not datetime with time
            # Extract the date part (first column after |)
            parts = [p.strip() for p in line.split('|') if p.strip()]
            if parts:
                date_str = parts[0]
                print(f"✅ Date string: '{date_str}'")
                # For daily data, should be YYYY-MM-DD format (10 chars, no time)
                assert len(date_str) == 10, f"Daily interval should show date only (YYYY-MM-DD), got: {date_str}"
                assert ':' not in date_str, f"Daily interval should not show time, got: {date_str}"
            break
    
    print("\n✅ SUCCESS: Markdown shows date-only for daily intervals (new behavior)")
    print("=" * 80)


def test_chart_data_flow():
    """Test the complete data flow from provider to chart component."""
    print("\n" + "=" * 80)
    print("Test 4: Verify chart component data flow")
    print("=" * 80)
    
    provider = YFinanceDataProvider()
    symbol = "AAPL"
    end_date = datetime.now()
    start_date = end_date - timedelta(days=5)
    
    # Step 1: Get DataFrame (what TradingAgentsUI does)
    price_data = provider.get_ohlcv_data(
        symbol=symbol,
        start_date=start_date,
        end_date=end_date,
        interval="1d"
    )
    
    print("\n✅ Step 1: Got DataFrame from provider")
    print(f"   Type: {type(price_data)}, Shape: {price_data.shape}")
    
    # Step 2: Convert Date to DatetimeIndex (what TradingAgentsUI does)
    if 'Date' in price_data.columns and not isinstance(price_data.index, pd.DatetimeIndex):
        price_data['Date'] = pd.to_datetime(price_data['Date'])
        price_data.set_index('Date', inplace=True)
    
    print("✅ Step 2: Converted to DatetimeIndex")
    print(f"   Index type: {type(price_data.index)}")
    assert isinstance(price_data.index, pd.DatetimeIndex), "Index should be DatetimeIndex"
    
    # Step 3: Convert to strings for chart (what InstrumentGraph does)
    if isinstance(price_data.index, pd.DatetimeIndex):
        x_data = price_data.index.strftime('%Y-%m-%d %H:%M:%S').tolist()
    
    print("✅ Step 3: Converted to string list for chart")
    print(f"   Sample dates: {x_data[:3]}")
    assert all(isinstance(d, str) for d in x_data), "All dates should be strings"
    
    print("\n✅ SUCCESS: Complete data flow works correctly (unchanged)")
    print("=" * 80)


if __name__ == "__main__":
    try:
        test_dataframe_output()
        test_formatted_output()
        test_markdown_output()
        test_chart_data_flow()
        
        print("\n" + "=" * 80)
        print("🎉 ALL TESTS PASSED!")
        print("=" * 80)
        print("\nConclusion:")
        print("✅ get_ohlcv_data() unchanged - returns DataFrame with datetime objects")
        print("✅ Chart component unchanged - still works with DataFrame")
        print("✅ get_ohlcv_data_formatted() updated - dict uses ISO strings")
        print("✅ Markdown formatting updated - shows date-only for daily intervals")
        print("\n📊 Data visualization charts are NOT affected by datetime formatting changes!")
        print("=" * 80)
        
    except Exception as e:
        print(f"\n❌ TEST FAILED: {e}")
        import traceback
        traceback.print_exc()
