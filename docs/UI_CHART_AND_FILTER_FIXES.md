# UI Chart and Filter Fixes

## Overview
Fixed three UI display issues reported by user:
1. Balance Usage Per Expert chart showing $0 on bar tops
2. Position Distribution pie charts not using full card width
3. Transactions page expert dropdown not populating with expert list

## Changes Made

### 1. Balance Usage Per Expert Chart - Text Display Fix
**File**: `ba2_trade_platform/ui/components/BalanceUsagePerExpertChart.py`

**Problem**: 
- The stacked bar chart showed "$0" text on top of bars
- Hover tooltip displayed correct total values
- Root cause: The label formatter was using `{c}` which only showed the current series value (pending orders), not the total

**Solution**:
- Modified data structure to include total values per expert
- Changed pending orders data from simple array to array of objects with `value` and `total` properties
- Updated label formatter to use `{@total}` to display sum of pending + filled orders
- Applied same fix to the `refresh()` method

**Code Changes**:
```python
# Calculate total values per expert for top label display
total_per_expert = [round(pending_values[i] + filled_values[i], 2) for i in range(len(expert_names))]

# Updated Pending Orders series data structure
{
    'name': 'Pending Orders',
    'type': 'bar',
    'stack': 'total',
    'data': [
        {'value': round(pending_values[i], 2), 'total': total_per_expert[i]}
        for i in range(len(pending_values))
    ],
    'label': {
        'show': True,
        'position': 'top',
        'formatter': '${@total}',  # Changed from '${c}' to '${@total}'
        'fontSize': 10
    }
}
```

### 2. Position Distribution Pie Chart - Width Fix
**File**: `ba2_trade_platform/ui/components/InstrumentDistributionChart.py`

**Problem**: 
- Pie charts (by label and by sector) were not using full width of their cards
- Charts appeared narrower than expected

**Solution**:
- Changed chart styling from CSS classes to inline style
- Removed fixed height class `h-96` 
- Added explicit inline style with `height: 400px; min-height: 400px;`
- Kept `w-full` class for responsive width

**Code Changes**:
```python
# Before:
self.chart = ui.echart(options).classes('w-full h-96')

# After:
self.chart = ui.echart(options).classes('w-full').style('height: 400px; min-height: 400px;')
```

### 3. Transactions Expert Dropdown - Population Fix
**File**: `ba2_trade_platform/ui/pages/overview.py`

**Problem**: 
- Expert dropdown in transactions filter only showed "All", not individual experts
- Expert list population logic was inside `_get_transactions_data()`, which built table rows
- Dropdown wasn't populated during initial render

**Solution**:
- Created dedicated `_populate_expert_filter()` method to load all expert instances
- Called this method during initial render in `render()`
- Also call it in `_refresh_transactions()` to catch newly added experts
- Removed duplicate expert population logic from `_get_transactions_data()`
- Added safety check for `expert_id_map` existence before filtering

**Code Changes**:

**New Method**:
```python
def _populate_expert_filter(self):
    """Populate the expert filter dropdown with all available experts."""
    from ...core.models import ExpertInstance
    
    session = get_db()
    try:
        # Get ALL expert instances
        expert_statement = select(ExpertInstance)
        experts = list(session.exec(expert_statement).all())
        
        # Build expert options list with shortnames
        expert_options = ['All']
        expert_map = {'All': 'All'}
        for expert in experts:
            # Create shortname: use alias, user_description, or fallback to "expert_name-id"
            shortname = expert.alias or expert.user_description or f"{expert.expert}-{expert.id}"
            expert_options.append(shortname)
            expert_map[shortname] = expert.id
        
        # Store the map for filtering
        self.expert_id_map = expert_map
        
        # Update expert filter options
        if hasattr(self, 'expert_filter'):
            current_value = self.expert_filter.value
            self.expert_filter.options = expert_options
            if current_value not in expert_options:
                self.expert_filter.value = 'All'
        
        logger.debug(f"[POPULATE] Populated expert filter with {len(expert_options)} options")
    
    except Exception as e:
        logger.error(f"Error populating expert filter: {e}", exc_info=True)
    finally:
        session.close()
```

**Updated render() method**:
```python
def render(self):
    # ... existing code ...
    
    # Populate expert filter options (NEW)
    self._populate_expert_filter()
    
    # Transactions table container
    self.transactions_container = ui.column().classes('w-full')
    self._render_transactions_table()
```

**Updated _refresh_transactions() method**:
```python
def _refresh_transactions(self):
    logger.debug("[REFRESH] _refresh_transactions() - Updating table rows")
    
    # Refresh expert filter options (in case new experts were added) (NEW)
    self._populate_expert_filter()
    
    # ... rest of existing code ...
```

**Updated _get_transactions_data() method**:
```python
# Removed duplicate expert population logic (40+ lines removed)
# Added safety check for expert_id_map existence:

# Apply expert filter
if hasattr(self, 'expert_filter') and self.expert_filter.value != 'All':
    # Ensure expert_id_map exists (NEW)
    if hasattr(self, 'expert_id_map'):
        expert_id = self.expert_id_map.get(self.expert_filter.value)
        if expert_id and expert_id != 'All':
            statement = statement.where(Transaction.expert_id == expert_id)
```

## Testing Recommendations

1. **Balance Usage Chart**:
   - Navigate to Overview page
   - Check that bar chart shows correct total amounts on top of bars
   - Verify that hover tooltip still works correctly
   - Test with multiple experts having different balance usages

2. **Pie Charts**:
   - Navigate to Overview page
   - Verify both "Position Distribution by Label" and "by Category" charts use full card width
   - Test on different screen sizes if possible
   - Verify charts remain responsive

3. **Transactions Expert Filter**:
   - Navigate to Transactions section (Overview page, scroll down)
   - Verify expert dropdown shows all expert instances
   - Test filtering by different experts
   - Verify "All" option shows all transactions
   - Test that newly created experts appear in the dropdown after refresh

## Related Files
- `ba2_trade_platform/ui/components/BalanceUsagePerExpertChart.py` - Balance usage chart
- `ba2_trade_platform/ui/components/InstrumentDistributionChart.py` - Pie charts
- `ba2_trade_platform/ui/pages/overview.py` - Transactions tab with filters

## Notes
- All fixes maintain backward compatibility
- No database changes required
- Changes are purely UI/display related
- Expert dropdown now properly separates concerns: population logic vs. filtering logic
