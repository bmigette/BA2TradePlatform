# Operands and Calculations Display Enhancement

**Date**: October 7, 2025  
**Status**: ✅ Complete - All Tests Passing (100%)

## Overview

Enhanced the ruleset testing system to show complete calculation details for both conditions and actions:
1. **Operand Display**: Show actual compared values (left vs right) for all conditions
2. **Action Calculations**: Display reference price, adjustment percent, and final calculated price for TP/SL actions
3. **Duplicate Prevention**: Ensure actions are generated only once, even in force mode

## Problem Statement

### Issue 1: Missing Operands for Non-Numeric Conditions

Flag conditions (like `new_target_higher`, `new_target_lower`) showed only PASSED/FAILED without showing the actual values being compared:

```
trigger_0: new_target_higher
Check if new expert target is higher than current TP for AAPL (>2.0% tolerance)
FAILED
✅  <-- What were the actual values?
```

### Issue 2: No Calculation Details for Actions

Actions showed generic descriptions without calculation breakdown:

```
Action 5: adjust_take_profit
Set or adjust take profit order for AAPL (auto-calculated)
<-- What's the reference price? What percent? What's the result?
```

### Issue 3: Duplicate Actions in Force Mode

When `force_generate_actions` debug mode was enabled, the same action was generated multiple times for each rule, creating confusion:

```
Action 1: adjust_take_profit (from rule 1)
Action 2: adjust_take_profit (from rule 2) <-- DUPLICATE
Action 3: adjust_take_profit (from rule 3) <-- DUPLICATE
```

## Solution

### 1. Operand Tracking for All Conditions

#### TradeActionEvaluator Changes

Added `left_operand`, `right_operand`, and `reference_value` tracking to condition evaluations:

```python
condition_evaluation = {
    "trigger_key": trigger_key,
    "event_type": None,
    "operator": trigger_config.get('operator'),
    "value": trigger_config.get('value'),
    "reference_value": trigger_config.get('reference_value'),  # NEW
    "condition_result": False,
    "condition_description": None,
    "error": None,
    "left_operand": None,  # NEW: Actual calculated/compared value
    "right_operand": None  # NEW: The threshold/target value
}
```

Capture operands after evaluation:

```python
# Capture calculated value for numeric conditions
if hasattr(condition, 'get_calculated_value'):
    calculated_value = condition.get_calculated_value()
    if calculated_value is not None:
        condition_evaluation["calculated_value"] = calculated_value
        # For numeric conditions, left_operand is calculated value, right_operand is the threshold
        condition_evaluation["left_operand"] = calculated_value
        condition_evaluation["right_operand"] = trigger_config.get('value')

# For flag conditions (like new_target_higher), try to capture comparison values
# Check if condition has stored comparison attributes after evaluation
if hasattr(condition, 'current_tp_price'):
    condition_evaluation["left_operand"] = getattr(condition, 'current_tp_price', None)
if hasattr(condition, 'new_target_price'):
    condition_evaluation["right_operand"] = getattr(condition, 'new_target_price', None)
if hasattr(condition, 'percent_diff'):
    condition_evaluation["calculated_value"] = getattr(condition, 'percent_diff', None)
```

#### TradeConditions Changes

**NewTargetHigherCondition** - Store comparison values:

```python
def evaluate(self) -> bool:
    try:
        # Initialize tracking variables
        self.current_tp_price = None
        self.new_target_price = None
        self.percent_diff = None
        
        # ... calculation logic ...
        
        # Calculate percent difference
        percent_diff = ((new_target_price - current_tp_price) / current_tp_price) * 100
        
        # Store values for external access
        self.current_tp_price = current_tp_price
        self.new_target_price = new_target_price
        self.percent_diff = percent_diff
        
        return is_higher
        
    except Exception as e:
        # Clear tracking variables on error
        self.current_tp_price = None
        self.new_target_price = None
        self.percent_diff = None
        return False
```

**NewTargetLowerCondition** - Same pattern as above.

#### UI Changes

Enhanced display to show operands:

```python
left_operand = condition.get('left_operand')
right_operand = condition.get('right_operand')

# Build condition label
condition_label = f'{trigger_key}: {event_type}'
if operator and value is not None:
    condition_label += f' {operator} {value}'
if reference_value:
    condition_label += f' (ref: {reference_value})'
if calculated_value is not None:
    condition_label += f' [actual: {calculated_value:.2f}]'

# Add operands for better clarity
if left_operand is not None and right_operand is not None:
    if operator:
        # Numeric conditions
        condition_label += f' [{left_operand:.2f} {operator} {right_operand:.2f}]'
    else:
        # Flag conditions (like new_target_higher)
        condition_label += f' [current: ${left_operand:.2f}, target: ${right_operand:.2f}]'
```

### 2. Action Calculation Preview

#### TradeActions Changes

**AdjustTakeProfitAction** - Added `get_calculation_preview()` method:

```python
def get_calculation_preview(self) -> Dict[str, Any]:
    """
    Get a preview of TP calculation without executing.
    
    Returns:
        Dictionary with reference_price, percent, calculated_price, reference_type
    """
    preview = {
        "reference_type": self.reference_value,
        "percent": self.percent,
        "reference_price": None,
        "calculated_price": self.take_profit_price
    }
    
    # Try to calculate reference price
    if self.reference_value and self.existing_order:
        from .types import ReferenceValue
        
        if self.reference_value == ReferenceValue.ORDER_OPEN_PRICE.value:
            preview["reference_price"] = self.existing_order.limit_price
        elif self.reference_value == ReferenceValue.CURRENT_PRICE.value:
            preview["reference_price"] = self.get_current_price()
        elif self.reference_value == ReferenceValue.EXPERT_TARGET_PRICE.value:
            # ... calculate from expert recommendation ...
            pass
        
        # Calculate final price
        if preview["reference_price"] and self.percent:
            if self.existing_order.side == "buy":
                preview["calculated_price"] = preview["reference_price"] * (1 + self.percent / 100)
            else:  # sell
                preview["calculated_price"] = preview["reference_price"] * (1 - self.percent / 100)
    
    return preview
```

**AdjustStopLossAction** - Same pattern as TP action.

#### TradeActionEvaluator Changes

Call `get_calculation_preview()` when creating action summaries:

```python
# Extract calculation details for TP/SL actions
if action_type in [ExpertActionType.ADJUST_TAKE_PROFIT, ExpertActionType.ADJUST_STOP_LOSS]:
    # Get calculation preview if action supports it
    if hasattr(trade_action, 'get_calculation_preview'):
        calc_preview = trade_action.get_calculation_preview()
        action_summary["action_config"]["reference_type"] = calc_preview.get("reference_type")
        action_summary["action_config"]["adjustment_percent"] = calc_preview.get("percent")
        action_summary["action_config"]["reference_price"] = calc_preview.get("reference_price")
        action_summary["action_config"]["calculated_price"] = calc_preview.get("calculated_price")
```

#### UI Changes

Display calculation details in action summary:

```python
action_config = result.get('action_config', {})
if action_config:
    params = []
    
    # For TP/SL actions, show calculation details
    if action_config.get('reference_type'):
        params.append(f"Ref: {action_config['reference_type']}")
    if action_config.get('reference_price') is not None:
        params.append(f"Ref Price: ${action_config['reference_price']:.2f}")
    if action_config.get('adjustment_percent') is not None:
        params.append(f"Adjust: {action_config['adjustment_percent']:+.2f}%")
    if action_config.get('calculated_price') is not None:
        params.append(f"→ ${action_config['calculated_price']:.2f}")
    
    if params:
        ui.label(' | '.join(params)).classes('text-sm font-medium text-blue-700 mt-1')
```

### 3. Duplicate Action Prevention

#### TradeActionEvaluator Changes

**Track generated actions** using hash-based deduplication:

```python
def evaluate(...):
    # Track generated actions to prevent duplicates
    generated_actions = set()
    
    # ... process rules ...
    
    # Pass tracking set to action creation
    new_actions = self._create_and_store_trade_actions(
        event_action, instrument_name, expert_recommendation, existing_order, generated_actions
    )
```

**Generate unique keys** for each action:

```python
def _create_and_store_trade_actions(..., generated_actions: set):
    for action_key, action_config in actions.items():
        # Create unique key for this action (prevents duplicates)
        import hashlib
        import json
        
        action_hash_data = {
            "type": action_type.value,
            "reference_value": action_config.get('reference_value'),
            "value": action_config.get('value'),
            "instrument": instrument_name
        }
        action_hash = hashlib.md5(json.dumps(action_hash_data, sort_keys=True).encode()).hexdigest()
        action_unique_key = f"{action_type.value}_{action_hash}"
        
        # Skip if this exact action was already generated
        if action_unique_key in generated_actions:
            logger.debug(f"Skipping duplicate action: {action_type.value} (already generated)")
            continue
        
        # Mark this action as generated
        generated_actions.add(action_unique_key)
        
        # Create action...
```

## Test Results

**Test File**: `test_files/test_operands_and_calculations.py`

### Test 1: Operands Display
```
1. Testing NewTargetHigherCondition...
   Result: True
   ✅ Current TP Price: $180.00
   ✅ New Target Price: $198.00
   ✅ Percent Difference: +10.00%

2. Testing NewTargetLowerCondition...
   Result: False
   ✅ Current TP: $180.00, New Target: $198.00

3. Testing ConfidenceCondition (numeric)...
   Result: False
   ✅ Calculated Value: 75.00

✅ PASS: All conditions properly track operands/calculated values!
```

### Test 2: Action Calculations
```
1. Testing AdjustTakeProfitAction calculation preview...
   ✅ Has get_calculation_preview method
   Reference Type: order_open_price
   Percent: 5.0%
   Reference Price: $150.00
   Calculated Price: $157.50

2. Testing AdjustStopLossAction calculation preview...
   ✅ Has get_calculation_preview method
   Reference Type: order_open_price
   Percent: -3.0%
   Reference Price: $150.00
   Calculated Price: $145.50

✅ PASS: All TP/SL actions provide calculation preview!
```

### Test 3: Duplicate Prevention
```
1. Testing action hash generation...
   Same config hash 1: 6a076e4c...
   Same config hash 2: 6a076e4c...
   Different config hash: f9fb1522...

2. Testing duplicate detection...
   First action: adjust_take_profit_6a076e4c... - Duplicate: False
   Same action:  adjust_take_profit_6a076e4c... - Duplicate: True
   Different action: adjust_take_profit_f9fb1522... - Duplicate: False

✅ PASS: Duplicate prevention works correctly!
```

**Overall**: 3/3 tests passed (100%)

## Usage Examples

### Example 1: Complete Condition Analysis with Operands

**Before** (missing operands):
```
trigger_0: new_target_higher
FAILED
```

**After** (with operands):
```
trigger_0: new_target_higher [actual: -10.00] [current: $180.00, target: $162.00]
FAILED
```

Now you can see:
- Current TP is $180.00
- New expert target is $162.00
- The difference is -10% (new target is LOWER, not higher)
- That's why the condition failed

### Example 2: Action Calculation Breakdown

**Before** (no details):
```
Action 5: adjust_take_profit
Set or adjust take profit order for AAPL (auto-calculated)
```

**After** (with calculations):
```
Action 5: adjust_take_profit
Set or adjust take profit order for AAPL (auto-calculated)
Ref: order_open_price | Ref Price: $150.00 | Adjust: +5.00% | → $157.50
```

Now you can see:
- Reference: Order open price
- Reference price: $150.00
- Adjustment: +5.00%
- Calculated TP: $157.50

### Example 3: Force Mode Without Duplicates

**Before** (with duplicates):
```
Rule 1: conditions_met=False
  Action 1: adjust_take_profit → $157.50

Rule 2: conditions_met=False  
  Action 2: adjust_take_profit → $157.50  <-- DUPLICATE

Rule 3: conditions_met=False
  Action 3: adjust_take_profit → $157.50  <-- DUPLICATE
```

**After** (deduplicated):
```
Rule 1: conditions_met=False
  Action 1: adjust_take_profit → $157.50

Rule 2: conditions_met=False
  (Skipping duplicate action: adjust_take_profit)

Rule 3: conditions_met=False
  (Skipping duplicate action: adjust_take_profit)

Total Actions: 1 (deduplicated)
```

## Benefits

### For Debugging
1. **Complete Visibility**: See actual values for ALL conditions (numeric and flag)
2. **Calculation Transparency**: Understand exactly how TP/SL prices are calculated
3. **Clean Output**: No duplicate actions cluttering the results
4. **Root Cause Analysis**: Quickly identify why conditions fail by seeing actual vs expected values

### For Development
1. **Rule Validation**: Verify conditions are comparing correct values
2. **Action Design**: See calculated prices before execution
3. **Threshold Tuning**: Adjust thresholds based on actual operand values
4. **Testing**: Debug force mode shows all actions without duplicates

### For Users
1. **Understanding**: See complete picture of condition evaluation
2. **Troubleshooting**: Diagnose why rules aren't firing
3. **Optimization**: Fine-tune rules based on actual market data
4. **Learning**: Understand how different conditions and actions work together

## Technical Details

### Operand Types

**Numeric Conditions** (e.g., `confidence >= 80`):
- `left_operand`: Calculated value (e.g., 75.00)
- `right_operand`: Threshold (e.g., 80.00)
- `operator`: Comparison operator (e.g., ">=")

**Flag Conditions** (e.g., `new_target_higher`):
- `left_operand`: Current value (e.g., current TP price: $180.00)
- `right_operand`: Target value (e.g., new expert target: $162.00)
- `calculated_value`: Percent difference (e.g., -10.00%)

### Action Calculation Preview

**Reference Types**:
- `order_open_price`: Use order's entry price
- `current_price`: Use current market price
- `expert_target_price`: Use expert's target price

**Calculation Formula**:
```
For BUY orders:
  TP = reference_price * (1 + percent/100)
  SL = reference_price * (1 + percent/100)  # percent is negative

For SELL orders:
  TP = reference_price * (1 - percent/100)
  SL = reference_price * (1 - percent/100)  # percent is positive
```

### Duplicate Detection

**Hash Components**:
- Action type (e.g., "adjust_take_profit")
- Reference value (e.g., "order_open_price")
- Adjustment percent (e.g., 5.0)
- Instrument name (e.g., "AAPL")

**Unique Key Format**: `{action_type}_{md5_hash}`

Example: `adjust_take_profit_6a076e4c171f2d3a8b4e5f6a7b8c9d0e`

## Files Modified

1. **ba2_trade_platform/core/TradeActionEvaluator.py**
   - Added `left_operand`, `right_operand`, `reference_value` to condition_evaluation
   - Added operand capture logic after condition evaluation
   - Added `generated_actions` set for tracking
   - Modified `_create_and_store_trade_actions` to accept tracking set
   - Added action hash generation and duplicate checking

2. **ba2_trade_platform/core/TradeConditions.py**
   - NewTargetHigherCondition: Store `current_tp_price`, `new_target_price`, `percent_diff`
   - NewTargetLowerCondition: Store `current_tp_price`, `new_target_price`, `percent_diff`

3. **ba2_trade_platform/core/TradeActions.py**
   - AdjustTakeProfitAction: Added `get_calculation_preview()` method
   - AdjustStopLossAction: Added `get_calculation_preview()` method

4. **ba2_trade_platform/ui/pages/rulesettest.py**
   - Enhanced condition display to show operands
   - Enhanced action display to show calculation details

5. **test_files/test_operands_and_calculations.py** (NEW)
   - Test operands display for conditions
   - Test action calculation preview
   - Test duplicate prevention
   - 100% pass rate

6. **docs/OPERANDS_AND_CALCULATIONS_DISPLAY.md** (NEW)
   - Complete feature documentation
   - Usage examples
   - Technical details

## Backward Compatibility

- ✅ Existing functionality unchanged
- ✅ New fields optional (gracefully handle missing data)
- ✅ Works with all condition types
- ✅ Works with all action types
- ✅ No breaking changes

## Future Enhancements

1. **Configurable Display Format**: Let users choose how operands are displayed
2. **Historical Comparison**: Show how operands changed over time
3. **Smart Deduplication**: Detect similar (not just identical) actions
4. **Operand Ranges**: Show acceptable ranges for condition operands
5. **Visual Calculation Trees**: Display TP/SL calculations as flowcharts
6. **Export Operands**: Export operand data to CSV/JSON for analysis

## Related Documentation

- [Enhanced Debug Features](./ENHANCED_DEBUG_FEATURES.md)
- [Evaluate All Conditions Feature](./EVALUATE_ALL_CONDITIONS_FEATURE.md)
- [Comprehensive Test Suite](./COMPREHENSIVE_TEST_SUITE.md)
- [Trading Rules System](../README.md)

---

**Last Updated**: October 7, 2025  
**Feature Version**: 1.0  
**Test Status**: ✅ All tests passing (3/3 - 100%)
