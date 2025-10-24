# Quantity Allocation Logging and Minimum Quantity Fix

**Date**: 2025-01-24  
**Status**: ✅ Complete  
**Related Issues**: Expert 9 had 13 pending orders with quantity=0 not submitted to broker

## Problem Summary

### Issue 1: Orders Created with Quantity=0
Expert 9 (taQuickGrok-ndq30-9) had 13 transactions (IDs 190-203) created with `quantity=0.0`, causing them to never be submitted to the broker.

**Root Cause Analysis:**
1. Expert has $3700 total equity, ~$2400 in open positions, ~$1300 available
2. Stock prices ranged from $150-$250, so expert should be able to buy at least 1 share of multiple symbols
3. User hypothesis: Quantity calculation like 0.5 getting rounded to 0 by `int()` conversion
4. Investigation confirmed TWO places where `int()` rounding could cause qty=0:
   - Line 413: First rounding after diversification factor
   - Line 426: Second rounding after instrument weighting (NO minimum qty check after this!)

### Issue 2: Insufficient Logging
When orders were created with qty=0, there was no detailed logging showing:
- Available equity and allocations
- Current price
- Maximum quantity calculations (by instrument and by balance)
- Diversification factor application
- Rounding effects (float to int conversions)
- Instrument weighting calculations
- Final quantity assignment

This made it impossible to debug why qty=0 was calculated.

## Solution Implemented

### 1. Comprehensive Logging Added

Added detailed logging at each stage of quantity calculation in `_calculate_order_quantities()` method:

**Input Logging** (after line 363):
```python
self.logger.info(f"Order {order.id} ({symbol}) - Calculating quantity:")
self.logger.info(f"  Inputs: price=${current_price:.2f}, remaining_balance=${remaining_balance:.2f}, "
               f"max_per_instrument=${max_equity_per_instrument:.2f}, "
               f"current_allocation=${current_allocation:.2f}, "
               f"available_for_instrument=${available_for_instrument:.2f}")
```

**Max Quantity Calculations** (after lines 373-376):
```python
self.logger.info(f"  Calculated: max_qty_by_instrument={max_quantity_by_instrument:.2f} shares, "
               f"max_qty_by_balance={max_quantity_by_balance:.2f} shares")
```

**Diversification Logging** (after line 408):
```python
self.logger.info(f"  Diversification ({num_remaining_instruments} remaining instruments): "
              f"applied factor 0.7: {original_max:.2f} -> {max_quantity:.2f} shares")
```

**First Rounding Logging** (after line 413):
```python
self.logger.info(f"  First rounding: {max_quantity:.2f} -> {quantity} shares (int conversion)")
```

**Instrument Weighting Logging** (after line 421):
```python
self.logger.info(f"  Instrument weight {instrument_weight}%: "
               f"{original_quantity} shares * {instrument_weight/100:.2f} = {weighted_quantity:.2f} shares")
self.logger.info(f"  Second rounding: {weighted_quantity:.2f} -> {quantity} shares (int conversion)")
```

**Final Result Logging** (after line 438):
```python
self.logger.info(f"  ✓ FINAL: Allocated {quantity} shares of {symbol} at ${current_price:.2f} "
               f"(cost: ${total_cost:.2f}, ROI: {recommendation.expected_profit_percent:.2f}%)")
self.logger.info(f"  Updated balances: remaining=${remaining_balance:.2f}, "
               f"{symbol}_allocation=${instrument_allocations[symbol]:.2f}")
```

Or for qty=0:
```python
self.logger.warning(f"  ✗ FINAL: Set quantity to 0 for {symbol} - insufficient funds or limits reached")
```

### 2. Fixed Missing Minimum Quantity Enforcement

**Critical Fix**: Added second minimum quantity check after instrument weighting rounding (line 445-448):

```python
# CRITICAL: Ensure minimum quantity of 1 if we have funds for at least 1 share
# This covers the case where weighting reduces quantity below 1 after rounding
if quantity == 0 and max_quantity_by_balance >= 1:
    quantity = 1
    self.logger.info(f"  Minimum allocation enforced after weighting: setting quantity to 1 share "
                  f"(weighted calc gave 0 but max_by_balance={max_quantity_by_balance:.2f})")
```

**Why This Was Critical:**
- Existing min qty=1 logic (lines 418-422) only covered first rounding
- Did NOT cover second rounding after instrument weighting
- Example scenario where bug occurred:
  1. Available balance = $1300, Stock price = $243.97
  2. max_quantity = 5.3 shares
  3. First rounding: qty = 5 shares
  4. Instrument weight = 15%
  5. weighted_quantity = 5 * 0.15 = 0.75 shares
  6. **Second rounding: qty = int(0.75) = 0** ← BUG: No min qty check here!
  7. Order created with qty=0, never submitted to broker

Now with the fix:
- After second rounding gives qty=0, check if we had funds for at least 1 share
- If `max_quantity_by_balance >= 1`, set qty=1
- This ensures orders get submitted when funds are available

### 3. Enhanced Min Qty Logic at First Rounding

Also improved logging for existing min qty enforcement (lines 418-422):
```python
if quantity == 0 and max_quantity_by_balance >= 1 and max_quantity_by_instrument >= 1:
    quantity = 1
    self.logger.info(f"  Minimum allocation enforced: setting quantity to 1 share "
                  f"(had funds: max_by_balance={max_quantity_by_balance:.2f}, "
                  f"max_by_instrument={max_quantity_by_instrument:.2f})")
```

## Technical Details

### File Modified
`ba2_trade_platform/core/TradeRiskManagement.py`

### Method Modified
`_calculate_order_quantities()` (lines 300-475)

### Changes Summary
1. Added 10+ new log statements showing calculation progression
2. Removed `exc_info=True` from line 359 error log (not in exception handler)
3. Added second minimum quantity enforcement after line 426 (weighted rounding)
4. Enhanced existing min qty logging at lines 418-422
5. Improved final allocation logging with ✓/✗ indicators

### Logging Level Strategy
- **INFO level**: Calculation steps, quantity allocations, important decisions
- **DEBUG level**: ROI values, detailed intermediate steps
- **WARNING level**: Quantity=0 final results (flags potential issues)
- **ERROR level**: Missing prices, critical failures

## Expected Outcomes

### With Example: $1300 Available, Stock $243.97, 15% Weight

**Before Fix:**
```
max_quantity = 5.3 shares
First round: qty = 5
Weighted: 5 * 15% = 0.75
Second round: qty = 0  ← BUG
Order created with qty=0, not submitted
```

**After Fix:**
```
Order 407 (CHTR) - Calculating quantity:
  Inputs: price=$243.97, remaining_balance=$1300.00, max_per_instrument=$500.00, ...
  Calculated: max_qty_by_instrument=2.05 shares, max_qty_by_balance=5.33 shares
  Using min of constraints: max_quantity=2.05 shares
  First rounding: 2.05 -> 2 shares (int conversion)
  Instrument weight 15%: 2 shares * 0.15 = 0.30 shares
  Second rounding: 0.30 -> 0 shares (int conversion)
  Minimum allocation enforced after weighting: setting quantity to 1 share (weighted calc gave 0 but max_by_balance=5.33)
  ✓ FINAL: Allocated 1 share of CHTR at $243.97 (cost: $243.97, ROI: 8.5%)
  Updated balances: remaining=$1056.03, CHTR_allocation=$243.97
```

### Benefits
1. **Prevents qty=0 Orders**: Expert can now allocate at least 1 share when funds available
2. **Comprehensive Debugging**: Full visibility into quantity calculation logic
3. **Clear Indicators**: ✓/✗ symbols in logs make it easy to spot successful vs failed allocations
4. **Rounding Transparency**: Shows exact float→int conversions and their effects
5. **Equity Tracking**: Shows remaining balance and allocations after each order

## Related Fixes in This Session

1. **Duplicate Smart Risk Manager Jobs** - Fixed queue and graph both creating job records
2. **process_recommendation() Return Bug** - Fixed always returning None instead of created order
3. **Quantity=0 Documentation** - Added comments explaining qty=0 before risk management is intentional
4. **Legacy Path Warnings** - Added deprecation warnings for process_recommendation() path

## Testing Recommendations

1. Monitor Expert 9 next Smart Risk Manager run
2. Check logs for "Minimum allocation enforced after weighting" messages
3. Verify no more qty=0 orders created when funds are available
4. Confirm at least 1 share allocated per symbol when balance > stock price
5. Edge case: Verify qty=0 still occurs correctly when truly insufficient funds (e.g., $100 available, stock $250)

## Future Enhancements

1. Consider making minimum quantity configurable per expert
2. Consider using fractional shares if broker supports it (eliminate rounding)
3. Add unit tests for quantity calculation edge cases
4. Consider more sophisticated weighting that preserves minimum quantities
