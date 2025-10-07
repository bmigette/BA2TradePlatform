# Balance Usage Per Expert Chart & Table Duplication Fix

## Summary
This update adds a new visualization widget and fixes a critical UI bug in the transactions table.

## 1. New Feature: Balance Usage Per Expert Chart

### Component Overview
**File**: `ba2_trade_platform/ui/components/BalanceUsagePerExpertChart.py`

A new stacked histogram chart that shows how much balance is allocated per expert, broken down by:
- **Filled Orders** (Green): Capital currently deployed in filled positions
- **Pending Orders** (Orange): Capital reserved for pending/unfilled orders

### Key Features
- **Stacked Bar Chart**: Each bar shows total balance usage, split into two colors
- **Smart Value Calculation**:
  - Uses `limit_price` or `stop_price` for unfilled orders
  - Uses `filled_avg_price` for executed orders
  - Handles partially filled orders (splits value between filled and pending)
- **Comprehensive Statistics**:
  - Total number of experts with active orders
  - Total filled balance across all experts
  - Total pending balance across all experts
  - Grand total balance usage
- **Sorted Display**: Experts sorted by total balance usage (highest to lowest)
- **Order Status Filtering**: Only counts active orders (excludes closed, canceled, rejected)

### Integration
- Added to `ba2_trade_platform/ui/components/__init__.py` exports
- Imported in `ba2_trade_platform/ui/pages/overview.py`
- Rendered in Row 2 of the dashboard (next to Profit Per Expert chart)
- Follows same design pattern as `ProfitPerExpertChart`

### Usage Example
The chart helps answer questions like:
- "Which expert is using the most capital?"
- "How much of my balance is tied up in pending orders vs. active positions?"
- "Is my capital allocation balanced across experts?"

### Technical Details
**Data Source**: `TradingOrder` table with expert attribution  
**Calculation Logic**:
```python
# For limit/stop orders
order_value = quantity × (limit_price OR stop_price)

# For filled orders
order_value = filled_qty × filled_avg_price

# For partially filled orders
filled_value = filled_qty × filled_avg_price
pending_value = remaining_qty × (limit_price OR stop_price)
```

**Color Scheme**:
- Filled Orders: `#4CAF50` (Green)
- Pending Orders: `#FF9800` (Orange)

## 2. Bug Fix: Transaction Table Duplication

### Problem
When closing a transaction, the transactions table would duplicate rows instead of properly refreshing. This happened because:
1. The table was being recreated entirely on each refresh
2. The container wasn't properly clearing before rebuilding
3. No reference to the table instance was maintained for updates

### Solution
Refactored the table refresh mechanism to use **row updates** instead of **table recreation**:

#### Changes to `ba2_trade_platform/ui/pages/overview.py`

**1. Added table instance variable** (Line ~1559):
```python
def __init__(self):
    self.transactions_container = None
    self.transactions_table = None  # NEW: Store table reference
    self.selected_transaction = None
    self.render()
```

**2. Extracted data fetching logic** (New method `_get_transactions_data()`):
- Moved all data querying and row building into separate method
- Returns list of row dictionaries
- Can be called independently for updates or initial rendering

**3. Updated refresh mechanism** (Line ~1615):
```python
def _refresh_transactions(self):
    """Refresh the transactions table."""
    logger.debug("[REFRESH] _refresh_transactions() - Updating table rows")
    
    # If table doesn't exist yet, create it
    if not self.transactions_table:
        logger.debug("[REFRESH] Table doesn't exist, creating new table")
        self.transactions_container.clear()
        with self.transactions_container:
            self._render_transactions_table()
        return
    
    # Otherwise, just update the rows data
    try:
        new_rows = self._get_transactions_data()
        logger.debug(f"[REFRESH] Updating table with {len(new_rows)} rows")
        
        # Update the table rows (NO recreation)
        self.transactions_table.rows.clear()
        self.transactions_table.rows.extend(new_rows)
        self.transactions_table.update()
        
        logger.debug("[REFRESH] _refresh_transactions() - Complete")
    except Exception as e:
        logger.error(f"Error refreshing transactions table: {e}", exc_info=True)
        # If update fails, fallback to recreate
        logger.debug("[REFRESH] Update failed, recreating table")
        self.transactions_container.clear()
        with self.transactions_container:
            self._render_transactions_table()
```

**4. Simplified table rendering**:
- Now stores table reference as `self.transactions_table` instead of local `table` variable
- Only creates table once on initial render
- Subsequent refreshes just update the rows

### Benefits
- ✅ **No more duplication**: Table rows properly replaced instead of duplicated
- ✅ **Better performance**: Updating rows is faster than recreating entire table
- ✅ **Maintains state**: Pagination, sorting, expansion state preserved during refresh
- ✅ **Robust error handling**: Fallback to recreation if row update fails

### Testing
To verify the fix:
1. Open the Overview page → Transactions tab
2. Close a transaction using the "Close Position" button
3. Observe that the table refreshes without duplicating rows
4. Check that transaction status updates correctly (OPENED → CLOSING → CLOSED)
5. Verify no duplicate entries appear in the table

## Files Modified

| File | Changes | Lines Added/Modified |
|------|---------|---------------------|
| `ba2_trade_platform/ui/components/BalanceUsagePerExpertChart.py` | **NEW FILE** | 265 lines (complete component) |
| `ba2_trade_platform/ui/components/__init__.py` | Added import and export | 2 lines |
| `ba2_trade_platform/ui/pages/overview.py` | Table refactoring + chart integration | ~50 lines modified, 200 lines refactored |

## Dashboard Layout Update

**Before**:
```
Row 1: [OpenAI Spending] [Analysis Jobs] [Order Stats] [Order Recs]
Row 2: [Profit Per Expert] [Position Dist (Labels)]
Row 3: [Position Dist (Categories)]
```

**After**:
```
Row 1: [OpenAI Spending] [Analysis Jobs] [Order Stats] [Order Recs]
Row 2: [Profit Per Expert] [Balance Usage Per Expert]  ← NEW
Row 3: [Position Dist (Labels)] [Position Dist (Categories)]
```

## Migration Notes
- **No database changes required**
- **No configuration changes required**
- **Backward compatible**: Existing functionality unchanged
- **Immediate effect**: Changes visible on next page load

## Future Enhancements

### Balance Usage Chart
- [ ] Add refresh button to update chart without page reload
- [ ] Add drill-down to see individual orders per expert
- [ ] Add time-series view to track balance usage over time
- [ ] Add alerts for over-allocation (expert using > X% of total balance)

### Table Refresh
- [ ] Add auto-refresh timer (optional)
- [ ] Add manual refresh button in UI
- [ ] Optimize query performance for large transaction counts
- [ ] Add loading indicator during data fetch

## Validation

All changes have been validated:
- ✅ No syntax errors in Python files
- ✅ Proper imports and exports
- ✅ NiceGUI table API usage correct
- ✅ Logging statements in place for debugging
- ✅ Error handling for edge cases
- ✅ Follows existing code patterns and conventions
