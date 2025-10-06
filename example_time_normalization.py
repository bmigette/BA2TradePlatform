"""
Example: Fetching properly aligned market data using time normalization

This script demonstrates how the normalize_time_to_interval function
ensures data is fetched from proper interval boundaries.
"""

from datetime import datetime, timedelta
from ba2_trade_platform.modules.dataproviders.YFinanceDataProvider import YFinanceDataProvider
from ba2_trade_platform.core.MarketDataProvider import MarketDataProvider
import os


def example_normalized_data_fetch():
    """Example showing normalized vs non-normalized timestamps."""
    
    print("=" * 80)
    print("Market Data Fetching with Time Normalization")
    print("=" * 80)
    
    # Create data provider
    cache_folder = os.path.expanduser("~/Documents/ba2_trade_platform/market_data_cache")
    provider = YFinanceDataProvider(cache_folder)
    
    # Example 1: User wants data "from now" with 15-minute bars
    print("\nExample 1: 15-Minute Bars")
    print("-" * 80)
    
    current_time = datetime.now()
    print(f"Current time: {current_time.strftime('%Y-%m-%d %H:%M:%S')}")
    
    # Without normalization (old behavior)
    print(f"\nWITHOUT normalization:")
    print(f"  Would fetch from: {current_time.strftime('%H:%M:%S')}")
    print(f"  Problem: Not aligned to 15-min boundary!")
    
    # With normalization (new behavior)
    normalized_time = MarketDataProvider.normalize_time_to_interval(current_time, '15m')
    print(f"\nWITH normalization:")
    print(f"  Fetches from: {normalized_time.strftime('%H:%M:%S')}")
    print(f"  Benefit: Aligned to 15-min boundary (00, 15, 30, 45)")
    
    # Example 2: 4-hour bars for intraday trading
    print("\n\nExample 2: 4-Hour Bars for Intraday Analysis")
    print("-" * 80)
    
    end_time = datetime.now()
    start_time = end_time - timedelta(days=7)  # Last week
    
    print(f"Original start: {start_time.strftime('%Y-%m-%d %H:%M:%S')}")
    
    normalized_start = MarketDataProvider.normalize_time_to_interval(start_time, '4h')
    print(f"Normalized start: {normalized_start.strftime('%Y-%m-%d %H:%M:%S')}")
    
    # Show the 4-hour blocks
    print(f"\n4-hour blocks throughout the day:")
    print(f"  00:00-04:00, 04:00-08:00, 08:00-12:00, 12:00-16:00, 16:00-20:00, 20:00-24:00")
    print(f"  Data will start at hour: {normalized_start.hour}")
    
    # Example 3: Daily bars for swing trading
    print("\n\nExample 3: Daily Bars for Swing Trading")
    print("-" * 80)
    
    end_date = datetime.now()
    start_date = end_date - timedelta(days=90)  # Last 3 months
    
    print(f"Original start: {start_date.strftime('%Y-%m-%d %H:%M:%S')}")
    
    normalized_daily = MarketDataProvider.normalize_time_to_interval(start_date, '1d')
    print(f"Normalized start: {normalized_daily.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Benefit: Starts at midnight (00:00:00) for clean daily bars")
    
    # Example 4: Fetching actual data with automatic normalization
    print("\n\nExample 4: Real Data Fetch with Automatic Normalization")
    print("-" * 80)
    print("\nFetching AAPL 15-minute data for today...")
    
    try:
        today = datetime.now().replace(hour=9, minute=30)  # Market open-ish
        end = datetime.now()
        
        # The get_data method automatically normalizes the start_date
        data = provider.get_data(
            symbol='AAPL',
            start_date=today,
            end_date=end,
            interval='15m',
            use_cache=True
        )
        
        if data:
            print(f"\nFetched {len(data)} data points")
            print(f"First data point: {data[0].timestamp.strftime('%Y-%m-%d %H:%M:%S')}")
            print(f"Last data point: {data[-1].timestamp.strftime('%Y-%m-%d %H:%M:%S')}")
            
            # Show first few timestamps to demonstrate alignment
            print(f"\nFirst 5 timestamps (notice they're aligned to :00, :15, :30, :45):")
            for i, point in enumerate(data[:5]):
                print(f"  {i+1}. {point.timestamp.strftime('%H:%M:%S')} - Close: ${point.close:.2f}")
        else:
            print("No data returned (market might be closed)")
            
    except Exception as e:
        print(f"Error fetching data: {e}")
        print("This is expected if run outside market hours or if API rate limited")
    
    print("\n" + "=" * 80)
    print("Examples completed!")
    print("=" * 80)
    print("\nKey Takeaway:")
    print("  All data fetching now automatically normalizes timestamps to interval")
    print("  boundaries, ensuring proper time series alignment for analysis and charts.")


if __name__ == '__main__':
    example_normalized_data_fetch()
