"""
Test script to verify time normalization to interval boundaries.

This demonstrates the normalize_time_to_interval function which floors
timestamps to interval boundaries for proper time series alignment.
"""

from datetime import datetime
from ba2_trade_platform.core.MarketDataProvider import MarketDataProvider


def test_time_normalization():
    """Test various time normalization scenarios."""
    
    # Test datetime
    test_time = datetime(2025, 10, 6, 15, 54, 37)  # 15:54:37
    
    print("=" * 80)
    print("Time Normalization Tests")
    print("=" * 80)
    print(f"\nOriginal time: {test_time.strftime('%Y-%m-%d %H:%M:%S')}\n")
    
    # Test minute intervals
    print("MINUTE INTERVALS:")
    print("-" * 80)
    for interval in ['1m', '5m', '15m', '30m']:
        normalized = MarketDataProvider.normalize_time_to_interval(test_time, interval)
        print(f"{interval:6s} -> {normalized.strftime('%Y-%m-%d %H:%M:%S')}")
    
    # Test hour intervals
    print("\nHOUR INTERVALS:")
    print("-" * 80)
    for interval in ['1h', '4h']:
        normalized = MarketDataProvider.normalize_time_to_interval(test_time, interval)
        print(f"{interval:6s} -> {normalized.strftime('%Y-%m-%d %H:%M:%S')}")
    
    # Test day/week/month intervals
    print("\nDAY/WEEK/MONTH INTERVALS:")
    print("-" * 80)
    for interval in ['1d', '1wk', '1mo']:
        normalized = MarketDataProvider.normalize_time_to_interval(test_time, interval)
        print(f"{interval:6s} -> {normalized.strftime('%Y-%m-%d %H:%M:%S')}")
    
    # Test edge cases
    print("\n" + "=" * 80)
    print("EDGE CASE TESTS")
    print("=" * 80)
    
    # Test 4h interval at different times to show 4-hour blocks
    print("\n4-hour intervals throughout the day (should be 0h, 4h, 8h, 12h, 16h, 20h):")
    print("-" * 80)
    for hour in [0, 3, 4, 7, 8, 11, 12, 15, 16, 19, 20, 23]:
        test_dt = datetime(2025, 10, 6, hour, 30, 0)
        normalized = MarketDataProvider.normalize_time_to_interval(test_dt, '4h')
        print(f"  {test_dt.strftime('%H:%M')} -> {normalized.strftime('%H:%M')} (block: {normalized.hour}h-{normalized.hour+4}h)")
    
    # Test week normalization on different days
    print("\nWeek normalization (should floor to Monday):")
    print("-" * 80)
    days = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
    for day_offset in range(7):
        test_dt = datetime(2025, 10, 6 + day_offset, 15, 30, 0)  # Oct 6 is Monday
        normalized = MarketDataProvider.normalize_time_to_interval(test_dt, '1wk')
        weekday_original = days[test_dt.weekday()]
        weekday_normalized = days[normalized.weekday()]
        print(f"  {weekday_original:9s} {test_dt.strftime('%Y-%m-%d')} -> {weekday_normalized:9s} {normalized.strftime('%Y-%m-%d')}")
    
    # Test month normalization
    print("\nMonth normalization (should floor to 1st of month):")
    print("-" * 80)
    for day in [1, 5, 15, 28, 31]:
        try:
            test_dt = datetime(2025, 10, day, 15, 30, 0)
            normalized = MarketDataProvider.normalize_time_to_interval(test_dt, '1mo')
            print(f"  Oct {day:2d} -> {normalized.strftime('%Y-%m-%d')}")
        except ValueError:
            # Skip invalid dates like Oct 31
            pass
    
    print("\n" + "=" * 80)
    print("All tests completed!")
    print("=" * 80)


if __name__ == '__main__':
    test_time_normalization()
