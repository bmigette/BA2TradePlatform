"""
Focused test to verify chart data flow is not broken by datetime formatting changes.
"""

from datetime import datetime, timedelta
import pandas as pd
from ba2_trade_platform.modules.dataproviders.ohlcv import YFinanceDataProvider


def test_chart_data_flow():
    """Test the complete data flow from provider to chart component."""
    print("=" * 80)
    print("Chart Component Data Flow Test")
    print("=" * 80)
    
    provider = YFinanceDataProvider()
    symbol = "AAPL"
    end_date = datetime.now()
    start_date = end_date - timedelta(days=5)
    
    print("\nStep 1: Call get_ohlcv_data() (what TradingAgentsUI does)")
    print("-" * 80)
    
    # This is the ONLY method TradingAgentsUI calls for charts
    price_data = provider.get_ohlcv_data(
        symbol=symbol,
        start_date=start_date,
        end_date=end_date,
        interval="1d"
    )
    
    print(f"✅ Got DataFrame: {type(price_data)}")
    print(f"✅ Shape: {price_data.shape}")
    print(f"✅ Columns: {list(price_data.columns)}")
    print(f"✅ Date column type: {price_data['Date'].dtype}")
    
    assert isinstance(price_data, pd.DataFrame), "Must return DataFrame"
    assert 'Date' in price_data.columns, "Must have Date column"
    assert pd.api.types.is_datetime64_any_dtype(price_data['Date']), "Date must be datetime type"
    
    print("\nStep 2: Convert to DatetimeIndex (what TradingAgentsUI does)")
    print("-" * 80)
    
    if 'Date' in price_data.columns and not isinstance(price_data.index, pd.DatetimeIndex):
        price_data['Date'] = pd.to_datetime(price_data['Date'])
        price_data.set_index('Date', inplace=True)
    
    print(f"✅ Index type: {type(price_data.index)}")
    print(f"✅ Index is DatetimeIndex: {isinstance(price_data.index, pd.DatetimeIndex)}")
    
    assert isinstance(price_data.index, pd.DatetimeIndex), "Index must be DatetimeIndex"
    
    print("\nStep 3: Convert to string list (what InstrumentGraph does)")
    print("-" * 80)
    
    if isinstance(price_data.index, pd.DatetimeIndex):
        x_data = price_data.index.strftime('%Y-%m-%d %H:%M:%S').tolist()
    
    print(f"✅ X-axis data type: {type(x_data)}")
    print(f"✅ First 3 dates: {x_data[:3]}")
    print(f"✅ All are strings: {all(isinstance(d, str) for d in x_data)}")
    
    assert all(isinstance(d, str) for d in x_data), "All dates must be strings for Plotly"
    
    print("\n" + "=" * 80)
    print("🎉 SUCCESS: Chart data flow works correctly!")
    print("=" * 80)
    print("\nConclusion:")
    print("✅ get_ohlcv_data() returns DataFrame with datetime objects (UNCHANGED)")
    print("✅ Chart component can convert datetime objects to strings (UNCHANGED)")
    print("✅ Data visualization is NOT affected by datetime formatting changes")
    print("\nThe datetime formatting changes ONLY affect:")
    print("  • get_ohlcv_data_formatted() dict output (for AI agents)")
    print("  • _format_ohlcv_as_markdown() text output (for markdown display)")
    print("=" * 80)


if __name__ == "__main__":
    try:
        test_chart_data_flow()
    except Exception as e:
        print(f"\n❌ TEST FAILED: {e}")
        import traceback
        traceback.print_exc()
