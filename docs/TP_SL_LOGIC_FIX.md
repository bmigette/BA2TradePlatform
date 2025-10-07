# Take Profit / Stop Loss Calculation Logic Fix

## Problem

Take Profit and Stop Loss calculations were using `existing_order.side` to determine direction, but this doesn't work correctly for **enter_market** rules where the order hasn't been placed yet. The logic needs to check:
- **enter_market rules**: Use `order_recommendation` (BUY/SELL from expert)
- **open_positions rules**: Use `existing_order.side` (direction of current position)

### Example of Incorrect Behavior
- Rule Type: enter_market (with both BUY and SELL actions)
- Order Recommendation: BUY
- Reference Price: $254.84
- Config Percent: +10%
- Expected TP: $280.32 (10% ABOVE for profit on BUY)
- Actual Result: $229.36 (10% BELOW - wrong! Used SELL logic instead of BUY)

## Root Cause

The code was checking `existing_order.side` which could be:
1. Not set yet (for enter_market before order placement)
2. Wrong direction (if rule generates both BUY and SELL actions)
3. Misaligned with the expert's intent

## Correct Logic

### Determining Position Direction
1. **First priority**: Use `order_recommendation` (BUY/SELL from expert analysis)
2. **Fallback**: Use `existing_order.side` (for open_positions where order already exists)

### Take Profit (TP)
- **LONG/BUY**: TP should be ABOVE entry → `price * (1 + percent/100)`
- **SHORT/SELL**: TP should be BELOW entry → `price * (1 - percent/100)`

### Stop Loss (SL)  
- **LONG/BUY**: SL should be BELOW entry → `price * (1 + percent/100)` with negative percent
- **SHORT/SELL**: SL should be ABOVE entry → `price * (1 - percent/100)` with negative percent

### Percent Sign Convention
- **TP percent**: Typically positive (e.g., +10% for 10% profit target)
- **SL percent**: Typically negative (e.g., -5% for 5% loss limit)
- **Logic handles both**: Code works regardless of sign, using `order_recommendation` to determine direction

## Solution

Check `order_recommendation` first, then fallback to `existing_order.side`:

```python
# Determine if we're going LONG (BUY) or SHORT (SELL)
is_long_position = False
if self.order_recommendation == OrderRecommendation.BUY:
    # Expert recommends BUY = going LONG
    is_long_position = True
    logger.info(f"TP Direction: Using order_recommendation={self.order_recommendation.value} → LONG position")
elif self.order_recommendation == OrderRecommendation.SELL:
    # Expert recommends SELL = going SHORT
    is_long_position = False
    logger.info(f"TP Direction: Using order_recommendation={self.order_recommendation.value} → SHORT position")
elif self.existing_order:
    # Fallback to existing order side for open_positions rules
    is_long_position = (self.existing_order.side.upper() == "BUY")
    logger.info(f"TP Direction: Using existing_order.side={self.existing_order.side.upper()} → {'LONG' if is_long_position else 'SHORT'} position")

# Take Profit Logic
if is_long_position:
    # LONG: TP above entry (profit when price goes up)
    self.take_profit_price = reference_price * (1 + self.percent / 100)
else:
    # SHORT: TP below entry (profit when price goes down)  
    self.take_profit_price = reference_price * (1 - self.percent / 100)

# Stop Loss Logic (INVERSE of TP)
if is_long_position:
    # LONG: SL below entry (stop loss when price goes down)
    self.stop_loss_price = reference_price * (1 + self.percent / 100)  # percent is negative
else:
    # SHORT: SL above entry (stop loss when price goes up)
    self.stop_loss_price = reference_price * (1 - self.percent / 100)  # percent is negative
```

## Implementation

### Files Modified
1. `ba2_trade_platform/core/TradeActions.py`
   - `AdjustTakeProfitAction.execute()` - lines ~664-700
   - `AdjustStopLossAction.execute()` - lines ~934-970

### Changes
- Added direction determination logic using `order_recommendation` first
- Fallback to `existing_order.side` for open_positions rules
- Updated log messages to show which source was used
- Added detailed comments explaining the logic
- Removed incorrect `abs()` usage that was hiding the real problem

## Testing

### Test Cases
#### Enter Market Rules (use order_recommendation)
1. BUY recommendation with +10% TP → TP = price * 1.10 (ABOVE)
2. SELL recommendation with +10% TP → TP = price * 0.90 (BELOW)
3. BUY recommendation with -5% SL → SL = price * 0.95 (BELOW)
4. SELL recommendation with -5% SL → SL = price * 1.05 (ABOVE)

#### Open Positions Rules (use existing_order.side)
5. Existing BUY order with +10% TP → TP = price * 1.10 (ABOVE)
6. Existing SELL order with +10% TP → TP = price * 0.90 (BELOW)
7. Existing BUY order with -5% SL → SL = price * 0.95 (BELOW)
8. Existing SELL order with -5% SL → SL = price * 1.05 (ABOVE)

#### Edge Cases
9. Rule with both BUY and SELL actions → Each uses its own recommendation
10. Negative TP percent → Still works correctly based on direction
11. Positive SL percent → Still works correctly based on direction

## Benefits

1. **Correct Direction**: Always uses expert's intent, not order execution state
2. **Flexible**: Works for both enter_market and open_positions rules
3. **Sign-Agnostic**: Handles positive/negative percents correctly
4. **Clear Logging**: Shows which source (recommendation vs existing_order) was used
5. **Robust**: Prevents miscalculation when rule generates multiple actions

## Migration Notes

No database migration needed. This is a pure logic fix in the calculation code.

Existing rules will now calculate correctly based on the expert's recommendation direction rather than the order execution state.
