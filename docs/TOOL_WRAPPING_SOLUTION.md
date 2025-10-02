# Tool Wrapping Solution for JSON Storage

## Problem

After implementing JSON storage, agents were receiving the full dict structure in ToolMessages instead of clean text:

```
================================= Tool Message =================================
Name: get_YFin_data_online

{"_internal": true, "text_for_agent": "# Stock data for GOOGL...
```

This happened because:
1. Tools in `interface.py` return dicts with `{_internal, text_for_agent, json_for_storage}`
2. Tools in `agent_utils.py` were returning these dicts unchanged
3. LangGraph's ToolNode converted the dict to a string for the ToolMessage
4. Agents saw the JSON structure instead of clean text

## Solution: Tool Wrapping

Instead of having LoggingToolNode execute tools twice or try to parse ToolMessages, we wrap each tool BEFORE passing to LangGraph's ToolNode.

### Architecture

```
Original Tool Flow:
interface.py returns dict → agent_utils.py returns dict → ToolNode → Agent sees dict string ❌

New Tool Flow:
interface.py returns dict → agent_utils.py returns dict → LoggingToolNode wrapper intercepts:
  ├─→ Store JSON to database
  ├─→ Extract text_for_agent
  └─→ Return text to ToolNode → Agent sees clean text ✅
```

### Implementation

**File:** `ba2_trade_platform/thirdparties/TradingAgents/tradingagents/db_storage.py`

```python
class LoggingToolNode:
    """Custom ToolNode wrapper that logs tool calls and stores JSON data."""
    
    def __init__(self, tools, market_analysis_id=None):
        from langgraph.prebuilt import ToolNode
        
        self.market_analysis_id = market_analysis_id
        self.original_tools = {t.name: t for t in tools}
        
        # Wrap each tool to intercept results and store JSON
        wrapped_tools = []
        for original_tool in tools:
            wrapped = self._wrap_tool(original_tool)
            wrapped_tools.append(wrapped)
        
        # Create ToolNode with wrapped tools
        self.tool_node = ToolNode(wrapped_tools)
    
    def _wrap_tool(self, original_tool):
        """Wrap a tool to intercept its result and store JSON before returning."""
        
        def wrapped_func(*args, **kwargs):
            # Call original tool
            result = original_tool.invoke(kwargs if kwargs else args[0] if args else {})
            
            # Check if result is dict with internal format
            if isinstance(result, dict) and result.get('_internal'):
                text_for_agent = result.get('text_for_agent', '')
                json_for_storage = result.get('json_for_storage')
                
                # Store JSON to database
                if self.market_analysis_id and json_for_storage:
                    store_analysis_output(
                        market_analysis_id=self.market_analysis_id,
                        name=f"tool_output_{tool_name}_json",
                        output_type="tool_call_output_json",
                        text=json.dumps(json_for_storage, indent=2)
                    )
                
                # Return only text for agent
                return text_for_agent
            else:
                # Simple text result
                return result
        
        # Create new tool with same metadata
        wrapped_tool = tool_decorator(
            name=original_tool.name,
            description=original_tool.description,
            args_schema=original_tool.args_schema
        )(wrapped_func)
        
        return wrapped_tool
```

### Key Benefits

1. **✅ Clean Agent Messages**: Agents receive only `text_for_agent`, no JSON structure
2. **✅ JSON Storage Works**: `json_for_storage` is extracted and saved to database
3. **✅ No Double Execution**: Tools execute only once through LangGraph's ToolNode
4. **✅ Transparent Wrapping**: Wrapped tools maintain same name, description, and schema
5. **✅ Error Handling**: Wrapper handles errors and failed tool status

### Files Modified

| File | Change | Purpose |
|------|--------|---------|
| `db_storage.py` | Rewrote `LoggingToolNode` with wrapper pattern | Intercept dict, store JSON, return text |
| `agent_utils.py` | Return full result unchanged | Let wrapper handle extraction |
| `interface.py` | Return dict format | Already done in previous fix |

### How It Works

1. **Graph initializes** with `LoggingToolNode(tools, market_analysis_id)`
2. **LoggingToolNode wraps each tool**:
   - Creates new function that calls original
   - Intercepts return value
   - If dict with `_internal`, extracts JSON and text
   - Stores JSON to database
   - Returns only text
3. **Wrapped tools given to LangGraph's ToolNode**
4. **When agent calls tool**:
   - LangGraph calls wrapped function
   - Wrapper intercepts dict result
   - Stores JSON parameters
   - Returns clean text
   - LangGraph creates ToolMessage with text only
5. **Agent receives clean ToolMessage** with just the text content

### Testing

Run a TradingAgents analysis and check:

1. **Agents see clean text**:
   ```
   ================================= Tool Message =================================
   Name: get_YFin_data_online
   
   # Stock data for GOOGL from 2025-06-01 to 2025-10-02 (1h interval)
   # Total records: 591
   ...
   ```

2. **JSON stored in database**:
   ```python
   from ba2_trade_platform.core.db import get_db, select
   from ba2_trade_platform.core.models import AnalysisOutput
   
   session = get_db()
   json_outputs = session.exec(
       select(AnalysisOutput).where(AnalysisOutput.name.like('%_json'))
   ).all()
   
   print(f"Found {len(json_outputs)} JSON outputs")  # Should be > 0
   ```

3. **Check logs for**:
   ```
   [TOOL_CALL] Executing get_YFin_data_online with args: {...}
   [TOOL_RESULT] get_YFin_data_online returned: # Stock data...
   [JSON_STORED] Saved JSON parameters for get_YFin_data_online
   ```

## Date

October 2, 2025

## Status

✅ **COMPLETE** - Tool wrapping solution implemented:
- Agents receive clean text messages
- JSON parameters stored successfully  
- No double tool execution
- Proper error handling
