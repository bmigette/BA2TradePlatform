# Agent Tools Fix - October 9, 2025

## Issue Summary

**Error**: `AttributeError: 'function' object has no attribute 'name'`

**Root Cause**: TradingAgents analyst agents were using raw toolkit method references instead of properly decorated LangChain tools. When the code tried to access `tool.name` to build prompts, it failed because Python functions don't have a `.name` attribute.

## Problem Details

### What Happened
The analyst agents were directly passing toolkit method references to LangChain's tool binding:

```python
# ❌ INCORRECT - Raw function reference
tools = [
    toolkit.get_social_media_sentiment,  # This is just a function
]

# This fails when trying to access tool.name
tool_names=[tool.name for tool in tools]  # AttributeError!
```

### Why It Failed
- Toolkit methods are plain Python functions/methods
- Python functions have `__name__` but not `name`
- LangChain tools need the `name` attribute for prompt building
- The `@tool` decorator from `langchain_core.tools` adds this attribute

## Solution

Wrap all toolkit method references with LangChain's `@tool` decorator, following the pattern used in `market_analyst.py`:

```python
# ✅ CORRECT - Wrapped with @tool decorator
from langchain_core.tools import tool

@tool
def get_social_media_sentiment(symbol: str, end_date: str, lookback_days: int = None) -> str:
    """Retrieve social media sentiment and discussions about a specific company."""
    return toolkit.get_social_media_sentiment(symbol, end_date, lookback_days)

tools = [
    get_social_media_sentiment,  # Now has .name attribute
]
```

## Files Fixed

### 1. **social_media_analyst.py**
- Added `from langchain_core.tools import tool` import
- Wrapped `get_social_media_sentiment` method with `@tool` decorator

### 2. **news_analyst.py**
- Added `from langchain_core.tools import tool` import
- Wrapped `get_global_news` method with `@tool` decorator

### 3. **fundamentals_analyst.py**
- Added `from langchain_core.tools import tool` import
- Wrapped 5 methods:
  - `get_balance_sheet`
  - `get_income_statement`
  - `get_cashflow_statement`
  - `get_insider_transactions`
  - `get_insider_sentiment`

### 4. **macro_analyst.py**
- Added `from langchain_core.tools import tool` import
- Wrapped 3 methods:
  - `get_economic_indicators`
  - `get_yield_curve`
  - `get_fed_calendar`

## Verification

### Checked Other Agent Types
- ✅ **Researchers** (bull_researcher.py, bear_researcher.py): Don't use tools
- ✅ **Risk Management** (aggresive_debator.py, conservative_debator.py, neutral_debator.py): Don't use tools
- ✅ **Trader** (trader.py): Doesn't use tools
- ✅ **Managers** (research_manager.py, risk_manager.py): Don't use tools
- ✅ **Market Analyst** (market_analyst.py): Already correctly implemented with `@tool` wrappers

**Result**: Only the analyst agents use tools, and all have now been fixed.

## Pattern to Follow

When adding new analyst agents that use toolkit methods:

```python
from langchain_core.tools import tool
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder

def create_my_analyst(llm, toolkit):
    def my_analyst_node(state):
        # 1. Wrap toolkit methods with @tool decorator
        @tool
        def my_tool_function(param1: str, param2: str = None) -> str:
            """Clear docstring describing what the tool does."""
            return toolkit.my_toolkit_method(param1, param2)
        
        # 2. Use wrapped tools in the list
        tools = [
            my_tool_function,
        ]
        
        # 3. Now tool.name works correctly
        prompt_config = format_analyst_prompt(
            system_prompt=system_message,
            tool_names=[tool.name for tool in tools],  # ✅ Works!
            current_date=current_date,
            ticker=ticker
        )
        
        # 4. Bind tools to LLM
        chain = prompt | llm.bind_tools(tools)
        
        return my_analyst_node
```

## Key Takeaways

1. **Always wrap toolkit methods** with `@tool` decorator when using them with LangChain
2. **Follow the market_analyst.py pattern** for consistency
3. **Test thoroughly** - this error only appears at runtime when the agent is executed
4. **The `@tool` decorator provides**:
   - `.name` attribute for tool identification
   - Proper LangChain tool interface
   - Type hints and docstring integration

## Testing

To verify the fix works:

1. Run a TradingAgents analysis that uses all analysts:
   ```python
   from ba2_trade_platform.modules.experts import TradingAgents
   
   expert = TradingAgents(expert_instance_id=1)
   expert.run_market_analysis("AAPL", "ENTER_MARKET")
   ```

2. Check logs for successful analyst execution (no AttributeError)

3. Verify each analyst produces output:
   - Market Analyst Report ✅
   - News Analyst Report ✅
   - Social Media Analyst Report ✅
   - Fundamentals Analyst Report ✅
   - Macro Analyst Report ✅

## Related Issues

This fix resolves:
- AttributeError during TradingAgents analysis
- Social Media Analyst node failures
- News Analyst node failures
- Fundamentals Analyst node failures
- Macro Analyst node failures

## Prevention

To prevent similar issues in the future:

1. **Code Review**: Always check that toolkit methods are properly wrapped
2. **Testing**: Run full analysis pipeline during development
3. **Documentation**: Keep this pattern documented for new contributors
4. **Linting**: Consider adding a custom lint rule to detect unwrapped toolkit usage
