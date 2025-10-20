# FRED Macro Provider Date Range Fix

**Date**: October 17, 2025  
**Issue**: `'NoneType' object has no attribute 'strftime'` errors in FREDMacroProvider

## Problem Description

The FREDMacroProvider's `get_economic_indicators()` and `get_yield_curve()` methods were crashing with:
```
Failed to fetch indicators: 'NoneType' object has no attribute 'strftime'
Failed to fetch yield curve: 'NoneType' object has no attribute 'strftime'
```

### Root Cause

The methods were incorrectly calling `validate_date_range(start_date, end_date, lookback_days)`, passing `lookback_days` as the 3rd parameter. However, `validate_date_range()` expects:
- Parameter 1: `start_date`
- Parameter 2: `end_date`
- Parameter 3: `max_days` (optional, for validation)

It does **NOT** handle `lookback_days` or calculate `start_date` from it. When `start_date` was `None` and only `end_date` and `lookback_days` were provided, the function returned `(None, end_date)`, causing the `None.strftime()` crash.

### Affected Code

**Before (INCORRECT)**:
```python
def get_economic_indicators(self, end_date, start_date=None, lookback_days=None, ...):
    # WRONG: validate_date_range doesn't handle lookback_days
    actual_start_date, actual_end_date = validate_date_range(start_date, end_date, lookback_days)
    
    # Crashes here if actual_start_date is None
    start_str = actual_start_date.strftime("%Y-%m-%d")
```

## Solution

Added proper date range calculation using the existing `calculate_date_range()` helper function:

1. **Check if `start_date` is None and `lookback_days` is provided**
   - If yes, calculate `start_date` using `calculate_date_range(end_date, lookback_days)`

2. **Validate the date range**
   - Call `validate_date_range(start_date, end_date, max_days=None)` for validation only

3. **Handle fallback case**
   - If `actual_start_date` is still None, default to **365 days (1 year)** lookback
   - Rationale: Economic indicators are typically monthly/quarterly (gives 4-12 data points)
   - Yield curve data is daily (1 year provides meaningful historical context)

### Fixed Code

**After (CORRECT)**:
```python
def get_economic_indicators(self, end_date, start_date=None, lookback_days=None, ...):
    # Calculate start_date from lookback_days if not provided
    if start_date is None and lookback_days:
        start_date, end_date = calculate_date_range(end_date, lookback_days)
    
    # Validate date range
    actual_start_date, actual_end_date = validate_date_range(start_date, end_date, max_days=None)
    
    # Use validated dates
    if actual_start_date is None:
        # Default to 365 days (1 year) lookback for meaningful trend analysis
        # Most economic indicators are monthly/quarterly, so 1 year gives ~4-12 data points
        actual_start_date, end_date = calculate_date_range(end_date, 365)
    if actual_end_date:
        end_date = actual_end_date
    
    # Now safe to call strftime
    start_str = actual_start_date.strftime("%Y-%m-%d")
    end_str = end_date.strftime("%Y-%m-%d")
```

## Files Modified

### ba2_trade_platform/modules/dataproviders/macro/FREDMacroProvider.py
- **Line ~141-169**: Fixed `get_economic_indicators()` method
- **Line ~244-272**: Fixed `get_yield_curve()` method

Both methods now:
1. Use `calculate_date_range()` to compute `start_date` from `lookback_days`
2. Use `validate_date_range()` only for validation (not calculation)
3. Provide 90-day default fallback if no dates are specified
4. Safely call `.strftime()` on guaranteed non-None datetime objects

## Testing

The fix ensures that:
- ✅ When `lookback_days` is provided, `start_date` is calculated correctly
- ✅ When neither `start_date` nor `lookback_days` is provided, defaults to **365 days (1 year)**
- ✅ Economic indicators (monthly/quarterly) get 4-12 data points for trend analysis
- ✅ Yield curve (daily) gets sufficient historical context
- ✅ Date validation still occurs for order checking and future date capping
- ✅ No more `NoneType.strftime()` crashes

## Related Issues

### Other Files with Similar Pattern (NOT FIXED YET)
The following files also call `validate_date_range()` with `lookback_days`/`lookback_periods` and may have the same bug:
- `ba2_trade_platform/modules/dataproviders/fundamentals/details/FMPCompanyDetailsProvider.py` (line 368)
- `ba2_trade_platform/modules/dataproviders/indicators/PandasIndicatorCalc.py` (line 191)
- `ba2_trade_platform/modules/dataproviders/indicators/AlphaVantageIndicatorsProvider.py` (line 166)

**Note**: These files may need similar fixes if they exhibit the same `NoneType.strftime()` error.

## Function Reference

### `validate_date_range(start_date, end_date, max_days)`
**Purpose**: Validates date order, timezone, and range limits  
**Does NOT**: Calculate start_date from lookback_days  
**Returns**: `(start_date, end_date)` - may return `(None, end_date)` if start_date was None

### `calculate_date_range(end_date, lookback_days)`
**Purpose**: Calculates start_date from end_date and lookback  
**Formula**: `start_date = end_date - timedelta(days=lookback_days)`  
**Returns**: `(start_date, end_date)` - always returns valid datetime objects

## Notes on `get_past_earnings` Error

The user mentioned an error: `"get_past_earnings is not a valid tool"`. This error was NOT found in the current logs (tradeagents-exp4.log). 

**Status**: `get_past_earnings` IS correctly defined in:
- `tradingagents/agents/analysts/fundamentals_analyst.py` (lines 41-43, included in tools list at line 57)
- `tradingagents/agents/utils/agent_utils_new.py` (line 703)

If this error appears, it may indicate:
1. An older log file
2. A different expert configuration issue
3. A langchain tool registration problem

**Action**: Monitor for this error in future runs. If it appears, investigate the fundamentals_analyst tool registration.
