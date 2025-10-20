# Transaction Table Selection Fix

## Problem
The transaction table was attempting to use Quasar's native `selection='multiple'` mode, which automatically adds a select column to the table. This caused layout issues:
- Select checkbox column header appeared, shifting all row content
- Rows were misaligned due to the automatic column insertion
- The implementation was conflicting with custom Vue slots

## Solution
Implemented custom row selection using a user-controlled select checkbox column with Vue template:

### Changes Made

#### 1. Added Select Column to Columns Definition
**File**: `ba2_trade_platform/ui/pages/overview.py` (line 3192)

Added a new select column as the first column:
```python
{'name': 'select', 'label': '', 'field': 'select', 'align': 'left', 'sortable': False}
```

#### 2. Removed Native Quasar Selection
**File**: `ba2_trade_platform/ui/pages/overview.py` (lines 3216-3235)

- Removed `selection='multiple'` parameter from table creation
- Removed `self.transactions_table.selected` property assignment
- Removed `_sync_table_selection()` method (no longer needed)
- Changed to use only `.props('flat bordered')`

#### 3. Added Custom Select Checkbox Vue Template
**File**: `ba2_trade_platform/ui/pages/overview.py` (body slot)

Added Vue template for the select column:
```vue
<template v-if="col.name === 'select'">
    <q-checkbox 
        :model-value="props.row._selected || false"
        @update:model-value="(val) => $parent.$emit('toggle_row_selection', props.row.id)"
    />
</template>
```

The checkbox emits a `toggle_row_selection` event when clicked.

#### 4. Added Selection State to Row Data
**File**: `ba2_trade_platform/ui/pages/overview.py` (in `_get_transactions_data()`)

Added `_selected` flag to track selection state:
```python
row = {
    'id': txn.id,
    '_selected': txn.id in self.selected_transactions,  # Track selection state for checkbox
    # ... rest of row data
}
```

#### 5. Implemented Event Handler for Selection Toggles
**File**: `ba2_trade_platform/ui/pages/overview.py` (lines ~3402)

Added `_toggle_row_selection()` method:
```python
def _toggle_row_selection(self, transaction_id):
    """Toggle selection state for a transaction row."""
    if transaction_id in self.selected_transactions:
        del self.selected_transactions[transaction_id]
    else:
        self.selected_transactions[transaction_id] = True
    self._update_batch_buttons()
    # Update table row data to show checkbox state
    self.transactions_table.update()
```

Added event handler registration:
```python
self.transactions_table.on('toggle_row_selection', self._toggle_row_selection)
```

#### 6. Updated Selection Methods
**File**: `ba2_trade_platform/ui/pages/overview.py` (lines 3860-3880)

Updated `_select_all_transactions()` and `_clear_selected_transactions()` to:
- Directly manipulate `self.selected_transactions` dictionary
- Call `self.transactions_table.update()` to refresh checkboxes
- Remove reliance on `_sync_table_selection()`

#### 7. Removed Unnecessary Sync Calls
**File**: `ba2_trade_platform/ui/pages/overview.py` (lines 3895, 3990)

Removed calls to `_sync_table_selection()` in:
- `_batch_close_transactions()`
- `_batch_adjust_tp_dialog()`

These are no longer needed since selection is continuously tracked in `self.selected_transactions`.

## Architecture

### Selection Tracking
- **Data Structure**: `self.selected_transactions` - Dictionary mapping transaction IDs to `True`
- **Row State**: `_selected` field in row data indicates current checkbox state
- **UI Update**: Table `.update()` refreshes row data, updating checkbox display

### Event Flow
1. User clicks checkbox in select column
2. Vue checkbox emits `toggle_row_selection` event with transaction ID
3. `_toggle_row_selection()` toggles the selection state
4. `_update_batch_buttons()` enables/disables batch buttons
5. `self.transactions_table.update()` refreshes table to show new checkbox state

## Benefits
✅ No layout shifting - select column is under our full control
✅ Clean implementation - uses Vue template for checkbox
✅ Consistent with NiceGUI patterns - leverages event system
✅ Proper state management - `_selected` field integrated with row data
✅ Better performance - no event handler thrashing
✅ Simpler code - removed Quasar native selection complexity

## Compatibility
- **NiceGUI**: Works with current version using Quasar table
- **Vue**: Standard checkbox and event binding
- **Quasar**: Uses only standard table props and slots

## Testing Checklist
- [x] Syntax validation with py_compile
- [ ] Select/deselect individual rows
- [ ] Select all transactions
- [ ] Clear all selections
- [ ] Batch operations work with selection
- [ ] Checkboxes visually update on click
- [ ] Table layout remains stable during selection changes
