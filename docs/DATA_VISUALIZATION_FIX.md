# Data Visualization Fix

## Issue

The data visualization panel in TradingAgents UI was showing "No price data available for visualization" because it was trying to reconstruct data from stored tool outputs instead of fetching fresh data using expert configuration settings.

### Problems Identified

1. **Relying on stored tool outputs**: The code was looking for `tool_output_get_YFin_data_online` in AnalysisOutput, but these outputs may not exist or may be incomplete
2. **Missing expert settings**: Not using the expert's configured `market_history_days` and `timeframe` settings
3. **Wrong date range**: Not using the analysis run date (`created_at`) as the end date for data fetching
4. **Incomplete indicator tracking**: Indicators were supposed to be recorded in the database but the tracking may not be working properly

## Solution

Modified `_render_data_visualization_panel()` in `TradingAgentsUI.py` to:

1. **Fetch expert settings** from the `ExpertInstance` associated with the analysis
2. **Calculate proper date range** using:
   - `end_date` = `market_analysis.created_at` (when the analysis ran)
   - `start_date` = `end_date - timedelta(days=market_history_days)`
3. **Fetch fresh price data** using the `YFinanceDataProvider` with expert settings
4. **Keep indicator reconstruction** from stored outputs (if available)

### Architecture Flow

```
┌─────────────────────────────────────────────────────────────┐
│ TradingAgentsUI._render_data_visualization_panel()          │
└─────────────────────────────────────────────────────────────┘
                          ↓
┌─────────────────────────────────────────────────────────────┐
│ Step 1: Get Expert Instance and Settings                    │
│ - ExpertInstance.id from market_analysis.expert_instance_id │
│ - Build expert config using TradingAgents._build_expert_config() │
│ - Extract: market_history_days, timeframe                   │
└─────────────────────────────────────────────────────────────┘
                          ↓
┌─────────────────────────────────────────────────────────────┐
│ Step 2: Calculate Date Range                                │
│ - end_date = market_analysis.created_at                     │
│ - start_date = end_date - timedelta(days=market_history_days)│
└─────────────────────────────────────────────────────────────┘
                          ↓
┌─────────────────────────────────────────────────────────────┐
│ Step 3: Fetch Price Data                                    │
│ - Use YFinanceDataProvider.get_dataframe()                  │
│ - Parameters: symbol, start_date, end_date, interval        │
│ - Data retrieved from cache or fetched from Yahoo Finance   │
└─────────────────────────────────────────────────────────────┘
                          ↓
┌─────────────────────────────────────────────────────────────┐
│ Step 4: Reconstruct Indicators (from stored outputs)        │
│ - Query AnalysisOutput for indicator parameters             │
│ - Use StockstatsUtils.get_stock_stats_range()              │
│ - Recalculate indicators from cached price data             │
└─────────────────────────────────────────────────────────────┘
                          ↓
┌─────────────────────────────────────────────────────────────┐
│ Step 5: Render InstrumentGraph                              │
│ - Pass price_data and indicators_data to graph component    │
│ - Display interactive chart with overlays                   │
└─────────────────────────────────────────────────────────────┘
```

## Code Changes

**File:** `ba2_trade_platform/modules/experts/TradingAgentsUI.py`

**Method:** `_render_data_visualization_panel()`

### Key Changes:

1. **Get Expert Configuration:**
```python
# Get expert instance to retrieve settings
from ...core.db import get_instance
from ...core.models import ExpertInstance
from datetime import timedelta

expert_instance = get_instance(ExpertInstance, self.market_analysis.expert_instance_id)

# Get expert settings
from ...modules.experts.TradingAgents import TradingAgents
trading_agents = TradingAgents(expert_instance.id)
expert_config = trading_agents._build_expert_config()

# Extract key parameters
market_history_days = expert_config.get('market_history_days', 90)
timeframe = expert_config.get('timeframe', '1d')
```

2. **Calculate Date Range:**
```python
# Calculate date range based on analysis run date
end_date = self.market_analysis.created_at
start_date = end_date - timedelta(days=market_history_days)
```

3. **Fetch Price Data Directly:**
```python
# Fetch price data using expert settings
price_data = provider.get_dataframe(
    symbol=self.market_analysis.symbol,
    start_date=start_date,
    end_date=end_date,
    interval=timeframe
)
```

4. **Enhanced Data Summary:**
```python
# Show retrieval parameters in data summary
ui.label('Data Retrieval Parameters:').classes('text-sm font-bold mb-2')
ui.label(f'  • Symbol: {self.market_analysis.symbol}')
ui.label(f'  • Date Range: {start_date.date()} to {end_date.date()}')
ui.label(f'  • Lookback Period: {market_history_days} days')
ui.label(f'  • Timeframe/Interval: {timeframe}')
```

## Benefits

✅ **Always shows price data** - Fetches fresh data even if tool outputs are missing  
✅ **Uses correct settings** - Respects expert's configured lookback period and timeframe  
✅ **Correct date range** - Uses the actual analysis run date for accurate historical view  
✅ **Better logging** - Shows what parameters were used for data retrieval  
✅ **Maintains indicator support** - Still reconstructs indicators from stored parameters  
✅ **Fallback support** - Legacy CSV parsing still works for old analyses  

## Indicator Tracking (Future Enhancement)

Currently, indicators are reconstructed from `AnalysisOutput` records that store indicator parameters in JSON format. The system:

1. **Stores indicator parameters** when tools run (via `db_storage.py`)
2. **Reconstructs indicators** by recalculating from cached price data using stored parameters
3. **Falls back to legacy parsing** if no JSON parameters are found

### How Indicators Are Tracked:

```
┌─────────────────────────────────────────────────────────────┐
│ During Analysis (db_storage.py)                             │
│ - Tool returns: {_internal, text_for_agent, json_for_storage}│
│ - json_for_storage contains parameters:                     │
│   {tool, symbol, indicator, interval, start_date, end_date} │
│ - Stored in AnalysisOutput with name ending in "_json"      │
└─────────────────────────────────────────────────────────────┘
                          ↓
┌─────────────────────────────────────────────────────────────┐
│ During Visualization (TradingAgentsUI.py)                   │
│ - Query AnalysisOutput for *_json outputs                   │
│ - Extract parameters from JSON                               │
│ - Recalculate indicator using StockstatsUtils               │
│ - Display on chart as overlay                               │
└─────────────────────────────────────────────────────────────┘
```

### Potential Improvements:

- **Explicit indicator list** in MarketAnalysis model
- **Indicator metadata table** for better tracking
- **Pre-calculated indicator storage** for faster loading
- **Indicator selection UI** to show/hide specific indicators

## Testing

### Test Scenarios:

1. **New Analysis:**
   - Run a fresh TradingAgents analysis
   - Navigate to Data Visualization tab
   - Verify price chart displays with expert's configured timeframe
   - Check that date range matches lookback period

2. **Old Analysis:**
   - View a previously completed analysis
   - Verify price data is fetched even without tool outputs
   - Check that indicators are reconstructed if available

3. **Different Settings:**
   - Test with 1d interval (daily data)
   - Test with 1h interval (hourly data)
   - Test with different lookback periods (30, 60, 90 days)

4. **Error Handling:**
   - Test with invalid symbol
   - Test with missing expert instance
   - Verify error messages are displayed properly

## Date

October 2, 2025
