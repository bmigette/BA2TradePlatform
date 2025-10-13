# Transactions Expert Dropdown Update Fix

## Issue
The expert dropdown in the transactions page only shows "All" and does not populate with the list of expert instances.

## Root Cause
When dynamically updating NiceGUI select component options after the component is created, the component may not automatically refresh its display. The code was correctly:
1. Fetching all expert instances from the database
2. Building the expert options list
3. Setting `self.expert_filter.options = expert_options`

However, NiceGUI select components require an explicit `.update()` call to refresh the UI when options are changed programmatically after initial render.

## Solution
Added `.update()` call after setting the options to force the select component to refresh its display.

**File**: `ba2_trade_platform/ui/pages/overview.py`

**Change** (in `_populate_expert_filter()` method, line ~2249):
```python
# BEFORE:
# Update expert filter options
if hasattr(self, 'expert_filter'):
    current_value = self.expert_filter.value
    self.expert_filter.options = expert_options
    # Reset to 'All' if current value is not in the new options
    if current_value not in expert_options:
        self.expert_filter.value = 'All'

logger.debug(f"[POPULATE] Populated expert filter with {len(expert_options)} options")

# AFTER:
# Update expert filter options
if hasattr(self, 'expert_filter'):
    current_value = self.expert_filter.value
    self.expert_filter.options = expert_options
    # Reset to 'All' if current value is not in the new options
    if current_value not in expert_options:
        self.expert_filter.value = 'All'
    # Force update of the select component
    self.expert_filter.update()

logger.debug(f"[POPULATE] Populated expert filter with {len(expert_options)} options")
```

## How It Works

### Initial Render Flow
1. `TransactionsTab.render()` is called
2. Expert filter select is created with `options=['All']`
3. `_populate_expert_filter()` is called immediately after
4. Expert instances are fetched from database
5. Options list is built with expert shortnames
6. Options are set: `self.expert_filter.options = expert_options`
7. **NEW**: `self.expert_filter.update()` forces UI refresh

### Refresh Flow
1. User clicks refresh button or filter changes
2. `_refresh_transactions()` is called
3. `_populate_expert_filter()` is called to catch new experts
4. Expert list is rebuilt and options are updated
5. **NEW**: `self.expert_filter.update()` forces UI refresh

## Expected Behavior After Fix
- Dropdown should show "All" plus all expert instances
- Expert names shown as: alias, user_description, or "ExpertName-ID"
- Example: "All", "TradingAgents-1", "FMPSenateTraderCopy-2", etc.
- Selecting an expert filters transactions to show only that expert's transactions
- Selecting "All" shows transactions from all experts

## Testing
1. Navigate to Overview page, scroll to Transactions section
2. Click on the Expert dropdown
3. Verify it shows "All" plus a list of all expert instances
4. Select a specific expert and verify transactions are filtered
5. Create a new expert instance
6. Click the Refresh button in the transactions section
7. Verify the new expert appears in the dropdown

## Related Code

### Expert Shortname Generation
```python
shortname = expert.alias or expert.user_description or f"{expert.expert}-{expert.id}"
```

Priority order:
1. Expert alias (if set in expert settings)
2. User description (if set)
3. Fallback: "ExpertClassName-ID" format

### Expert ID Mapping
The `expert_id_map` dictionary maps shortnames to expert IDs for filtering:
```python
expert_map = {'All': 'All'}
for expert in experts:
    shortname = expert.alias or expert.user_description or f"{expert.expert}-{expert.id}"
    expert_options.append(shortname)
    expert_map[shortname] = expert.id

self.expert_id_map = expert_map
```

### Filter Application
In `_get_transactions_data()`:
```python
# Apply expert filter
if hasattr(self, 'expert_filter') and self.expert_filter.value != 'All':
    # Ensure expert_id_map exists
    if hasattr(self, 'expert_id_map'):
        expert_id = self.expert_id_map.get(self.expert_filter.value)
        if expert_id and expert_id != 'All':
            statement = statement.where(Transaction.expert_id == expert_id)
```

## Related Issues Fixed Previously
1. **Initial Implementation** (docs/UI_CHART_AND_FILTER_FIXES.md): Created `_populate_expert_filter()` method and called it during render
2. **This Fix**: Added `.update()` call to force NiceGUI component refresh

## NiceGUI Component Update Pattern
This fix follows the NiceGUI pattern for dynamic option updates:
```python
# Pattern for updating select options dynamically
select_component.options = new_options  # Update the data
select_component.value = new_value      # Update the selected value (optional)
select_component.update()               # Force UI refresh (REQUIRED for dynamic updates)
```

## Notes
- The `.update()` method is part of NiceGUI's reactive system
- It triggers the component to re-render with the new options
- This is required when options are changed after initial component creation
- The same pattern applies to other NiceGUI components (tables, charts, etc.)
