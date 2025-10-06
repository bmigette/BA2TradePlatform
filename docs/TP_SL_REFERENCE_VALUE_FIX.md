# TP/SL Reference Value and Percent Calculation Fix

**Date:** October 6, 2025  
**Issue:** TP/SL actions were using hardcoded default values instead of calculating prices based on configurable reference values and percentages.

## Problem

The `AdjustTakeProfitAction` and `AdjustStopLossAction` classes had several critical issues:

1. **Default Value Assumptions**: When no price was provided, they would assume defaults (5% TP, 3% SL) instead of requiring explicit configuration
2. **Missing Reference Value Logic**: The actions didn't implement the reference price calculation system (order_open_price, current_price, expert_target_price)
3. **No Parameter Passing**: TradeActionEvaluator wasn't extracting `reference_value` and `value` (percent) from action_config

### Before: Problematic Code

```python
# AdjustTakeProfitAction.__init__
def __init__(self, instrument_name: str, account: AccountInterface, 
             order_recommendation: OrderRecommendation, existing_order: Optional[TradingOrder] = None,
             take_profit_price: Optional[float] = None):
    # Only accepted direct price

# AdjustTakeProfitAction.execute()
if self.take_profit_price is None:
    current_price = self.get_current_price()
    if current_price is None:
        return error
    
    # Default to 5% profit target ❌ WRONG - should not assume defaults!
    if self.existing_order.side == "buy":
        self.take_profit_price = current_price * 1.05
    else:
        self.take_profit_price = current_price * 0.95
```

## Solution

### 1. Enhanced Constructor Parameters

Both actions now accept:
- `reference_value`: Type of reference price ('order_open_price', 'current_price', 'expert_target_price')
- `percent`: Percentage to apply to the reference value (e.g., 5.0 for +5%, -3.0 for -3%)

```python
def __init__(self, instrument_name: str, account: AccountInterface, 
             order_recommendation: OrderRecommendation, existing_order: Optional[TradingOrder] = None,
             take_profit_price: Optional[float] = None,
             reference_value: Optional[str] = None, percent: Optional[float] = None):
    self.take_profit_price = take_profit_price
    self.reference_value = reference_value
    self.percent = percent
```

### 2. Reference Price Calculation Logic

The execute() methods now implement proper reference price resolution:

```python
# Calculate take profit price if not directly provided
if self.take_profit_price is None:
    if self.reference_value is None or self.percent is None:
        logger.error(f"No take profit price, reference_value, or percent provided")
        return error  # ✅ CORRECT - fail instead of assuming defaults
    
    # Get reference price based on reference_value type
    from .types import ReferenceValue
    reference_price = None
    
    if self.reference_value == ReferenceValue.ORDER_OPEN_PRICE.value:
        # Use the order's limit_price as open price
        reference_price = self.existing_order.limit_price
        if reference_price is None:
            logger.error(f"Order has no limit_price")
            return error
        
    elif self.reference_value == ReferenceValue.CURRENT_PRICE.value:
        reference_price = self.get_current_price()
        if reference_price is None:
            logger.error(f"Cannot get current price")
            return error
        
    elif self.reference_value == ReferenceValue.EXPERT_TARGET_PRICE.value:
        # Get target price from expert recommendation
        # Target = price_at_date * (1 + expected_profit_percent/100) for BUY
        # Target = price_at_date * (1 - expected_profit_percent/100) for SELL
        expert_rec = get_instance(ExpertRecommendation, self.existing_order.expert_recommendation_id)
        if expert_rec and hasattr(expert_rec, 'price_at_date') and hasattr(expert_rec, 'expected_profit_percent'):
            base_price = expert_rec.price_at_date
            expected_profit = expert_rec.expected_profit_percent
            
            # Calculate target price based on recommendation direction
            if expert_rec.recommended_action == OrderRecommendation.BUY:
                reference_price = base_price * (1 + expected_profit / 100)
                logger.info(f"EXPERT_TARGET_PRICE for BUY: {base_price:.2f} * (1 + {expected_profit:.1f}/100) = {reference_price:.2f}")
            elif expert_rec.recommended_action == OrderRecommendation.SELL:
                reference_price = base_price * (1 - expected_profit / 100)
                logger.info(f"EXPERT_TARGET_PRICE for SELL: {base_price:.2f} * (1 - {expected_profit:.1f}/100) = {reference_price:.2f}")
            else:
                logger.error(f"Invalid recommendation action")
                return error
        else:
            logger.error(f"Cannot get expert target price - missing price_at_date or expected_profit_percent")
            return error
    
    # Calculate TP price based on reference price and percent
    if self.existing_order.side.upper() == "BUY":
        self.take_profit_price = reference_price * (1 + self.percent / 100)
    else:  # SELL order
        self.take_profit_price = reference_price * (1 - self.percent / 100)
    
    logger.info(f"Calculated TP price: {self.take_profit_price:.2f} from {self.reference_value}={reference_price:.2f} with {self.percent:+.1f}%")
```

### 3. TradeActionEvaluator Parameter Extraction

Updated `_create_trade_action()` to extract and pass the configuration:

```python
if action_type == ExpertActionType.ADJUST_TAKE_PROFIT:
    # Extract reference_value and percent (value) for TP calculation
    kwargs['reference_value'] = action_config.get('reference_value')
    kwargs['percent'] = action_config.get('value')  # 'value' in config is the percentage
    # Also allow direct take_profit_price if provided
    kwargs['take_profit_price'] = action_config.get('take_profit_price')
```

## Examples

### Example 1: TP at 5% above order open price
```python
action_config = {
    'action_type': 'adjust_take_profit',
    'reference_value': 'order_open_price',
    'value': 5.0  # +5%
}

# For BUY order opened at $100:
# TP = $100 * (1 + 5.0/100) = $105
```

### Example 2: SL at 3% below current market price
```python
action_config = {
    'action_type': 'adjust_stop_loss',
    'reference_value': 'current_price',
    'value': -3.0  # -3%
}

# For BUY order with current price $110:
# SL = $110 * (1 + (-3.0)/100) = $106.70
```

### Example 3: TP using expert's target price + additional percent
```python
action_config = {
    'action_type': 'adjust_take_profit',
    'reference_value': 'expert_target_price',
    'value': 2.0  # +2% above expert target
}

# Expert recommendation: BUY at $100, expected_profit_percent = 20%
# Expert target price = $100 * (1 + 20/100) = $120
# TP = $120 * (1 + 2.0/100) = $122.40

# For SELL recommendation at $100, expected_profit_percent = 15%:
# Expert target price = $100 * (1 - 15/100) = $85
# TP = $85 * (1 - 2.0/100) = $83.30
```

**Important:** `EXPERT_TARGET_PRICE` first calculates the expert's target using:
- **BUY**: `price_at_date * (1 + expected_profit_percent/100)` 
- **SELL**: `price_at_date * (1 - expected_profit_percent/100)`

Then applies the configured percent to that calculated target price.

## Benefits

1. **No Default Assumptions**: ✅ Actions fail explicitly when configuration is incomplete
2. **Flexible Reference Prices**: ✅ Supports order_open_price, current_price, expert_target_price
3. **Configurable Percentages**: ✅ Users control exact profit/loss targets via rule configuration
4. **Better Error Handling**: ✅ Clear error messages when required data is unavailable
5. **Alignment with UI**: ✅ Matches the reference_value selector in settings page

## Enhanced Logging

All TP/SL calculations now include comprehensive logging for troubleshooting:

### Example Log Output (TP Calculation with EXPERT_TARGET_PRICE)

```
TP Calculation START for AAPL - Order ID: 123, Side: BUY, reference_value: expert_target_price, percent: +2.00%
TP Reference: EXPERT_TARGET_PRICE - base_price: $100.00, expected_profit: 20.0%, action: BUY
TP Target (BUY): $100.00 * (1 + 20.0/100) = $120.00
TP Final (BUY): $120.00 * (1 + 2.00/100) = $122.40
TP Calculation COMPLETE for AAPL - Final TP Price: $122.40
```

### Example Log Output (SL Calculation with CURRENT_PRICE)

```
SL Calculation START for TSLA - Order ID: 456, Side: BUY, reference_value: current_price, percent: -3.00%
SL Reference: CURRENT_PRICE = $250.00 (from market data)
SL Final (BUY): $250.00 * (1 + -3.00/100) = $242.50
SL Calculation COMPLETE for TSLA - Final SL Price: $242.50
```

### Example Log Output (TP Calculation with ORDER_OPEN_PRICE)

```
TP Calculation START for NVDA - Order ID: 789, Side: SELL, reference_value: order_open_price, percent: +5.00%
TP Reference: ORDER_OPEN_PRICE = $500.00 (from order.limit_price)
TP Final (SELL): $500.00 * (1 - 5.00/100) = $475.00
TP Calculation COMPLETE for NVDA - Final TP Price: $475.00
```

## Testing

To test the fix:

1. **Create a rule with TP adjustment**:
   - Trigger: `has_position` (flag condition)
   - Action: `adjust_take_profit`
   - Reference Value: `Current Market Price`
   - Percent: `5.0` (for +5%)

2. **Verify calculation in logs** - you will see detailed logging like:
   ```
   TP Calculation START for AAPL - Order ID: 123, Side: BUY, reference_value: current_price, percent: +5.00%
   TP Reference: CURRENT_PRICE = $100.00 (from market data)
   TP Final (BUY): $100.00 * (1 + 5.00/100) = $105.00
   TP Calculation COMPLETE for AAPL - Final TP Price: $105.00
   ```

3. **Test error handling**:
   - Create rule without reference_value → should fail with clear error
   - Create rule without percent → should fail with clear error

4. **Check all calculation steps** in the logs to ensure:
   - Reference value is correctly resolved
   - Expert profit percentage is properly applied (for EXPERT_TARGET_PRICE)
   - Final percent adjustment is correctly calculated
   - Order side (BUY/SELL) affects formula direction

## Files Modified

- `ba2_trade_platform/core/TradeActions.py`
  - Updated `AdjustTakeProfitAction.__init__()` - added reference_value and percent parameters
  - Rewrote `AdjustTakeProfitAction.execute()` - implemented reference price calculation
  - Updated `AdjustStopLossAction.__init__()` - added reference_value and percent parameters
  - Rewrote `AdjustStopLossAction.execute()` - implemented reference price calculation

- `ba2_trade_platform/core/TradeActionEvaluator.py`
  - Updated `_create_trade_action()` - extracts reference_value and value from action_config

## Related Code

- `ba2_trade_platform/core/types.py` - Defines `ReferenceValue` enum
- `ba2_trade_platform/ui/pages/settings.py` - UI for configuring reference_value and value
