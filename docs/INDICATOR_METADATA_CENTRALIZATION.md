# Indicator Metadata Centralization

**Date:** 2025-01-XX  
**Status:** ✅ Complete

## Overview

Centralized technical indicator metadata in `MarketIndicatorsInterface.ALL_INDICATORS` to eliminate duplicate SUPPORTED_INDICATORS dictionaries across provider implementations.

## Problem

Previously, each indicator provider (YFinanceIndicatorsProvider, AlphaVantageIndicatorsProvider) maintained its own `SUPPORTED_INDICATORS` dictionary with complete metadata for each indicator:

```python
class YFinanceIndicatorsProvider(MarketIndicatorsInterface):
    SUPPORTED_INDICATORS = {
        "rsi": {
            "name": "Relative Strength Index",
            "description": "RSI: Measures momentum...",
            "usage": "Apply 70/30 thresholds...",
            "tips": "In strong trends..."
        },
        # ... 12 more indicators with full metadata
    }
```

**Issues:**
1. **Code Duplication**: Same metadata duplicated in multiple providers
2. **Maintenance Burden**: Changes to indicator descriptions required updates in multiple files
3. **Inconsistency Risk**: Metadata could diverge between providers over time
4. **Violates DRY**: Don't Repeat Yourself principle violated

## Solution

Moved all indicator metadata to a centralized `ALL_INDICATORS` class variable in `MarketIndicatorsInterface`:

```python
class MarketIndicatorsInterface(DataProviderInterface):
    """
    Interface for technical indicator providers.
    """
    
    # Centralized indicator metadata - all providers share this catalog
    ALL_INDICATORS = {
        "close_50_sma": {
            "name": "50-day Simple Moving Average",
            "description": "50 SMA: A medium-term trend indicator.",
            "usage": "Identify trend direction and serve as dynamic support/resistance.",
            "tips": "It lags price; combine with faster indicators for timely signals."
        },
        "close_200_sma": { ... },
        "close_10_ema": { ... },
        "macd": { ... },
        "macds": { ... },
        "macdh": { ... },
        "rsi": { ... },
        "boll": { ... },
        "boll_ub": { ... },
        "boll_lb": { ... },
        "atr": { ... },
        "vwma": { ... },
        "mfi": { ... }
    }
```

Each provider now only lists the **keys** of indicators it supports:

```python
class YFinanceIndicatorsProvider(MarketIndicatorsInterface):
    # Indicators supported by this provider (references centralized ALL_INDICATORS)
    SUPPORTED_INDICATOR_KEYS = [
        "close_50_sma",
        "close_200_sma",
        "close_10_ema",
        "macd",
        "macds",
        "macdh",
        "rsi",
        "boll",
        "boll_ub",
        "boll_lb",
        "atr",
        "vwma",
        "mfi"
    ]
```

```python
class AlphaVantageIndicatorsProvider(MarketIndicatorsInterface):
    # Indicators supported by this provider (references centralized ALL_INDICATORS)
    SUPPORTED_INDICATOR_KEYS = [
        "close_50_sma",
        "close_200_sma",
        "close_10_ema",
        "macd",
        "macds",
        "macdh",
        "rsi",
        "boll",
        "boll_ub",
        "boll_lb",
        "atr"
        # Note: Alpha Vantage doesn't support vwma or mfi
    ]
```

Providers access metadata via `MarketIndicatorsInterface.ALL_INDICATORS[indicator]`:

```python
def get_indicator(self, ...):
    # Validate indicator is supported by THIS provider
    if indicator not in self.SUPPORTED_INDICATOR_KEYS:
        raise ValueError(...)
    
    # Get metadata from centralized ALL_INDICATORS
    ind_meta = MarketIndicatorsInterface.ALL_INDICATORS[indicator]
    
    # Use metadata in response
    response = {
        "indicator_name": ind_meta["name"],
        "metadata": {
            "description": ind_meta["description"],
            "usage": ind_meta["usage"],
            "tips": ind_meta["tips"]
        }
    }
```

## Implementation Details

### Files Modified

1. **MarketIndicatorsInterface.py**
   - Added `ALL_INDICATORS` class variable with complete metadata for all 13 indicators
   - Positioned at top of class for easy reference
   
2. **YFinanceIndicatorsProvider.py**
   - Removed `SUPPORTED_INDICATORS` dictionary (120+ lines)
   - Added `SUPPORTED_INDICATOR_KEYS` list (13 keys)
   - Updated `get_indicator()` to reference `MarketIndicatorsInterface.ALL_INDICATORS[indicator]`
   - Updated `get_supported_indicators()` to return `self.SUPPORTED_INDICATOR_KEYS`
   - Updated validation to use `self.SUPPORTED_INDICATOR_KEYS`

3. **AlphaVantageIndicatorsProvider.py**
   - Removed `SUPPORTED_INDICATORS` dictionary (100+ lines)
   - Added `SUPPORTED_INDICATOR_KEYS` list (11 keys - excludes vwma and mfi)
   - Updated `get_indicator()` to reference `MarketIndicatorsInterface.ALL_INDICATORS[indicator]`
   - Updated `get_supported_indicators()` to return `self.SUPPORTED_INDICATOR_KEYS`
   - Updated validation to use `self.SUPPORTED_INDICATOR_KEYS`

### Indicators Catalog

The centralized `ALL_INDICATORS` contains metadata for 13 technical indicators:

**Moving Averages:**
- `close_50_sma` - 50-day Simple Moving Average
- `close_200_sma` - 200-day Simple Moving Average
- `close_10_ema` - 10-day Exponential Moving Average

**MACD:**
- `macd` - MACD Line
- `macds` - MACD Signal Line
- `macdh` - MACD Histogram

**Momentum:**
- `rsi` - Relative Strength Index

**Volatility:**
- `boll` - Bollinger Middle Band
- `boll_ub` - Bollinger Upper Band
- `boll_lb` - Bollinger Lower Band
- `atr` - Average True Range
- `vwma` - Volume Weighted Moving Average
- `mfi` - Money Flow Index

### Provider Coverage

**YFinanceIndicatorsProvider**: Supports all 13 indicators (uses stockstats)

**AlphaVantageIndicatorsProvider**: Supports 11 indicators (excludes vwma and mfi - not available in Alpha Vantage API)

## Benefits

1. **Single Source of Truth**: All indicator metadata lives in one place
2. **DRY Compliance**: Eliminated ~220 lines of duplicate code
3. **Easy Maintenance**: Update metadata once, all providers get the change
4. **Consistency Guaranteed**: Impossible for metadata to diverge between providers
5. **Scalability**: New providers can reference the same metadata
6. **Clear Separation**: Interface defines what exists, providers define what they support

## Testing

Verify the refactoring with:

```python
from ba2_trade_platform.core.interfaces import MarketIndicatorsInterface
from ba2_trade_platform.modules.dataproviders.indicators import YFinanceIndicatorsProvider, AlphaVantageIndicatorsProvider

# Check centralized metadata exists
assert len(MarketIndicatorsInterface.ALL_INDICATORS) == 13
assert "rsi" in MarketIndicatorsInterface.ALL_INDICATORS

# Check YFinance supports all indicators
yf_provider = YFinanceIndicatorsProvider()
assert len(yf_provider.get_supported_indicators()) == 13
assert "vwma" in yf_provider.get_supported_indicators()

# Check Alpha Vantage supports subset
av_provider = AlphaVantageIndicatorsProvider()
assert len(av_provider.get_supported_indicators()) == 11
assert "vwma" not in av_provider.get_supported_indicators()
assert "mfi" not in av_provider.get_supported_indicators()

# Verify metadata access
yf_result = yf_provider.get_indicator(
    symbol="AAPL",
    indicator="rsi",
    end_date=datetime.now(),
    lookback_days=30
)
assert "Relative Strength Index" in str(yf_result)
```

## Migration Path for New Providers

When creating a new indicator provider:

1. Extend `MarketIndicatorsInterface`
2. Define `SUPPORTED_INDICATOR_KEYS` list with keys from `ALL_INDICATORS`
3. In `get_indicator()`, validate using `SUPPORTED_INDICATOR_KEYS`
4. Access metadata via `MarketIndicatorsInterface.ALL_INDICATORS[indicator]`
5. Return `SUPPORTED_INDICATOR_KEYS` from `get_supported_indicators()`

Example:

```python
class NewIndicatorProvider(MarketIndicatorsInterface):
    SUPPORTED_INDICATOR_KEYS = ["rsi", "macd", "close_50_sma"]
    
    def get_indicator(self, symbol, indicator, ...):
        if indicator not in self.SUPPORTED_INDICATOR_KEYS:
            raise ValueError(f"Not supported: {indicator}")
        
        # Get centralized metadata
        metadata = MarketIndicatorsInterface.ALL_INDICATORS[indicator]
        
        # ... implementation ...
        
        return {
            "indicator_name": metadata["name"],
            "metadata": metadata
        }
    
    def get_supported_indicators(self) -> list[str]:
        return self.SUPPORTED_INDICATOR_KEYS
```

## Related Documentation

- **PROVIDER_ARCHITECTURE_REVIEW.md**: Overall provider architecture and data flow
- **CIRCULAR_DEPENDENCY_FIX.md**: Removal of TradingAgents dependencies from YFinanceIndicatorsProvider

## Status

✅ **Complete** - Refactoring successfully applied to both providers with no errors
