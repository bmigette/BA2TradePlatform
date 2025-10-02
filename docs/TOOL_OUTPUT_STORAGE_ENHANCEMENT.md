# Tool Output Storage Enhancement

## Issue Analysis

### Current State
- `get_YFin_data_online` and `get_stockstats_indicators_report_online` store outputs as text
- Price data stored as CSV with header comments
- Indicator data stored as markdown-formatted text
- Timeframe support IS implemented and working correctly

### Problems Identified
1. **Parsing Complexity**: CSV text requires string parsing, error-prone
2. **Indicator Format**: Markdown format requires custom parsing logic
3. **No JSON Format**: No structured data format for easy parsing
4. **Verification Needed**: Need to confirm datetime information is preserved for intraday data

## Proposed Solution

### 1. Dual Storage Format
Store tool outputs in TWO formats:
- **Text Format** (existing): Human-readable, for display in UI
- **JSON Format** (new): Structured data, for programmatic parsing

### 2. JSON Structure

#### Price Data JSON Format
```json
{
  "symbol": "AAPL",
  "start_date": "2025-10-01",
  "end_date": "2025-10-02",
  "interval": "1h",
  "total_records": 7,
  "data": [
    {
      "Datetime": "2025-10-01 09:30:00",
      "Open": 220.50,
      "High": 221.00,
      "Low": 220.20,
      "Close": 220.80,
      "Volume": 1234567
    },
    ...
  ]
}
```

#### Indicator Data JSON Format
```json
{
  "indicator": "close_50_sma",
  "symbol": "AAPL",
  "start_date": "2025-07-03",
  "end_date": "2025-10-01",
  "interval": "1d",
  "data": [
    {
      "Date": "2025-09-30",
      "value": 355.00339782714843
    },
    {
      "Date": "2025-09-29",
      "value": 355.30199768066404
    },
    ...
  ]
}
```

### 3. Implementation Plan

#### Phase 1: Update interface.py
- Modify `get_YFin_data_online()` to return both CSV and JSON
- Modify indicator functions to return both markdown and JSON

#### Phase 2: Update db_storage.py
- Store two AnalysisOutput records per tool call:
  - One with `name=f"tool_output_{tool_name}"` (text format - existing)
  - One with `name=f"tool_output_{tool_name}_json"` (JSON format - new)

#### Phase 3: Update TradingAgentsUI.py
- Prefer JSON format for parsing if available
- Fall back to text parsing if JSON not found
- Much simpler and more reliable parsing

### 4. Benefits
- **Backward Compatible**: Existing text format still works
- **Easier Parsing**: JSON is native Python data structure
- **Better Type Safety**: Datetime objects preserved correctly
- **Faster**: No string parsing overhead
- **More Reliable**: No parsing errors from malformed text

### 5. Migration Strategy
- New analyses will have both formats
- Old analyses still work with text parsing
- No database migration needed
- Gradual improvement over time

## Implementation Details

### Modified Functions

#### get_YFin_data_online (interface.py)
```python
def get_YFin_data_online(...):
    # Existing code to fetch data
    data = ticker.history(start=start_date, end=end_date, interval=interval)
    
    # Convert to JSON structure
    json_data = {
        "symbol": symbol.upper(),
        "start_date": start_date,
        "end_date": end_date,
        "interval": interval,
        "total_records": len(data),
        "data": data.reset_index().to_dict(orient='records')
    }
    
    # Return both formats
    return {
        "text": header + csv_string,  # Existing CSV format
        "json": json_data  # New JSON format
    }
```

#### Tool Storage (db_storage.py)
```python
# In DatabaseToolNode.call_tools
result_content = tool_msg.content

# Check if result is dict with text/json
if isinstance(result_content, dict) and 'text' in result_content:
    # Store text format
    store_analysis_output(
        market_analysis_id=self.market_analysis_id,
        name=f"tool_output_{tool_name}",
        output_type="tool_call_output",
        text=result_content['text']
    )
    
    # Store JSON format if available
    if 'json' in result_content:
        store_analysis_output(
            market_analysis_id=self.market_analysis_id,
            name=f"tool_output_{tool_name}_json",
            output_type="tool_call_output_json",
            text=json.dumps(result_content['json'])
        )
else:
    # Legacy format - just store as text
    store_analysis_output(...)
```

#### UI Parsing (TradingAgentsUI.py)
```python
# In _render_data_visualization_panel
for output in outputs:
    output_obj = output[0] if isinstance(output, tuple) else output
    
    # Try JSON format first
    if output_obj.name.endswith('_json') and 'yfin_data' in output_obj.name.lower():
        try:
            json_data = json.loads(output_obj.text)
            price_data = pd.DataFrame(json_data['data'])
            
            # Set datetime index
            if 'Datetime' in price_data.columns:
                price_data['Datetime'] = pd.to_datetime(price_data['Datetime'])
                price_data.set_index('Datetime', inplace=True)
            
            logger.info(f"Loaded price data from JSON: {len(price_data)} rows")
        except Exception as e:
            logger.error(f"Error parsing JSON price data: {e}", exc_info=True)
    
    # Fall back to CSV parsing if JSON not found or failed
    elif 'yfin_data' in output_obj.name.lower() and not output_obj.name.endswith('_json'):
        # Existing CSV parsing logic
        ...
```

## Testing Plan

### Test Cases
1. **Daily Data**: Verify 1d interval stores single date per record
2. **Hourly Data**: Verify 1h interval stores datetime with hour
3. **Minute Data**: Verify 1m interval stores datetime with minutes
4. **JSON Parsing**: Verify JSON format parses correctly
5. **CSV Fallback**: Verify old analyses still work
6. **Visualization**: Verify chart displays both formats correctly

### Test Script
```bash
# Test different timeframes
python test_tool.py get_YFin_data_online 1m 1 SPY 2025-10-01
python test_tool.py get_YFin_data_online 1h 3 AAPL 2025-10-01
python test_tool.py get_YFin_data_online 1d 7 MSFT 2025-10-01
```

## Next Steps

1. ✅ Document current state and proposed solution
2. ⏳ Implement dual-format storage in interface.py
3. ⏳ Update db_storage.py to handle both formats
4. ⏳ Update TradingAgentsUI.py to prefer JSON parsing
5. ⏳ Test with various timeframes
6. ⏳ Update documentation
