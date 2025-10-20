# Enhancement: Direct Market TP Order Submission for Filled Positions

## Problem

When adjusting take-profit (TP) in the transaction table, the system was only updating the TP field on the transaction without actually submitting a market order. This meant:
- If a position was already filled (entry order executed), the new TP just sat as a database value
- Users had to manually wait for another system process to convert it into an actual order
- During volatile markets, the position could close without proper TP protection

## Solution

Modified `_execute_batch_adjust_tp()` in `overview.py` to distinguish between two scenarios:

### 1. Position Already Filled
**When**: Entry order is fully executed and position is open
**Action**: Submit a TP limit order **directly to the market** instead of just updating the database field
**Benefits**: 
- TP protection starts immediately
- No delay waiting for background processes
- Better risk management for filled positions

### 2. Position Not Yet Filled
**When**: Entry order is pending or partially filled
**Action**: Update the TP field on the transaction for later processing
**Benefits**:
- Prevents premature TP orders
- Avoids filling wrong quantities if entry fills partially

## Implementation Details

**Location**: `ba2_trade_platform/ui/pages/overview.py` - `_execute_batch_adjust_tp()` method

### Logic Flow

```python
if open_qty > 0 and open_qty == entry_qty:
    # Position is FILLED - submit TP limit order to market
    → Create TradingOrder with TP price as limit
    → Submit to broker via account.submit_order()
    → Store order in database with broker ID
else:
    # Position NOT filled - just update TP field
    → Update Transaction.take_profit field
    → Order will be created later when position fills
```

### Key Checks

1. **Position Filled Detection**: `open_qty == entry_qty`
   - `open_qty`: Current open quantity from transaction tracking
   - `entry_qty`: Original transaction quantity
   - Equal values mean entry is fully executed

2. **Order Direction**: 
   - Long positions (qty > 0) → SELL limit order for TP
   - Short positions (qty < 0) → BUY limit order for TP

3. **Order Details**:
   - Type: LIMIT (to guarantee exit price)
   - Price: User-specified TP price
   - Quantity: Full open quantity
   - Good For: DAY (standard for limit orders)

## Code Changes

### Before
```python
else:
    # No existing TP order - just update the transaction TP field
    txn.take_profit = new_tp_price
    update_instance(txn)
```

### After
```python
else:
    # No existing TP order
    open_qty = txn.get_current_open_qty()
    entry_qty = txn.quantity
    
    if open_qty > 0 and open_qty == entry_qty:
        # Position FILLED - submit market TP order
        tp_order = TradingOrder(...)
        alpaca_order = account.submit_order(tp_order)
        # Store in database with broker ID
    else:
        # Position NOT filled - update TP field
        txn.take_profit = new_tp_price
        update_instance(txn)
```

## User Experience

### Before
1. User adjusts TP in transaction table
2. TP field is updated in database
3. Background process later creates the actual order
4. During wait, position is unprotected
5. User must monitor manually

### After
1. User adjusts TP in transaction table
2. System checks if position is filled
3. If filled → TP limit order submitted **immediately** to broker
4. If not filled → TP field updated for later
5. Position protection starts right away for filled positions

## Testing Scenarios

### Scenario 1: Filled Position with New TP
- Create transaction and fill entry order (buy 100 shares)
- Adjust TP via batch dialog to +5%
- **Expected**: TP limit order submitted to market immediately
- **Verify**: Order appears in Alpaca orders, order ID stored in DB

### Scenario 2: Pending Position with New TP
- Create transaction with pending entry order
- Adjust TP via batch dialog to +5%
- **Expected**: TP field updated, no market order created
- **Verify**: Transaction shows new TP value, no new orders in Alpaca

### Scenario 3: Partially Filled Position
- Create transaction with 100-share entry, only 50 filled
- Adjust TP via batch dialog to +5%
- **Expected**: TP field updated, no market order created (avoids wrong qty)
- **Verify**: Transaction shows new TP value, no premature orders

### Scenario 4: Existing TP Order Modification
- Create transaction with existing TP limit order
- Adjust TP via batch dialog to new price
- **Expected**: Existing order modified via Alpaca modify_order
- **Verify**: Order price updated on broker side

## Error Handling

- **Order submission failure**: Logged and noted as failed in batch result
- **Invalid quantity**: Caught by order validation
- **Price validation**: Broker validates limit price is reasonable
- **Position closed**: Would have 0 open_qty, caught by check

## Backward Compatibility

✅ No breaking changes:
- Existing transactions unaffected
- Filled positions get better protection
- Unfilled positions behave same as before
- Failed orders gracefully handled

## Performance Impact

✅ Minimal impact:
- Single DB query per filled position (get_current_open_qty)
- One broker API call per filled position to submit TP
- Batch operations remain async and non-blocking

## Files Modified

- `ba2_trade_platform/ui/pages/overview.py` - Lines 4097-4140 in `_execute_batch_adjust_tp()` method

## Dependencies

Uses existing:
- `TradingOrder` model for order creation
- `account.submit_order()` method from account interface
- `add_instance()` for database persistence
- Order validation and broker integration

## Future Enhancements

1. **Stop Loss Support**: Similar logic for stop-loss orders
2. **Partial Fills**: Handle positions with multiple partial fills
3. **Order Management UI**: Show submitted TP orders separately from pending TPs
4. **Notifications**: Alert user when TP order is submitted to broker
