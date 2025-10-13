# Market Analysis Table - Remove Trader Column

## Change Summary
Removed the "Trader" column from the Market Analysis Job Monitoring table. Trader information is now only displayed in the Market Analysis detail page, where it is rendered by the expert itself.

## Rationale
- The trader name is expert-specific information that belongs in the detailed analysis view
- Simplifies the main monitoring table by removing a column that's only relevant for certain expert types
- Trader information is better presented in context within the expert's detailed analysis rendering
- Reduces table width and improves readability of the job monitoring view

## Changes Made

### File: `ba2_trade_platform/ui/pages/marketanalysis.py`

**1. Removed 'trader' column from table definition** (line ~108):
```python
# BEFORE:
columns = [
    {'name': 'id', 'label': 'ID', 'field': 'id', 'sortable': True, 'style': 'width: 80px'},
    {'name': 'symbol', 'label': 'Symbol', 'field': 'symbol', 'sortable': True, 'style': 'width: 100px'},
    {'name': 'trader', 'label': 'Trader', 'field': 'trader', 'sortable': True, 'style': 'width: 150px'},  # REMOVED
    {'name': 'expert', 'label': 'Expert', 'field': 'expert_name', 'sortable': True, 'style': 'width: 150px'},
    ...
]

# AFTER:
columns = [
    {'name': 'id', 'label': 'ID', 'field': 'id', 'sortable': True, 'style': 'width: 80px'},
    {'name': 'symbol', 'label': 'Symbol', 'field': 'symbol', 'sortable': True, 'style': 'width: 100px'},
    {'name': 'expert', 'label': 'Expert', 'field': 'expert_name', 'sortable': True, 'style': 'width: 150px'},
    ...
]
```

**2. Removed trader display logic** (lines ~260-277):
```python
# REMOVED entire section:
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

**3. Removed 'trader' field from row data** (line ~340):
```python
# BEFORE:
analysis_data.append({
    'id': analysis.id,
    'symbol': symbol_display,
    'trader': trader_display,  # REMOVED
    'expert_name': expert_name,
    ...
})

# AFTER:
analysis_data.append({
    'id': analysis.id,
    'symbol': symbol_display,
    'expert_name': expert_name,
    ...
})
```

## Impact

### What Changed
- Market Analysis Job Monitoring table now has one fewer column
- Table is slightly narrower and easier to scan
- Trader information is no longer visible in the list view

### What Stayed the Same
- Trader information is still available in the Market Analysis detail page
- FMPSenateTraderCopy expert still stores trader names in analysis state
- Expert detail rendering still displays trader information via `_render_multi_symbol_completed()` and `_render_single_symbol_completed()` methods
- All other table columns remain unchanged

### Where Trader Info is Still Shown
The trader information is displayed in the Market Analysis detail page within the expert's rendering:

**For Multi-Symbol Analysis** (`FMPSenateTraderCopy._render_multi_symbol_completed()`):
- Shows trader names in the "Recommendations Generated" section per symbol
- Displays followed traders in the "Followed Traders" section

**For Single-Symbol Analysis** (`FMPSenateTraderCopy._render_single_symbol_completed()`):
- Shows trader name in individual trade cards
- Displays in the "Copy Trades for {symbol}" section

## Testing
1. Navigate to Market Analysis page
2. Verify the table no longer has a "Trader" column
3. Click "View Details" on a SenateCopy analysis job
4. Verify trader information is still displayed in the detail view rendered by the expert

## Related Files
- `ba2_trade_platform/ui/pages/marketanalysis.py` - Main monitoring table
- `ba2_trade_platform/modules/experts/FMPSenateTraderCopy.py` - Expert rendering (unchanged)
- `docs/SENATE_COPY_TRADER_NAME_FEATURE.md` - Original trader name feature
- `docs/SENATE_COPY_TRADER_NAME_STATE_FIX.md` - State storage fix

## Notes
- This change maintains clean separation of concerns: the monitoring table shows job status and basic metrics, while detailed expert-specific information is shown in the detail view
- The trader name state storage remains intact and functional
- Future experts can continue to use the state field to store their own custom data for rendering in detail views
