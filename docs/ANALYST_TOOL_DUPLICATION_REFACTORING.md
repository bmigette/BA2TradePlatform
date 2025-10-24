# Analyst Tool Duplication Refactoring

**Date**: 2025-01-XX  
**Status**: ✅ Complete

## Problem Statement

TradingAgents analyst files had architectural duplication where tools were defined in TWO places:

1. **Inside analyst creation functions** (`create_X_analyst`): 15-30 lines of `@tool` decorators wrapping toolkit methods
2. **In trading_graph.py** (`_create_tool_nodes()`): Identical tool definitions wrapped in `LoggingToolNode`

This created ~100+ lines of duplicate code across 5 analyst files with several issues:
- Maintenance burden: Changes required updating multiple locations
- Confusion: Unclear which tool definitions were actually used
- Dead code: Analyst-level tool definitions were bound to LLM but never executed (graph-level LoggingToolNode used instead)

## Root Cause

LangChain's `ToolNode` uses the tools passed to its constructor, not the tools bound to the LLM. The analyst functions bound tools to LLM for schema generation, but actual execution went through `trading_graph._create_tool_nodes()`.

## Solution

**Refactored analyst signatures** to accept pre-defined tools as a parameter:

```python
# BEFORE
def create_news_analyst(llm, toolkit):
    def news_analyst_node(state):
        @tool
        def get_company_news(...):
            return toolkit.get_company_news(...)
        
        tools = [get_company_news, ...]
        chain = prompt | llm.bind_tools(tools)

# AFTER
def create_news_analyst(llm, toolkit, tools):
    """
    Args:
        llm: Language model
        toolkit: Toolkit instance (backward compat, not used)
        tools: List of pre-defined tool objects
    """
    def news_analyst_node(state):
        # Use pre-defined tools directly
        chain = prompt | llm.bind_tools(tools)
```

**Updated setup.py** to pass tools from `LoggingToolNode`:

```python
# Extract tools from LoggingToolNode.original_tools
analyst_nodes["news"] = create_news_analyst(
    self.quick_thinking_llm, 
    self.toolkit,
    list(self.tool_nodes["news"].original_tools.values())
)
```

## Files Modified

### Analyst Files (Removed Duplicate Tool Definitions)

1. **news_analyst.py** (Lines 1-32 → Lines 1-18)
   - Removed: `get_company_news`, `get_global_news`, `extract_web_content` tool definitions (~15 lines)
   - Added: `tools` parameter to signature
   
2. **market_analyst.py** (Lines 1-27 → Lines 1-13)
   - Removed: `get_ohlcv_data`, `get_indicator_data` tool definitions (~13 lines)
   - Added: `tools` parameter to signature
   
3. **social_media_analyst.py** (Lines 1-24 → Lines 1-17)
   - Removed: `get_social_media_sentiment` tool definition (~6 lines)
   - Added: `tools` parameter to signature
   
4. **fundamentals_analyst.py** (Lines 1-60 → Lines 1-18)
   - Removed: 7 tool definitions (~42 lines): `get_balance_sheet`, `get_income_statement`, `get_cashflow_statement`, `get_insider_transactions`, `get_insider_sentiment`, `get_past_earnings`, `get_earnings_estimates`
   - Added: `tools` parameter to signature
   
5. **macro_analyst.py** (Lines 1-36 → Lines 1-18)
   - Removed: 3 tool definitions (~18 lines): `get_economic_indicators`, `get_yield_curve`, `get_fed_calendar`
   - Added: `tools` parameter to signature

### Graph Setup (Pass Pre-Defined Tools)

6. **setup.py** (Lines 64-96)
   - Updated all 5 analyst creation calls to pass `list(self.tool_nodes[type].original_tools.values())`
   - Maintains existing tool_nodes assignment for graph wiring

## Code Reduction

- **Total Lines Removed**: ~94 lines of duplicate tool definitions
- **Per-File Reduction**: 6-42 lines per analyst file
- **Maintenance Points**: Reduced from 10 locations (5 analysts + graph) to 1 location (graph only)

## Architecture Benefits

1. **Single Source of Truth**: All tool definitions in `trading_graph._create_tool_nodes()`
2. **Database Logging Preserved**: Tools still wrapped in `LoggingToolNode` for execution tracking
3. **Backward Compatibility**: `toolkit` parameter kept for potential future use
4. **Clear Separation**: Tool *definitions* in graph, tool *usage* in analysts
5. **Maintainability**: Changes to tools only require updating one location

## Testing Checklist

- [ ] Run TradingAgents analysis to verify all 5 analyst types work
- [ ] Confirm tool calls execute correctly through ToolNodes
- [ ] Verify database logging still captures tool executions (AnalysisOutput table)
- [ ] Check no regression in analyst outputs or recommendations
- [ ] Validate LLM tool schema generation still works

## Technical Notes

- **LoggingToolNode Structure**: Has `original_tools` dict mapping tool names to tool objects
- **Tool Extraction**: Use `list(tool_node.original_tools.values())` to get tool list
- **LLM Binding**: Tools bound to LLM for schema/planning, executed via ToolNode in graph
- **No Import Changes**: All `from langchain_core.tools import tool` imports removed from analyst files

## Related Documentation

- See `DATA_PROVIDER_FORMAT_SPECIFICATION.md` for provider tool patterns
- See `AGENT_PROVIDER_REFACTORING_SUMMARY.md` for provider_args threading
- See `.github/copilot-instructions.md` for overall architecture

## Completion Status

✅ All 5 analyst files refactored  
✅ setup.py updated to pass tools  
✅ No compilation errors  
✅ ~94 lines duplicate code eliminated  
⏳ Integration testing pending
