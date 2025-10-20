# Batch Transactions Operations

## Overview

Added batch operations for transactions in the transactions page, allowing users to select multiple transactions and perform bulk actions: batch close and batch adjust take profit (TP). All operations are fully asynchronous and non-blocking to the UI.

## Features

### 1. Multi-Select Transactions

**Checkbox Selection Column**
- Added a checkbox column to the transactions table for selection
- Each row can be individually selected or deselected
- Visual feedback shows selected transaction count

**Selection Tracking**
- Instance variable `selected_transactions` stores selected transaction IDs as dict
- Keys: transaction IDs, Values: checkbox state

### 2. Batch Close Operation

**Async Non-Blocking Execution**
- Runs in background using NiceGUI's `background_tasks`
- UI remains responsive during operation
- Uses context client's `safe_invoke` for UI updates from background

**Process**
1. User selects multiple transactions
2. Clicks "Batch Close" button
3. Confirmation dialog appears
4. On confirmation, operation runs asynchronously
5. Each transaction is closed using `account.close_transaction_async()`
6. UI automatically refreshes when all operations complete

**Error Handling**
- Individual transaction failures don't stop other closures
- Failed transaction IDs are tracked and reported
- Success/warning notifications show completion status

### 3. Batch Adjust Take Profit Operation

**Dialog Input**
- Allows entering TP percentage from open price
- Example: 5.0% means TP = Open Price × 1.05
- Input validation: 0.1% to 100.0%

**Smart TP Handling**
- **New TP Creation**: If no existing TP order:
  - Updates transaction's `take_profit` field
  - Stores calculated TP price
- **Existing TP Modification**: If TP order exists:
  - Uses Alpaca's `modify_order` function
  - Updates the actual trading order on Alpaca
  - Modifies limit_price on the existing order

**Process**
1. User selects multiple transactions
2. Clicks "Batch Adjust TP" button
3. Dialog prompts for TP percentage
4. On apply, operation runs asynchronously
5. For each transaction:
   - Searches for existing TP order
   - If found: modifies order via Alpaca API
   - If not found: updates transaction TP field
6. Detailed results shown (modified vs created)
7. UI automatically refreshes

**Async Non-Blocking Execution**
- Runs in background using NiceGUI's `background_tasks`
- UI remains responsive during TP adjustments
- Handles API calls to Alpaca without blocking

**Error Handling**
- Gracefully handles both creation and modification failures
- Failed operations tracked separately
- Detailed feedback: "X modified existing orders • Y created new TPs"

## Technical Implementation

### Core Methods

#### `_execute_batch_close(dialog)`
- Closes dialog
- Launches async background task for closing all selected transactions
- Shows progress notification
- Uses `account.close_transaction_async()` for each transaction

#### `_execute_batch_adjust_tp(tp_percent, dialog)`
- Closes dialog
- Launches async background task for TP adjustment
- Searches for existing TP orders using database query
- Two paths:
  - **Existing TP**: Uses `account.modify_order()` (Alpaca API)
  - **New TP**: Updates transaction model and database
- Shows detailed results with counts

### Async Pattern

All batch operations follow this pattern:

```python
def _execute_batch_operation(self, dialog):
    dialog.close()
    
    from nicegui import context, background_tasks
    client = context.client
    
    async def batch_operation_async():
        try:
            # Do work asynchronously
            results = await perform_operations()
            
            # Schedule UI update
            def show_result():
                ui.notify(results_message)
                self._refresh_transactions()
            
            client.safe_invoke(show_result)
        except Exception as e:
            def show_error():
                ui.notify(f'Error: {e}', type='negative')
            client.safe_invoke(show_error)
    
    background_tasks.create(batch_operation_async(), name='operation_name')
```

### Database Query Pattern

For finding existing TP orders:

```python
from sqlmodel import Session, select
from ...core.models import TradingOrder
from ...core.types import OrderType, OrderDirection

session = get_db()
statement = select(TradingOrder).where(
    TradingOrder.transaction_id == txn_id,
    TradingOrder.type == OrderType.LIMIT,
    TradingOrder.side == OrderDirection.SELL  # For long positions
)
results = session.exec(statement).all()
```

## Alpaca Integration

### modify_order Implementation

**Location**: `ba2_trade_platform/modules/accounts/AlpacaAccount.py` (line 490)

**Signature**
```python
def modify_order(self, order_id: str, trading_order: TradingOrder):
    """
    Modify an existing order in Alpaca.
    
    Args:
        order_id (str): The ID of the order to modify
        trading_order (TradingOrder): New order details
        
    Returns:
        TradingOrder: Modified order or None if error
    """
```

**Features**
- Rounds prices to 4 decimal places (Alpaca requirement)
- Supports modifying: quantity, limit_price, stop_price, time_in_force
- Uses `client.replace_order()` internally
- Includes error logging and retry logic via `@alpaca_api_retry` decorator

**Usage**
```python
# Create new TradingOrder with updated TP
modified_order = TradingOrder(
    symbol="AAPL",
    quantity=100,
    side=OrderDirection.SELL,
    type=OrderType.LIMIT,
    limit_price=150.25  # New TP price
)

# Modify order in Alpaca
result = account.modify_order(order_id, modified_order)
```

## UI Flow

### 1. Selection Phase
```
┌─────────────────────────────────┐
│ Transactions Table              │
│ ☐ AAPL  │ 100 │ $150.00 │ OPEN │ ← Checkbox
│ ☑ MSFT  │ 50  │ $320.00 │ OPEN │ ← Selected
│ ☑ GOOGL │ 25  │ $140.00 │ OPEN │ ← Selected
└─────────────────────────────────┘
```

### 2. Button Activation
```
When transactions selected:
┌──────────────────────────────────────────────┐
│ Batch Operations (2 selected)                │
│ [Batch Close]  [Batch Adjust TP]  [Clear]   │
└──────────────────────────────────────────────┘
```

### 3. Batch Close Flow
```
User clicks "Batch Close"
           ↓
Confirmation dialog shows
           ↓
User confirms
           ↓
Background task starts (UI responsive)
           ↓
Close each transaction async
           ↓
Update UI when complete
           ↓
Refresh transaction list
```

### 4. Batch Adjust TP Flow
```
User clicks "Batch Adjust TP"
           ↓
Dialog: "Enter TP % from open price"
Input: 5.0
           ↓
User clicks "Apply"
           ↓
Background task starts (UI responsive)
           ↓
For each transaction:
  ├─ Search for existing TP order
  ├─ If found: modify via Alpaca API
  └─ If not: update transaction TP field
           ↓
Show results: "2 modified • 1 created"
           ↓
Refresh transaction list
```

## Error Scenarios

### Batch Close Errors
- Transaction not found → Skipped with notification
- Account not available → Skipped with notification
- Close fails on broker → Tracked and reported
- Network error → Caught and shown in UI

**Result**: "Closed 2/3 transactions. 1 failed."

### Batch Adjust TP Errors
- Invalid transaction → Skipped
- No open price → Skipped
- Modify order fails → Tracked separately
- Database update fails → Tracked separately

**Result**: "Updated 2/3 transactions • 1 modified existing • 1 created"

## Performance Considerations

### Async Operations
- Prevents UI freezing for multiple transactions
- Uses `background_tasks` for non-blocking execution
- Context client's `safe_invoke` for thread-safe UI updates

### Database Optimization
- Batch database queries where possible
- Searches for TP orders only when needed
- Session cleanup after operations

### API Calls
- Alpaca API calls made asynchronously
- Retry logic via `@alpaca_api_retry` decorator
- Error handling prevents cascading failures

## Testing Recommendations

- [ ] Test batch close with 5+ transactions
- [ ] Test batch close with confirmation cancel
- [ ] Test batch close with partial failures
- [ ] Test batch adjust TP with new TP creation
- [ ] Test batch adjust TP with existing TP modification
- [ ] Test batch adjust TP with mixed (some exist, some don't)
- [ ] Test with invalid selections
- [ ] Test network error handling
- [ ] Verify UI remains responsive during operations
- [ ] Verify transaction list refreshes correctly
- [ ] Test with different TP percentages (0.1%, 50%, 100%)

## Future Enhancements

1. **Bulk Edit Other Fields**
   - Batch adjust stop loss
   - Batch adjust position size
   - Batch update expert assignment

2. **Export Operations**
   - Export selected transactions to CSV
   - Export batch operation results

3. **Conditional Batch Operations**
   - Apply to specific symbols only
   - Apply to specific price ranges
   - Apply to transactions older than X days

4. **Scheduling**
   - Schedule batch operations for specific time
   - Recurring batch operations

5. **Analytics**
   - Show impact of batch operations
   - Statistics on operation success rates
