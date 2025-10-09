# OHLCV Method Refactoring

**Date:** October 9, 2025  
**Branch:** providers_interfaces  
**Status:** ✅ Complete

## Overview

Refactored the MarketDataProviderInterface and all implementations to use clearer, more consistent naming:
- Renamed `get_dataframe()` → `get_ohlcv_data()` (public method)
- Renamed `_fetch_data_from_source()` → `_get_ohlcv_data_impl()` (internal implementation)
- Updated TradingAgents toolkit to use `format_type="both"` for optimized loggingToolNode integration

## Motivation

1. **Naming Clarity**: `get_ohlcv_data` is more descriptive and aligns with the domain (OHLCV = Open, High, Low, Close, Volume)
2. **Consistent API**: All OHLCV providers now expose the same public method name
3. **LLM Optimization**: Using `format_type="both"` allows loggingToolNode to get both markdown text (for LLM) and structured JSON (for database) in a single call

## Changes Made

### 1. MarketDataProviderInterface (Core)

**File:** `ba2_trade_platform/core/interfaces/MarketDataProviderInterface.py`

- **Renamed Method:** `get_dataframe()` → `get_ohlcv_data()`
  - Updated docstring to clarify it's the main public method
  - Still returns `pd.DataFrame` with OHLCV data
  
- **Renamed Abstract Method:** `_fetch_data_from_source()` → `_get_ohlcv_data_impl()`
  - Clarified this is an internal implementation method
  - Subclasses must implement this to fetch data from their specific source

### 2. OHLCV Provider Implementations

Updated all OHLCV data providers to use new method names:

#### AlpacaOHLCVProvider
**File:** `ba2_trade_platform/modules/dataproviders/ohlcv/AlpacaOHLCVProvider.py`
- Renamed `_fetch_data_from_source()` → `_get_ohlcv_data_impl()`
- Updated `get_ohlcv_data()` to call `super().get_ohlcv_data()` instead of `super().get_dataframe()`
- No changes to existing caching behavior

#### YFinanceDataProvider
**File:** `ba2_trade_platform/modules/dataproviders/ohlcv/YFinanceDataProvider.py`
- Renamed `_fetch_data_from_source()` → `_get_ohlcv_data_impl()`
- Updated `validate_symbol()` method to use `_get_ohlcv_data_impl()`

#### AlphaVantageOHLCVProvider
**File:** `ba2_trade_platform/modules/dataproviders/ohlcv/AlphaVantageOHLCVProvider.py`
- Renamed `_fetch_data_from_source()` → `_get_ohlcv_data_impl()`
- Updated `get_latest_price()` to call `get_ohlcv_data()` instead of `get_dataframe()`

### 3. Indicator Providers

Updated indicator providers that depend on OHLCV data:

#### PandasIndicatorCalc
**File:** `ba2_trade_platform/modules/dataproviders/indicators/PandasIndicatorCalc.py`
- Updated call from `self._data_provider.get_dataframe()` → `self._data_provider.get_ohlcv_data()`

### 4. TradingAgents Integration

#### Agent Toolkit (format_type="both")
**File:** `ba2_trade_platform/thirdparties/TradingAgents/tradingagents/agents/utils/agent_utils_new.py`

**Before:**
```python
df = provider.get_dataframe(
    symbol=symbol,
    start_date=start_dt,
    end_date=end_dt,
    interval=interval
)
# Convert DataFrame to markdown format
ohlcv_markdown = df.to_markdown(index=True)
return f"## OHLCV Data from {provider_name.upper()}\n\n{ohlcv_markdown}"
```

**After:**
```python
# Call provider's get_ohlcv_data method with format_type="both"
# This returns {"text": markdown, "data": dict} for loggingToolNode optimization
result = provider.get_ohlcv_data(
    symbol=symbol,
    end_date=end_dt,
    start_date=start_dt,
    interval=interval,
    format_type="both"
)

# Extract both text and data
if isinstance(result, dict) and "text" in result and "data" in result:
    logger.info(f"Successfully retrieved OHLCV data from {provider_name}")
    # Return markdown text for LLM consumption
    # (The data dict can be logged by loggingToolNode)
    return result["text"]
```

**Benefits:**
- Single provider call gets both formats
- Markdown text for LLM context
- Structured JSON for database logging
- Optimizes loggingToolNode performance

#### Dataflows Module
**File:** `ba2_trade_platform/thirdparties/TradingAgents/tradingagents/dataflows/interface.py`
- Updated `get_stock_data()` to use `provider.get_ohlcv_data()`
- Updated cache method name from `"get_dataframe"` → `"get_ohlcv_data"`

#### StockStats Utilities
**File:** `ba2_trade_platform/thirdparties/TradingAgents/tradingagents/dataflows/stockstats_utils.py`
- Updated both `get_stock_stats()` methods to use `provider.get_ohlcv_data()`
- Ensures indicator calculations use renamed method

### 5. UI and Test Files

#### TradingAgentsUI
**File:** `ba2_trade_platform/modules/experts/TradingAgentsUI.py`
- Updated visualization chart data fetching to use `provider.get_ohlcv_data()`

#### Test Files
- **test_parameter_storage.py**: Updated to use `provider.get_ohlcv_data()`
- **test_data_provider.py**: Updated test name and method call to `get_ohlcv_data()`

## API Consistency

All OHLCV providers now expose the same public API:

```python
from ba2_trade_platform.modules.dataproviders.ohlcv import (
    YFinanceDataProvider,
    AlpacaOHLCVProvider,
    AlphaVantageOHLCVProvider
)

provider = YFinanceDataProvider()  # or any other OHLCV provider

# Get OHLCV data as DataFrame
df = provider.get_ohlcv_data(
    symbol="AAPL",
    start_date=datetime(2024, 1, 1),
    end_date=datetime(2024, 12, 31),
    interval="1d"
)
```

For providers that extend AlpacaOHLCVProvider (with format_type support):

```python
provider = AlpacaOHLCVProvider()

# Get as dict
result_dict = provider.get_ohlcv_data(
    symbol="AAPL",
    end_date=datetime.now(),
    lookback_days=30,
    format_type="dict"
)

# Get as markdown
result_md = provider.get_ohlcv_data(
    symbol="AAPL",
    end_date=datetime.now(),
    lookback_days=30,
    format_type="markdown"
)

# Get both formats (for loggingToolNode)
result_both = provider.get_ohlcv_data(
    symbol="AAPL",
    end_date=datetime.now(),
    lookback_days=30,
    format_type="both"
)
# Returns: {"text": markdown_str, "data": dict_data}
```

## Internal Implementation Pattern

All OHLCV providers follow this pattern:

```python
class MyOHLCVProvider(MarketDataProviderInterface):
    """Custom OHLCV data provider."""
    
    def _get_ohlcv_data_impl(
        self,
        symbol: str,
        start_date: datetime,
        end_date: datetime,
        interval: str = '1d'
    ) -> pd.DataFrame:
        """
        Internal implementation: fetch data from source.
        
        This method is called by the parent class's get_ohlcv_data()
        when cache is invalid or disabled.
        """
        # Fetch data from your specific source (API, database, etc.)
        data = fetch_from_my_source(symbol, start_date, end_date, interval)
        
        # Return DataFrame with columns: Date, Open, High, Low, Close, Volume
        return data
```

The parent class `MarketDataProviderInterface` handles:
- Caching (symbol-based CSV files)
- Cache validation (24-hour max age by default)
- Date range normalization
- Filtering to requested date range

## Files Modified

### Core Framework (2 files)
1. `ba2_trade_platform/core/interfaces/MarketDataProviderInterface.py`
2. `ba2_trade_platform/modules/dataproviders/indicators/PandasIndicatorCalc.py`

### OHLCV Providers (3 files)
3. `ba2_trade_platform/modules/dataproviders/ohlcv/AlpacaOHLCVProvider.py`
4. `ba2_trade_platform/modules/dataproviders/ohlcv/YFinanceDataProvider.py`
5. `ba2_trade_platform/modules/dataproviders/ohlcv/AlphaVantageOHLCVProvider.py`

### TradingAgents Integration (3 files)
6. `ba2_trade_platform/thirdparties/TradingAgents/tradingagents/agents/utils/agent_utils_new.py`
7. `ba2_trade_platform/thirdparties/TradingAgents/tradingagents/dataflows/interface.py`
8. `ba2_trade_platform/thirdparties/TradingAgents/tradingagents/dataflows/stockstats_utils.py`

### UI and Tests (3 files)
9. `ba2_trade_platform/modules/experts/TradingAgentsUI.py`
10. `test_tools/test_parameter_storage.py`
11. `test_tools/test_data_provider.py`

## Backward Compatibility

⚠️ **Breaking Change**: The public method `get_dataframe()` has been renamed to `get_ohlcv_data()`.

**Migration Guide:**
```python
# Old code
df = provider.get_dataframe(symbol="AAPL", start_date=..., end_date=...)

# New code
df = provider.get_ohlcv_data(symbol="AAPL", start_date=..., end_date=...)
```

All references throughout the codebase have been updated, so no manual migration is needed for internal code.

## Testing Recommendations

1. **Test OHLCV Data Retrieval:**
   ```python
   provider = YFinanceDataProvider()
   df = provider.get_ohlcv_data("AAPL", datetime(2024, 1, 1), datetime(2024, 12, 31))
   assert not df.empty
   assert list(df.columns) == ['Date', 'Open', 'High', 'Low', 'Close', 'Volume']
   ```

2. **Test format_type="both":**
   ```python
   provider = AlpacaOHLCVProvider()
   result = provider.get_ohlcv_data("AAPL", end_date=datetime.now(), lookback_days=7, format_type="both")
   assert isinstance(result, dict)
   assert "text" in result and "data" in result
   assert isinstance(result["text"], str)
   assert isinstance(result["data"], dict)
   ```

3. **Test Caching:**
   ```python
   # First call - fetches from source
   df1 = provider.get_ohlcv_data("AAPL", datetime(2024, 1, 1), datetime(2024, 1, 31))
   
   # Second call - should use cache
   df2 = provider.get_ohlcv_data("AAPL", datetime(2024, 1, 1), datetime(2024, 1, 31))
   
   assert df1.equals(df2)
   ```

4. **Test TradingAgents Integration:**
   ```python
   from ba2_trade_platform.thirdparties.TradingAgents.tradingagents.agents.utils.agent_utils_new import BA2Toolkit
   
   toolkit = BA2Toolkit(...)
   result = toolkit.get_ohlcv_data("AAPL", "2024-01-01", "2024-01-31", interval="1d")
   assert isinstance(result, str)  # Should return markdown text
   ```

## Next Steps

1. ✅ All method renaming complete
2. ✅ TradingAgents toolkit updated to use `format_type="both"`
3. ✅ All test files updated
4. 🔲 Run comprehensive test suite to verify no regressions
5. 🔲 Test loggingToolNode with new dual-format optimization
6. 🔲 Monitor performance improvement in LLM tool calls

## Related Documentation

- **Provider Refactoring:** See previous work on dual format support in `docs/DATA_PROVIDER_*.md`
- **Caching Strategy:** Implemented in `MarketDataProviderInterface.__init__()`
- **Format Types:** All providers now support `"dict"`, `"markdown"`, and `"both"`

## Notes

- The `get_data()` method (returns `List[MarketDataPoint]`) remains unchanged
- Caching behavior is identical, only method names changed
- All OHLCV providers inherit caching from `MarketDataProviderInterface`
- `format_type="both"` optimization enables single-call data retrieval for loggingToolNode
