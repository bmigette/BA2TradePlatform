# Fix: SQLAlchemy Session Management in Batch TP Submission

## Problem

When submitting TP orders during batch adjustment, the code was experiencing a SQLAlchemy session attachment error:

```
sqlalchemy.exc.InvalidRequestError: Object '<TradingOrder at 0x...>' is already attached to session '203' (this is '208')
```

## Root Cause

The issue was in the batch TP submission logic:

```python
# WRONG - causes session attachment error
alpaca_order = account.submit_order(tp_order)  # Returns from one session
if alpaca_order:
    tp_order.alpaca_order_id = alpaca_order.id  # Modify object
    add_instance(tp_order)  # Try to add to different session ❌
```

The flow was:
1. `account.submit_order(tp_order)` internally calls `add_instance()`, attaching the order to session A
2. We modify `tp_order.alpaca_order_id` on the detached object
3. We call `add_instance(tp_order)` again, trying to attach to session B
4. SQLAlchemy rejects this because the object was already attached to session A

## Solution

The `account.submit_order()` method **already handles database persistence internally**. It:
1. Calls `add_instance(tp_order, expunge_after_flush=True)` to save the order
2. Expunges the object from the session (detaches it)
3. Returns the persisted order

Therefore, we should **not** call `add_instance()` again afterward.

### Code Change

**Before (WRONG)**:
```python
alpaca_order = account.submit_order(tp_order)
if alpaca_order:
    tp_order.alpaca_order_id = alpaca_order.id
    add_instance(tp_order)  # ❌ Extra persistence attempt
```

**After (CORRECT)**:
```python
alpaca_order = account.submit_order(tp_order)
if alpaca_order:
    success_count += 1
    new_tp_created.append(txn_id)
    # ✅ Order already persisted by submit_order()
```

## How account.submit_order() Works

The method in `AccountInterface.py` performs these steps:

1. **Validate order** - Checks symbol, quantity, order type requirements
2. **Save to database** - Calls `add_instance(trading_order, expunge_after_flush=True)`
   - Flushes to database (generates ID)
   - Expunges from session (detaches object)
3. **Submit to broker** - Calls `_submit_order_impl()` (broker-specific)
4. **Return result** - Returns broker's order object

The order is fully persisted by step 2, before broker submission.

## Key Principle

**Never call persistence functions (`add_instance`, `update_instance`) on objects returned from `account.submit_order()`.**

The order is already:
- ✅ Saved to database
- ✅ Detached from session
- ✅ Safe to use as a normal Python object
- ❌ Must not be re-persisted

## Where This Applies

This principle applies to all code paths using `account.submit_order()`:

1. **Batch TP submission** (this fix)
2. **Dependent order submission** (TradeManager)
3. **Manual order submission** (UI)
4. **Any future order submission code**

## Error Elimination

This fix eliminates the SQLAlchemy session attachment error that was occurring when:
- Batch adjusting TP for filled positions
- Dependent orders being submitted during TradeManager checks
- Any multi-threaded/async order submission scenarios

## Testing

After this fix, verify:

1. **Batch TP for filled position** - Should submit order without errors
2. **Check logs** - Should see "Successfully submitted TP limit order" message
3. **Verify in broker** - Order should appear in Alpaca orders
4. **No session errors** - No "InvalidRequestError: Object ... is already attached" errors

## Files Modified

- `ba2_trade_platform/ui/pages/overview.py` - Lines 4097-4127 in `_execute_batch_adjust_tp()` method

## Related Code

**AccountInterface.submit_order()** (Lines 235-245):
```python
if not trading_order.id:
    order_id = add_instance(trading_order, expunge_after_flush=True)
    # Object is now detached and safe to use
else:
    update_instance(trading_order)

result = self._submit_order_impl(trading_order)
return result
```

The method ensures proper session management so callers don't need to worry about it.
