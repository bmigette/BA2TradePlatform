# Tool Result Extraction Fix

## Issue

After implementing the internal format for tool results (with `_internal`, `text_for_agent`, and `json_for_storage`), LangGraph agents were receiving the full JSON dictionary instead of just the text content. This caused agent messages to display JSON structures instead of clean, formatted text.

### Example of the Problem

**Before Fix:**
```
================================= Tool Message =================================
Name: get_stockstats_indicators_report_online

{"_internal": true, "text_for_agent": "## vwma values from 2025-09-02 to 2025-10-02:\n\n2025-09-02: 164.0888028624584\n2025-09-02: 163.47874626194482\n..."}
```

**Expected Result:**
```
================================= Tool Message =================================
Name: get_stockstats_indicators_report_online

## vwma values from 2025-09-02 to 2025-10-02:

2025-09-02: 164.0888028624584
2025-09-02: 163.47874626194482
...
```

## Root Cause

The issue was in `agent_utils.py` where tool methods were returning the raw result from interface functions without extracting the `text_for_agent` field. LangChain's `@tool` decorator was passing the entire dictionary to the agent.

### Architecture Flow

```
┌─────────────────────────────────────────────────────────────┐
│ interface.py (Data Layer)                                    │
│ - get_stock_stats_indicators_window()                        │
│ - get_YFin_data_online()                                     │
│                                                               │
│ Returns: {                                                    │
│   "_internal": True,                                          │
│   "text_for_agent": "formatted text",                        │
│   "json_for_storage": {...}                                  │
│ }                                                             │
└─────────────────────────────────────────────────────────────┘
                          ↓
┌─────────────────────────────────────────────────────────────┐
│ agent_utils.py (Tool Wrapper) ← FIX APPLIED HERE            │
│ - get_stockstats_indicators_report_online()                  │
│ - get_stockstats_indicators_report()                         │
│ - get_YFin_data_online()                                     │
│                                                               │
│ BEFORE: return result_stockstats                             │
│ AFTER:  if isinstance(result, dict) and result.get('_internal'): │
│             return result.get('text_for_agent', str(result)) │
│         return result                                         │
└─────────────────────────────────────────────────────────────┘
                          ↓
┌─────────────────────────────────────────────────────────────┐
│ LangGraph Agent (Receives Clean Text)                        │
│ - Agent sees only text_for_agent content                     │
│ - No JSON structure in messages                              │
└─────────────────────────────────────────────────────────────┘
                          ↓
┌─────────────────────────────────────────────────────────────┐
│ db_storage.py (Storage Layer)                                │
│ - Intercepts tool messages                                   │
│ - Extracts text_for_agent for storage                        │
│ - Extracts json_for_storage for parameter-based caching      │
└─────────────────────────────────────────────────────────────┘
```

## Solution

Added extraction logic in three tool methods in `agent_utils.py`:

1. **`get_stockstats_indicators_report()`** - offline version
2. **`get_stockstats_indicators_report_online()`** - online version  
3. **`get_YFin_data_online()`** - Yahoo Finance data fetcher

### Code Changes

**File:** `ba2_trade_platform/thirdparties/TradingAgents/tradingagents/agents/utils/agent_utils.py`

**Pattern Applied to All Three Tools:**

```python
# Before
result_stockstats = interface.get_stock_stats_indicators_window(
    symbol, indicator, curr_date, look_back_days, True, None
)
return result_stockstats

# After
result_stockstats = interface.get_stock_stats_indicators_window(
    symbol, indicator, curr_date, look_back_days, True, None
)

# Extract text for agent if result is in internal format
if isinstance(result_stockstats, dict) and result_stockstats.get('_internal'):
    return result_stockstats.get('text_for_agent', str(result_stockstats))

return result_stockstats
```

### Why This Works

1. **LangGraph gets clean text** - The `@tool` decorator receives a string instead of a dict, so LangGraph shows clean formatted text to the agent
2. **db_storage.py still works** - The storage layer intercepts the raw tool messages before they reach the agent and can still access the full dictionary structure
3. **Backward compatible** - If a tool returns a plain string, it passes through unchanged
4. **Robust fallback** - If extraction fails, it converts to string rather than crashing

## Modified Tools

### 1. get_stockstats_indicators_report() (Offline)

- **Location:** Lines ~195-211
- **Returns:** Technical indicator data from cached files
- **Change:** Added extraction of `text_for_agent`

### 2. get_stockstats_indicators_report_online() (Online)

- **Location:** Lines ~215-249
- **Returns:** Technical indicator data fetched online
- **Change:** Added extraction of `text_for_agent`

### 3. get_YFin_data_online() (Online)

- **Location:** Lines ~156-173
- **Returns:** Stock price data from Yahoo Finance
- **Change:** Added extraction of `text_for_agent`

## Testing

### Verification Steps

1. **Run a market analysis** with TradingAgents
2. **Check the logs** for tool result messages
3. **Verify** that tool outputs show clean text instead of JSON

### Expected Log Output

**Correct (After Fix):**
```
2025-10-02 16:21:21,297 - tradingagents_exp2 - ℹ️  INFO - [TOOL_RESULT] get_stockstats_indicators_report_online returned: ## vwma values from 2025-09-02 to 2025-10-02:

2025-09-02: 164.0888028624584
2025-09-02: 163.478746626194482
...
```

**Incorrect (Before Fix):**
```
2025-10-02 16:21:21,297 - tradingagents_exp2 - ℹ️  INFO - [TOOL_RESULT] get_stockstats_indicators_report_online returned: {"_internal": true, "text_for_agent": "## vwma values...
```

## Impact

✅ **Agents receive clean text** - No more JSON structures in agent conversations  
✅ **Better agent reasoning** - Cleaner inputs lead to better trading decisions  
✅ **Log readability** - Logs show actual data instead of JSON  
✅ **Storage unchanged** - db_storage.py still captures both text and JSON  
✅ **Backward compatible** - Old-style string returns still work  

## Related Files

- `ba2_trade_platform/thirdparties/TradingAgents/tradingagents/agents/utils/agent_utils.py` - **MODIFIED**
- `ba2_trade_platform/thirdparties/TradingAgents/tradingagents/dataflows/interface.py` - No changes (still returns internal format)
- `ba2_trade_platform/thirdparties/TradingAgents/tradingagents/db_storage.py` - No changes (storage layer unaffected)

## Date

October 2, 2025
