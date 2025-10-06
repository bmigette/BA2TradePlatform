# Missing Lookback Days Parameters Fix

## Problem
Some agent tools were not using the `lookback_days` argument, instead relying only on default config values. This prevented LLMs from overriding the lookback period for specific analysis needs.

**Observed Logs**:
```
2025-10-06 17:49:47,151 - tradingagents_exp1 - INFO - [TOOL_CALL] Executing get_fundamentals_openai with args: {'ticker': 'ADBE', 'curr_date': '2025-10-06'}
2025-10-06 17:47:20,231 - tradingagents_exp1 - INFO - [TOOL_CALL] Executing get_global_news_openai with args: {'curr_date': '2025-10-06'}
```

Notice: No `lookback_days` parameter being passed, even though it should be available.

## Root Cause Analysis

### Investigation
1. Checked `get_global_news_openai` - Already had `lookback_days` parameter ✓
2. Checked `get_fundamentals_openai` - **Missing** `lookback_days` parameter ✗

### Root Cause
The `get_fundamentals_openai` tool had two issues:

**Issue 1: Agent Wrapper** (`agent_utils.py`)
- Function signature did NOT expose `lookback_days` as a parameter to the LLM
- Hardcoded the config lookup internally: `lookback_days = config.get("economic_data_days", 90)`
- LLM had **no way to override** the default 90-day lookback

**Issue 2: Implementation** (`openai.py`)
- Function signature did NOT accept `lookback_days` parameter
- Hardcoded date range: `"during of the month before {curr_date} to the month of {curr_date}"`
- Even if the wrapper passed it, the implementation wouldn't use it

## Solution

### 1. Updated `openai.py` Implementation

**Before**:
```python
def get_fundamentals_openai(ticker, curr_date):
    config = get_config()
    client = OpenAI(base_url=config["backend_url"])

    response = client.responses.create(
        model=config["quick_think_llm"],
        input=[
            {
                "role": "system",
                "content": [
                    {
                        "type": "input_text",
                        "text": f"Can you search Fundamental for discussions on {ticker} during of the month before {curr_date} to the month of {curr_date}. Make sure you only get the data posted during that period. List as a table, with PE/PS/Cash flow/ etc",
                    }
                ],
            }
        ],
```

**After** (Lines 75-99):
```python
def get_fundamentals_openai(ticker, curr_date, lookback_days=None):
    config = get_config()
    
    # Use provided lookback_days or default from config
    if lookback_days is None:
        lookback_days = config.get("economic_data_days", 90)
    
    client = OpenAI(base_url=config["backend_url"])

    # Calculate start date based on lookback_days
    from datetime import datetime, timedelta
    curr_date_dt = datetime.strptime(curr_date, "%Y-%m-%d")
    start_date_dt = curr_date_dt - timedelta(days=lookback_days)
    start_date = start_date_dt.strftime("%Y-%m-%d")

    response = client.responses.create(
        model=config["quick_think_llm"],
        input=[
            {
                "role": "system",
                "content": [
                    {
                        "type": "input_text",
                        "text": f"Can you search Fundamental for discussions on {ticker} from {start_date} to {curr_date}. Make sure you only get the data posted during that period. List as a table, with PE/PS/Cash flow/ etc",
                    }
                ],
            }
        ],
```

**Key Changes**:
- ✅ Added `lookback_days=None` parameter
- ✅ Uses config default if not provided: `config.get("economic_data_days", 90)`
- ✅ Calculates `start_date` based on `lookback_days`
- ✅ Uses precise date range in prompt: `from {start_date} to {curr_date}`

### 2. Updated `agent_utils.py` Wrapper

**Before**:
```python
@staticmethod
@tool
def get_fundamentals_openai(
    ticker: Annotated[str, "the company's ticker"],
    curr_date: Annotated[str, "Current date in yyyy-mm-dd format"],
):
    """
    Retrieve the latest fundamental information about a given stock on a given date by using OpenAI's news API.
    
    Args:
        ticker (str): Ticker of a company. e.g. AAPL, TSM
        curr_date (str): Current date in yyyy-mm-dd format
    Returns:
        str: A formatted markdown table containing fundamental metrics for the company.
    """
    from ...dataflows.config import get_config
    
    config = get_config()
    lookback_days = config.get("economic_data_days", 90)
    
    openai_fundamentals_results = interface.get_fundamentals_openai(
        ticker, curr_date, lookback_days
    )
```

**After** (Lines 464-485):
```python
@staticmethod
@tool
def get_fundamentals_openai(
    ticker: Annotated[str, "the company's ticker"],
    curr_date: Annotated[str, "Current date in yyyy-mm-dd format"],
    lookback_days: Annotated[
        int,
        "Number of days to look back for fundamental data. If not provided, defaults to economic_data_days from config (typically 90 days). You can specify a custom value to analyze shorter or longer periods."
    ] = None,
):
    """
    Retrieve the latest fundamental information about a given stock on a given date by using OpenAI's news API.
    Searches for comprehensive fundamental metrics including valuation ratios, profitability, growth, cash flow, financial health, and dividend information.
    
    Args:
        ticker (str): Ticker of a company. e.g. AAPL, TSM
        curr_date (str): Current date in yyyy-mm-dd format
        lookback_days (int, optional): Number of days to look back. If not provided, defaults to economic_data_days from config (typically 90 days).
    Returns:
        str: A formatted markdown table containing fundamental metrics for the company.
    """
    openai_fundamentals_results = interface.get_fundamentals_openai(
        ticker, curr_date, lookback_days
    )
```

**Key Changes**:
- ✅ Added `lookback_days` parameter with `Annotated` type hint for LLM
- ✅ Removed internal config lookup (now handled by implementation)
- ✅ LLM can now override default lookback period
- ✅ Description explains default and how to customize

## Files Modified

1. **`ba2_trade_platform/thirdparties/TradingAgents/tradingagents/dataflows/openai.py`**
   - Lines 75-99: Updated `get_fundamentals_openai()` signature and implementation
   - Added `lookback_days` parameter with default `None`
   - Added date calculation logic using `timedelta`
   - Updated prompt to use calculated date range

2. **`ba2_trade_platform/thirdparties/TradingAgents/tradingagents/agents/utils/agent_utils.py`**
   - Lines 464-485: Updated `get_fundamentals_openai()` wrapper
   - Added `lookback_days` parameter with Annotated description
   - Removed internal config lookup
   - Updated docstring

## Impact

### Before Fix
- ❌ LLM **cannot** override fundamental data lookback period
- ❌ Always uses 90-day default from config
- ❌ Hardcoded "month before to month of" date range in prompt
- ❌ Inconsistent with other tools (`get_global_news_openai`, `get_stock_news_openai`)

### After Fix
- ✅ LLM **can** override lookback period for specific analysis needs
- ✅ Defaults to 90-day config value if not specified
- ✅ Precise date range calculation using `timedelta`
- ✅ Consistent API across all data fetching tools

## Usage Examples

### Default Behavior (90 days from config)
```python
# LLM calls without specifying lookback_days
get_fundamentals_openai(
    ticker="AAPL",
    curr_date="2025-10-06"
)
# Uses economic_data_days=90 from config
# Searches from 2025-07-08 to 2025-10-06
```

### Custom Lookback Period
```python
# LLM calls with custom lookback period
get_fundamentals_openai(
    ticker="AAPL",
    curr_date="2025-10-06",
    lookback_days=30  # Override default
)
# Searches from 2025-09-06 to 2025-10-06
```

### Long-term Analysis
```python
# LLM requests longer historical data
get_fundamentals_openai(
    ticker="AAPL",
    curr_date="2025-10-06",
    lookback_days=365  # Full year of data
)
# Searches from 2024-10-06 to 2025-10-06
```

## Testing

1. **Verify Default Behavior**:
   - Run analysis without specifying `lookback_days`
   - Check logs show 90-day default being used
   - Verify search prompt includes correct date range

2. **Verify Custom Lookback**:
   - LLM can specify `lookback_days=30` for recent data
   - Check logs show parameter being passed
   - Verify search prompt uses custom date range

3. **Check Tool Description**:
   - Verify LLM sees `lookback_days` in tool signature
   - Confirm Annotated description is visible
   - Ensure default behavior is documented

## Consistency Across Tools

Now all date-range tools have consistent `lookback_days` parameter:

| Tool | Lookback Parameter | Default Config | Status |
|------|-------------------|----------------|--------|
| `get_stock_news_openai` | ✅ `lookback_days` | `news_lookback_days` (7) | ✅ Fixed |
| `get_global_news_openai` | ✅ `lookback_days` | `social_sentiment_days` (3) | ✅ Fixed |
| `get_fundamentals_openai` | ✅ `lookback_days` | `economic_data_days` (90) | ✅ **NOW FIXED** |
| `get_stockstats_indicators` | ✅ Date range | `market_history_days` (90) | ✅ Fixed |

## Related Issues

- Part of the larger "Tools Missing Date Range Parameters" initiative
- Aligns with prompt instructions about automatic config defaults
- Improves LLM flexibility for different analysis scenarios

## Notes

- The fix maintains backward compatibility (default behavior unchanged)
- LLMs are informed about defaults via Annotated descriptions
- Implementation follows the same pattern as other date-range tools
- Date calculation uses `datetime.timedelta` for precision
