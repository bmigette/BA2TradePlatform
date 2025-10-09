# FRED Macro Provider Date Handling Fix

**Date:** October 9, 2025  
**Issue:** `'NoneType' object has no attribute 'strftime'`

## Problem

The `get_fed_calendar()` method in FREDMacroProvider was failing with:

```
Failed to fetch Fed calendar: 'NoneType' object has no attribute 'strftime'
```

### Error Location

```python
# Old code (line ~313):
actual_start_date, actual_end_date = validate_date_range(start_date, end_date, lookback_days)

# Later (line ~319):
start_str = actual_start_date.strftime("%Y-%m-%d")  # ❌ actual_start_date could be None
```

## Root Cause

The method was using `validate_date_range()` incorrectly:

1. **Wrong function:** `validate_date_range(start_date, end_date, max_days)` 
   - This function expects **both** `start_date` and `end_date` to be provided
   - It only validates the range, doesn't calculate dates from lookback
   - Returns `(start_date, end_date)` which can be `None` if not provided

2. **Wrong parameters:** Called as `validate_date_range(start_date, end_date, lookback_days)`
   - Passed `lookback_days` as the `max_days` parameter
   - When `start_date` is `None` (which it is when using lookback), the function returns `(None, end_date)`

3. **No validation:** The code didn't check if `actual_start_date` was `None` before calling `.strftime()`

### Why It Failed

When calling from agents with `lookback_days=30`:
```python
provider.get_fed_calendar(
    end_date=datetime(2025, 10, 9),
    start_date=None,        # ← None passed
    lookback_days=30        # ← Should calculate start_date from this
)
```

The old code path:
```python
actual_start_date, actual_end_date = validate_date_range(None, end_date, 30)
# Returns: (None, end_date)

start_str = None.strftime("%Y-%m-%d")  # ❌ AttributeError!
```

## Solution

Changed to use the **correct date handling pattern** used by other providers:

### Pattern for Date Parameters

```python
# Validate parameters (mutually exclusive)
if start_date and lookback_days:
    raise ValueError("Provide either start_date OR lookback_days, not both")
if not start_date and not lookback_days:
    raise ValueError("Must provide either start_date or lookback_days")

# Calculate date range based on which parameter is provided
if lookback_days:
    # Use calculate_date_range to compute start_date from lookback
    lookback_days = validate_lookback_days(lookback_days, max_lookback=365)
    start_date, end_date = calculate_date_range(end_date, lookback_days)
else:
    # Use validate_date_range to validate provided dates
    start_date, end_date = validate_date_range(start_date, end_date, max_days=365)
```

### Key Functions

| Function | Purpose | When to Use |
|----------|---------|-------------|
| `calculate_date_range(end_date, lookback_days)` | Calculate start_date from lookback | When user provides `lookback_days` |
| `validate_date_range(start_date, end_date, max_days)` | Validate provided date range | When user provides both dates |
| `validate_lookback_days(days, max_lookback)` | Validate lookback is within limits | Before calling `calculate_date_range` |

### Result

Both functions guarantee non-None datetime objects:
- `calculate_date_range()` always returns `(datetime, datetime)`
- `validate_date_range()` with both dates returns `(datetime, datetime)`

## Changes Made

### File: `ba2_trade_platform/modules/dataproviders/macro/FREDMacroProvider.py`

#### 1. Import Additional Utilities

```python
# Old:
from ba2_trade_platform.core.provider_utils import validate_date_range, log_provider_call

# New:
from ba2_trade_platform.core.provider_utils import (
    validate_date_range,
    validate_lookback_days,
    calculate_date_range,
    log_provider_call
)
```

#### 2. Updated `get_fed_calendar()` Method

```python
@log_provider_call
def get_fed_calendar(
    self,
    end_date: datetime,
    start_date: Optional[datetime] = None,
    lookback_days: Optional[int] = None,
    format_type: Literal["dict", "markdown", "both"] = "markdown"
) -> Dict[str, Any] | str:
    """Get Federal Reserve calendar and meeting minutes."""
    
    # Validate date parameters
    if start_date and lookback_days:
        raise ValueError("Provide either start_date OR lookback_days, not both")
    if not start_date and not lookback_days:
        raise ValueError("Must provide either start_date or lookback_days")
    
    # Calculate date range
    if lookback_days:
        lookback_days = validate_lookback_days(lookback_days, max_lookback=365)
        start_date, end_date = calculate_date_range(end_date, lookback_days)
    else:
        start_date, end_date = validate_date_range(start_date, end_date, max_days=365)
    
    # Now start_date and end_date are guaranteed to be non-None datetime objects
    start_str = start_date.strftime("%Y-%m-%d")  # ✅ Safe
    end_str = end_date.strftime("%Y-%m-%d")      # ✅ Safe
    
    # ... rest of method
```

## Same Pattern in Other Providers

This is the **standard pattern** used throughout the codebase:

- ✅ `OpenAINewsProvider.get_company_news()`
- ✅ `OpenAINewsProvider.get_global_news()`
- ✅ `AlpacaNewsProvider.get_company_news()`
- ✅ `AlpacaNewsProvider.get_global_news()`
- ✅ `FMPNewsProvider.get_company_news()`
- ✅ `FMPNewsProvider.get_global_news()`
- ✅ `AlphaVantageNewsProvider` methods
- ✅ `GoogleNewsProvider` methods
- ✅ `MarketDataProviderInterface.get_ohlcv_data()`

All follow the same pattern for handling `start_date` vs `lookback_days` parameters.

## Testing

To verify the fix:

```python
from ba2_trade_platform.modules.dataproviders.macro import FREDMacroProvider
from datetime import datetime

provider = FREDMacroProvider()

# Test with lookback_days (previously failed)
result = provider.get_fed_calendar(
    end_date=datetime(2025, 10, 9),
    lookback_days=30
)
print(result)

# Test with start_date (should still work)
result = provider.get_fed_calendar(
    end_date=datetime(2025, 10, 9),
    start_date=datetime(2025, 9, 9)
)
print(result)
```

Both should now work without errors.

## Benefits

1. ✅ **Consistent:** Uses same pattern as all other providers
2. ✅ **Type Safe:** Guarantees non-None datetime objects
3. ✅ **Validated:** Properly validates lookback_days limits
4. ✅ **Clear Errors:** Raises ValueError for invalid parameter combinations
5. ✅ **Documented:** Clear error messages guide users

## Related Documentation

- **Date Handling Best Practices:** `docs/SESSION_SUMMARY_2025-10-09.md` (line 492)
- **Provider Utils Reference:** `ba2_trade_platform/core/provider_utils.py`
- **Interface Standards:** `ba2_trade_platform/core/interfaces/MacroEconomicsInterface.py`

## Key Takeaway

**Always use `calculate_date_range()` when converting `lookback_days` to a date range.**

Only use `validate_date_range()` when you already have both `start_date` and `end_date` and just need to validate them.
