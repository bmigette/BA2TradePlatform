# Analysis Skip Logic - Transaction-Based Filtering

## Overview
Changed the logic for determining when to skip `ENTER_MARKET` and `OPEN_POSITIONS` analysis tasks from checking order states to checking transaction states.

## Problem
Previously, the system checked for existing **orders** to decide whether to skip analysis:
- `ENTER_MARKET`: Skipped if orders existed in states `[PENDING, OPEN, FILLED]`
- `OPEN_POSITIONS`: Skipped if no transactions found (this was already correct)

**Issues with order-based checking**:
1. Orders can exist in many states (pending, open, filled, canceled, etc.)
2. An expert might have canceled orders but no actual position
3. Checking order states is less reliable than checking transaction states
4. Inconsistent between ENTER_MARKET (checked orders) and OPEN_POSITIONS (checked transactions)

## Solution
Both analysis types now check for **transactions** in `OPENED` or `WAITING` status:

### ENTER_MARKET Analysis
**Old Logic**: Skip if orders exist in `[PENDING, OPEN, FILLED]` states
**New Logic**: Skip if transactions exist in `[OPENED, WAITING]` status

**Rationale**: 
- A transaction represents the actual position lifecycle
- If a transaction is OPENED or WAITING, the expert already has a market entry
- No need to analyze entering the market again

### OPEN_POSITIONS Analysis
**Old Logic**: Skip if NO transactions exist (unchanged)
**New Logic**: Skip if NO transactions exist in `[OPENED, WAITING]` status (clarified)

**Rationale**:
- OPEN_POSITIONS analysis is for managing existing positions
- Only makes sense if there are actual transactions to manage
- Inverted logic from ENTER_MARKET

## Implementation Details

### New Utility Function
Added `has_existing_transactions_for_expert_and_symbol()` in `core/utils.py`:

```python
def has_existing_transactions_for_expert_and_symbol(expert_instance_id: int, symbol: str) -> bool:
    """
    Check if there are existing OPENED or WAITING transactions for a specific expert and symbol.
    
    Returns:
        bool: True if transactions exist in OPENED or WAITING status, False otherwise
    """
    # Queries Transaction table for:
    # - expert_id == expert_instance_id
    # - symbol == symbol
    # - status in [WAITING, OPENED]
```

### Updated Files

1. **`core/utils.py`**
   - Added `has_existing_transactions_for_expert_and_symbol()`
   - Kept existing `has_existing_orders_for_expert_and_symbol()` for potential future use

2. **`core/JobManager.py`**
   - Changed `submit_analysis_job()` to use transaction checking for ENTER_MARKET
   - Updated log message to clarify "existing transactions found (OPENED or WAITING)"

3. **`core/WorkerQueue.py`**
   - Updated `_should_skip_task()` comments to clarify logic
   - Added explicit comments about inverted logic for OPEN_POSITIONS
   - Updated log message for ENTER_MARKET to match JobManager

## Behavior Changes

### Before
```
ENTER_MARKET Analysis:
├─ Check: Orders exist in [PENDING, OPEN, FILLED]?
├─ Skip if: YES (orders found)
└─ Log: "existing orders found in states ['pending', 'open', 'filled']"

OPEN_POSITIONS Analysis:
├─ Check: Transactions exist in [WAITING, OPENED]?
├─ Skip if: NO (no transactions)
└─ Log: "no existing transactions found"
```

### After
```
ENTER_MARKET Analysis:
├─ Check: Transactions exist in [WAITING, OPENED]?
├─ Skip if: YES (transactions found)
└─ Log: "existing transactions found (OPENED or WAITING)"

OPEN_POSITIONS Analysis:
├─ Check: Transactions exist in [WAITING, OPENED]?
├─ Skip if: NO (no transactions)
└─ Log: "no existing transactions found"
```

## Examples

### Example 1: Expert Has Open Position
```
State:
- Transaction #123: Expert 3, AAPL, status=OPENED
- Orders: BUY 100 AAPL (FILLED), SELL 100 AAPL @ $150 (OPEN, TP)

ENTER_MARKET Analysis:
- Check: has_existing_transactions_for_expert_and_symbol(3, "AAPL")
- Result: TRUE (Transaction #123 is OPENED)
- Action: SKIP analysis
- Log: "Skipping ENTER_MARKET analysis for expert 3, symbol AAPL: existing transactions found (OPENED or WAITING)"

OPEN_POSITIONS Analysis:
- Check: has_existing_transactions_for_expert_and_symbol(3, "AAPL")
- Result: TRUE (Transaction #123 is OPENED)
- Action: PROCEED with analysis (inverted logic)
```

### Example 2: Expert Has Waiting Transaction
```
State:
- Transaction #456: Expert 5, TSLA, status=WAITING
- Orders: BUY 100 TSLA @ $200 (PENDING, limit order)

ENTER_MARKET Analysis:
- Check: has_existing_transactions_for_expert_and_symbol(5, "TSLA")
- Result: TRUE (Transaction #456 is WAITING)
- Action: SKIP analysis
- Log: "Skipping ENTER_MARKET analysis for expert 5, symbol TSLA: existing transactions found (OPENED or WAITING)"
```

### Example 3: Expert Has Closed Transaction Only
```
State:
- Transaction #789: Expert 7, NVDA, status=CLOSED
- Orders: All orders FILLED or CANCELED

ENTER_MARKET Analysis:
- Check: has_existing_transactions_for_expert_and_symbol(7, "NVDA")
- Result: FALSE (no OPENED or WAITING transactions)
- Action: PROCEED with analysis
- Log: No skip message

OPEN_POSITIONS Analysis:
- Check: has_existing_transactions_for_expert_and_symbol(7, "NVDA")
- Result: FALSE (no OPENED or WAITING transactions)
- Action: SKIP analysis
- Log: "Skipping OPEN_POSITIONS analysis for expert 7, symbol NVDA: no existing transactions found"
```

### Example 4: Expert Has Canceled Orders, No Transaction
```
State:
- No transactions for Expert 9, MSFT
- Orders: BUY 100 MSFT @ $300 (CANCELED)

Old Behavior (order-based):
- ENTER_MARKET would SKIP (order exists in FILLED state - wait, CANCELED is not in list)
- Actually, CANCELED is not in [PENDING, OPEN, FILLED], so it would PROCEED

New Behavior (transaction-based):
- ENTER_MARKET: PROCEED (no transactions exist)
- More accurate - expert doesn't actually have a position
```

## Benefits

1. **More Accurate**: Transactions represent actual positions, not just order attempts
2. **Consistent**: Both analysis types now check the same thing (transactions)
3. **Clearer Logic**: OPENED/WAITING states are clear position indicators
4. **Reliable**: Transaction status is authoritative for position lifecycle
5. **Cleaner Logs**: Messages explicitly state what was checked

## Testing Checklist

### Test Case 1: Open Position
- [ ] Create transaction with status=OPENED for expert X, symbol Y
- [ ] Submit ENTER_MARKET analysis for expert X, symbol Y
- [ ] Verify analysis is SKIPPED
- [ ] Verify log: "existing transactions found (OPENED or WAITING)"
- [ ] Submit OPEN_POSITIONS analysis for expert X, symbol Y
- [ ] Verify analysis PROCEEDS

### Test Case 2: Waiting Transaction
- [ ] Create transaction with status=WAITING for expert X, symbol Y
- [ ] Submit ENTER_MARKET analysis for expert X, symbol Y
- [ ] Verify analysis is SKIPPED
- [ ] Submit OPEN_POSITIONS analysis for expert X, symbol Y
- [ ] Verify analysis PROCEEDS

### Test Case 3: Closed Transaction
- [ ] Create transaction with status=CLOSED for expert X, symbol Y
- [ ] Submit ENTER_MARKET analysis for expert X, symbol Y
- [ ] Verify analysis PROCEEDS (not skipped)
- [ ] Submit OPEN_POSITIONS analysis for expert X, symbol Y
- [ ] Verify analysis is SKIPPED
- [ ] Verify log: "no existing transactions found"

### Test Case 4: No Transactions
- [ ] Ensure no transactions exist for expert X, symbol Y
- [ ] Submit ENTER_MARKET analysis for expert X, symbol Y
- [ ] Verify analysis PROCEEDS
- [ ] Submit OPEN_POSITIONS analysis for expert X, symbol Y
- [ ] Verify analysis is SKIPPED

### Test Case 5: Bypass Flag
- [ ] Create transaction with status=OPENED for expert X, symbol Y
- [ ] Submit ENTER_MARKET with bypass_transaction_check=True
- [ ] Verify analysis PROCEEDS (bypass works)
- [ ] Verify log: "Bypassing transaction check"

## Migration Notes

### Backward Compatibility
This is a **behavior change** that affects when analysis tasks are executed:

**Potential Impact**:
- Experts with canceled/rejected orders but no transactions will now be allowed to analyze ENTER_MARKET (more accurate)
- Experts with transactions will be filtered more reliably
- Overall, this should improve accuracy and reduce unnecessary analysis

**No Database Changes**: No schema migrations required

### Deprecated Functions
- `has_existing_orders_for_expert_and_symbol()` is still available but no longer used for skip logic
- May be removed in future versions if no other use cases emerge

## Related Files
- `ba2_trade_platform/core/utils.py` - New utility function
- `ba2_trade_platform/core/JobManager.py` - ENTER_MARKET skip logic
- `ba2_trade_platform/core/WorkerQueue.py` - Task skip logic with comments
- `ba2_trade_platform/core/models.py` - Transaction model
- `ba2_trade_platform/core/types.py` - TransactionStatus enum

## Future Enhancements
Potential improvements for future iterations:
1. Add configurable transaction status filtering (e.g., include PARTIALLY_FILLED)
2. Cache transaction existence checks to reduce database queries
3. Add metrics for skipped vs executed analysis tasks
4. Consider symbol-level analysis cooldowns to prevent rapid re-analysis
