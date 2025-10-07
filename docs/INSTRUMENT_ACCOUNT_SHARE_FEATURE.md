# Instrument Account Share - Position Sizing & Rebalancing Feature

## Overview
New trade condition and actions for managing instrument position sizes relative to expert virtual equity. Enables dynamic portfolio rebalancing, position scaling, and risk management based on percentage allocation.

## Components Added

### 1. Trade Condition: `N_INSTRUMENT_ACCOUNT_SHARE`

**Purpose**: Check current instrument value as percentage of expert virtual equity

**Type**: Numeric (comparison-based)

**Calculation**:
```
Current Position Value = abs(position_quantity) × current_price
Virtual Equity = expert.get_available_balance()
Share Percentage = (Current Position Value / Virtual Equity) × 100
```

**Usage in Rules**:
```
IF instrument_account_share > 15.0 THEN decrease_instrument_share to 10.0
IF instrument_account_share < 5.0 AND confidence >= 80 THEN increase_instrument_share to 8.0
```

**Class**: `InstrumentAccountShareCondition` (in `TradeConditions.py`)

**Features**:
- Returns 0% if no position exists
- Uses absolute position quantity (handles both long and short)
- Gets real-time virtual equity from expert
- Logs calculation details for debugging

### 2. Action: `INCREASE_INSTRUMENT_SHARE`

**Purpose**: Increase position size to reach target allocation percentage

**Type**: Order-creating action (BUY or SELL depending on existing position)

**Parameters**:
- `target_percent` (float): Target percentage of virtual equity (e.g., 15.0 for 15%)

**Logic Flow**:
```python
1. Get virtual equity from expert
2. Calculate target_value = virtual_equity × (target_percent / 100)
3. Get current position value
4. Calculate additional_value = target_value - current_value
5. Check if additional_value > 0 (else skip - already at/above target)
6. Validate against available balance
7. Validate against max_virtual_equity_per_instrument_percent setting
8. Calculate additional_qty = additional_value / current_price
9. Round to minimum 1 share
10. Create market order (BUY for long, SELL for short)
```

**Safety Constraints**:
- ✅ Respects `max_virtual_equity_per_instrument_percent` expert setting (default 10%)
- ✅ Cannot exceed available account balance
- ✅ Logs warning and caps at max if target exceeds limit
- ✅ Skips action if already at or above target
- ✅ Minimum order size of 1 share

**Class**: `IncreaseInstrumentShareAction` (in `TradeActions.py`)

**Example Usage**:
```
Rule: High Confidence Scale-In
Condition: confidence >= 85% AND instrument_account_share < 10%
Action: INCREASE_INSTRUMENT_SHARE target_percent=12%
Result: Buys additional shares to bring position to 12% of portfolio
```

### 3. Action: `DECREASE_INSTRUMENT_SHARE`

**Purpose**: Decrease position size to reach target allocation percentage

**Type**: Order-creating action (SELL for long, BUY for short)

**Parameters**:
- `target_percent` (float): Target percentage of virtual equity (e.g., 5.0 for 5%, 0.0 to close)

**Logic Flow**:
```python
1. Get virtual equity from expert
2. Calculate target_value = virtual_equity × (target_percent / 100)
3. Get current position value
4. Calculate reduction_value = current_value - target_value
5. Check if reduction_value > 0 (else skip - already at/below target)
6. Calculate reduction_qty = reduction_value / current_price
7. If target_percent > 0: ensure remaining_qty >= 1 (minimum position)
8. Create market order (SELL for long, BUY for short)
```

**Safety Constraints**:
- ✅ Maintains minimum 1 share if `target_percent > 0`
- ✅ Allows full close if `target_percent = 0`
- ✅ Skips action if already at or below target
- ✅ Prevents reduction if it would violate minimum 1 share rule

**Class**: `DecreaseInstrumentShareAction` (in `TradeActions.py`)

**Example Usage**:
```
Rule: Portfolio Rebalancing
Condition: instrument_account_share > 15%
Action: DECREASE_INSTRUMENT_SHARE target_percent=10%
Result: Sells shares to bring position down to 10% of portfolio

Rule: Partial Profit Taking
Condition: profit_loss_percent >= 20% AND instrument_account_share > 8%
Action: DECREASE_INSTRUMENT_SHARE target_percent=5%
Result: Reduces position while maintaining exposure
```

## Files Modified

### 1. `ba2_trade_platform/core/types.py`
**Changes**:
- Added `N_INSTRUMENT_ACCOUNT_SHARE` to `ExpertEventType` enum
- Added `INCREASE_INSTRUMENT_SHARE` to `ExpertActionType` enum
- Added `DECREASE_INSTRUMENT_SHARE` to `ExpertActionType` enum
- Updated `get_numeric_event_values()` to include new condition

### 2. `ba2_trade_platform/core/TradeConditions.py`
**Changes**:
- Added `InstrumentAccountShareCondition` class (89 lines)
  - `evaluate()`: Compares position share to threshold
  - `_get_instrument_position_value()`: Calculates position market value
  - `_get_expert_virtual_equity()`: Gets expert's available balance
  - `get_description()`: Human-readable description
- Updated condition factory `create_condition()` to map new event type

### 3. `ba2_trade_platform/core/TradeActions.py`
**Changes**:
- Added `IncreaseInstrumentShareAction` class (174 lines)
  - Validates target percentage
  - Checks max allocation limits
  - Calculates additional quantity needed
  - Creates market order to increase position
- Added `DecreaseInstrumentShareAction` class (185 lines)
  - Validates target percentage
  - Ensures minimum 1 share (if not closing)
  - Calculates reduction quantity
  - Creates market order to decrease position
- Updated action factory `create_action()` to map new action types

### 4. `ba2_trade_platform/core/rules_documentation.py`
**Changes**:
- Added documentation for `N_INSTRUMENT_ACCOUNT_SHARE` event type
- Added documentation for `INCREASE_INSTRUMENT_SHARE` action
- Added documentation for `DECREASE_INSTRUMENT_SHARE` action
- Includes use cases, parameters, and examples for each

## Use Cases

### Use Case 1: Portfolio Diversification
**Problem**: Single position becomes too large relative to portfolio
**Rule**:
```
Condition: instrument_account_share > 15%
Action: DECREASE_INSTRUMENT_SHARE target_percent=10%
```
**Result**: Automatically rebalances oversized positions to maintain diversification

### Use Case 2: Confidence-Based Scaling
**Problem**: Want larger positions in high-confidence trades
**Rules**:
```
Rule 1: Scale Up
Condition: confidence >= 85% AND instrument_account_share < 10%
Action: INCREASE_INSTRUMENT_SHARE target_percent=12%

Rule 2: Scale Down
Condition: confidence < 70% AND instrument_account_share > 5%
Action: DECREASE_INSTRUMENT_SHARE target_percent=3%
```
**Result**: Position size dynamically adjusts based on expert confidence

### Use Case 3: Gradual Position Building
**Problem**: Want to accumulate position over time without large orders
**Rules**:
```
Rule 1: Initial Entry
Condition: bullish AND has_no_position AND confidence >= 75%
Action: INCREASE_INSTRUMENT_SHARE target_percent=5%

Rule 2: First Add
Condition: days_opened >= 7 AND profit_loss_percent >= 3% AND instrument_account_share < 8%
Action: INCREASE_INSTRUMENT_SHARE target_percent=8%

Rule 3: Full Position
Condition: days_opened >= 14 AND profit_loss_percent >= 5% AND instrument_account_share < 12%
Action: INCREASE_INSTRUMENT_SHARE target_percent=12%
```
**Result**: Builds position gradually as conviction and profits confirm thesis

### Use Case 4: Risk-Adjusted Rebalancing
**Problem**: Need to reduce exposure based on risk level
**Rules**:
```
Rule 1: High Risk Cap
Condition: highrisk AND instrument_account_share > 8%
Action: DECREASE_INSTRUMENT_SHARE target_percent=6%

Rule 2: Medium Risk Cap
Condition: mediumrisk AND instrument_account_share > 12%
Action: DECREASE_INSTRUMENT_SHARE target_percent=10%

Rule 3: Low Risk Allow
Condition: lowrisk AND confidence >= 80% AND instrument_account_share < 15%
Action: INCREASE_INSTRUMENT_SHARE target_percent=15%
```
**Result**: Position sizes respect risk levels

### Use Case 5: Profit-Taking Ladder
**Problem**: Want to take profits incrementally as position appreciates
**Rules**:
```
Rule 1: First Target
Condition: profit_loss_percent >= 10% AND instrument_account_share > 8%
Action: DECREASE_INSTRUMENT_SHARE target_percent=6%

Rule 2: Second Target
Condition: profit_loss_percent >= 20% AND instrument_account_share > 6%
Action: DECREASE_INSTRUMENT_SHARE target_percent=4%

Rule 3: Third Target
Condition: profit_loss_percent >= 30% AND instrument_account_share > 4%
Action: DECREASE_INSTRUMENT_SHARE target_percent=2%
```
**Result**: Systematically reduces position as profits accumulate

## Safety Features

### 1. Maximum Allocation Enforcement
```python
max_percent_per_instrument = expert.settings.get('max_virtual_equity_per_instrument_percent', 10.0)
if target_percent > max_percent_per_instrument:
    logger.warning(f"Target {target_percent}% exceeds max {max_percent_per_instrument}%. Using max.")
    target_percent = max_percent_per_instrument
```
**Protection**: Cannot create positions larger than expert's max setting

### 2. Available Balance Check
```python
account_balance = self.account.get_account_info().get('buying_power', 0)
if additional_value > account_balance:
    logger.warning(f"Additional value ${additional_value:.2f} exceeds available ${account_balance:.2f}")
    additional_value = account_balance
```
**Protection**: Cannot exceed available buying power

### 3. Minimum Position Size
```python
remaining_qty = abs(current_position_qty) - reduction_qty
if target_percent > 0 and remaining_qty < 1:
    reduction_qty = abs(current_position_qty) - 1  # Keep 1 share
```
**Protection**: Maintains minimum 1 share unless fully closing (target=0%)

### 4. No-Op Detection
```python
if additional_value <= 0:
    return TradeActionResult(success=False, message="Position already at target")
```
**Protection**: Skips unnecessary orders if already at target

## Integration with Existing Systems

### Virtual Equity Calculation
Uses expert's `get_available_balance()` method:
```python
from .utils import get_expert_instance_from_id
expert = get_expert_instance_from_id(expert_instance_id)
virtual_equity = expert.get_available_balance()
```
**Source**: Account balance × expert's `virtual_equity_pct` setting

### Expert Settings
Respects existing `max_virtual_equity_per_instrument_percent` setting:
- Default: 10% (recommended 5-15%)
- Configurable per expert instance
- Enforced in `IncreaseInstrumentShareAction`

### Order Creation
Uses standard order creation pipeline:
```python
order = self.create_order_record(
    side=side,
    quantity=additional_qty,
    order_type="market"
)
order_id = add_instance(order)
```
**Benefits**:
- Proper transaction linking
- Expert recommendation association
- Risk management integration
- Order status tracking

## Testing Checklist

### Unit Tests
- [ ] `InstrumentAccountShareCondition.evaluate()` with various position sizes
- [ ] `InstrumentAccountShareCondition._get_instrument_position_value()` edge cases
- [ ] `InstrumentAccountShareCondition._get_expert_virtual_equity()` error handling
- [ ] `IncreaseInstrumentShareAction.execute()` with valid target
- [ ] `IncreaseInstrumentShareAction` max allocation enforcement
- [ ] `IncreaseInstrumentShareAction` available balance check
- [ ] `DecreaseInstrumentShareAction.execute()` with valid target
- [ ] `DecreaseInstrumentShareAction` minimum 1 share enforcement
- [ ] `DecreaseInstrumentShareAction` full close (target=0%)

### Integration Tests
- [ ] Create rule with `instrument_account_share` condition
- [ ] Execute `INCREASE_INSTRUMENT_SHARE` action in enter_market ruleset
- [ ] Execute `DECREASE_INSTRUMENT_SHARE` action in open_positions ruleset
- [ ] Verify order creation with correct quantity
- [ ] Verify max percent limit enforced
- [ ] Verify minimum 1 share maintained
- [ ] Test with long and short positions
- [ ] Test with zero balance (should fail gracefully)

### End-to-End Tests
- [ ] Build position from 0% to 12% using multiple increase actions
- [ ] Reduce position from 15% to 5% using decrease action
- [ ] Rebalance portfolio with multiple instruments
- [ ] Verify transaction linking and recommendation association
- [ ] Check logging output for debugging

## Example Rule Configurations

### Ruleset: Adaptive Position Sizing
```yaml
name: "Adaptive Position Sizing"
use_case: "open_positions"

rules:
  - name: "Scale Up on High Confidence"
    conditions:
      - event: confidence
        operator: ">="
        value: 85
      - event: instrument_account_share
        operator: "<"
        value: 10
    actions:
      - type: INCREASE_INSTRUMENT_SHARE
        parameters:
          target_percent: 12
  
  - name: "Rebalance Oversized"
    conditions:
      - event: instrument_account_share
        operator: ">"
        value: 15
    actions:
      - type: DECREASE_INSTRUMENT_SHARE
        parameters:
          target_percent: 10
  
  - name: "Scale Down on Low Confidence"
    conditions:
      - event: confidence
        operator: "<"
        value: 65
      - event: instrument_account_share
        operator: ">"
        value: 3
    actions:
      - type: DECREASE_INSTRUMENT_SHARE
        parameters:
          target_percent: 2
```

### Ruleset: Risk-Adjusted Allocation
```yaml
name: "Risk-Adjusted Allocation"
use_case: "open_positions"

rules:
  - name: "High Risk - Cap at 6%"
    conditions:
      - event: highrisk
        value: true
      - event: instrument_account_share
        operator: ">"
        value: 6
    actions:
      - type: DECREASE_INSTRUMENT_SHARE
        parameters:
          target_percent: 5
  
  - name: "Low Risk - Allow 15%"
    conditions:
      - event: lowrisk
        value: true
      - event: confidence
        operator: ">="
        value: 80
      - event: instrument_account_share
        operator: "<"
        value: 15
    actions:
      - type: INCREASE_INSTRUMENT_SHARE
        parameters:
          target_percent: 15
```

## Performance Considerations

### Database Queries
- **Condition Evaluation**: 1 query to get positions, 1 for expert instance
- **Action Execution**: 1 query for expert instance, 1 for account info, 1 to create order
- **Total**: ~3-5 queries per evaluation/execution cycle

### Calculation Overhead
- Position value calculation: O(1) - simple multiplication
- Virtual equity retrieval: O(1) - cached in expert instance
- Quantity calculation: O(1) - simple arithmetic
- **Total**: Negligible computational overhead

### Order Volume
- Each action creates at most 1 order
- Orders are market orders (fill quickly)
- No dependency chains or complex sequences
- **Impact**: Minimal on order management system

## Migration Notes

### Backward Compatibility
✅ **100% Backward Compatible**:
- No changes to existing event types or actions
- No database schema changes
- No changes to existing rulesets
- New features are opt-in

### For Existing Experts
1. No action required - new condition/actions available immediately
2. Can add new rules without modifying existing ones
3. `max_virtual_equity_per_instrument_percent` setting already exists (default 10%)

## Future Enhancements

### Potential Improvements
1. **Target Position Schedules**: Gradually adjust targets over time
2. **Multi-Instrument Rebalancing**: Coordinate across correlated positions
3. **Volatility-Adjusted Sizing**: Scale positions based on instrument volatility
4. **Sector Limits**: Cap total allocation per sector/industry
5. **Correlation Awareness**: Reduce correlated positions together
6. **Dynamic Max Percent**: Adjust max allocation based on market conditions

## Conclusion

The Instrument Account Share feature provides powerful portfolio management capabilities:

✅ **Position Sizing**: Dynamic allocation based on conviction and risk  
✅ **Rebalancing**: Automatic portfolio diversification  
✅ **Risk Management**: Enforces max allocation limits  
✅ **Scaling**: Gradual position building and reduction  
✅ **Safety**: Multiple constraints prevent over-allocation  

This enables sophisticated, institutional-grade portfolio management while maintaining simplicity and safety through built-in constraints and clear documentation.
