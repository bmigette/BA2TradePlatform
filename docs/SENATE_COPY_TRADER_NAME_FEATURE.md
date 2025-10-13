# Senate Copy Expert - Trader Name Display Feature

## Overview
Added functionality to display the senator/representative trader names in the Market Analysis UI for FMPSenateTraderCopy expert recommendations.

## Implementation Approach
Instead of adding a new database field, trader names are stored in the `MarketAnalysis.state` JSON field, which is already available and flexible for storing expert-specific metadata.

## Changes Made

### 1. FMPSenateTraderCopy Expert (`ba2_trade_platform/modules/experts/FMPSenateTraderCopy.py`)

#### Store Trader Names in State
- **Multi-Symbol Analysis**: Added `traders_by_symbol` dictionary to the `copy_trade_multi` state
  ```python
  traders_by_symbol = {}
  for trade_symbol, recs in symbol_recommendations.items():
      if recs and 'trader_name' in recs:
          traders_by_symbol[trade_symbol] = recs['trader_name']
  
  market_analysis.state = {
      'copy_trade_multi': {
          'traders_by_symbol': traders_by_symbol,  # Store trader names for UI
          # ... other state fields
      }
  }
  ```

#### Trader Name Already in Recommendation Data
- The `_generate_recommendations()` method already extracts and returns `trader_name` from the most recent trade
- This trader name is now stored in the state for UI consumption

### 2. Market Analysis UI (`ba2_trade_platform/ui/pages/marketanalysis.py`)

#### Added Trader Column to Table
```python
columns = [
    {'name': 'id', 'label': 'ID', 'field': 'id', 'sortable': True, 'style': 'width: 80px'},
    {'name': 'symbol', 'label': 'Symbol', 'field': 'symbol', 'sortable': True, 'style': 'width: 100px'},
    {'name': 'trader', 'label': 'Trader', 'field': 'trader', 'sortable': True, 'style': 'width: 150px'},  # NEW
    {'name': 'expert', 'label': 'Expert', 'field': 'expert_name', 'sortable': True, 'style': 'width: 150px'},
    # ... rest of columns
]
```

#### Extract Trader Name from State
```python
trader_display = '-'

# Get trader name from state for copy trade experts
if analysis.state and isinstance(analysis.state, dict):
    if 'copy_trade_multi' in analysis.state:
        # Multi-symbol copy trade - get trader names by symbol
        traders_by_symbol = analysis.state['copy_trade_multi'].get('traders_by_symbol', {})
        if traders_by_symbol:
            unique_traders = list(set(traders_by_symbol.values()))
            if len(unique_traders) == 1:
                trader_display = unique_traders[0]
            elif len(unique_traders) <= 3:
                trader_display = ', '.join(unique_traders)
            else:
                trader_display = f'{", ".join(unique_traders[:3])}... (+{len(unique_traders)-3})'
    elif 'copy_trade' in analysis.state:
        # Single symbol copy trade
        trader_display = analysis.state['copy_trade'].get('trader_name', '-')
```

#### Added to Data Dictionary
```python
analysis_data.append({
    'id': analysis.id,
    'symbol': symbol_display,
    'trader': trader_display,  # NEW
    'expert_name': expert_name,
    # ... rest of fields
})
```

## Display Logic

### Single Symbol Analysis
- Displays the trader name directly from `state['copy_trade']['trader_name']`
- Example: "Nancy Pelosi"

### Multi-Symbol Analysis
- **One Trader**: Displays single trader name
  - Example: "Nancy Pelosi"
- **2-3 Traders**: Displays comma-separated list
  - Example: "Nancy Pelosi, Paul Pelosi"
- **More than 3 Traders**: Displays first 3 plus count
  - Example: "Nancy Pelosi, Paul Pelosi, Josh Gottheimer... (+5)"

### Non-Copy Trade Experts
- Displays "-" for experts that don't have trader information

## Benefits of State-Based Approach

1. **No Database Migration**: Avoids adding a new column to the database schema
2. **Flexibility**: `state` field can store any expert-specific metadata without schema changes
3. **Backward Compatibility**: Existing records without state data simply show "-"
4. **JSON Storage**: Easy to add more trader-related metadata in the future (e.g., party affiliation, state, etc.)
5. **Expert-Specific**: Each expert can store different state information without affecting others

## Testing

To verify the feature works:

1. **Run Manual Analysis** for Expert 8 (FMPSenateTraderCopy) with EXPERT instrument selection
2. **Check the Market Analysis page** - the "Trader" column should appear between "Symbol" and "Expert"
3. **Verify Trader Names** appear for completed copy trade analyses:
   - Single symbol: Should show trader name (e.g., "Nancy Pelosi")
   - Multiple symbols: Should show trader names (e.g., "Nancy Pelosi, Paul Pelosi" or "Nancy Pelosi, Paul Pelosi... (+3)")
4. **Non-Copy Experts**: Should show "-" in the Trader column

## Future Enhancements

Possible future additions to the state data:
- Political party affiliation
- State representation
- Committee memberships
- Trade execution vs disclosure lag
- Historical trading performance

These can all be added to the `state` dictionary without requiring database migrations.

## Technical Notes

- Trader name is extracted from the most recent trade for each symbol
- Format: `"{firstName} {lastName}"` from the FMP API response
- Stored in `MarketAnalysis.state` as a JSON dictionary
- UI reads from state and handles missing data gracefully (displays "-")
- No changes to `ExpertRecommendation` model or database schema
