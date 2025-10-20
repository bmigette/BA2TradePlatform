# TradeManager Attribute Name Fix

**Date:** 2025-10-17  
**Issue:** AttributeError: 'TradingOrder' object has no attribute 'direction'  
**Severity:** HIGH - Prevents dependent orders (TP/SL) from being submitted

## Problem

TradeManager was trying to access `dependent_order.direction` in log messages, but the TradingOrder model uses `side` not `direction`.

### Error Message
```
AttributeError: 'TradingOrder' object has no attribute 'direction'
Traceback (most recent call last):
  File "ba2_trade_platform\core\TradeManager.py", line 239
    f"Submitting dependent order {dependent_order.id}: {dependent_order.direction.value} "
                                                        ^^^^^^^^^^^^^^^^^^^^^^^^^
```

### Root Cause

The TradingOrder model defines the field as:
```python
class TradingOrder(SQLModel, table=True):
    side: OrderDirection  # ✅ Correct field name
    # NOT direction!
```

But TradeManager was using:
```python
f"... {dependent_order.direction.value} ..."  # ❌ Wrong field name
```

## The Fix

**File:** `ba2_trade_platform/core/TradeManager.py`

### Location 1: Line 238-241
```python
# Before (wrong)
f"Submitting dependent order {dependent_order.id}: {dependent_order.direction.value} "

# After (correct)
f"Submitting dependent order {dependent_order.id}: {dependent_order.side.value} "
```

### Location 2: Line 393-396
```python
# Before (wrong)
f"Submitting dependent order {dependent_order.id}: {dependent_order.direction.value} "

# After (correct)
f"Submitting dependent order {dependent_order.id}: {dependent_order.side.value} "
```

## Impact

### Before Fix ❌
- Dependent orders (TP/SL) failed to submit
- AttributeError raised during submission
- Orders stuck in WAITING_TRIGGER status
- Position management broken

### After Fix ✅
- Dependent orders submit successfully
- Proper logging shows BUY/SELL direction
- Orders transition to OPEN status
- Position management works correctly

## Testing

### Verification Steps
1. Create market order with take profit
2. Wait for market order to fill
3. TradeManager processes WAITING_TRIGGER orders
4. TP order submits successfully
5. Check logs for proper "Submitting dependent order" message

### Expected Log Output
```
INFO - Submitting dependent order 233: SELL 1.0 INTU @ limit (triggered by parent order 232)
INFO - Successfully submitted order to Alpaca: broker_order_id=ABC123
INFO - Successfully submitted dependent order 233
```

## Terminology Clarification

The confusion comes from terminology:

### TradingOrder Model
- Uses `side: OrderDirection` 
- Values: `OrderDirection.BUY` or `OrderDirection.SELL`
- Represents which side of the trade (buy/sell)

### Why "side" not "direction"?
- Industry standard terminology (Alpaca, most brokers use "side")
- "Direction" might be confused with LONG/SHORT positions
- "Side" is clearer: which side of the order book

### Related Fields
- `side` - The order side (BUY/SELL) ✅
- `order_type` - The order type (MARKET/LIMIT/STOP)
- `status` - The order status (OPEN/FILLED/CANCELED)

## Prevention

### Code Review Checklist
When working with TradingOrder objects:
- [ ] Use `order.side` not `order.direction`
- [ ] Use `order.side.value` to get string value
- [ ] Check enum is `OrderDirection.BUY` or `OrderDirection.SELL`
- [ ] Verify field names against models.py

### IDE Setup
Configure IDE to:
- Show field definitions on hover
- Enable type checking for SQLModel models
- Warn on undefined attributes

## Related Issues

This is similar to the price_type confusion fixed earlier:
- Models define specific field names
- Code must use exact field names
- Logging/debugging statements are common places for errors
- Always verify field names against model definitions

## Success Metrics

✅ **Fixed** - 2 locations updated  
✅ **No AttributeError** - Dependent orders submit successfully  
✅ **Proper logging** - Shows correct BUY/SELL side  
✅ **Tests pass** - TP/SL orders work correctly
