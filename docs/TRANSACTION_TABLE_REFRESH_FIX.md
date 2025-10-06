# Transaction Table Refresh Fix

**Date**: October 6, 2025

## Issue

When clicking the "Close Position" button for a transaction, the transaction table was not refreshing to show the updated status. Instead, the entire page was reloading via `ui.navigate.reload()`, which was inefficient and disruptive to the user experience.

## Root Cause

In the `_close_position` method of `TransactionsTab`, after successfully closing a position, the code was calling:

```python
def reload_page():
    ui.navigate.reload()
ui.timer(0.3, reload_page, once=True)
```

This reloaded the entire page instead of just refreshing the transactions table.

## Solution

Changed the reload logic to call the existing `_refresh_transactions()` method instead:

**File**: `ba2_trade_platform/ui/pages/overview.py`

### Before:
```python
ui.notify(msg, type='positive')
dialog.close()
# Reload page to refresh transactions after a short delay
def reload_page():
    ui.navigate.reload()
ui.timer(0.3, reload_page, once=True)
```

### After:
```python
ui.notify(msg, type='positive')
dialog.close()
# Refresh transactions table after a short delay
ui.timer(0.3, self._refresh_transactions, once=True)
```

This change was applied in **two locations** within the `_close_position` method:
1. After successfully submitting a closing order (line ~2236)
2. After cleanup when no filled position exists (line ~2248)

## Impact

**Before**:
- ❌ Entire page reloaded (inefficient)
- ❌ User lost scroll position
- ❌ All UI state reset
- ❌ Slower user experience

**After**:
- ✅ Only transactions table refreshes
- ✅ Scroll position maintained
- ✅ Filter settings preserved
- ✅ Faster, smoother user experience
- ✅ Consistent with TP/SL update behavior

## Related Code

The `_update_tp_sl` method was already correctly refreshing the table:
```python
dialog.close()
self._refresh_transactions()  # ✅ Already correct
```

Now both methods use the same refresh pattern for consistency.

## Testing

1. Open a transaction (ensure it has status OPENED)
2. Click the "Close Position" button
3. Confirm the close operation
4. Verify:
   - Transaction table refreshes automatically
   - Transaction status updates to CLOSING or CLOSED
   - Scroll position is maintained
   - Filter selections are preserved
   - No full page reload occurs

---

## Technical Details

The `_refresh_transactions()` method:
```python
def _refresh_transactions(self):
    """Refresh the transactions table."""
    self.transactions_container.clear()
    with self.transactions_container:
        self._render_transactions_table()
```

This efficiently:
1. Clears the existing table
2. Re-queries the database for fresh transaction data
3. Re-renders only the table component
4. Preserves all filter and pagination settings
