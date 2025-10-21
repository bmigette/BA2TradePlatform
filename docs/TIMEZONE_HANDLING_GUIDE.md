# Timezone Handling Guide

## Critical Rule: UTC Consistency

**All DatetimeIndex objects must be timezone-aware and localized to UTC** for proper data alignment and charting.

## Pattern

### When Loading Date Data

```python
# After creating DataFrame with date index
df['Date'] = pd.to_datetime(df['Date'])
df.set_index('Date', inplace=True)

# ✅ ALWAYS localize to UTC if not already aware
if df.index.tz is None:
    df.index = df.index.tz_localize('UTC')
```

### When Reindexing/Joining

```python
# When aligning multiple DataFrames (price + indicators)
# Ensure ALL have timezone-aware UTC index

price_index = price_df.index  # Must be tz=UTC
indicator_df = indicator_df.reindex(price_index)  # Will preserve values!
```

### Common Pitfalls

❌ **WRONG: Missing timezone localization**
```python
indicator_df.set_index('Date', inplace=True)
price_df.reindex(indicator_df.index)  
# Result: NaN values because indices don't align exactly
```

✅ **CORRECT: Timezone-aware localization**
```python
indicator_df.set_index('Date', inplace=True)
if indicator_df.index.tz is None:
    indicator_df.index = indicator_df.index.tz_localize('UTC')

price_df.set_index('Date', inplace=True)
if price_df.index.tz is None:
    price_df.index = price_df.index.tz_localize('UTC')

price_df.reindex(indicator_df.index)  
# Result: All values preserved!
```

## Where This Matters

1. **Data Providers** - When loading external data
   - OHLCV providers should localize dates to UTC
   - Indicator providers should localize dates to UTC

2. **Data Visualization** - When aligning price + indicators
   - Price data: Must have tz=UTC
   - Indicator data: Must have tz=UTC
   - Reindexing: Will only work correctly if both are UTC-aware

3. **Database Storage** - When storing timestamps
   - Store as ISO 8601 strings in database
   - Localize to UTC when re-loading

## Debugging

Check timezone info:
```python
df.index.tz  # Should print: <UTC>

# Before operations
print(f"Price index tz: {price_df.index.tz}")
print(f"Indicator index tz: {indicator_df.index.tz}")

# Before/after reindex
print(f"Before reindex: {len(indicator_df)} rows")
print(f"After reindex: {non_nan_count}/{total_values} non-NaN values")
```

## Related Code Locations

- Price data timezone fix: `ba2_trade_platform/modules/experts/TradingAgentsUI.py` (line ~791)
- Indicator timezone localization: `ba2_trade_platform/modules/experts/TradingAgentsUI.py` (line ~870, 1045)
- Chart rendering: `ba2_trade_platform/ui/components/InstrumentGraph.py` (line ~125)
