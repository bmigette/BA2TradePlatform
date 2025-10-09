# Social Media Tool Fix - October 9, 2025

## Issue Summary

**Error**: `Error: get_social_media_sentiment is not a valid tool, try one of [get_company_news]`

**Root Cause**: The social media analyst was correctly configured with the `get_social_media_sentiment` tool wrapped with the `@tool` decorator, BUT the corresponding ToolNode in the graph was using `get_company_news` instead of `get_social_media_sentiment`.

## Problem Details

### What Happened

The TradingAgents framework uses a two-part system for tools:

1. **Analyst Node**: The agent that decides to call tools (fixed in previous commit with `@tool` decorator)
2. **Tool Node**: The LangGraph ToolNode that actually executes the tools

The mismatch was:
- ✅ **Analyst Node** (`social_media_analyst.py`): Correctly wrapped `get_social_media_sentiment`
- ❌ **Tool Node** (`trading_graph.py`): Was using `get_company_news` instead

### The Error Flow

```python
# In social_media_analyst.py (CORRECT)
@tool
def get_social_media_sentiment(...):
    return toolkit.get_social_media_sentiment(...)

tools = [get_social_media_sentiment]  # LLM binds to this

# In trading_graph.py (INCORRECT)
"social": LoggingToolNode(
    [
        get_company_news,  # ❌ WRONG - doesn't match what analyst expects
    ],
    self.market_analysis_id
),
```

When the LLM tried to call `get_social_media_sentiment`, LangGraph's ToolNode only had `get_company_news` available, resulting in:
```
Error: get_social_media_sentiment is not a valid tool, try one of [get_company_news]
```

## Solution

### 1. Added Tool Definition in trading_graph.py

Added the missing `get_social_media_sentiment` tool definition (lines 347-354):

```python
@tool
def get_social_media_sentiment(
    symbol: str,
    end_date: str,
    lookback_days: int = None
) -> str:
    """Retrieve social media sentiment and discussions about a specific company."""
    return self.toolkit.get_social_media_sentiment(symbol, end_date, lookback_days)
```

### 2. Updated Social ToolNode

Changed the social ToolNode to use the correct tool (line 427-431):

```python
# BEFORE
"social": LoggingToolNode(
    [
        get_company_news,  # ❌ WRONG
    ],
    self.market_analysis_id
),

# AFTER
"social": LoggingToolNode(
    [
        get_social_media_sentiment,  # ✅ CORRECT
    ],
    self.market_analysis_id
),
```

## Files Modified

### ba2_trade_platform/thirdparties/TradingAgents/tradingagents/graph/trading_graph.py

1. **Added** `get_social_media_sentiment` tool definition (after `get_global_news`)
2. **Modified** social ToolNode to use `get_social_media_sentiment` instead of `get_company_news`

## Understanding the Architecture

### TradingAgents Tool System

```
┌─────────────────────────────────────────────────────────────┐
│                     Analyst Node                             │
│  - Decides WHICH tools to call                              │
│  - Uses @tool wrapped functions bound to LLM                │
│  - Example: social_media_analyst.py                         │
└────────────────────┬────────────────────────────────────────┘
                     │
                     │ LLM decides to call tool
                     ↓
┌─────────────────────────────────────────────────────────────┐
│                     Tool Node                                │
│  - EXECUTES the tools                                       │
│  - LangGraph's ToolNode with actual tool implementations    │
│  - Example: trading_graph.py _create_tool_nodes()          │
└─────────────────────────────────────────────────────────────┘
```

**CRITICAL**: Both components must have the SAME tool names and signatures!

### Current Tool Mapping

| Analyst       | Tool Node Key | Tools Available                                    |
|---------------|---------------|---------------------------------------------------|
| Market        | "market"      | get_ohlcv_data, get_indicator_data               |
| Social Media  | "social"      | get_social_media_sentiment ✅                    |
| News          | "news"        | get_global_news                                  |
| Fundamentals  | "fundamentals"| get_balance_sheet, get_income_statement, etc.    |
| Macro         | "macro"       | get_economic_indicators, get_yield_curve, etc.   |

## Testing

To verify the fix:

1. Run a TradingAgents analysis with social media analyst enabled:
   ```python
   from ba2_trade_platform.modules.experts import TradingAgents
   
   expert = TradingAgents(expert_instance_id=1)
   expert.run_market_analysis("AAPL", "ENTER_MARKET")
   ```

2. Check logs for successful social media analyst execution
3. Verify social sentiment report is generated without errors

## Prevention

To prevent similar issues:

1. **When adding new analyst tools**:
   - Add tool wrapper in analyst file (e.g., `social_media_analyst.py`)
   - Add tool definition in `trading_graph.py` `_create_tool_nodes()` method
   - Add tool to appropriate ToolNode in tool_nodes dictionary
   - Ensure tool names match exactly

2. **Code Review Checklist**:
   - [ ] Tool wrapper added in analyst file with `@tool` decorator
   - [ ] Tool definition added in `trading_graph.py`
   - [ ] Tool added to correct ToolNode
   - [ ] Tool names match between analyst and ToolNode
   - [ ] Function signatures match toolkit method

3. **Testing**:
   - Always test analyst end-to-end after adding new tools
   - Check both tool binding (analyst) and tool execution (ToolNode)
   - Verify error messages don't show tool name mismatches

## Related Issues

This fix resolves:
- Social Media Analyst tool calling errors
- "get_social_media_sentiment is not a valid tool" error
- Mismatch between analyst-bound tools and ToolNode available tools

## Key Takeaways

1. **Two-Part System**: Tools must be defined in BOTH places:
   - Analyst file (for LLM tool binding)
   - trading_graph.py (for actual execution)

2. **Tool Names Must Match**: The `@tool` function name in the analyst MUST match the function name in `_create_tool_nodes()`

3. **ToolNode is a Router**: The ToolNode acts as a router that executes the tool the LLM decided to call. If the tool isn't available in the ToolNode, execution fails even if the analyst knows about it.

4. **Follow the Pattern**: When adding tools, always follow the existing pattern in both locations
