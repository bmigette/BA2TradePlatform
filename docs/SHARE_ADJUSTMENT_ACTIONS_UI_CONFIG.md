# Share Adjustment Actions - UI Configuration Enhancement

**Date**: 2025-10-07  
**Status**: ✅ Completed

## Overview

This document describes the enhancement made to add configurable `target_percent` values for INCREASE_INSTRUMENT_SHARE and DECREASE_INSTRUMENT_SHARE actions in the UI.

## Problem Statement

Previously, the increase/decrease instrument share actions had no configurable values in the UI. These actions require a `target_percent` parameter to specify what percentage of account equity the position should be adjusted to (e.g., "decrease instrument share to 5% of account equity").

Without this UI configuration:
- ❌ Users couldn't set target percentages when creating rules
- ❌ Actions would fail because `target_percent` was None
- ❌ No validation of target percentage values
- ❌ Difficult to understand what the action would do

## Solution

Added a dedicated input field for `target_percent` configuration when users select INCREASE_INSTRUMENT_SHARE or DECREASE_INSTRUMENT_SHARE actions.

### Features Implemented

1. **Target Percent Input Field**
   - Number input with range validation (0-100%)
   - Step: 0.1 (allows precision like 5.5%)
   - Default value: 10.0%
   - Format: %.1f (displays as "10.0")

2. **Validation Rules**
   - Minimum: 0.0% (allows full close for DECREASE)
   - Maximum: 100.0% (theoretical maximum)
   - Required field (cannot be empty)
   - Range checked on save

3. **Help Text**
   - Displays: "Minimum: 1 share | Maximum: Respects max_virtual_equity_per_instrument_percent setting and available balance"
   - Clarifies that actual execution has additional constraints

4. **UI Display**
   - Shows target percent prominently in rule evaluation results
   - Format: "Target: 10.0% of equity"
   - Displayed alongside other action parameters (TP/SL/Qty)

## Changes Made

### 1. `ba2_trade_platform/core/types.py`

**Added New Helper Function**:
```python
def get_share_adjustment_action_values():
    """Return list of share adjustment action type values (INCREASE/DECREASE_INSTRUMENT_SHARE)."""
    return [
        ExpertActionType.INCREASE_INSTRUMENT_SHARE.value,
        ExpertActionType.DECREASE_INSTRUMENT_SHARE.value
    ]


def is_share_adjustment_action(action_value):
    """Check if an action value corresponds to a share adjustment action type."""
    return action_value in get_share_adjustment_action_values()
```

**Purpose**: Identify share adjustment actions to apply special UI treatment

### 2. `ba2_trade_platform/ui/pages/settings.py`

#### Import Update
```python
from ...core.types import InstrumentType, ExpertEventRuleType, ExpertEventType, ExpertActionType, ReferenceValue, is_numeric_event, is_adjustment_action, is_share_adjustment_action, AnalysisUseCase, MarketAnalysisStatus
```

#### _add_action_row() Enhancement

**Before**:
```python
# Value input (for ADJUST_ types)
value_row = ui.row().classes('w-full')
value_input = None
reference_select = None

def update_action_inputs():
    # ... only handled adjustment actions
    if selected_type and is_adjustment_action(selected_type):
        # Show value input and reference selector
    else:
        # Simple action - no additional inputs needed
```

**After**:
```python
# Value input (for ADJUST_ types and INCREASE/DECREASE_INSTRUMENT_SHARE)
value_row = ui.row().classes('w-full')
value_input = None
reference_select = None
target_percent_input = None

def update_action_inputs():
    # ... handles adjustment actions AND share adjustment actions
    if selected_type and is_adjustment_action(selected_type):
        # Show value input and reference selector (existing)
    elif selected_type and is_share_adjustment_action(selected_type):
        # NEW: Show target_percent input
        target_percent_input = ui.number(
            label='Target Percent of Account Equity (%)',
            value=action_config.get('target_percent', 10.0) if action_config else 10.0,
            min=0.0,
            max=100.0,
            step=0.1,
            format='%.1f',
            placeholder='e.g. 10.0 for 10%'
        ).classes('w-full')
        
        # Help text
        ui.label('Minimum: 1 share | Maximum: Respects max_virtual_equity_per_instrument_percent setting and available balance').classes('text-xs text-grey-6 mt-1')
    else:
        # Simple action - no additional inputs needed
```

**Storage Update**:
```python
# Store references
self.actions[action_id] = {
    'card': action_card,
    'type_select': action_select,
    'value_input': lambda: value_input,
    'reference_select': lambda: reference_select,
    'target_percent_input': lambda: target_percent_input  # NEW
}
```

#### _save_rule() Enhancement

**Added Validation and Save Logic**:
```python
# Collect actions
actions_data = {}
for action_id, action_refs in self.actions.items():
    action_type = action_refs['type_select'].value
    action_config = {'action_type': action_type}
    
    if is_adjustment_action(action_type):
        # ... existing adjustment action logic
    
    elif is_share_adjustment_action(action_type):  # NEW
        # Share adjustment action (INCREASE/DECREASE_INSTRUMENT_SHARE)
        target_percent_input = action_refs['target_percent_input']()
        
        if target_percent_input and target_percent_input.value is not None:
            try:
                target_percent = float(target_percent_input.value)
                # Validate range
                if target_percent < 0 or target_percent > 100:
                    ui.notify(f'Target percent must be between 0 and 100 for action {action_type}', type='negative')
                    return
                action_config['target_percent'] = target_percent
            except (ValueError, TypeError):
                ui.notify(f'Invalid target percent value for action {action_type}', type='negative')
                return
        else:
            ui.notify(f'Target percent is required for action {action_type}', type='negative')
            return
    
    actions_data[action_id] = action_config
```

**Validation Features**:
- ✅ Checks if value is not None
- ✅ Validates numeric conversion
- ✅ Enforces 0-100 range
- ✅ Shows user-friendly error messages
- ✅ Prevents save if validation fails

### 3. `ba2_trade_platform/ui/pages/rulesettest.py`

**Enhanced Action Display**:
```python
# Display action-specific values (TP/SL, target_percent, etc.)
action_config = result.get('action_config', {})
if action_config:
    # Build a readable summary of important parameters
    params = []
    if 'take_profit_percent' in action_config:
        params.append(f"TP: {action_config['take_profit_percent']}%")
    if 'stop_loss_percent' in action_config:
        params.append(f"SL: {action_config['stop_loss_percent']}%")
    if 'quantity_percent' in action_config:
        params.append(f"Qty: {action_config['quantity_percent']}%")
    if 'target_percent' in action_config:  # NEW
        params.append(f"Target: {action_config['target_percent']}% of equity")
    # ... other parameters
    
    if params:
        ui.label(' | '.join(params)).classes('text-sm font-medium text-blue-700 mt-1')
```

**Display Format**:
- Shows as: `Target: 10.0% of equity`
- Appears alongside other action parameters
- Clear indication of what the value represents

## Usage Examples

### Example 1: Decrease Position on High Allocation

**Scenario**: Reduce position to 5% when instrument exceeds 15% of portfolio

**Rule Configuration**:
```yaml
Condition: instrument_account_share > 15.0
Action: DECREASE_INSTRUMENT_SHARE
  - Target Percent: 5.0%
```

**UI Configuration**:
1. Add condition: `N_INSTRUMENT_ACCOUNT_SHARE > 15.0`
2. Add action: Select `decrease_instrument_share`
3. Enter target percent: `5.0`
4. Save rule

**Expected Behavior**:
- When position exceeds 15% of equity, sell shares to reach 5%
- Minimum 1 share maintained (unless target is 0%)
- Respects available positions

### Example 2: Increase Position on High Confidence

**Scenario**: Grow position to 12% when confidence is high and current allocation is low

**Rule Configuration**:
```yaml
Condition: confidence >= 85.0 AND instrument_account_share < 8.0
Action: INCREASE_INSTRUMENT_SHARE
  - Target Percent: 12.0%
```

**UI Configuration**:
1. Add condition 1: `N_CONFIDENCE >= 85.0`
2. Add condition 2: `N_INSTRUMENT_ACCOUNT_SHARE < 8.0`
3. Add action: Select `increase_instrument_share`
4. Enter target percent: `12.0`
5. Save rule

**Expected Behavior**:
- When confidence ≥85% AND position <8%, buy shares to reach 12%
- Capped by `max_virtual_equity_per_instrument_percent` setting
- Respects available balance
- Minimum 1 share order size

### Example 3: Full Position Close

**Scenario**: Exit position completely

**Rule Configuration**:
```yaml
Condition: confidence < 30.0
Action: DECREASE_INSTRUMENT_SHARE
  - Target Percent: 0.0%
```

**UI Configuration**:
1. Add condition: `N_CONFIDENCE < 30.0`
2. Add action: Select `decrease_instrument_share`
3. Enter target percent: `0.0`
4. Save rule

**Expected Behavior**:
- When confidence drops below 30%, sell entire position
- Equivalent to CLOSE action but more explicit about intent

## Validation and Safety

### Input Validation
1. **Range Check**: 0.0 ≤ target_percent ≤ 100.0
2. **Type Check**: Must be valid float
3. **Required**: Cannot be None/empty
4. **Precision**: Accepts up to 1 decimal place

### Execution Safety

The actual execution in `TradeActions.py` enforces additional rules:

1. **INCREASE_INSTRUMENT_SHARE**:
   - ✅ Caps at `max_virtual_equity_per_instrument_percent` (default 10%)
   - ✅ Cannot exceed available balance
   - ✅ Minimum 1 share order size
   - ✅ Skips if already at/above target

2. **DECREASE_INSTRUMENT_SHARE**:
   - ✅ Maintains minimum 1 share if `target_percent > 0`
   - ✅ Allows full close if `target_percent = 0`
   - ✅ Skips if already at/below target
   - ✅ Cannot sell more shares than owned

### User Feedback

**UI Notifications**:
- ❌ "Target percent must be between 0 and 100 for action {type}" - Range violation
- ❌ "Invalid target percent value for action {type}" - Type conversion error
- ❌ "Target percent is required for action {type}" - Missing value
- ✅ "Rule saved successfully!" - Successful save

## Testing Checklist

### Manual Testing

- [ ] Create rule with INCREASE_INSTRUMENT_SHARE action
  - [ ] Input shows with default 10.0%
  - [ ] Can set to 15.5%
  - [ ] Cannot set to -5 (validation error)
  - [ ] Cannot set to 150 (validation error)
  - [ ] Cannot save without value
  - [ ] Saves successfully with valid value

- [ ] Create rule with DECREASE_INSTRUMENT_SHARE action
  - [ ] Input shows with default 10.0%
  - [ ] Can set to 0.0% (full close)
  - [ ] Can set to 5.5%
  - [ ] Validation works correctly

- [ ] Edit existing rule with share adjustment action
  - [ ] Loads saved target_percent value
  - [ ] Can modify value
  - [ ] Saves changes correctly

- [ ] View rule in ruleset test page
  - [ ] Target percent displays in action summary
  - [ ] Format: "Target: X.X% of equity"
  - [ ] Shows in action details expansion

### Integration Testing

- [ ] Rule evaluation with INCREASE_INSTRUMENT_SHARE
  - [ ] target_percent passed to TradeAction
  - [ ] Action executes with correct target
  - [ ] Respects safety constraints

- [ ] Rule evaluation with DECREASE_INSTRUMENT_SHARE
  - [ ] target_percent passed to TradeAction
  - [ ] Action executes with correct target
  - [ ] Handles 0% (full close) correctly

## Benefits

### For Users
1. **Clear Configuration**: Explicitly set target allocation percentages
2. **Validation**: Prevents invalid values before rule creation
3. **Visual Feedback**: See target percent in rule evaluation results
4. **Help Text**: Understand constraints and limitations

### For System
1. **Type Safety**: Validation ensures numeric values within range
2. **Explicit Intent**: Clear what action should accomplish
3. **Debugging**: Easy to see what target was configured
4. **Consistency**: Same pattern as other configurable actions

## Related Documentation

- **INSTRUMENT_ACCOUNT_SHARE_SUMMARY.md** - Overall feature documentation
- **INSTRUMENT_ACCOUNT_SHARE_FEATURE.md** - Original implementation details
- **TradeActions.py** - Execution logic for share adjustment actions
- **types.py** - Action type definitions and helper functions

## Future Enhancements

Potential improvements:

1. **Dynamic Max Validation**: Validate against account's `max_virtual_equity_per_instrument_percent` setting
2. **Current Position Display**: Show current position % when configuring rule
3. **Preview Calculation**: Show estimated shares to buy/sell based on target %
4. **Templates**: Preset common target percent values (5%, 10%, 15%, etc.)
5. **Percentage Selector**: Dropdown with common values + custom option
