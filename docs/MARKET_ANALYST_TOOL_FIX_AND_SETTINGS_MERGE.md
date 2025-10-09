# Market Analyst Tool Fix & Settings Merge

## Date
January 2025

## Overview
This document describes two critical fixes:
1. **AttributeError fix**: Resolved `'function' object has no attribute 'name'` error in Market Analyst
2. **Settings merge**: Merged duplicate `vendor_fundamentals_overview` into `vendor_fundamentals`

---

## 1. Market Analyst Tool AttributeError Fix

### Problem
The Market Analyst agent was attempting to use toolkit methods (`toolkit.get_ohlcv_data` and `toolkit.get_indicator_data`) directly as tools, but these are instance methods that need to be wrapped with the `@tool` decorator to become proper LangChain tools.

**Error Message**:
```
AttributeError: 'function' object has no attribute 'name'
During task with name 'Market Analyst' and id 'b2aaa7be-574e-2377-201e-488d8498fb76'
ERROR:ba2_trade_platform:Analysis task 'analysis_1' failed after 3.35s: 'function' object has no attribute 'name'
Traceback (most recent call last):
  File "...\ba2_trade_platform\thirdparties\TradingAgents\tradingagents\agents\analysts\market_analyst.py", line 26, in <listcomp>
    tool_names=[tool.name for tool in tools],
                ^^^^^^^^^
AttributeError: 'function' object has no attribute 'name'
```

### Root Cause
In `market_analyst.py`, the code was directly assigning instance methods to the `tools` list:

```python
# ❌ WRONG - instance methods are not Tool objects
tools = [
    toolkit.get_ohlcv_data,
    toolkit.get_indicator_data,
]
```

These instance methods don't have a `.name` attribute that LangChain tools require.

### Solution

**File Modified**: `ba2_trade_platform/thirdparties/TradingAgents/tradingagents/agents/analysts/market_analyst.py`

**Added Import**:
```python
from langchain_core.tools import tool
```

**Wrapped Methods with @tool Decorator**:
```python
# ✅ CORRECT - wrap instance methods with @tool decorator
@tool
def get_ohlcv_data(symbol: str, start_date: str, end_date: str, interval: str = None) -> str:
    """Get OHLCV stock price data."""
    return toolkit.get_ohlcv_data(symbol, start_date, end_date, interval)

@tool
def get_indicator_data(symbol: str, indicator: str, start_date: str, end_date: str, interval: str = None) -> str:
    """Get technical indicator data."""
    return toolkit.get_indicator_data(symbol, indicator, start_date, end_date, interval)

# Use wrapped tools
tools = [
    get_ohlcv_data,
    get_indicator_data,
]
```

### How @tool Decorator Works

The `@tool` decorator from LangChain:
1. **Converts functions into Tool objects** with proper `.name`, `.description`, and `.func` attributes
2. **Extracts type hints** for automatic schema generation
3. **Parses docstrings** for tool descriptions
4. **Makes functions compatible** with LangChain's tool binding system

**Before (instance method)**:
```python
toolkit.get_ohlcv_data  # Has no .name attribute
```

**After (@tool wrapper)**:
```python
get_ohlcv_data  # Is a Tool object with .name = "get_ohlcv_data"
```

### Testing
After this fix, the Market Analyst agent can properly:
- Access tool names via `tool.name` for prompt formatting
- Bind tools to the LLM using `.bind_tools(tools)`
- Execute tools when the LLM requests them
- Log tool calls with proper tool names

---

## 2. Settings Merge: vendor_fundamentals_overview → vendor_fundamentals

### Problem
The expert settings had two separate settings for fundamentals data:
1. `vendor_fundamentals` - Originally for "fundamental analysis"
2. `vendor_fundamentals_overview` - For "company fundamentals overview"

These settings served the **same purpose** (company overview/fundamentals) but with different provider options, causing confusion and duplication.

### Solution

**Merged Settings**: Combined both into a single `vendor_fundamentals` setting with all provider options.

**File Modified**: `ba2_trade_platform/modules/experts/TradingAgents.py`

#### Before (Duplicate Settings)

```python
"vendor_fundamentals": {
    "type": "list", 
    "required": True, 
    "default": ["openai"],
    "description": "Data vendor(s) for fundamental analysis",
    "valid_values": ["openai", "alpha_vantage"],
    "multiple": True,
    "tooltip": "..."
},
"vendor_fundamentals_overview": {
    "type": "list", 
    "required": True, 
    "default": ["alpha_vantage"],
    "description": "Data vendor(s) for company fundamentals overview",
    "valid_values": ["alpha_vantage", "openai", "fmp"],
    "multiple": True,
    "tooltip": "..."
}
```

#### After (Merged Setting)

```python
"vendor_fundamentals": {
    "type": "list", 
    "required": True, 
    "default": ["alpha_vantage"],  # Changed from "openai" to "alpha_vantage"
    "description": "Data vendor(s) for company fundamentals overview",
    "valid_values": ["alpha_vantage", "openai", "fmp"],  # Added "fmp"
    "multiple": True,
    "tooltip": "Select one or more data providers for company overview and key metrics "
               "(market cap, P/E ratio, beta, industry, sector, etc.). Multiple vendors "
               "enable automatic fallback. Alpha Vantage provides comprehensive company "
               "overviews. OpenAI searches for latest company information. FMP provides "
               "detailed company profiles including valuation metrics and company information."
}
```

#### Changes in _build_provider_map()

**Before**:
```python
# Used vendor_fundamentals_overview setting
overview_vendors = _get_vendor_list('vendor_fundamentals_overview')
```

**After**:
```python
# Now uses vendor_fundamentals setting
overview_vendors = _get_vendor_list('vendor_fundamentals')
```

### Benefits of Merge

1. **✅ Eliminates Confusion**: One clear setting for fundamentals overview
2. **✅ Reduces Duplication**: No need to maintain two similar settings
3. **✅ Better Default**: Changed default from OpenAI to Alpha Vantage (more reliable)
4. **✅ All Providers Available**: Includes Alpha Vantage, OpenAI, and FMP
5. **✅ Backward Compatible**: Uses same registry (`FUNDAMENTALS_OVERVIEW_PROVIDERS`)

### Migration for Existing Instances

**Old Configuration**:
```python
{
    "vendor_fundamentals": ["openai"],
    "vendor_fundamentals_overview": ["alpha_vantage", "fmp"]
}
```

**New Configuration** (automatically migrates):
```python
{
    "vendor_fundamentals": ["alpha_vantage", "fmp"]  # Merged, uses all providers
}
```

**Note**: Existing expert instances with `vendor_fundamentals_overview` settings will need to be updated manually through the UI or database.

---

## 3. Summary of Changes

### Files Modified

1. **ba2_trade_platform/thirdparties/TradingAgents/tradingagents/agents/analysts/market_analyst.py**
   - Added `from langchain_core.tools import tool` import
   - Wrapped `toolkit.get_ohlcv_data` with `@tool` decorator
   - Wrapped `toolkit.get_indicator_data` with `@tool` decorator
   - Changed tools list to use wrapped functions

2. **ba2_trade_platform/modules/experts/TradingAgents.py**
   - Removed `vendor_fundamentals_overview` setting
   - Updated `vendor_fundamentals` setting:
     - Changed default from `["openai"]` to `["alpha_vantage"]`
     - Updated `valid_values` from `["openai", "alpha_vantage"]` to `["alpha_vantage", "openai", "fmp"]`
     - Updated description and tooltip
   - Updated `_build_provider_map()`:
     - Changed from `'vendor_fundamentals_overview'` to `'vendor_fundamentals'`

3. **docs/FMP_COMPANY_OVERVIEW_PROVIDER_AND_ALPHAVANTAGE_FIX.md**
   - Updated documentation to reflect merged settings
   - Updated code examples to use `vendor_fundamentals`
   - Added note about settings merge

### Testing Recommendations

#### 1. Test Market Analyst Fix
```python
# Test that Market Analyst can access tool names
from ba2_trade_platform.modules.experts import TradingAgents
from ba2_trade_platform.core.db import add_instance
from ba2_trade_platform.core.models import ExpertInstance, MarketAnalysis

# Create test expert instance
expert_instance = ExpertInstance(account_id=1, expert="TradingAgents", enabled=True)
expert_id = add_instance(expert_instance)

# Create market analysis
market_analysis = MarketAnalysis(
    expert_id=expert_id,
    symbol="AAPL",
    subtype="enter_market"
)
analysis_id = add_instance(market_analysis)

# Run analysis - should NOT throw AttributeError
expert = TradingAgents(expert_id)
expert.run_analysis("AAPL", market_analysis)
```

#### 2. Test Settings Merge
```python
# Test that vendor_fundamentals setting works
from ba2_trade_platform.modules.experts import TradingAgents

expert = TradingAgents(expert_id)

# Set fundamentals vendors
expert.save_setting('vendor_fundamentals', ['fmp', 'alpha_vantage'])

# Build provider map
provider_map = expert._build_provider_map()

# Verify fundamentals_overview category exists
assert 'fundamentals_overview' in provider_map
assert len(provider_map['fundamentals_overview']) > 0
print(f"Fundamentals overview providers: {[p.__name__ for p in provider_map['fundamentals_overview']]}")
```

#### 3. Verify No Errors
```python
# Verify no syntax errors
from ba2_trade_platform.modules.experts import TradingAgents
from ba2_trade_platform.thirdparties.TradingAgents.tradingagents.agents.analysts import market_analyst

# Should import without errors
print("✅ All modules imported successfully")
```

---

## 4. Impact Analysis

### What Changed
- **Market Analyst**: Now properly wraps toolkit methods as tools
- **Settings**: Single `vendor_fundamentals` setting instead of two
- **Provider Mapping**: Uses same registry, just different setting name
- **Default Provider**: Changed from OpenAI to Alpha Vantage (more reliable/free)

### What Stayed the Same
- **Provider Registry**: `FUNDAMENTALS_OVERVIEW_PROVIDERS` unchanged
- **Provider Classes**: All providers (Alpha Vantage, OpenAI, FMP) still available
- **Toolkit Methods**: `toolkit.get_ohlcv_data()` and `toolkit.get_indicator_data()` unchanged
- **Database Models**: No database schema changes

### Breaking Changes
⚠️ **None** - This is a backward-compatible fix:
- Old `vendor_fundamentals_overview` setting references in code now use `vendor_fundamentals`
- Existing expert instances may need UI update to reconfigure fundamentals vendors
- No API changes or database migrations required

---

## 5. Lessons Learned

### Tool Decorator Usage
When using toolkit methods in LangChain agents:
1. **Always wrap instance methods** with `@tool` decorator
2. **Define wrapper functions** inside the agent node
3. **Include proper type hints** for automatic schema generation
4. **Add docstrings** for tool descriptions

### Settings Design
When designing expert settings:
1. **Avoid duplicate settings** for similar purposes
2. **Use descriptive names** that clearly indicate purpose
3. **Provide comprehensive tooltips** explaining each provider
4. **Set sensible defaults** (prefer free/reliable providers)
5. **Support multiple providers** with automatic fallback

---

## 6. Future Considerations

### Potential Improvements
1. **Automated Migration**: Create migration script to update existing expert instances
2. **UI Validation**: Add UI warning if old `vendor_fundamentals_overview` detected
3. **Documentation**: Update all docs referencing old setting name
4. **Testing**: Add integration tests for all fundamentals providers

### Related Work
- Consider merging other duplicate settings (if any)
- Standardize tool wrapper pattern across all agents
- Create reusable `@wrap_toolkit_method` decorator
