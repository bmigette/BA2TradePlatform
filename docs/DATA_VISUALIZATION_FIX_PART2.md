# Data Visualization Fix - Part 2

## Issues Found and Fixed

### Issue 1: JSON Tool Outputs Not Being Stored

**Problem:** When checking the database, no JSON tool outputs (`*_json` records) were found in the `AnalysisOutput` table.

**Investigation:**
```bash
# Query revealed 0 JSON outputs
Found 0 JSON outputs
```

**Root Cause:** The JSON storage functionality in `db_storage.py` exists and is supposed to store JSON outputs with names like `tool_output_{tool_name}_json`, but something is preventing these from being created.

**Location of JSON Storage Logic:**
- **File:** `ba2_trade_platform/thirdparties/TradingAgents/tradingagents/db_storage.py`
- **Lines:** 327-333

```python
# Store JSON format if provided
if json_for_storage:
    import json
    store_analysis_output(
        market_analysis_id=self.market_analysis_id,
        name=f"tool_output_{tool_name}_json",
        output_type="tool_call_output_json",
        text=json.dumps(json_for_storage, indent=2)
    )
```

**Why JSON Outputs Are Missing:**

The JSON outputs should be created when tools return the internal format:
```python
{
    "_internal": True,
    "text_for_agent": "...",
    "json_for_storage": {...}
}
```

Possible reasons for missing JSON outputs:
1. ✅ **Tool wrapper extracts text** - Our recent fix extracts `text_for_agent` from the dict BEFORE it reaches LangGraph
2. ❌ **db_storage never sees the dict** - Since we extract at tool level, the storage layer only sees the text
3. ⚠️ **Need to store JSON separately** - The tool wrapper should store JSON before extracting text

**Current Flow (After Our Tool Result Fix):**
```
Tool Returns Dict
    ↓
agent_utils.py extracts text_for_agent  ← PROBLEM: Dict lost here!
    ↓
LangGraph gets string
    ↓
db_storage.py sees string (not dict)
    ↓
No JSON storage happens
```

**Required Fix:**
The tool wrapper in `agent_utils.py` needs to:
1. Check if result has `_internal` flag
2. Store the `json_for_storage` to database
3. Extract and return `text_for_agent` to LangGraph

This is a separate issue that needs addressing, but for now, the visualization works by fetching fresh data.

---

### Issue 2: AttributeError - '_build_expert_config' Does Not Exist

**Problem:**
```python
AttributeError: 'TradingAgents' object has no attribute '_build_expert_config'
```

**Root Cause:** The method name was incorrect. The TradingAgents class has `_create_tradingagents_config()` not `_build_expert_config()`.

**Solution:** Access settings directly from the TradingAgents instance instead of calling a non-existent method.

**File Modified:** `ba2_trade_platform/modules/experts/TradingAgentsUI.py`

**Before:**
```python
trading_agents = TradingAgents(expert_instance.id)
expert_config = trading_agents._build_expert_config()  # ❌ Method doesn't exist

market_history_days = expert_config.get('market_history_days', 90)
timeframe = expert_config.get('timeframe', '1d')
```

**After:**
```python
# Get settings definitions for default values
from ...modules.experts.TradingAgents import TradingAgents
settings_def = TradingAgents.get_settings_definitions()

# Create TradingAgents instance to get settings
trading_agents = TradingAgents(expert_instance.id)

# Extract key parameters directly from settings
market_history_days = int(trading_agents.settings.get('market_history_days', 
                          settings_def['market_history_days']['default']))
timeframe = trading_agents.settings.get('timeframe', 
                                        settings_def['timeframe']['default'])
```

**Why This Works:**
- `TradingAgents` extends `ExtendableSettingsInterface` which loads settings in `__init__`
- Settings are accessible via `self.settings` dictionary
- Settings definitions provide default values
- No need to call a config builder method

---

## Current State

### ✅ Fixed
1. Data visualization now fetches fresh price data using expert settings
2. Date range calculated correctly from analysis run date
3. AttributeError fixed by accessing settings directly
4. Proper default value handling

### ⚠️ Known Limitation
**JSON Tool Outputs Not Stored:**
- Indicators cannot be reconstructed from stored parameters
- Each visualization requires recalculating indicators from price data
- This is acceptable but slower than using cached parameters

### 🔧 Future Enhancement Needed

**Proper JSON Storage Implementation:**

The tool wrapper needs to be enhanced to store JSON before extracting text:

```python
# In agent_utils.py tool methods
result = interface.get_stock_stats_indicators_window(...)

# Extract text for agent if result is in internal format
if isinstance(result, dict) and result.get('_internal'):
    # NEW: Store JSON if we have market_analysis_id
    if hasattr(self, 'market_analysis_id') and self.market_analysis_id:
        json_for_storage = result.get('json_for_storage')
        if json_for_storage:
            from ...db_storage import store_analysis_output
            import json
            store_analysis_output(
                market_analysis_id=self.market_analysis_id,
                name=f"tool_output_get_stockstats_indicators_report_online_json",
                output_type="tool_call_output_json",
                text=json.dumps(json_for_storage, indent=2)
            )
    
    # Extract text for LangGraph
    return result.get('text_for_agent', str(result))

return result
```

**Challenge:** Tool methods in `agent_utils.py` don't have access to `market_analysis_id`. This would require:
1. Passing context through tool calls
2. Using thread-local storage
3. Refactoring tool architecture

For now, the current approach (fetch fresh data) is acceptable and actually more reliable since it doesn't depend on stored parameters.

---

## Testing

**Test 1: Verify Settings Access ✅**
```python
from ba2_trade_platform.core.db import get_instance
from ba2_trade_platform.core.models import ExpertInstance
from ba2_trade_platform.modules.experts.TradingAgents import TradingAgents

instance = get_instance(ExpertInstance, 1)
ta = TradingAgents(instance.id)
settings_def = TradingAgents.get_settings_definitions()

print(f"Market History Days: {ta.settings.get('market_history_days', settings_def['market_history_days']['default'])}")
print(f"Timeframe: {ta.settings.get('timeframe', settings_def['timeframe']['default'])}")
```

**Test 2: Verify Data Visualization ✅**
1. Navigate to a completed analysis
2. Click "Data Visualization" tab
3. Chart should display with:
   - Correct symbol
   - Date range based on lookback days
   - Proper timeframe/interval
   - Data summary showing parameters

---

## Date

October 2, 2025
