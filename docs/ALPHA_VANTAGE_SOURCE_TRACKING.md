# Alpha Vantage Source Tracking Refactoring

**Date**: October 10, 2025  
**Purpose**: Refactor Alpha Vantage providers to support configurable source tracking for API analytics

## Overview

This refactoring introduces a base class pattern for all Alpha Vantage providers, enabling configurable source tracking for API calls. This allows different components of the BA2 platform (e.g., direct usage vs TradingAgents) to be identified separately in Alpha Vantage's usage analytics.

## Key Benefits

1. **Better API Analytics**: Differentiate API usage between platform components
2. **Cleaner Architecture**: All Alpha Vantage providers inherit from common base class
3. **Backward Compatible**: Existing code continues to work with default source
4. **Configurable**: TradingAgents can use custom source identifier

## Architecture Changes

### 1. New Base Class: `AlphaVantageBaseProvider`

**File**: `ba2_trade_platform/modules/dataproviders/alpha_vantage_common.py`

```python
class AlphaVantageBaseProvider:
    """
    Base class for all Alpha Vantage data providers.
    
    This class provides common functionality for Alpha Vantage API calls,
    including source tracking for better usage analytics.
    
    Child classes should call super().__init__(source) in their constructors.
    """
    
    def __init__(self, source: str = "ba2_trade_platform"):
        """
        Initialize the Alpha Vantage base provider.
        
        Args:
            source: Source identifier for API tracking (e.g., 'ba2_trade_platform', 'trading_agents')
        """
        self.source = source
        logger.debug(f"AlphaVantageBaseProvider initialized with source: {source}")
    
    def make_api_request(self, function_name: str, params: dict) -> dict | str:
        """
        Make API request to Alpha Vantage with source tracking.
        
        This is an instance method that uses the provider's configured source.
        """
        return make_api_request(function_name, params, source=self.source)
```

**Key Features**:
- Stores `source` parameter as instance variable
- Provides `make_api_request()` instance method that automatically passes source
- Default source is `"ba2_trade_platform"` for backward compatibility

### 2. Updated `make_api_request()` Function

**File**: `ba2_trade_platform/modules/dataproviders/alpha_vantage_common.py`

```python
def make_api_request(function_name: str, params: dict, source: str = "ba2_trade_platform") -> dict | str:
    """
    Make API request to Alpha Vantage.
    
    Args:
        function_name: Alpha Vantage function name
        params: Additional parameters for the API request
        source: Source identifier for API tracking (default: 'ba2_trade_platform')
    """
    api_params = params.copy()
    api_params.update({
        "function": function_name,
        "apikey": get_api_key(),
        "source": source,  # Now configurable!
    })
    # ... rest of implementation
```

**Changes**:
- Added optional `source` parameter (default: `"ba2_trade_platform"`)
- Source is now passed in API params instead of hardcoded

### 3. Updated Alpha Vantage Providers

All Alpha Vantage providers now:
1. Inherit from `AlphaVantageBaseProvider`
2. Accept optional `source` parameter in constructor
3. Call both parent `__init__` methods (multiple inheritance)
4. Use `self.make_api_request()` instead of module-level function

**Updated Providers**:
- `AlphaVantageNewsProvider`
- `AlphaVantageOHLCVProvider`
- `AlphaVantageIndicatorsProvider`
- `AlphaVantageCompanyOverviewProvider`
- `AlphaVantageCompanyDetailsProvider`

**Example Pattern**:
```python
class AlphaVantageNewsProvider(AlphaVantageBaseProvider, MarketNewsInterface):
    def __init__(self, source: str = "ba2_trade_platform"):
        """
        Initialize the Alpha Vantage News Provider.
        
        Args:
            source: Source identifier for API tracking
        """
        AlphaVantageBaseProvider.__init__(self, source)
        MarketNewsInterface.__init__(self)
        logger.info(f"AlphaVantageNewsProvider initialized with source: {source}")
    
    def get_company_news(self, ...):
        # Use instance method that automatically includes source
        raw_data = self.make_api_request("NEWS_SENTIMENT", params)
```

### 4. Enhanced `get_provider()` Function

**File**: `ba2_trade_platform/modules/dataproviders/__init__.py`

```python
def get_provider(category: str, provider_name: str, **kwargs) -> DataProviderInterface:
    """
    Get a provider instance by category and name.
    
    Args:
        category: Provider category
        provider_name: Provider name
        **kwargs: Additional arguments to pass to the provider constructor
                 (e.g., source='trading_agents' for Alpha Vantage providers)
    
    Example:
        # Default source
        news_provider = get_provider("news", "alphavantage")
        
        # Custom source
        av_news = get_provider("news", "alphavantage", source="trading_agents")
    """
    # ... get provider_class ...
    
    # Try to instantiate with kwargs, fall back to no-arg constructor
    try:
        return provider_class(**kwargs)
    except TypeError:
        # Provider doesn't accept these kwargs, use default constructor
        if kwargs:
            logger.warning(f"Provider {provider_name} doesn't accept arguments: {list(kwargs.keys())}")
        return provider_class()
```

**Key Features**:
- Accepts `**kwargs` to pass to provider constructor
- Gracefully handles providers that don't support kwargs
- Maintains backward compatibility with existing code

## TradingAgents Integration

### 1. New Setting: `alpha_vantage_source`

**File**: `ba2_trade_platform/modules/experts/TradingAgents.py`

Added new setting to TradingAgents expert:

```python
"alpha_vantage_source": {
    "type": "str", 
    "required": True, 
    "default": "trading_agents",
    "description": "Source identifier for Alpha Vantage API calls",
    "tooltip": "This identifier is sent with Alpha Vantage API requests for usage tracking..."
}
```

**Benefits**:
- Users can see where their API calls are coming from
- Default `"trading_agents"` differentiates from platform usage
- Can be customized for advanced tracking scenarios

### 2. Provider Arguments Configuration

**File**: `ba2_trade_platform/modules/experts/TradingAgents.py`

```python
# Build provider_args for OpenAI and Alpha Vantage providers
settings_def = self.get_settings_definitions()
openai_model = self.settings.get('openai_provider_model', ...)
alpha_vantage_source = self.settings.get('alpha_vantage_source', ...)

provider_args = {
    'openai_model': openai_model,
    'alpha_vantage_source': alpha_vantage_source  # NEW!
}
```

Provider args are passed to `TradingAgentsGraph` → `Toolkit` → providers

### 3. Toolkit Provider Instantiation

**File**: `ba2_trade_platform/thirdparties/TradingAgents/tradingagents/agents/utils/agent_utils_new.py`

```python
def _instantiate_provider(self, provider_class: Type[DataProviderInterface]) -> DataProviderInterface:
    """Instantiate a provider with appropriate arguments."""
    provider_name = provider_class.__name__
    
    # ... existing MarketIndicatorsInterface handling ...
    
    # Check if this is an OpenAI provider
    if 'OpenAI' in provider_name and 'openai_model' in self.provider_args:
        model = self.provider_args['openai_model']
        return provider_class(model=model)
    
    # Check if this is an Alpha Vantage provider (NEW!)
    elif 'AlphaVantage' in provider_name and 'alpha_vantage_source' in self.provider_args:
        source = self.provider_args['alpha_vantage_source']
        logger.debug(f"Instantiating {provider_name} with source={source}")
        return provider_class(source=source)
    
    else:
        return provider_class()
```

**Also updated** `_get_ohlcv_provider()` with same logic for OHLCV providers.

## Usage Examples

### Direct Provider Usage (Platform)

```python
# Default source: "ba2_trade_platform"
from ba2_trade_platform.modules.dataproviders import get_provider

news_provider = get_provider("news", "alphavantage")
# API calls will have source="ba2_trade_platform"
```

### Custom Source (Advanced)

```python
# Custom source for specific use case
news_provider = get_provider("news", "alphavantage", source="my_custom_app")
# API calls will have source="my_custom_app"
```

### TradingAgents (Automatic)

```python
# TradingAgents automatically uses configured source
# Default: source="trading_agents"
# Configurable through expert settings UI
```

## Backward Compatibility

✅ **All existing code continues to work unchanged!**

1. **Default Source**: All providers default to `"ba2_trade_platform"`
2. **No-Arg Constructors**: Providers can still be instantiated without arguments
3. **Graceful Fallback**: `get_provider()` handles providers that don't accept kwargs
4. **Optional Parameter**: `source` parameter is optional everywhere

## Testing

### Test Alpha Vantage Provider with Custom Source

```python
from ba2_trade_platform.modules.dataproviders import get_provider
from datetime import datetime

# Test with custom source
news_provider = get_provider("news", "alphavantage", source="test_harness")

news = news_provider.get_company_news(
    symbol="AAPL",
    end_date=datetime.now(),
    lookback_days=7
)

# Check logs for: "AlphaVantageNewsProvider initialized with source: test_harness"
```

### Test TradingAgents Source Configuration

1. Open TradingAgents expert settings in UI
2. Locate "Alpha Vantage Source" setting
3. Change value (e.g., to `"my_trading_bot"`)
4. Save settings
5. Run analysis
6. Check logs for: `Instantiating AlphaVantageNewsProvider with source=my_trading_bot`

## Files Modified

### Core Infrastructure
- `ba2_trade_platform/modules/dataproviders/alpha_vantage_common.py`
  - Added `AlphaVantageBaseProvider` class
  - Updated `make_api_request()` with source parameter

- `ba2_trade_platform/modules/dataproviders/__init__.py`
  - Updated `get_provider()` to accept **kwargs
  - Added import for logger

### Alpha Vantage Providers
- `ba2_trade_platform/modules/dataproviders/news/AlphaVantageNewsProvider.py`
- `ba2_trade_platform/modules/dataproviders/ohlcv/AlphaVantageOHLCVProvider.py`
- `ba2_trade_platform/modules/dataproviders/indicators/AlphaVantageIndicatorsProvider.py`
- `ba2_trade_platform/modules/dataproviders/fundamentals/overview/AlphaVantageCompanyOverviewProvider.py`
- `ba2_trade_platform/modules/dataproviders/fundamentals/details/AlphaVantageCompanyDetailsProvider.py`

All updated to:
- Inherit from `AlphaVantageBaseProvider`
- Accept `source` parameter in constructor
- Use `self.make_api_request()` instance method

### TradingAgents Integration
- `ba2_trade_platform/modules/experts/TradingAgents.py`
  - Added `alpha_vantage_source` setting
  - Updated provider_args to include alpha_vantage_source

- `ba2_trade_platform/thirdparties/TradingAgents/tradingagents/agents/utils/agent_utils_new.py`
  - Updated `_instantiate_provider()` to handle Alpha Vantage source
  - Updated `_get_ohlcv_provider()` to handle Alpha Vantage source

## Benefits Summary

### For Platform Administrators
- ✅ **Better API Analytics**: See exactly where API calls originate
- ✅ **Usage Attribution**: Differentiate TradingAgents from other components
- ✅ **Cost Tracking**: Identify which features use most API quota

### For Developers
- ✅ **Cleaner Code**: Inheritance-based architecture vs function calls
- ✅ **Extensibility**: Easy to add new Alpha Vantage providers
- ✅ **Type Safety**: Instance methods instead of module-level functions

### For Users
- ✅ **Transparency**: See source identifier in expert settings
- ✅ **Customization**: Can change source for tracking purposes
- ✅ **No Breaking Changes**: Everything works as before

## Future Enhancements

Potential improvements for future releases:

1. **Per-Analyst Sources**: Different source for each TradingAgents analyst
2. **Dynamic Sources**: Include timestamp or session ID in source
3. **Source Analytics Dashboard**: UI to view API usage by source
4. **Source-Based Rate Limiting**: Different limits for different sources
5. **Multi-Account Sources**: Different sources for different trading accounts

## Migration Notes

**For Existing Deployments:**
- No action required! All defaults maintain current behavior
- Optional: Configure `alpha_vantage_source` in TradingAgents for better tracking
- Optional: Review Alpha Vantage analytics to see source attribution

**For Custom Integrations:**
- If you've created custom Alpha Vantage providers, consider inheriting from `AlphaVantageBaseProvider`
- If you call `get_provider()`, you can now pass `source` parameter
- Check logs to verify source parameter is being passed correctly

## Related Documentation

- [Data Provider Architecture](./DATA_PROVIDER_QUICK_REFERENCE.md)
- [TradingAgents Configuration](./TRADINGAGENTS_CONFIGURATION.md)
- [Alpha Vantage API Documentation](https://www.alphavantage.co/documentation/)
