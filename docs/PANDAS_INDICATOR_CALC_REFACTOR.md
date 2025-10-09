# PandasIndicatorCalc Refactoring

**Date:** October 9, 2025  
**Feature:** Renamed YFinanceIndicatorsProvider to PandasIndicatorCalc with OHLCV provider dependency injection  
**Type:** Architecture Improvement

## Overview

Refactored the indicators provider architecture to:
1. Rename `YFinanceIndicatorsProvider` to `PandasIndicatorCalc` (more accurate name reflecting pandas/stockstats usage)
2. Change from hardcoded YFinance dependency to accepting any OHLCV provider via constructor
3. Update `MarketIndicatorsInterface` to require OHLCV provider as mandatory constructor argument
4. Update toolkit to automatically pass configured OHLCV provider to indicator providers
5. Update all references in codebase and UI settings

## Motivation

### Why Rename?
- **Old Name:** `YFinanceIndicatorsProvider` implied dependency on Yahoo Finance specifically
- **New Name:** `PandasIndicatorCalc` accurately describes what it does:
  - Uses `pandas` DataFrames
  - Uses `stockstats` library for indicator calculations
  - Can work with ANY OHLCV data provider (not just YFinance)

### Why Dependency Injection?
- **Before:** Indicator provider hardcoded `YFinanceDataProvider()` internally
- **After:** Accepts any OHLCV provider via constructor
- **Benefits:**
  - Flexibility: Can use Alpha Vantage, local data, or any other OHLCV source
  - Consistency: OHLCV provider configured once in expert settings
  - Testability: Easy to inject mock providers for testing

## Architecture Changes

### 1. MarketIndicatorsInterface

**BEFORE:**
```python
class MarketIndicatorsInterface(DataProviderInterface):
    """Interface for technical indicator providers."""
    # No constructor requirement
```

**AFTER:**
```python
class MarketIndicatorsInterface(DataProviderInterface):
    """
    Interface for technical indicator providers.
    
    All indicator providers must accept an OHLCV data provider in their constructor
    to retrieve price data for indicator calculation.
    """
    
    @abstractmethod
    def __init__(self, ohlcv_provider: DataProviderInterface):
        """
        Initialize the indicator provider with an OHLCV data provider.
        
        Args:
            ohlcv_provider: Data provider implementing MarketDataProviderInterface
                           for retrieving OHLCV (Open, High, Low, Close, Volume) data
        """
        pass
```

### 2. PandasIndicatorCalc (formerly YFinanceIndicatorsProvider)

**File Renamed:**
- `YFinanceIndicatorsProvider.py` → `PandasIndicatorCalc.py`

**Class Renamed:**
- `YFinanceIndicatorsProvider` → `PandasIndicatorCalc`

**Constructor Changed:**

**BEFORE:**
```python
class YFinanceIndicatorsProvider(MarketIndicatorsInterface):
    """Yahoo Finance technical indicators provider."""
    
    def __init__(self):
        """Initialize YFinance indicators provider."""
        self._data_provider = YFinanceDataProvider()
        logger.debug("Initialized YFinanceIndicatorsProvider")
```

**AFTER:**
```python
class PandasIndicatorCalc(MarketIndicatorsInterface):
    """Pandas-based technical indicators calculator."""
    
    def __init__(self, ohlcv_provider: MarketDataProviderInterface):
        """
        Initialize Pandas indicator calculator.
        
        Args:
            ohlcv_provider: Any OHLCV data provider implementing MarketDataProviderInterface
        """
        self._data_provider = ohlcv_provider
        logger.debug(f"Initialized PandasIndicatorCalc with provider: {ohlcv_provider.__class__.__name__}")
```

### 3. Toolkit Provider Instantiation

Updated `_instantiate_provider()` method in `agent_utils_new.py`:

**BEFORE:**
```python
def _instantiate_provider(self, provider_class: Type[DataProviderInterface]) -> DataProviderInterface:
    """Instantiate a provider with appropriate arguments."""
    provider_name = provider_class.__name__
    
    # Check if this is an OpenAI provider that needs model argument
    if 'OpenAI' in provider_name and 'openai_model' in self.provider_args:
        model = self.provider_args['openai_model']
        return provider_class(model=model)
    else:
        return provider_class()
```

**AFTER:**
```python
def _instantiate_provider(self, provider_class: Type[DataProviderInterface]) -> DataProviderInterface:
    """Instantiate a provider with appropriate arguments."""
    provider_name = provider_class.__name__
    
    # Check if this is a MarketIndicatorsInterface that needs OHLCV provider
    from ba2_trade_platform.core.interfaces import MarketIndicatorsInterface
    if issubclass(provider_class, MarketIndicatorsInterface):
        # Get the first OHLCV provider from the provider_map
        ohlcv_provider = self._get_ohlcv_provider()
        if ohlcv_provider is None:
            raise ValueError(f"Cannot instantiate {provider_name}: No OHLCV provider configured")
        logger.debug(f"Instantiating {provider_name} with OHLCV provider: {ohlcv_provider.__class__.__name__}")
        return provider_class(ohlcv_provider=ohlcv_provider)
    
    # Check if this is an OpenAI provider that needs model argument
    elif 'OpenAI' in provider_name and 'openai_model' in self.provider_args:
        model = self.provider_args['openai_model']
        return provider_class(model=model)
    else:
        return provider_class()

def _get_ohlcv_provider(self) -> Optional[DataProviderInterface]:
    """
    Get the first available OHLCV provider instance.
    
    Returns:
        Instantiated OHLCV provider or None if not configured
    """
    if "ohlcv" not in self.provider_map or not self.provider_map["ohlcv"]:
        return None
    
    # Instantiate the first OHLCV provider
    ohlcv_provider_class = self.provider_map["ohlcv"][0]
    
    # Check if OpenAI provider needs model argument
    if 'OpenAI' in ohlcv_provider_class.__name__ and 'openai_model' in self.provider_args:
        model = self.provider_args['openai_model']
        return ohlcv_provider_class(model=model)
    else:
        return ohlcv_provider_class()
```

**Key Features:**
- Automatically detects if provider is a `MarketIndicatorsInterface`
- Instantiates first OHLCV provider from `provider_map["ohlcv"]`
- Passes OHLCV provider to indicator provider constructor
- Handles OpenAI model argument for OHLCV providers too
- Raises clear error if no OHLCV provider configured

### 4. Provider Registry Updates

**File:** `ba2_trade_platform/modules/dataproviders/__init__.py`

**Imports Changed:**
```python
# BEFORE
from .indicators import YFinanceIndicatorsProvider, AlphaVantageIndicatorsProvider

# AFTER
from .indicators import PandasIndicatorCalc, AlphaVantageIndicatorsProvider
```

**Registry Changed:**
```python
# BEFORE
INDICATORS_PROVIDERS: Dict[str, Type[MarketIndicatorsInterface]] = {
    "yfinance": YFinanceIndicatorsProvider,
    "alphavantage": AlphaVantageIndicatorsProvider,
}

# AFTER
INDICATORS_PROVIDERS: Dict[str, Type[MarketIndicatorsInterface]] = {
    "pandas": PandasIndicatorCalc,
    "alphavantage": AlphaVantageIndicatorsProvider,
}
```

**__all__ Export Changed:**
```python
# BEFORE
"YFinanceIndicatorsProvider",

# AFTER
"PandasIndicatorCalc",
```

**Usage Example Changed:**
```python
# BEFORE
yfinance_indicators = get_provider("indicators", "yfinance")
rsi = yfinance_indicators.get_indicator("AAPL", "rsi", end_date=datetime.now(), lookback_days=30)

# AFTER
from ba2_trade_platform.modules.dataproviders import YFinanceDataProvider
ohlcv_provider = YFinanceDataProvider()
pandas_indicators = get_provider("indicators", "pandas")(ohlcv_provider)
rsi = pandas_indicators.get_indicator("AAPL", "rsi", end_date=datetime.now(), lookback_days=30)
```

### 5. Indicators Submodule Updates

**File:** `ba2_trade_platform/modules/dataproviders/indicators/__init__.py`

```python
# BEFORE
from .YFinanceIndicatorsProvider import YFinanceIndicatorsProvider
__all__ = ["YFinanceIndicatorsProvider", "AlphaVantageIndicatorsProvider"]

# AFTER
from .PandasIndicatorCalc import PandasIndicatorCalc
__all__ = ["PandasIndicatorCalc", "AlphaVantageIndicatorsProvider"]
```

### 6. Expert Settings UI Updates

**File:** `ba2_trade_platform/modules/experts/TradingAgents.py`

**Setting Changed:**
```python
# BEFORE
"vendor_indicators": {
    "type": "list", "required": True, "default": ["yfinance"],
    "description": "Data vendor(s) for technical indicators",
    "valid_values": ["yfinance", "alpha_vantage", "local"],
    "tooltip": "...YFinance calculates indicators locally..."
},

# AFTER
"vendor_indicators": {
    "type": "list", "required": True, "default": ["pandas"],
    "description": "Data vendor(s) for technical indicators",
    "valid_values": ["pandas", "alpha_vantage", "local"],
    "tooltip": "...Pandas calculates indicators locally using configured OHLCV provider..."
},
```

## Files Modified

### Core Changes
1. **`ba2_trade_platform/core/interfaces/MarketIndicatorsInterface.py`**
   - Added abstract `__init__` method requiring `ohlcv_provider` parameter
   - Updated docstring to reflect dependency injection pattern

2. **`ba2_trade_platform/modules/dataproviders/indicators/YFinanceIndicatorsProvider.py`**
   - **RENAMED TO:** `PandasIndicatorCalc.py`
   - Changed class name from `YFinanceIndicatorsProvider` to `PandasIndicatorCalc`
   - Changed constructor to accept `ohlcv_provider` parameter
   - Removed hardcoded `YFinanceDataProvider()` instantiation
   - Updated docstrings and comments

3. **`ba2_trade_platform/thirdparties/TradingAgents/tradingagents/agents/utils/agent_utils_new.py`**
   - Updated `_instantiate_provider()` to detect `MarketIndicatorsInterface` subclasses
   - Added `_get_ohlcv_provider()` helper method
   - Automatically passes OHLCV provider to indicator providers

### Registry Changes
4. **`ba2_trade_platform/modules/dataproviders/__init__.py`**
   - Changed import from `YFinanceIndicatorsProvider` to `PandasIndicatorCalc`
   - Updated `INDICATORS_PROVIDERS` registry: `"yfinance"` → `"pandas"`
   - Updated `__all__` export list
   - Updated usage example in docstring

5. **`ba2_trade_platform/modules/dataproviders/indicators/__init__.py`**
   - Changed import from `YFinanceIndicatorsProvider` to `PandasIndicatorCalc`
   - Updated `__all__` export list
   - Updated docstring

### UI/Settings Changes
6. **`ba2_trade_platform/modules/experts/TradingAgents.py`**
   - Updated `vendor_indicators` setting:
     - Changed default from `["yfinance"]` to `["pandas"]`
     - Changed valid_values from `["yfinance", ...]` to `["pandas", ...]`
     - Updated tooltip text

## Migration Guide

### For Users
1. **Expert Settings Update:**
   - Old value: `vendor_indicators: ["yfinance"]`
   - New value: `vendor_indicators: ["pandas"]`
   - **Action:** Edit expert settings in UI and change "yfinance" to "pandas"

2. **Behavior:**
   - No functional change if using default YFinance OHLCV provider
   - Indicators now use same OHLCV provider as configured in `vendor_stock_data`
   - More consistent data sourcing

### For Developers
1. **Direct Instantiation:**
   ```python
   # OLD (won't work anymore)
   from ba2_trade_platform.modules.dataproviders import YFinanceIndicatorsProvider
   indicators = YFinanceIndicatorsProvider()  # ❌ Missing required argument
   
   # NEW (correct)
   from ba2_trade_platform.modules.dataproviders import PandasIndicatorCalc, YFinanceDataProvider
   ohlcv = YFinanceDataProvider()
   indicators = PandasIndicatorCalc(ohlcv_provider=ohlcv)  # ✅ Correct
   ```

2. **Via Registry:**
   ```python
   # OLD
   from ba2_trade_platform.modules.dataproviders import get_provider
   indicators = get_provider("indicators", "yfinance")  # ❌ Won't work
   
   # NEW
   from ba2_trade_platform.modules.dataproviders import get_provider, YFinanceDataProvider
   ohlcv = YFinanceDataProvider()
   IndicatorClass = get_provider("indicators", "pandas")
   indicators = IndicatorClass(ohlcv_provider=ohlcv)  # ✅ Correct
   ```

3. **In Toolkit (automatic):**
   ```python
   # Toolkit automatically handles OHLCV provider injection
   # No code changes needed
   ```

## Testing Recommendations

### 1. Basic Functionality
```python
# Test PandasIndicatorCalc with YFinance OHLCV
from ba2_trade_platform.modules.dataproviders import PandasIndicatorCalc, YFinanceDataProvider

ohlcv = YFinanceDataProvider()
indicators = PandasIndicatorCalc(ohlcv_provider=ohlcv)

# Test RSI calculation
rsi_data = indicators.get_indicator(
    symbol="AAPL",
    indicator="rsi",
    end_date=datetime.now(),
    lookback_days=30
)
assert rsi_data is not None
```

### 2. Alternative OHLCV Provider
```python
# Test PandasIndicatorCalc with Alpha Vantage OHLCV
from ba2_trade_platform.modules.dataproviders import PandasIndicatorCalc, AlphaVantageOHLCVProvider

ohlcv = AlphaVantageOHLCVProvider()
indicators = PandasIndicatorCalc(ohlcv_provider=ohlcv)

# Should work seamlessly with different OHLCV source
macd_data = indicators.get_indicator(
    symbol="MSFT",
    indicator="macd",
    end_date=datetime.now(),
    lookback_days=60
)
assert macd_data is not None
```

### 3. Toolkit Integration
```python
# Test automatic OHLCV provider injection in Toolkit
from ba2_trade_platform.thirdparties.TradingAgents.tradingagents.agents.utils.agent_utils_new import Toolkit
from ba2_trade_platform.modules.dataproviders import PandasIndicatorCalc, YFinanceDataProvider

provider_map = {
    "ohlcv": [YFinanceDataProvider],
    "indicators": [PandasIndicatorCalc]
}

toolkit = Toolkit(provider_map=provider_map)

# Should automatically pass YFinance OHLCV to PandasIndicatorCalc
indicator_data = toolkit.get_indicator_data(
    symbol="TSLA",
    indicator="close_50_sma",
    start_date="2024-01-01",
    end_date="2024-10-09"
)
assert "50-day Simple Moving Average" in indicator_data
```

### 4. Error Handling
```python
# Test error when no OHLCV provider configured
from ba2_trade_platform.thirdparties.TradingAgents.tradingagents.agents.utils.agent_utils_new import Toolkit
from ba2_trade_platform.modules.dataproviders import PandasIndicatorCalc

provider_map = {
    # No OHLCV provider configured
    "indicators": [PandasIndicatorCalc]
}

toolkit = Toolkit(provider_map=provider_map)

# Should raise clear error
try:
    toolkit.get_indicator_data("AAPL", "rsi", "2024-01-01", "2024-10-09")
    assert False, "Should have raised ValueError"
except ValueError as e:
    assert "No OHLCV provider configured" in str(e)
```

## Benefits

### 1. Flexibility
- ✅ Can use ANY OHLCV provider (YFinance, Alpha Vantage, custom, etc.)
- ✅ No longer hardcoded to YFinance
- ✅ Easy to test with mock providers

### 2. Consistency
- ✅ OHLCV provider configured once in expert settings
- ✅ Same data source for price data and indicators
- ✅ Reduces data inconsistencies

### 3. Clarity
- ✅ Name accurately reflects what the class does (pandas calculations)
- ✅ Dependency injection makes dependencies explicit
- ✅ Clear error messages when OHLCV provider missing

### 4. Maintainability
- ✅ Toolkit automatically handles provider wiring
- ✅ Easy to add new OHLCV providers
- ✅ Follows dependency injection best practices

## Backward Compatibility

### Breaking Changes
1. **Class Name:** `YFinanceIndicatorsProvider` → `PandasIndicatorCalc`
2. **Constructor:** Now requires `ohlcv_provider` parameter
3. **Registry Key:** `"yfinance"` → `"pandas"` in `INDICATORS_PROVIDERS`

### Non-Breaking
- **Toolkit Usage:** Automatic OHLCV injection means no code changes needed in toolkit-based code
- **Functionality:** Same indicator calculations using same stockstats library
- **Data Format:** Same output format and API signatures

## Future Enhancements

### Possible Improvements
1. **Multiple OHLCV Sources:**
   - Could accept list of OHLCV providers for fallback
   - Try first provider, fall back to second if data missing

2. **Caching:**
   - Cache calculated indicators to avoid recalculation
   - Share cache across multiple indicator provider instances

3. **Custom Indicators:**
   - Allow registration of custom indicator formulas
   - Extend beyond stockstats built-in indicators

4. **Parallel Calculation:**
   - Calculate multiple indicators in parallel
   - Optimize for batch requests

## Conclusion

The refactoring from `YFinanceIndicatorsProvider` to `PandasIndicatorCalc` with dependency injection:

✅ **Improves naming accuracy** - reflects actual implementation (pandas/stockstats)  
✅ **Increases flexibility** - works with any OHLCV provider  
✅ **Enhances maintainability** - explicit dependencies via constructor  
✅ **Maintains compatibility** - toolkit automatically handles injection  
✅ **Follows best practices** - dependency injection pattern  

**Key Takeaway:** Indicator providers now accept OHLCV providers via constructor, making the system more flexible and testable while maintaining the same calculation logic and output format.
