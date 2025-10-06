# Time Normalization for Proper Time Series Alignment

## Overview

Added `normalize_time_to_interval()` static method to `MarketDataProvider` class to ensure proper time series alignment by flooring timestamps to interval boundaries.

## Problem Statement

When requesting market data with a specific time like `15:54:00`, the data should be aligned to the interval boundary to create a proper time series:
- For 15-minute intervals, data should start at `15:45:00` (not `15:54:00`)
- For 1-hour intervals, data should start at `15:00:00`
- For 4-hour intervals, data should start at `12:00:00` (4h blocks: 0h, 4h, 8h, 12h, 16h, 20h)

Without normalization, time series data would be misaligned and cause issues with:
- Technical indicator calculations
- Chart rendering
- Data aggregation
- Historical comparisons

## Implementation

### Function Signature

```python
@staticmethod
def normalize_time_to_interval(dt: datetime, interval: str) -> datetime:
    """
    Normalize (floor) a datetime to the given interval.
    
    Args:
        dt: Datetime to normalize
        interval: Interval string ('1m', '5m', '15m', '30m', '1h', '4h', '1d', '1wk', '1mo')
    
    Returns:
        Normalized datetime floored to the interval boundary
    """
```

### Normalization Rules

#### Minute Intervals (`1m`, `5m`, `15m`, `30m`)
- Floors to the nearest minute boundary within the hour
- Examples:
  - `15:54:00` with `15m` → `15:45:00`
  - `15:54:00` with `5m` → `15:50:00`
  - `15:54:00` with `1m` → `15:54:00`

#### Hour Intervals (`1h`, `4h`)
- Floors to hour blocks starting from midnight (00:00)
- For `4h`: blocks are 0h-4h, 4h-8h, 8h-12h, 12h-16h, 16h-20h, 20h-24h
- Examples:
  - `15:54:00` with `1h` → `15:00:00`
  - `15:54:00` with `4h` → `12:00:00`
  - `03:30:00` with `4h` → `00:00:00`

#### Day Intervals (`1d`)
- Floors to start of day (midnight)
- Example: `15:54:00` → `00:00:00`

#### Week Intervals (`1wk`)
- Floors to start of week (Monday at midnight)
- Example: Any day in the week → Monday 00:00:00

#### Month Intervals (`1mo`)
- Floors to 1st day of month at midnight
- Example: Oct 15 → Oct 1

## Integration

The normalization is automatically applied in both main data retrieval methods:

### 1. `get_data()` Method
```python
# Normalize start_date to interval boundary
normalized_start = self.normalize_time_to_interval(start_date, interval)

# Use normalized_start for fetching and filtering
```

### 2. `get_dataframe()` Method
```python
# Normalize start_date to interval boundary
normalized_start = self.normalize_time_to_interval(start_date, interval)

# Use normalized_start for fetching and filtering
```

## Benefits

1. **Consistent Time Series**: All data points align to interval boundaries
2. **Accurate Technical Indicators**: Indicators calculate correctly with properly aligned data
3. **Clean Charts**: Chart data renders with proper time axis alignment
4. **Predictable Behavior**: Users know exactly what time range they'll get
5. **API Compatibility**: Aligns with how market data providers structure their data

## Examples

### Example 1: 15-Minute Chart
```python
from datetime import datetime
from ba2_trade_platform.core.MarketDataProvider import MarketDataProvider

# User requests data starting at 15:54
start_time = datetime(2025, 10, 6, 15, 54, 0)

# Automatically normalized to 15:45
normalized = MarketDataProvider.normalize_time_to_interval(start_time, '15m')
print(normalized)  # 2025-10-06 15:45:00

# Data will start from proper 15-minute boundary
```

### Example 2: 4-Hour Blocks
```python
# Different times within same 4-hour block
times = [
    datetime(2025, 10, 6, 12, 30, 0),  # 12:30
    datetime(2025, 10, 6, 14, 45, 0),  # 14:45
    datetime(2025, 10, 6, 15, 54, 0),  # 15:54
]

for t in times:
    normalized = MarketDataProvider.normalize_time_to_interval(t, '4h')
    print(f"{t.strftime('%H:%M')} -> {normalized.strftime('%H:%M')}")

# Output:
# 12:30 -> 12:00
# 14:45 -> 12:00
# 15:54 -> 12:00
```

## Testing

Run the test script to verify normalization behavior:

```bash
python test_time_normalization.py
```

The test verifies:
- Minute intervals (1m, 5m, 15m, 30m)
- Hour intervals (1h, 4h)
- Day, week, month intervals
- Edge cases and boundary conditions
- 4-hour block alignment throughout the day
- Week alignment to Monday
- Month alignment to 1st of month

## Technical Notes

1. **Static Method**: Can be called without instantiating MarketDataProvider
2. **Immutable**: Returns new datetime, doesn't modify original
3. **Timezone Aware**: Preserves timezone information if present
4. **Logging**: Logs normalization in debug mode when start_date changes
5. **Backward Compatible**: Existing code continues to work, just with better alignment

## Related Files

- **Implementation**: `ba2_trade_platform/core/MarketDataProvider.py`
- **Test Script**: `test_time_normalization.py`
- **Time Intervals**: `ba2_trade_platform/core/types.py` (TimeInterval enum)

## Future Enhancements

Potential improvements:
1. Support for custom intervals (e.g., "2h", "3m")
2. Configurable week start day (Sunday vs Monday)
3. Market hours awareness (e.g., align to market open/close)
4. Timezone conversion helpers
