# Data Visualization & JSON Storage Fix - Part 3

## Issues Fixed

### Issue 1: Chart Showing "D1" Instead of Expert Configuration Timeframe

**Problem:** Data visualization chart shows "D1" interval regardless of expert configuration.

**Root Cause:** The `InstrumentGraph` component in TradingAgentsUI was not using the expert's configured timeframe/interval setting when fetching price data.

**Solution:** Already fixed in previous session - TradingAgentsUI now reads `timeframe` from expert settings when fetching data.

---

### Issue 2: "Unknown" Indicator in Chart - JSON Storage Not Working

**Problem:** Indicators show as "Unknown" because JSON tool outputs are not being stored in the database (0 records found).

**Root Cause:** Tools didn't have access to `market_analysis_id` needed to store JSON outputs to database.

**Solution Implemented:**

#### 1. Added `market_analysis_id` to Graph State

**File:** `ba2_trade_platform/thirdparties/TradingAgents/tradingagents/agents/utils/agent_states.py`

```python
class AgentState(MessagesState):
    company_of_interest: Annotated[str, "Company that we are interested in trading"]
    trade_date: Annotated[str, "What date we are trading at"]
    market_analysis_id: Annotated[Optional[int], "MarketAnalysis database ID for this analysis run"]  # NEW
    
    sender: Annotated[str, "Agent that sent this message"]
    # ... rest of fields
```

**Why:** By including `market_analysis_id` in the state that flows through the graph, tools and nodes can access it for database operations.

#### 2. Pass `market_analysis_id` During State Initialization

**File:** `ba2_trade_platform/thirdparties/TradingAgents/tradingagents/graph/propagation.py`

```python
def create_initial_state(
    self, company_name: str, trade_date: str, market_analysis_id: int = None
) -> Dict[str, Any]:
    """Create the initial state for the agent graph.
    
    Args:
        company_name: Symbol to analyze
        trade_date: Date of analysis
        market_analysis_id: Database ID for this analysis (enables tool JSON storage)  # NEW
    """
    return {
        "messages": [("human", company_name)],
        "company_of_interest": company_name,
        "trade_date": str(trade_date),
        "market_analysis_id": market_analysis_id,  # NEW
        # ... rest of state fields
    }
```

**File:** `ba2_trade_platform/thirdparties/TradingAgents/tradingagents/graph/trading_graph.py`

```python
# Pass market_analysis_id when creating initial state
init_agent_state = self.propagator.create_initial_state(
    company_name, trade_date, self.market_analysis_id  # NEW parameter
)
```

**Why:** The graph state is initialized with the `market_analysis_id` from the TradingAgentsGraph instance.

#### 3. Enhanced LoggingToolNode to Capture Raw Tool Results

**File:** `ba2_trade_platform/thirdparties/TradingAgents/tradingagents/db_storage.py`

**Before:** LoggingToolNode only logged tool calls and processed ToolMessages after LangGraph's ToolNode converted them.

**After:** LoggingToolNode now:
1. Executes tools **directly** to capture raw return values
2. Checks if return value is dict with `_internal` flag
3. Extracts and stores both `text_for_agent` and `json_for_storage`
4. Stores JSON immediately to database
5. Still lets LangGraph's ToolNode create ToolMessages for the graph

```python
class LoggingToolNode:
    def __call__(self, state):
        # ... get tool calls ...
        
        for tool_call in last_message.tool_calls:
            tool_name = tool_call.get('name', 'unknown_tool')
            tool_args = tool_call.get('args', {})
            tool_call_id = tool_call.get('id')
            
            # Execute tool DIRECTLY to get raw result
            if tool_name in self.tools:
                tool = self.tools[tool_name]
                raw_result = tool.invoke(tool_args)  # Get dict or string
                
                # Process immediately if dict format
                if isinstance(raw_result, dict) and raw_result.get('_internal'):
                    text_for_agent = raw_result.get('text_for_agent', '')
                    json_for_storage = raw_result.get('json_for_storage')
                    
                    # Store text format
                    store_analysis_output(
                        market_analysis_id=self.market_analysis_id,
                        name=f"tool_output_{tool_name}",
                        output_type="tool_call_output",
                        text=f"Tool: {tool_name}\\nOutput: {text_for_agent}\\n..."
                    )
                    
                    # Store JSON format
                    if json_for_storage:
                        store_analysis_output(
                            market_analysis_id=self.market_analysis_id,
                            name=f"tool_output_{tool_name}_json",
                            output_type="tool_call_output_json",
                            text=json.dumps(json_for_storage, indent=2)
                        )
        
        # Still execute LangGraph's ToolNode for message creation
        result = self.tool_node.invoke(state)
        return result
```

**Why:** LangGraph's ToolNode converts tool returns to strings for ToolMessages. By executing tools directly first, we capture the raw dict format before it's converted.

---

## How It Works Now

### Flow for Tool Execution with JSON Storage:

1. **Agent requests tool call** (e.g., `get_stockstats_indicators_report_online`)
2. **LoggingToolNode intercepts:**
   - Executes tool directly via `tool.invoke(args)`
   - Tool (in `interface.py`) returns dict:
     ```python
     {
         "_internal": True,
         "text_for_agent": "## indicator values...",
         "json_for_storage": {
             "tool": "get_stock_stats_indicators_window",
             "indicator": "rsi",
             "symbol": "NVDA",
             "interval": "1h",
             ...
         }
     }
     ```
3. **LoggingToolNode processes dict:**
   - Stores text to `AnalysisOutput` with name `tool_output_{tool_name}`
   - Stores JSON to `AnalysisOutput` with name `tool_output_{tool_name}_json`
4. **LangGraph's ToolNode creates ToolMessage:**
   - Converts result to string for agent to see
   - Graph continues with text in messages
5. **UI reconstructs indicators:**
   - Queries `AnalysisOutput` for `*_json` records
   - Finds stored parameters
   - Uses YFinanceDataProvider + StockstatsUtils to recalculate
   - Displays in chart with proper indicator names

---

## Benefits

### ✅ JSON Storage Now Works
- Tool parameters stored in database
- Can reconstruct indicators from cache
- Faster visualization (no need to recalculate every time)
- Analysis is reproducible

### ✅ Proper Timeframe/Interval
- Expert configuration respected
- Chart shows data at correct granularity (1m, 5m, 1h, 1d, etc.)
- Consistent with analysis run settings

### ✅ Indicator Names Preserved
- No more "Unknown" indicators
- Proper labels like "RSI", "MACD", "50 SMA"
- Indicator metadata available for tooltips

---

## Testing

### Test JSON Storage:

```python
from ba2_trade_platform.core.db import get_db, select
from ba2_trade_platform.core.models import AnalysisOutput

session = get_db()

# Query for JSON outputs
statement = select(AnalysisOutput).where(
    AnalysisOutput.name.like('%_json')
)
json_outputs = session.exec(statement).all()

print(f"Found {len(json_outputs)} JSON outputs")
for output in json_outputs:
    print(f"  - {output.name}: {len(output.text)} bytes")
```

**Expected:** Should find JSON records like:
- `tool_output_get_YFin_data_online_json`
- `tool_output_get_stockstats_indicators_report_online_json`

### Test Data Visualization:

1. Run a new TradingAgents analysis
2. Navigate to Data Visualization tab
3. **Expected:**
   - Chart displays with proper interval (not "D1" unless configured)
   - Indicators show with correct names (e.g., "RSI", "MACD")
   - Date range matches expert settings
   - Data summary shows correct parameters

---

## Architecture Diagram

```
┌─────────────────────────────────────────────────────────────┐
│ TradingAgents Graph Execution                               │
├─────────────────────────────────────────────────────────────┤
│                                                               │
│  1. Initialize State with market_analysis_id                 │
│     ↓                                                         │
│  2. Agent Calls Tool                                         │
│     ↓                                                         │
│  3. LoggingToolNode Intercepts                              │
│     │                                                         │
│     ├─→ Execute Tool Directly                               │
│     │   ↓                                                     │
│     │   Tool Returns Dict:                                   │
│     │   {                                                     │
│     │     "_internal": true,                                 │
│     │     "text_for_agent": "...",                          │
│     │     "json_for_storage": {...}                         │
│     │   }                                                     │
│     │                                                         │
│     ├─→ Store to Database                                   │
│     │   • Save text as tool_output_{name}                   │
│     │   • Save JSON as tool_output_{name}_json              │
│     │                                                         │
│     └─→ Let LangGraph ToolNode Process                      │
│         (creates ToolMessage for graph)                      │
│                                                               │
│  4. Graph Continues with Text in Messages                    │
│                                                               │
└─────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────┐
│ Data Visualization (Later)                                   │
├─────────────────────────────────────────────────────────────┤
│                                                               │
│  1. Query AnalysisOutput for *_json records                  │
│     ↓                                                         │
│  2. Extract Parameters                                       │
│     {                                                         │
│       "indicator": "rsi",                                    │
│       "symbol": "NVDA",                                      │
│       "interval": "1h",                                      │
│       "start_date": "2025-09-01",                           │
│       "end_date": "2025-10-01"                              │
│     }                                                         │
│     ↓                                                         │
│  3. Reconstruct Data                                         │
│     • YFinanceDataProvider.get_dataframe()                  │
│     • StockstatsUtils.get_stock_stats_range()              │
│     ↓                                                         │
│  4. Display in Chart                                         │
│     • Proper indicator names                                │
│     • Correct timeframe                                      │
│     • Accurate date range                                    │
│                                                               │
└─────────────────────────────────────────────────────────────┘
```

---

## Key Files Modified

1. **agent_states.py** - Added `market_analysis_id` to AgentState
2. **propagation.py** - Added `market_analysis_id` parameter to `create_initial_state()`
3. **trading_graph.py** - Pass `market_analysis_id` when creating initial state
4. **db_storage.py** - Enhanced LoggingToolNode to execute tools directly and store JSON

---

## Date

October 2, 2025
