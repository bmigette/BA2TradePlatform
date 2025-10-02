# Overview Charts Fix - Avoid Duplicate Position Fetching

## Problem
The Instrument Distribution Chart was querying the `Position` database table, which is not populated in real-time. The actual position data comes from live account provider API calls, not from the database. This caused the chart to show "No open positions found" even when positions existed.

Meanwhile, the "Open Positions Across All Accounts" table was correctly fetching positions by calling `provider_obj.get_positions()` for each account.

## Solution
Refactored the code to:
1. Fetch positions **once** from account providers in `AccountOverviewTab`
2. Pass the position data to `InstrumentDistributionChart` as a parameter
3. Avoid duplicate API calls to account providers

## Changes Made

### 1. InstrumentDistributionChart.py
**Updated constructor to accept positions parameter:**
```python
def __init__(self, positions: List[Dict] = None):
    """
    Initialize the chart with positions data.
    
    Args:
        positions: List of position dictionaries from account providers.
                  If None, chart will display "No data" message.
    """
    self.chart = None
    self.positions = positions or []
    self.render()
```

**Changed data source:**
- **Before**: Queried `Position` table from database
- **After**: Uses `self.positions` passed from parent component

**Updated position processing:**
- Works with dict objects from account providers (not SQLModel Position objects)
- Handles string-formatted values (e.g., market_value as "123.45")
- Converts strings to floats for calculations
- Maintains error handling for missing/invalid data

**Updated refresh method:**
```python
def refresh(self, positions: List[Dict] = None):
    """
    Refresh the chart with updated data.
    
    Args:
        positions: New list of position dictionaries. If None, uses existing positions.
    """
```

### 2. overview.py - AccountOverviewTab
**Modified render method to keep raw positions:**
```python
# Keep unformatted positions for chart calculations
all_positions_raw = []

for acc in accounts:
    # ... fetch positions ...
    for pos in positions:
        pos_dict = pos if isinstance(pos, dict) else dict(pos)
        pos_dict['account'] = acc.name
        
        # Keep raw copy for chart (with float values)
        all_positions_raw.append(pos_dict.copy())
        
        # Format for display table (strings)
        for k, v in pos_dict.items():
            if isinstance(v, float):
                pos_dict[k] = f"{v:.2f}"
        all_positions.append(pos_dict)
```

**Added chart with positions data:**
```python
# Position Distribution Chart (uses same data as table)
if all_positions_raw:
    InstrumentDistributionChart(positions=all_positions_raw)
```

### 3. overview.py - OverviewTab
**Removed InstrumentDistributionChart from OverviewTab:**
- This tab doesn't have access to live position data
- Chart now only appears in AccountOverviewTab where positions are fetched
- ProfitPerExpertChart remains in OverviewTab (uses database Transaction data)

## Benefits

1. **Single API Call**: Positions fetched once from account providers, used by both table and chart
2. **Consistent Data**: Table and chart show exactly the same positions
3. **Better Performance**: No duplicate API calls to broker APIs
4. **Correct Data Source**: Uses live data from providers, not stale database table
5. **Clear Separation**: 
   - OverviewTab: Database-driven widgets (transactions, analysis jobs)
   - AccountOverviewTab: Live account data (positions, distribution)

## Chart Location

**Before**: 
- OverviewTab (incorrect - showed "No data")
- No chart in AccountOverviewTab

**After**:
- OverviewTab: Profit Per Expert Chart only
- AccountOverviewTab: Position Distribution Chart (after positions table)

## Data Flow

```
AccountOverviewTab.render()
    ↓
For each account:
    provider.get_positions() [LIVE API CALL]
    ↓
    all_positions_raw (floats) → InstrumentDistributionChart
    all_positions (strings) → Table display
```

## Testing Notes

After restart, verify:
1. Navigate to "Account Overview" tab
2. Positions table should show your open positions
3. Below the table, "Position Distribution by Sector" chart should appear
4. Chart should show same symbols as table
5. No "No open positions found" error
6. Check logs - should only see ONE set of position fetch logs per account

## Future Enhancements

Consider caching position data in a reactive variable that can be:
- Refreshed on demand with a button
- Auto-refreshed on a timer
- Shared across multiple components without re-fetching
