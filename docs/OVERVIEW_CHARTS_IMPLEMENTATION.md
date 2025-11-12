# Overview Page Charts Implementation

## Overview
Added two new chart widgets to the Overview page to provide better insights into trading performance and portfolio distribution.

## New Components

### 1. ProfitPerExpertChart
**Location**: `ba2_trade_platform/ui/components/ProfitPerExpertChart.py`

**Purpose**: Displays a histogram showing profit/loss for each expert instance based on completed transactions.

**Features**:
- Calculates profit from closed transactions: `(close_price - open_price) * quantity`
- Groups profits by expert instance
- Color-coded bars (green for profit, red for loss)
- Shows profit values on top of bars
- Summary statistics:
  - Total number of experts
  - Number of profitable experts
  - Total profit across all experts

**Data Source**: 
- `Transaction` table (status = CLOSED)
- Linked to `ExpertInstance` via `expert_id` foreign key

**Chart Type**: Bar chart (histogram) using ECharts

### 2. InstrumentDistributionChart
**Location**: `ba2_trade_platform/ui/components/InstrumentDistributionChart.py`

**Purpose**: Displays a pie chart showing distribution of open positions by instrument sector/label.

**Features**:
- Aggregates open positions by instrument categories/labels
- Uses market value for sizing pie slices
- Categorization hierarchy:
  1. First label from `Instrument.labels[]` (preferred)
  2. First category from `Instrument.categories[]` (fallback)
  3. `Instrument.instrument_type` (fallback)
  4. "Uncategorized" (if not in database)
- Interactive legend with scroll for many categories
- Displays percentage of total for each sector
- Expandable detailed breakdown table with:
  - Category name
  - Market value (dollar amount)
  - Percentage of total portfolio

**Data Source**:
- `Position` table (all open positions)
- `Instrument` table (for labels, categories, type)

**Chart Type**: Donut pie chart using ECharts

## Integration

### Files Modified

1. **`ba2_trade_platform/ui/components/__init__.py`**
   - Added exports for both new components

2. **`ba2_trade_platform/ui/pages/overview.py`**
   - Imported new chart components
   - Added charts to overview grid (Row 3)

### Layout

The Overview page now has a 2-column grid with 3 rows:
- **Row 1**: OpenAI Spending | Analysis Jobs
- **Row 2**: Order Statistics | Trade Recommendations  
- **Row 3**: Profit Per Expert | Instrument Distribution *(NEW)*

## Usage

The charts are automatically rendered when the Overview tab is opened. They:
- Fetch fresh data from the database on each render
- Use NiceGUI's ECharts integration for interactive visualizations
- Include error handling and logging for data issues
- Display "No data" messages when no relevant data exists

## Refresh Capability

Both components include a `refresh()` method that can be called to update the charts with fresh data without re-rendering the entire component.

Example:
```python
# Create chart instance
profit_chart = ProfitPerExpertChart()

# Later, refresh with new data
profit_chart.refresh()
```

## Data Requirements

### For Profit Chart
- Requires `Transaction` records with:
  - `status = CLOSED`
  - `expert_id` set (not NULL)
  - `open_price` and `close_price` set
  - Linked to valid `ExpertInstance`

### For Distribution Chart
- Requires `Position` records (any status)
- Optimal with `Instrument` records having:
  - `labels[]` populated (best categorization)
  - `categories[]` as fallback
  - `instrument_type` as final fallback

## Logging

Both components log:
- Debug: Individual transaction/position processing
- Info: Summary of calculations (e.g., "Calculated profits for 5 experts")
- Warning: Missing data (e.g., expert not found, instrument not in database)
- Error: Processing errors with full stack traces

## Future Enhancements

Potential improvements:
1. **Profit Chart**:
   - Filter by date range
   - Show per-symbol breakdown within expert
   - Add win rate percentage
   - Compare experts side-by-side

2. **Distribution Chart**:
   - Multiple categorization views (by type, sector, label)
   - Historical distribution tracking
   - Target allocation vs actual
   - Risk concentration warnings

## Technical Notes

- Uses SQLModel ORM for database queries
- ECharts for charting (JavaScript library)
- Follows BA2 Trade Platform component patterns
- All database sessions properly closed in finally blocks
- Follows logging best practices (no `exc_info` outside exception handlers)
