# Retry Close Transaction Feature

## Overview
Added a retry mechanism for transactions that get stuck in `CLOSING` status. This allows users to reset the transaction status and retry the close operation if it fails.

## Problem Solved
When closing a transaction, it transitions to `CLOSING` status to prevent duplicate close attempts. However, if the close operation fails (network error, API timeout, etc.), the transaction can get stuck in this status with no way to retry without manual database intervention.

## Solution
A **Retry Close** button replaces the disabled "closing in progress" indicator for transactions in `CLOSING` status, allowing users to:
1. Reset the transaction status back to `OPENED` or `WAITING`
2. Retry the close operation
3. Avoid manual database fixes

---

## User Interface Changes

### Before
```
Transaction in CLOSING status:
┌─────────────────────────────────────┐
│ Symbol  | Status   | Actions        │
├─────────────────────────────────────┤
│ AAPL    | CLOSING  | ⏳ (disabled)  │ ← User is stuck!
└─────────────────────────────────────┘
```

### After
```
Transaction in CLOSING status:
┌─────────────────────────────────────┐
│ Symbol  | Status   | Actions        │
├─────────────────────────────────────┤
│ AAPL    | CLOSING  | 🔄 Retry       │ ← User can retry!
└─────────────────────────────────────┘
```

---

## Technical Implementation

### 1. UI Button Change

**File**: `ba2_trade_platform/ui/pages/overview.py`

**Changed from** (disabled hourglass icon):
```html
<q-btn v-else-if="props.row.is_closing"
       icon="hourglass_empty"
       size="sm"
       flat
       round
       color="orange"
       disable
       title="Closing in progress..."
/>
```

**Changed to** (clickable refresh icon):
```html
<q-btn v-else-if="props.row.is_closing"
       icon="refresh"
       size="sm"
       flat
       round
       color="orange"
       @click="$parent.$emit('retry_close_transaction', props.row.id)"
       title="Retry Close (reset status and try again)"
/>
```

**Visual Difference**:
- **Icon**: ⏳ `hourglass_empty` → 🔄 `refresh`
- **State**: `disabled` → `clickable`
- **Action**: None → Opens retry dialog

---

### 2. Event Handler Registration

**File**: `ba2_trade_platform/ui/pages/overview.py` (line ~2041)

```python
# Added new event handler
self.transactions_table.on('retry_close_transaction', self._show_retry_close_dialog)
```

---

### 3. Retry Dialog Implementation

**Method**: `_show_retry_close_dialog(event_data)`

**Purpose**: Display confirmation dialog explaining what the retry will do

**Dialog Content**:
```
┌─────────────────────────────────────────────┐
│ ⚠️ Retry Close Transaction                  │
├─────────────────────────────────────────────┤
│ This transaction is stuck in CLOSING status.│
│ AAPL: +10.00 @ $150.00                      │
│                                             │
│ ─────────────────────────────────────────── │
│                                             │
│ This will:                                  │
│   1. Reset status back to OPENED/WAITING    │
│   2. Allow you to retry closing position    │
│   3. You can then click Close again         │
│                                             │
│ ⚠️ Use this if the close operation failed   │
│    or got stuck.                            │
│                                             │
│           [Cancel]  [Reset & Retry]         │
└─────────────────────────────────────────────┘
```

**Validation**:
- Checks transaction exists
- Verifies transaction status is `CLOSING`
- Shows warning if status is different

---

### 4. Retry Logic Implementation

**Method**: `_retry_close_position(transaction_id, dialog)`

**Purpose**: Reset transaction status to allow retry

**Logic Flow**:

```python
1. Get transaction from database
   ↓
2. Verify status is CLOSING
   ↓
3. Query all orders for transaction
   ↓
4. Check if any orders are filled
   ↓
5. Determine new status:
   - If has filled orders → OPENED
   - If no filled orders → WAITING
   ↓
6. Update transaction status
   ↓
7. Notify user & refresh table
```

**Status Decision Logic**:
```python
# Check if any orders are filled
has_filled = any(order.status in OrderStatus.get_executed_statuses() 
                 for order in orders)

# Reset status based on current state
if has_filled:
    new_status = TransactionStatus.OPENED  # Has position to close
else:
    new_status = TransactionStatus.WAITING  # No position yet
```

**Why this logic?**
- `OPENED`: Transaction has filled orders = open position exists
- `WAITING`: Transaction has no fills = still waiting for entry

---

## Usage Scenarios

### Scenario 1: Network Error During Close

**Problem**:
1. User clicks "Close Position"
2. Status changes to `CLOSING`
3. Network timeout occurs
4. Closing order never submitted
5. Transaction stuck in `CLOSING`

**Solution**:
1. User clicks **Retry Close** button (🔄)
2. Dialog confirms action
3. Status resets to `OPENED`
4. User clicks "Close Position" again
5. Close succeeds

---

### Scenario 2: Broker API Failure

**Problem**:
1. User closes position
2. Status → `CLOSING`
3. Broker API returns error
4. Transaction remains in `CLOSING`
5. Orders not canceled

**Solution**:
1. Click **Retry Close** button
2. Status resets to `OPENED`
3. User can try again when broker is available
4. Or manually manage orders if needed

---

### Scenario 3: Application Crash During Close

**Problem**:
1. Close operation started
2. Status → `CLOSING`
3. Application crashes
4. On restart, transaction stuck in `CLOSING`

**Solution**:
1. User sees **Retry Close** button on restart
2. Clicks to reset status
3. Can inspect orders and retry close

---

## Safety Features

### 1. **Status Validation**
```python
if txn.status != TransactionStatus.CLOSING:
    ui.notify('Transaction is not in CLOSING status', type='warning')
    return
```
- Only allows retry if status is actually `CLOSING`
- Prevents accidental status changes

### 2. **Smart Status Reset**
```python
has_filled = any(order.status in OrderStatus.get_executed_statuses() 
                 for order in orders)

if has_filled:
    new_status = TransactionStatus.OPENED  # Has position
else:
    new_status = TransactionStatus.WAITING  # No position
```
- Automatically determines correct status
- Based on actual order state
- Prevents invalid transitions

### 3. **User Confirmation**
- Requires explicit dialog confirmation
- Explains what will happen
- Shows transaction details

### 4. **Comprehensive Logging**
```python
logger.info(f"Reset transaction {transaction_id} from CLOSING to {new_status.value} for retry")
```
- Logs all retry attempts
- Tracks status changes
- Aids debugging

### 5. **Error Handling**
```python
try:
    # Retry logic
except Exception as e:
    ui.notify(f'Error: {str(e)}', type='negative')
    logger.error(f"Error retrying close position: {e}", exc_info=True)
```
- Catches all exceptions
- Shows user-friendly error
- Logs full stack trace

---

## Code Locations

### Files Modified

1. **ba2_trade_platform/ui/pages/overview.py**
   - **Line ~1957**: Updated button template (hourglass → refresh icon)
   - **Line ~2041**: Added event handler registration
   - **Line ~2154**: Added `_show_retry_close_dialog()` method
   - **Line ~2196**: Added `_retry_close_position()` method

---

## Testing Checklist

- [x] Button appears for CLOSING transactions
- [x] Button has correct icon (refresh)
- [x] Button shows tooltip on hover
- [x] Click opens retry dialog
- [x] Dialog shows transaction details
- [x] Dialog explains retry action
- [x] Cancel button closes dialog
- [x] Reset button resets status
- [x] Status resets to OPENED (if filled)
- [x] Status resets to WAITING (if not filled)
- [x] Success notification shows
- [x] Table refreshes after reset
- [x] User can close transaction after retry
- [x] Error handling works
- [x] Logging captures retry events

---

## Benefits

### For Users
✅ **No more stuck transactions** - Easy recovery from failed closes
✅ **Self-service** - No need to contact support or edit database
✅ **Clear feedback** - Understand why transaction is stuck
✅ **Safe operation** - Confirmation required before reset

### For Developers
✅ **Reduced support tickets** - Users can fix themselves
✅ **Better debugging** - Retry events are logged
✅ **Robust error handling** - Graceful failure recovery
✅ **Code reuse** - Leverages existing close logic

---

## Future Enhancements

### Potential Improvements

1. **Auto-Retry on Failure**
   - Detect failed close attempts automatically
   - Show retry suggestion notification
   - One-click retry from notification

2. **Retry with Different Account**
   - Allow switching broker if one is down
   - Submit close order to backup account

3. **Batch Retry**
   - Retry multiple stuck transactions at once
   - Useful after system-wide outage

4. **Status Health Check**
   - Background job to detect stuck CLOSING transactions
   - Automatic notification to users
   - Dashboard showing retry-needed transactions

5. **Close History**
   - Track all close attempts
   - Show failure reasons
   - Help identify recurring issues

---

## Examples

### Example 1: Simple Retry Flow

```python
# Initial state
Transaction ID: 123
Status: CLOSING
Symbol: AAPL
Quantity: +10.00

# User action: Click Retry Close button

# System action:
1. Show retry dialog
2. User confirms
3. Check orders → has filled entry order
4. Reset status → OPENED
5. Notify: "Transaction reset to OPENED. You can now try closing again."

# User action: Click Close Position again

# System action:
1. Status → CLOSING
2. Cancel unfilled orders
3. Submit closing order
4. Status → CLOSED (when filled)
```

---

### Example 2: Retry After Network Error

```python
# Scenario: Network timeout during close

# Before retry:
Status: CLOSING (stuck for 5 minutes)
Orders:
  - BUY 10 AAPL @ $150.00 (FILLED)
  - SELL 10 AAPL @ MARKET (PENDING - network error, never submitted)

# After retry:
Status: OPENED
Orders:
  - BUY 10 AAPL @ $150.00 (FILLED)
  # Pending order was never created, so just reset status

# User tries close again:
Status: CLOSING → CLOSED
Orders:
  - BUY 10 AAPL @ $150.00 (FILLED)
  - SELL 10 AAPL @ $151.50 (FILLED) ← Successfully created this time
```

---

## Summary

The **Retry Close Transaction** feature provides a safe, user-friendly way to recover from failed close operations. By replacing the disabled "closing in progress" indicator with an actionable retry button, users can self-service stuck transactions without requiring database access or developer intervention.

**Key Points**:
- ✅ Simple one-click retry
- ✅ Smart status detection
- ✅ Safe confirmation dialog
- ✅ Comprehensive error handling
- ✅ Full audit logging
- ✅ No data loss risk
