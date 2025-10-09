# OpenAI Provider Model Configuration via Expert Settings

**Date:** October 9, 2025  
**Feature:** Added expert setting to configure OpenAI model for data providers  
**Default:** `gpt-5`

## Overview

Added ability to configure which OpenAI model is used by OpenAI data providers (OpenAINewsProvider, OpenAICompanyOverviewProvider) through TradingAgents expert settings, rather than hardcoding or using global app settings.

## Changes Implemented

### 1. Added Expert Setting: `openai_provider_model`

**File:** `ba2_trade_platform/modules/experts/TradingAgents.py`

**New Setting:**
```python
"openai_provider_model": {
    "type": "str", 
    "required": True, 
    "default": "gpt-5",
    "description": "OpenAI model for data provider web searches",
    "valid_values": ["gpt-5", "gpt-5-mini", "gpt-5-nano", "gpt-4.1", "gpt-4.1-mini", "gpt-4.1-nano", "o4-mini", "o4-mini-deep-research"],
    "help": "For more information on available models, see [OpenAI Models Documentation](https://platform.openai.com/docs/models)",
    "tooltip": "The OpenAI model used by OpenAI data providers (news, fundamentals) for web search and data gathering. Higher-tier models (gpt-5) provide better search results and data extraction. Use mini/nano for cost savings. This setting only affects OpenAI-based data providers."
}
```

**Purpose:**
- Allows per-expert configuration of OpenAI model used for data gathering
- Separate from LLM models used for analysis (deep_think_llm, quick_think_llm)
- Default to `gpt-5` for best data quality

### 2. Enhanced Toolkit with Provider Arguments

**File:** `ba2_trade_platform/thirdparties/TradingAgents/tradingagents/agents/utils/agent_utils_new.py`

**Updated Constructor:**
```python
def __init__(self, provider_map: Dict[str, List[Type[DataProviderInterface]]], provider_args: Dict[str, any] = None):
    """
    Initialize toolkit with provider configuration.
    
    Args:
        provider_map: Dictionary mapping provider categories to list of provider classes
        provider_args: Optional dictionary with arguments for provider instantiation
                      (e.g., {"openai_model": "gpt-5"})
    """
    self.provider_map = provider_map
    self.provider_args = provider_args or {}
    logger.debug(f"Toolkit initialized with provider_map keys: {list(provider_map.keys())}")
    if self.provider_args:
        logger.debug(f"Provider args: {self.provider_args}")
```

**New Method:**
```python
def _instantiate_provider(self, provider_class: Type[DataProviderInterface]) -> DataProviderInterface:
    """
    Instantiate a provider with appropriate arguments.
    
    Args:
        provider_class: Provider class to instantiate
        
    Returns:
        Instantiated provider
    """
    provider_name = provider_class.__name__
    
    # Check if this is an OpenAI provider that needs model argument
    if 'OpenAI' in provider_name and 'openai_model' in self.provider_args:
        model = self.provider_args['openai_model']
        logger.debug(f"Instantiating {provider_name} with model={model}")
        return provider_class(model=model)
    else:
        # Standard instantiation with no arguments
        return provider_class()
```

**Updated All Provider Instantiations:**
```python
# BEFORE
provider = provider_class()

# AFTER
provider = self._instantiate_provider(provider_class)
```

### 3. Updated TradingAgentsGraph

**File:** `ba2_trade_platform/thirdparties/TradingAgents/tradingagents/graph/trading_graph.py`

**Updated Constructor:**
```python
def __init__(
    self,
    selected_analysts=["market", "social", "news", "fundamentals", "macro"],
    debug=False,
    config: Dict[str, Any] = None,
    market_analysis_id: Optional[int] = None,
    expert_instance_id: Optional[int] = None,
    provider_map: Optional[Dict[str, List[type]]] = None,
    provider_args: Optional[Dict[str, Any]] = None,  # NEW PARAMETER
):
    """Initialize the trading agents graph and components.

    Args:
        ...
        provider_args: Optional arguments for provider instantiation (e.g., {"openai_model": "gpt-5"})
    """
    ...
    self.provider_args = provider_args or {}  # Store provider_args
    ...
    self.toolkit = Toolkit(provider_map=self.provider_map, provider_args=self.provider_args)
```

### 4. Updated TradingAgents Expert

**File:** `ba2_trade_platform/modules/experts/TradingAgents.py`

**Build Provider Args:**
```python
def _execute_tradingagents_analysis(self, symbol: str, market_analysis_id: int, subtype: str) -> tuple:
    """Execute the core TradingAgents analysis."""
    # Create configuration
    config = self._create_tradingagents_config(subtype)
    
    # Build provider_map for new toolkit
    provider_map = self._build_provider_map()
    
    # Build provider_args for OpenAI providers
    settings_def = self.get_settings_definitions()
    openai_model = self.settings.get('openai_provider_model', settings_def['openai_provider_model']['default'])
    provider_args = {
        'openai_model': openai_model
    }
    
    # Log provider_map configuration
    logger.info(f"=== TradingAgents Provider Configuration ===")
    for category, providers in provider_map.items():
        provider_names = [p.__name__ for p in providers] if providers else ["None"]
        logger.info(f"  {category}: {', '.join(provider_names)}")
    logger.info(f"  provider_args: {provider_args}")
    logger.info(f"============================================")
    
    # Initialize TradingAgents graph
    debug_mode = self.settings.get('debug_mode', True)
    
    ta_graph = TradingAgentsGraph(
        debug=debug_mode,
        config=config,
        market_analysis_id=market_analysis_id,
        expert_instance_id=self.id,
        provider_map=provider_map,
        provider_args=provider_args  # Pass provider_args
    )
```

## Data Flow

```
User configures TradingAgents expert
    ↓
Expert Settings: openai_provider_model = "gpt-5"
    ↓
TradingAgents._execute_tradingagents_analysis()
    ↓
Build provider_args = {"openai_model": "gpt-5"}
    ↓
TradingAgentsGraph(provider_map=..., provider_args=...)
    ↓
Toolkit(provider_map=..., provider_args=...)
    ↓
toolkit.get_company_news(...)
    ↓
for provider_class in provider_map["news"]:
    provider = self._instantiate_provider(provider_class)
    ↓
    if "OpenAI" in provider_class.__name__:
        provider = OpenAINewsProvider(model="gpt-5")  ← Uses configured model
    else:
        provider = FMPNewsProvider()  ← Standard instantiation
```

## Configuration Examples

### Example 1: High-Quality Data (Expensive)
```python
expert_settings = {
    "openai_provider_model": "gpt-5",  # Best quality
    "deep_think_llm": "gpt-5",
    "quick_think_llm": "gpt-5-mini"
}
```

### Example 2: Cost-Optimized (Cheaper)
```python
expert_settings = {
    "openai_provider_model": "gpt-5-nano",  # Minimal cost for data gathering
    "deep_think_llm": "gpt-5-mini",
    "quick_think_llm": "gpt-5-nano"
}
```

### Example 3: Balanced (Recommended)
```python
expert_settings = {
    "openai_provider_model": "gpt-5",  # Default - good data quality
    "deep_think_llm": "gpt-5-mini",    # Good analysis at reasonable cost
    "quick_think_llm": "gpt-5-nano"    # Fast for simple tasks
}
```

## Benefits

### 1. Per-Expert Configuration
- Each TradingAgents expert instance can use different OpenAI model
- Useful for A/B testing different model tiers
- Cost optimization per use case

### 2. Separation of Concerns
- **Analysis LLMs** (deep_think_llm, quick_think_llm): AI reasoning and decision-making
- **Provider LLM** (openai_provider_model): Web search and data gathering
- Can optimize each independently

### 3. Flexibility
- Can use expensive model for analysis, cheap model for data gathering (or vice versa)
- Easy to test impact of different models on data quality
- Future-proof for new OpenAI models

### 4. Extensibility
- `provider_args` dictionary can be extended with other parameters
- Pattern can be reused for other configurable providers
- Clean architecture for provider instantiation

## Logging

The system now logs provider arguments at startup:

```
=== TradingAgents Provider Configuration ===
  news: OpenAINewsProvider, AlphaVantageNewsProvider
  social_media: OpenAINewsProvider, AlphaVantageNewsProvider
  insider: FMPInsiderProvider
  macro: FREDMacroProvider
  fundamentals_details: OpenAICompanyOverviewProvider, YFinanceCompanyDetailsProvider
  ohlcv: YFinanceDataProvider
  indicators: YFinanceIndicatorsProvider
  provider_args: {'openai_model': 'gpt-5'}
============================================
```

And when instantiating OpenAI providers:

```
Instantiating OpenAINewsProvider with model=gpt-5
OpenAINewsProvider initialized with model=gpt-5, backend_url=https://api.openai.com/v1
```

## Files Modified

1. **TradingAgents.py** - Added `openai_provider_model` setting, build and pass provider_args
2. **agent_utils_new.py** - Added `provider_args` parameter, `_instantiate_provider` method, updated all provider instantiations
3. **trading_graph.py** - Added `provider_args` parameter, pass to Toolkit

## Testing Recommendations

### 1. Test Default Model
```python
# Create expert with default settings
# Verify OpenAI providers use gpt-5
```

### 2. Test Custom Model
```python
# Set openai_provider_model to "gpt-5-mini"
# Verify OpenAI providers use gpt-5-mini
```

### 3. Test Non-OpenAI Providers
```python
# Verify FMP, AlphaVantage, YFinance providers still work
# Should use standard instantiation (no model parameter)
```

### 4. Test Mixed Providers
```python
# Configure: vendor_news = ["openai", "fmp", "alpha_vantage"]
# Verify only OpenAI provider gets model argument
# Verify all providers return data successfully
```

### 5. Test Logging
```python
# Check startup logs show provider_args
# Check OpenAI provider instantiation logs show correct model
```

## Migration Notes

### For Existing Experts

**No migration needed** - existing TradingAgents expert instances will automatically use default `gpt-5` for OpenAI providers.

### For New Experts

When creating new TradingAgents expert instance, user can now configure `openai_provider_model` in settings UI.

### For Custom Code

If instantiating TradingAgentsGraph directly:

```python
# BEFORE
ta_graph = TradingAgentsGraph(
    provider_map=provider_map
)

# AFTER (optional - can omit provider_args to use defaults)
ta_graph = TradingAgentsGraph(
    provider_map=provider_map,
    provider_args={"openai_model": "gpt-5"}
)
```

## Future Enhancements

### 1. Per-Provider Model Configuration
Allow different models for different OpenAI providers:

```python
provider_args = {
    "news_model": "gpt-5",
    "fundamentals_model": "gpt-5-mini"
}
```

### 2. Provider-Specific Parameters
Extend to other provider types:

```python
provider_args = {
    "openai_model": "gpt-5",
    "fmp_api_key": "custom_key",
    "alpha_vantage_timeout": 30
}
```

### 3. Dynamic Model Selection
Choose model based on task complexity:

```python
def _get_model_for_task(task_type):
    if task_type == "simple":
        return "gpt-5-nano"
    elif task_type == "complex":
        return "gpt-5"
```

## Conclusion

This enhancement provides:
- ✅ **Per-expert OpenAI model configuration** via `openai_provider_model` setting
- ✅ **Clean provider instantiation** through `provider_args` dictionary
- ✅ **Backward compatibility** - defaults to `gpt-5` for best quality
- ✅ **Extensibility** - pattern can be reused for other configurable providers
- ✅ **Good logging** - visibility into which models are being used

The system now allows fine-grained control over OpenAI model selection for data gathering, separate from analysis LLM configuration, enabling better cost optimization and quality tuning.
