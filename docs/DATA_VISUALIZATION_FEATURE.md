# Data Visualization Feature - InstrumentGraph Component

## Overview
Added interactive data visualization capabilities to the BA2 Trade Platform, enabling traders to view price action and technical indicators for analyzed instruments through an intuitive charting interface.

## Implementation Date
October 2, 2025

## Components Created

### 1. InstrumentGraph Component
**File:** `ba2_trade_platform/ui/components/InstrumentGraph.py` (329 lines)

#### Features
- **Interactive Candlestick Charts**: OHLC price visualization using Plotly
- **Volume Subplot**: Separate volume bars for trading volume analysis
- **Technical Indicators**: Multiple indicator overlays with toggleable visibility
- **Checkbox Controls**: Show/hide individual indicators dynamically
- **Fallback Support**: Highcharts configuration if Plotly is unavailable
- **Responsive Design**: Auto-scaling layout with proper sizing

#### Constructor Parameters
```python
InstrumentGraph(
    symbol: str,              # Instrument symbol (e.g., "AAPL")
    price_data: pd.DataFrame, # OHLC data with Date index
    indicators_data: Dict[str, pd.DataFrame] = None  # Technical indicators
)
```

#### Price Data Format
```python
# Required columns: Open, High, Low, Close, Volume
# Index: DatetimeIndex
price_data = pd.DataFrame({
    'Open': [...],
    'High': [...],
    'Low': [...],
    'Close': [...],
    'Volume': [...]
}, index=pd.DatetimeIndex([...]))
```

#### Indicators Data Format
```python
# Dictionary of indicator name -> DataFrame
indicators_data = {
    'SMA 20': pd.DataFrame({'value': [...]}, index=pd.DatetimeIndex([...])),
    'RSI': pd.DataFrame({'value': [...]}, index=pd.DatetimeIndex([...])),
    'MACD': pd.DataFrame({'value': [...]}, index=pd.DatetimeIndex([...]))
}
```

#### Key Methods
- `render()`: Creates UI with chart and checkbox controls
- `_toggle_indicator(name, visible)`: Shows/hides indicator overlays
- `_render_chart()`: Builds and displays Plotly figure
- `_build_chart_config()`: Constructs candlestick + volume + indicators
- `_build_fallback_chart()`: Provides Highcharts alternative
- `update_data(price_data, indicators_data)`: Dynamic data updates

### 2. TradingAgentsUI Integration
**File:** `ba2_trade_platform/modules/experts/TradingAgentsUI.py`

#### Changes Made
1. **Added Imports**
   - `import pandas as pd` - DataFrame handling
   - `import io` - CSV parsing
   - `from ...core.models import AnalysisOutput` - Database queries
   - `from ...ui.components import InstrumentGraph` - Chart component

2. **Added Data Visualization Tab**
   - Tab label: "📉 Data Visualization"
   - Added to both `_render_completed_ui()` and `_render_in_progress_ui()`
   - Wired to call `_render_data_visualization_panel()`

3. **Implemented _render_data_visualization_panel() Method**
   - Queries `AnalysisOutput` table for market_analysis_id
   - Searches for tool outputs:
     * `tool_output_get_YFin_data_online` - Price OHLC data
     * `tool_output_get_stockstats_indicators_report_online` - Technical indicators
   - Parses CSV data from `text` field
   - Converts to pandas DataFrames with proper datetime indexing
   - Handles both JSON and CSV formatted indicator data
   - Creates and renders InstrumentGraph component
   - Shows data summary in expandable section
   - Graceful error handling with user-friendly messages

## Data Flow

### 1. Analysis Execution
```
TradingAgents Expert
    ↓
Calls get_YFin_data_online tool
    ↓
Stores CSV result in AnalysisOutput.text
    ↓
Calls get_stockstats_indicators_report_online tool
    ↓
Stores CSV result in AnalysisOutput.text
```

### 2. Visualization Rendering
```
User opens TradingAgentsUI
    ↓
Clicks "📉 Data Visualization" tab
    ↓
_render_data_visualization_panel() called
    ↓
Query AnalysisOutput for market_analysis_id
    ↓
Parse price CSV → pd.DataFrame
    ↓
Parse indicator CSV → Dict[str, pd.DataFrame]
    ↓
InstrumentGraph(symbol, price_data, indicators_data)
    ↓
Render interactive Plotly chart
```

## Database Schema

### AnalysisOutput Table
```python
class AnalysisOutput(SQLModel, table=True):
    id: int
    created_at: DateTime
    market_analysis_id: int  # Foreign key to MarketAnalysis
    name: str                # Tool output name
    type: str                # Output type
    text: str | None         # CSV data stored here
    blob: bytes | None       # Alternative binary storage
```

### Tool Output Naming Convention
- **Price Data**: `tool_output_get_YFin_data_online`
- **Indicators**: `tool_output_get_stockstats_indicators_report_online_<indicator_name>`

## User Interface

### Tab Structure
```
TradingAgents Analysis View
├── 🎯 Overview
├── 💬 Analysis Content (Debate)
├── 📊 Expert Recommendation
└── 📉 Data Visualization  ← NEW TAB
    ├── Price Chart (Candlestick)
    ├── Volume Chart (Bars)
    ├── Technical Indicators (Lines)
    ├── Indicator Checkboxes (Toggle visibility)
    └── 📊 Data Summary (Expandable)
        ├── Price Data: X data points
        └── Technical Indicators: Y indicators
```

### Chart Features
- **Candlestick Chart**: Green (up) / Red (down) candles
- **Volume Bars**: Color-coded by price movement
- **Indicator Lines**: Multiple colors for clarity
- **Hover Tooltips**: Detailed values on mouse hover
- **Zoom/Pan**: Interactive chart navigation
- **Legend**: Toggle indicators on/off
- **Responsive**: Adapts to screen size

## Error Handling

### Graceful Degradation
1. **No Price Data**: Shows message "No price data available for visualization"
2. **No Indicators**: Chart renders with price only
3. **Parse Errors**: Logs error, continues with available data
4. **Missing Plotly**: Falls back to Highcharts configuration
5. **Database Errors**: Shows user-friendly error message

### Logging
All errors logged with full stack traces:
```python
logger.error(f"Error parsing price data: {e}", exc_info=True)
```

## Dependencies

### New Dependency
- **plotly** (6.3.0): Interactive charting library
  - Added to `requirements.txt`
  - Installed via: `pip install plotly`
  - Includes: `narwhals` (2.6.0) as sub-dependency

### Existing Dependencies
- pandas: DataFrame operations
- nicegui: UI framework
- sqlmodel: ORM for database queries

## Testing

### Test Script
**File:** `test_data_visualization.py`

#### What It Tests
1. Finds completed MarketAnalysis in database
2. Checks for AnalysisOutput records with price/indicator data
3. Verifies TradingAgentsUI instantiation
4. Reports data availability

#### Running the Test
```powershell
.venv\Scripts\python.exe test_data_visualization.py
```

#### Expected Output
```
================================================================================
Testing InstrumentGraph Integration with TradingAgentsUI
================================================================================
✅ Found analysis: ID=29, Symbol=AAPL, Status=COMPLETED

📊 Analysis Outputs (2 found):
   • tool_output_get_YFin_data_online: 5432 chars
     ✓ Price data found
   • tool_output_get_stockstats_indicators_report_online_SMA: 3421 chars
     ✓ Indicator data found

🎨 Testing UI Instantiation:
✅ TradingAgentsUI instantiated successfully

✅ Test completed successfully!
```

## Usage Guide

### For Users
1. Start the application: `.venv\Scripts\python.exe main.py`
2. Navigate to **Market Analysis** page
3. View a completed **TradingAgents** analysis
4. Click the **📉 Data Visualization** tab
5. Use checkboxes to toggle indicator visibility
6. Hover over chart for detailed values
7. Expand **📊 Data Summary** for data details

### For Developers

#### Adding New Indicator Support
```python
# In expert tool, store indicator output
output = AnalysisOutput(
    market_analysis_id=analysis_id,
    name=f"tool_output_get_stockstats_indicators_report_online_RSI",
    type="text/csv",
    text=indicator_df.to_csv(index=True)
)
add_instance(output)
```

#### Custom Chart Configuration
```python
# Modify InstrumentGraph._build_chart_config()
# Add new chart types, layouts, or styling
```

## Performance Considerations

### Data Loading
- CSV parsing done on-demand (only when tab opened)
- DataFrames created once per view
- Database query limited to single market_analysis_id

### Chart Rendering
- Plotly uses WebGL for smooth performance
- Large datasets (>10,000 points) may slow rendering
- Consider data sampling for very long time series

### Memory Usage
- DataFrames kept in memory during UI lifecycle
- Automatically garbage collected when analysis view closed
- Database session properly closed after data fetch

## Future Enhancements

### Potential Features
1. **Export Chart**: Download as PNG/SVG
2. **Date Range Selector**: Zoom to specific time period
3. **Indicator Comparison**: Side-by-side indicator views
4. **Custom Indicators**: User-defined indicator calculations
5. **Real-time Updates**: Live price streaming
6. **Multiple Timeframes**: Switch between 1m, 5m, 1h, 1d, etc.
7. **Drawing Tools**: Trendlines, support/resistance levels
8. **Annotation Support**: Add notes to specific price points

### Code Improvements
1. Async data loading for better UI responsiveness
2. Caching parsed DataFrames to avoid re-parsing
3. Progressive rendering for large datasets
4. More sophisticated error recovery

## Known Limitations

1. **Expert-Specific**: Only works with experts that generate YFin and stockstats tool outputs
2. **Static Data**: Shows analysis-time data, not real-time
3. **Single Symbol**: One chart per analysis (no multi-symbol comparison)
4. **No Editing**: Read-only view (cannot modify price/indicator data)
5. **Desktop-First**: Mobile experience may vary

## Compatibility

### Experts Supporting Visualization
- ✅ **TradingAgents**: Full support (price + indicators)
- ❌ **FinnHubRating**: No chart data generated

### Browser Support
- Chrome/Edge: Full support with WebGL
- Firefox: Full support
- Safari: Full support
- Mobile browsers: Limited (touch gestures may vary)

## Related Documentation
- InstrumentGraph Component API
- TradingAgents Expert Documentation
- AnalysisOutput Database Schema
- NiceGUI Charting Guide

## Change Log

### 2025-10-02: Initial Release
- Created InstrumentGraph component (329 lines)
- Integrated with TradingAgentsUI
- Added Data Visualization tab
- Implemented CSV parsing logic
- Added fallback chart support
- Created test script
- Installed plotly dependency
- Documented feature
