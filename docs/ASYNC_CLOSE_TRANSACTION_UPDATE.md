# Async Close Transaction Update

## Summary
Updated the close transaction process to run asynchronously with automatic refresh steps to prevent UI blocking and ensure data is synced with the broker.

## Key Changes

### 1. New Async Method in AccountInterface

**File**: `ba2_trade_platform/core/AccountInterface.py`

**Added**: `async def close_transaction_async(transaction_id: int) -> dict`

This new async wrapper:
1. Runs the synchronous `close_transaction()` in a thread pool executor
2. After successful close, refreshes orders from broker (`refresh_orders()`)
3. Then refreshes transactions to update status (`refresh_transactions()`)
4. Returns the same result dict as the sync version

**Benefits**:
- ✅ Prevents UI blocking during broker API calls
- ✅ Automatically syncs order status from broker after close
- ✅ Updates transaction status based on latest order states
- ✅ Better user experience with responsive UI

---

### 2. Updated UI Close Logic

**File**: `ba2_trade_platform/ui/pages/overview.py`

**Updated Methods**:
- `_close_position()` - Initial close position
- `_retry_close_position()` - Retry close for CLOSING transactions

**New Flow**:

#### Before (Synchronous)
```python
# Blocking UI during close
result = account.close_transaction(transaction_id)

if result['success']:
    ui.notify(result['message'], type='positive')
else:
    ui.notify(result['message'], type='negative')

dialog.close()
ui.timer(0.3, self._refresh_transactions, once=True)
```

**Issues**:
- ❌ UI freezes during broker API calls
- ❌ Manual refresh with timer delay
- ❌ No guarantee orders are synced with broker

#### After (Asynchronous)
```python
async def close_async():
    try:
        # Non-blocking async call with auto-refresh
        result = await account.close_transaction_async(transaction_id)
        
        if result['success']:
            ui.notify(result['message'], type='positive')
        else:
            ui.notify(result['message'], type='negative')
        
        # Refresh UI after all operations complete
        self._refresh_transactions()
    except Exception as e:
        ui.notify(f'Error during close: {str(e)}', type='negative')
        logger.error(f"Error in close_async: {e}", exc_info=True)

# Run in background
asyncio.create_task(close_async())

dialog.close()
ui.notify('Closing transaction...', type='info')  # Immediate feedback
```

**Benefits**:
- ✅ UI remains responsive
- ✅ Immediate feedback to user
- ✅ Automatic refresh after all operations complete
- ✅ Orders synced with broker before UI refresh
- ✅ Better error handling

---

## Complete Async Flow

### Step-by-Step Process

1. **User Action**: Clicks "Close Position" or "Retry Close"

2. **UI Response**: 
   - Shows immediate feedback: "Closing transaction..."
   - Closes dialog
   - Starts async task in background

3. **Async Task**:
   - **Step 1**: Execute close logic
     - Cancel unfilled orders
     - Delete WAITING_TRIGGER orders
     - Check for existing close order
     - Create/retry close order as needed
   
   - **Step 2**: Refresh orders from broker
     - Get latest order statuses from broker API
     - Update database with current states
   
   - **Step 3**: Refresh transactions
     - Update transaction status based on order states
     - Set OPENED/CLOSED/CLOSING appropriately
   
   - **Step 4**: Refresh UI
     - Update transactions table
     - Show final result notification

4. **Final Result**: User sees updated transaction status with broker-synced data

---

## Technical Details

### Async Method Signature

```python
async def close_transaction_async(self, transaction_id: int) -> dict:
    """
    Close a transaction asynchronously by:
    1. For unfilled orders: Cancel them at broker and delete WAITING_TRIGGER orders from DB
    2. For filled positions: Check if there's already a pending close order
       - If close order exists and is in ERROR state: Retry submitting it
       - If close order exists and is not in ERROR: Do nothing (log it)
       - If no close order exists: Create and submit a new closing order
    3. Refresh orders from broker
    4. Refresh transactions to update status
    
    Returns:
        dict: {
            'success': bool,
            'message': str,
            'canceled_count': int,
            'deleted_count': int,
            'close_order_id': int or None
        }
    """
```

### Thread Pool Execution

Uses `asyncio.get_event_loop().run_in_executor(None, ...)` to run blocking operations:
- `close_transaction()` - Main close logic
- `refresh_orders()` - Broker API call
- `refresh_transactions()` - Database update

This prevents blocking the async event loop while still using synchronous code.

---

## Benefits Summary

### User Experience
- ✅ **Responsive UI** - No freezing during close operations
- ✅ **Immediate Feedback** - "Closing transaction..." notification
- ✅ **Accurate Data** - Synced with broker before display
- ✅ **Clear Status** - Final notification after all operations complete

### Code Quality
- ✅ **Non-blocking** - Uses async/await pattern
- ✅ **Automatic Refresh** - No manual timer management
- ✅ **Error Handling** - Try/except in async wrapper
- ✅ **Logging** - Comprehensive logging at each step

### Data Integrity
- ✅ **Broker Sync** - Orders refreshed from broker API
- ✅ **Status Accuracy** - Transaction status based on real broker data
- ✅ **Atomic Updates** - All steps complete before UI refresh

---

## Testing Checklist

- [ ] Close position with unfilled orders - UI remains responsive
- [ ] Close position with filled orders - creates close order, refreshes, updates UI
- [ ] Retry close with ERROR order - resubmits, refreshes, updates UI
- [ ] Retry close with PENDING order - skips retry, refreshes, updates UI
- [ ] Multiple rapid closes - each runs independently without blocking
- [ ] Network delay - UI doesn't freeze, shows "Closing..." feedback
- [ ] Error during close - shows error notification, doesn't crash
- [ ] Transaction status - correctly updates to CLOSING → CLOSED after refresh

---

## Migration Notes

### Breaking Changes
None - the synchronous `close_transaction()` method is still available.

### New Dependencies
- Uses `asyncio` standard library (already available)
- No additional package dependencies

### Backward Compatibility
- ✅ Sync method (`close_transaction`) still works
- ✅ Can be called from sync or async contexts
- ✅ All existing code continues to work

---

## Future Enhancements

### Potential Improvements

1. **Progress Indicators**
   - Show progress bar during close operation
   - Display current step (closing, refreshing orders, updating status)

2. **Batch Close**
   - Close multiple transactions asynchronously in parallel
   - Show overall progress for batch operations

3. **Retry Logic**
   - Auto-retry on network errors with exponential backoff
   - Configurable retry attempts and delays

4. **Notifications**
   - Desktop notification when close operation completes
   - Email/SMS notification for large positions

5. **Order Monitoring**
   - Background task to monitor close orders
   - Auto-retry if close order stuck in PENDING too long

---

## Code Locations

### Files Modified

1. **ba2_trade_platform/core/AccountInterface.py**
   - **Added**: `close_transaction_async()` method (lines ~844-878)
   - **Unchanged**: `close_transaction()` method (lines ~879-1032)

2. **ba2_trade_platform/ui/pages/overview.py**
   - **Modified**: `_close_position()` (lines ~2300-2370)
     - Now calls `close_transaction_async()` with `asyncio.create_task()`
   - **Modified**: `_retry_close_position()` (lines ~2220-2260)
     - Now calls `close_transaction_async()` with `asyncio.create_task()`

---

## Logging Examples

### Initial Close (Async)
```
INFO - Closing transaction 123
INFO - Set transaction 123 status to CLOSING
INFO - Canceled unfilled order 456 (broker: ABC123)
INFO - Creating new closing order for transaction 123
INFO - Close transaction 123: Closing order submitted for AAPL (1 orders canceled)
INFO - Refreshing orders from broker after close transaction 123
INFO - Refreshing transactions after close transaction 123
INFO - Updated transaction 123 status: CLOSING -> CLOSED
```

### Retry Close (Async)
```
INFO - Retrying close for transaction 123
INFO - Found existing closing order 789 with status ERROR
INFO - Retrying close order 789 which is in ERROR state
INFO - Retry close transaction 123: Retried close order for TSLA
INFO - Refreshing orders from broker after close transaction 123
INFO - Refreshing transactions after close transaction 123
INFO - Updated transaction 123 status: CLOSING -> CLOSED
```

### User Feedback Flow
```
UI: "Closing transaction..." (immediate)
   ↓
[Async operations running in background]
   ↓
UI: "Closing order submitted for AAPL (1 orders canceled)" (after completion)
   ↓
[Transaction table automatically refreshes with updated status]
```

---

## Summary

The async update successfully:
- ✅ Prevents UI blocking during close operations
- ✅ Automatically syncs orders with broker
- ✅ Refreshes transaction status based on real data
- ✅ Provides immediate user feedback
- ✅ Maintains backward compatibility
- ✅ Improves overall user experience
- ✅ Ensures data integrity with broker
- ✅ Better error handling and logging
