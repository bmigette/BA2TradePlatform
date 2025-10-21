# Data Visualizer Provider Refactoring

## Problem
The TradingAgentsUI data visualizer always uses YFinanceDataProvider for fetching price data, regardless of which provider the expert actually used during analysis. This can cause:
- Different data than what the analysis was based on
- Inconsistent technical indicators
- Misleading chart visualizations

## Solution Overview

### Step 1: Store Provider Information (Already Done)
The toolkit's `get_ohlcv_data()` method already:
- Calls `provider.get_ohlcv_data_formatted()` with `format_type="both"`
- Returns both markdown (for LLM) and dict (for storage)
- LoggingToolNode stores this as `tool_output_get_ohlcv_data_json`

### Step 2: Enhance JSON Storage (Needed)
The JSON output needs to include provider metadata:
```json
{
  "provider_class": "YFinanceDataProvider",
  "provider_module": "ba2_trade_platform.modules.dataproviders.ohlcv.YFinanceDataProvider",
  "symbol": "AAPL",
  "start_date": "2025-01-01",
  "end_date": "2025-10-21",
  "interval": "1h",
  "data": [
    {"date": "2025-01-01T09:30:00Z", "open": 150.0, "high": 151.0, "low": 149.5, "close": 150.5, "volume": 1000000},
    ...
  ]
}
```

### Step 3: Modify TradingAgentsUI (Main Change)
Replace the hardcoded YFinanceDataProvider with dynamic provider selection:

```python
def _get_ohlcv_data_provider(self):
    """
    Get the OHLCV provider that was used during analysis.
    
    Returns provider instance and metadata:
    1. First: Check database for tool_output_get_ohlcv_data_json
    2. Fallback: Check expert settings for configured provider
    3. Default: Use YFinanceDataProvider
    """
    session = get_db()
    try:
        # Look for stored OHLCV data with provider info
        statement = (
            select(AnalysisOutput)
            .where(AnalysisOutput.market_analysis_id == self.market_analysis.id)
            .where(AnalysisOutput.name == 'tool_output_get_ohlcv_data_json')
        )
        output = session.exec(statement).first()
        
        if output and output.text:
            try:
                data = json.loads(output.text)
                provider_module = data.get('provider_module')
                if provider_module:
                    # Dynamically import the provider class
                    parts = provider_module.split('.')
                    class_name = parts[-1]
                    module_path = '.'.join(parts[:-1])
                    module = __import__(module_path, fromlist=[class_name])
                    provider_class = getattr(module, class_name)
                    return provider_class(), data
            except Exception as e:
                logger.warning(f"Could not reconstruct provider from database: {e}")
        
        # Fallback: Get from expert settings
        from ...modules.experts.TradingAgents import TradingAgents
        trading_agents = TradingAgents(self.market_analysis.expert_instance_id)
        provider_setting = trading_agents.settings.get('ohlcv_provider', 'YFinance')
        
        if provider_setting == 'FMP':
            from ba2_trade_platform.modules.dataproviders import FMPOHLCVProvider
            return FMPOHLCVProvider(), None
        elif provider_setting == 'AlphaVantage':
            from ba2_trade_platform.modules.dataproviders import AlphaVantageOHLCVProvider
            return AlphaVantageOHLCVProvider(), None
        else:
            from ba2_trade_platform.modules.dataproviders import YFinanceDataProvider
            return YFinanceDataProvider(), None
            
    finally:
        session.close()
```

### Step 4: Add Indicator Toggle Control
Add checkbox in data visualization panel:
- **Checkbox: "Use Stored Indicators"** (default: checked)
  - When checked: Load indicators from database (AnalysisOutput)
  - When unchecked: Recalculate indicators live using StockstatsUtils
- Display note showing which source is being used

## Implementation Steps

### Change 1: Toolkit Enhancement (Optional - For Future)
Modify `agent_utils_new.py` `get_ohlcv_data()` to extract and return provider info:
```python
# Ensure provider name is passed to LoggingToolNode
if isinstance(result, dict) and "text" in result and "data" in result:
    # Enhance data dict with provider info
    result["data"]["provider_class"] = provider_name
    result["data"]["provider_module"] = f"{provider.__class__.__module__}.{provider.__class__.__name__}"
    return result["text"]
```

### Change 2: TradingAgentsUI Refactoring (Main)
1. Add `_get_ohlcv_data_provider()` method to detect correct provider
2. Modify price data fetching to use detected provider
3. Add toggle control for indicators
4. Update data summary to show:
   - Which provider was used (stored vs live)
   - Data source for indicators (database vs recalculated)

### Change 3: UI Enhancement
Add controls in `_render_data_visualization_panel()`:
```python
with ui.row().classes('gap-2 mb-4'):
    use_stored_checkbox = ui.checkbox(
        'Use Stored Indicators',
        value=True,
        on_change=lambda: self._refresh_visualization(use_stored_checkbox.value)
    )
    ui.label('(Uncheck to recalculate live)').classes('text-xs text-gray-500')
```

## Data Flow

### Current (Broken):
```
TradingAgents Analysis
  ├─ Uses FMP/Alpaca/etc Provider
  ├─ Stores data in AnalysisOutput
  └─ Stores indicators in AnalysisOutput

Data Visualizer (TradingAgentsUI)
  ├─ Ignores stored provider
  ├─ Always uses YFinance
  └─ Shows different data → Misleading chart
```

### Fixed:
```
TradingAgents Analysis
  ├─ Uses FMP/Alpaca/etc Provider
  ├─ Stores provider info in tool_output_get_ohlcv_data_json
  └─ Stores indicators in AnalysisOutput

Data Visualizer (TradingAgentsUI)
  ├─ Detects provider from database or settings
  ├─ Fetches fresh data using same provider
  ├─ Toggle: Use stored vs recalculated indicators
  └─ Accurate chart matching original analysis
```

## Testing

### Test Cases:
1. **Verify provider detection**: Check that correct provider is identified from database
2. **Compare outputs**: Fetch data with detected provider vs hardcoded YFinance
3. **Toggle functionality**: Switch between stored and live indicators, verify updates
4. **Fallback logic**: Disable provider storage and verify fallback to expert settings
5. **Default fallback**: Verify YFinance is used when all else fails

## Files to Modify

1. `ba2_trade_platform/modules/experts/TradingAgentsUI.py`
   - Add `_get_ohlcv_data_provider()` method
   - Modify `_render_data_visualization_panel()` to use detected provider
   - Add toggle control for indicators
   - Update data summary display

2. `ba2_trade_platform/thirdparties/TradingAgents/tradingagents/agents/utils/agent_utils_new.py` (Optional)
   - Enhance `get_ohlcv_data()` to store provider metadata in JSON

## Benefits

✅ **Accurate Visualization**: Charts show the same data the analysis was based on
✅ **Provider Flexibility**: Works with any configured provider (YFinance, FMP, AlphaVantage, Alpaca)
✅ **Debugging**: Easy to compare stored vs live data
✅ **Performance**: Can use stored data for faster loading or recalculate for latest
✅ **Transparency**: UI shows which source is being used
