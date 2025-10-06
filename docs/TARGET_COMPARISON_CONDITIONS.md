# Target Comparison Conditions for Open Positions

**Date:** October 6, 2025  
**Feature:** New conditions for comparing current TP prices with new expert recommendations

## Overview

Added new conditions to enable intelligent TP/SL adjustments for open positions by comparing the current take-profit target with the new expert recommendation's target price.

## New Event Types

### Numeric Conditions

#### 1. `N_PERCENT_TO_CURRENT_TARGET` (renamed from `N_PERCENT_TO_TARGET`)
**Purpose:** Calculate distance from current market price to the existing TP price.

**Use Case:** Check if current price is close to hitting the existing TP target.

**Example:**
```python
# Trigger when price is within 5% of current TP
event_type: N_PERCENT_TO_CURRENT_TARGET
operator: <=
value: 5.0
```

**Calculation:**
```python
percent_to_current_target = ((current_tp_price - current_price) / current_price) * 100
```

**Log Output:**
```
Percent to CURRENT target for AAPL: current=$100.00, TP=$110.00, distance=+10.00%
```

---

#### 2. `N_PERCENT_TO_NEW_TARGET` (new)
**Purpose:** Calculate distance from current market price to the new expert's target price.

**Use Case:** Evaluate if the new expert target is realistic or too far from current price.

**Example:**
```python
# Trigger when new target is more than 15% away
event_type: N_PERCENT_TO_NEW_TARGET
operator: >
value: 15.0
```

**Calculation:**
```python
# For BUY recommendations
new_target_price = price_at_date * (1 + expected_profit_percent / 100)

# For SELL recommendations
new_target_price = price_at_date * (1 - expected_profit_percent / 100)

percent_to_new_target = ((new_target_price - current_price) / current_price) * 100
```

**Log Output:**
```
Percent to NEW target for AAPL: current=$100.00, new_target=$125.00 (base=$100.00, profit=25.0%), distance=+25.00%
```

---

### Flag Conditions

#### 3. `F_NEW_TARGET_HIGHER` (new)
**Purpose:** Check if the new expert target is significantly higher than the current TP.

**Tolerance:** 2% (configurable via `NewTargetHigherCondition.TOLERANCE_PERCENT`)

**Use Case:** Increase TP when expert becomes more bullish.

**Example:**
```python
# Trigger: New target is higher than current TP
event_type: F_NEW_TARGET_HIGHER

# Action: Adjust TP to new target + 2%
action: ADJUST_TAKE_PROFIT
reference_value: expert_target_price
value: 2.0
```

**Calculation:**
```python
percent_diff = ((new_target_price - current_tp_price) / current_tp_price) * 100
is_higher = percent_diff > 2.0  # TOLERANCE_PERCENT
```

**Log Output:**
```
New target comparison for AAPL: current_TP=$110.00, new_target=$125.00, diff=+13.64%, is_higher=True (tolerance=2.0%)
```

---

#### 4. `F_NEW_TARGET_LOWER` (new)
**Purpose:** Check if the new expert target is significantly lower than the current TP.

**Tolerance:** 2% (configurable via `NewTargetLowerCondition.TOLERANCE_PERCENT`)

**Use Case:** Reduce TP when expert becomes less optimistic, or close position early.

**Example:**
```python
# Trigger: New target is lower than current TP
event_type: F_NEW_TARGET_LOWER

# Action: Close position to lock in profits
action: CLOSE
```

**Calculation:**
```python
percent_diff = ((new_target_price - current_tp_price) / current_tp_price) * 100
is_lower = percent_diff < -2.0  # -TOLERANCE_PERCENT
```

**Log Output:**
```
New target comparison for TSLA: current_TP=$250.00, new_target=$230.00, diff=-8.00%, is_lower=True (tolerance=2.0%)
```

---

## Use Cases

### Use Case 1: Progressive TP Increase
**Scenario:** Expert becomes more bullish, increase TP to new target.

```
Rule: "Increase TP when expert raises target"
Triggers:
  - has_position (F_HAS_POSITION)
  - new_target_higher (F_NEW_TARGET_HIGHER)
  
Actions:
  - ADJUST_TAKE_PROFIT
    reference_value: expert_target_price
    value: 0.0  # Use expert target exactly
```

**Example Flow:**
1. Position opened at $100, initial TP at $110 (expert: +10%)
2. New recommendation: BUY at $105, expected_profit: +20%
3. New target: $105 * 1.20 = $126
4. Comparison: $126 vs $110 = +14.5% → `F_NEW_TARGET_HIGHER` = True
5. Action: Adjust TP to $126

---

### Use Case 2: Early Exit on Reduced Target
**Scenario:** Expert lowers profit expectation, close position early to secure gains.

```
Rule: "Close position when target drops significantly"
Triggers:
  - has_position (F_HAS_POSITION)
  - new_target_lower (F_NEW_TARGET_LOWER)
  - profit_loss_percent >= 5% (N_PROFIT_LOSS_PERCENT)
  
Actions:
  - CLOSE
```

**Example Flow:**
1. Position opened at $100, current price $108, TP at $115 (expert: +15%)
2. New recommendation: BUY at $108, expected_profit: +5%
3. New target: $108 * 1.05 = $113.40
4. Comparison: $113.40 vs $115 = -1.4% → `F_NEW_TARGET_LOWER` = False (within tolerance)
5. Update profit to +10%:
6. New target: $108 * 1.10 = $118.80
7. Comparison: $118.80 vs $115 = +3.3% → Still higher, don't close

Alternative:
1. Position at $108, TP at $115
2. New recommendation: expected_profit: -5% (revised down)
3. New target: $108 * 0.95 = $102.60
4. Comparison: $102.60 vs $115 = -10.8% → `F_NEW_TARGET_LOWER` = True
5. Current P/L: +8% → Close position early

---

### Use Case 3: Near Target Detection
**Scenario:** Price is approaching current TP, decide whether to keep or adjust.

```
Rule: "Reassess when near target"
Triggers:
  - has_position (F_HAS_POSITION)
  - percent_to_current_target <= 5% (N_PERCENT_TO_CURRENT_TARGET)
  
Actions:
  - If new_target_higher: Increase TP
  - If new_target_lower: Close position
```

---

## Implementation Details

### Transaction TP Price Retrieval
All new conditions retrieve the current TP price from the `Transaction` table:

```python
if self.existing_order.transaction_id:
    from .db import get_instance
    from .models import Transaction
    transaction = get_instance(Transaction, self.existing_order.transaction_id)
    if transaction and transaction.take_profit:
        current_tp_price = transaction.take_profit
```

### Expert Target Calculation
New target price is calculated using the same logic as `EXPERT_TARGET_PRICE` reference value:

```python
base_price = expert_recommendation.price_at_date
expected_profit = expert_recommendation.expected_profit_percent

# For BUY
new_target_price = base_price * (1 + expected_profit / 100)

# For SELL
new_target_price = base_price * (1 - expected_profit / 100)
```

### Tolerance Configuration
The 2% tolerance for `F_NEW_TARGET_HIGHER` and `F_NEW_TARGET_LOWER` is defined as a class constant:

```python
class NewTargetHigherCondition(FlagCondition):
    TOLERANCE_PERCENT = 2.0  # Can be modified if needed
```

To change tolerance globally, update this constant in both classes.

---

## Testing

### Test 1: Percent to Current Target
```python
# Setup
current_price = 100.0
current_tp = 110.0

# Expected result
percent_to_current_target = ((110 - 100) / 100) * 100 = +10.0%

# Trigger with operator <=
value: 15.0
Result: True (10.0 <= 15.0)
```

### Test 2: Percent to New Target
```python
# Setup
current_price = 100.0
expert_base_price = 98.0
expert_expected_profit = 20.0%
expert_action = BUY

# Calculation
new_target = 98.0 * (1 + 20.0/100) = 117.60
percent_to_new_target = ((117.60 - 100) / 100) * 100 = +17.60%

# Trigger with operator >
value: 15.0
Result: True (17.60 > 15.0)
```

### Test 3: New Target Higher
```python
# Setup
current_tp = 110.0
new_target = 125.0

# Calculation
percent_diff = ((125 - 110) / 110) * 100 = +13.64%

# Comparison
is_higher = 13.64 > 2.0
Result: True
```

### Test 4: New Target Lower
```python
# Setup
current_tp = 110.0
new_target = 105.0

# Calculation
percent_diff = ((105 - 110) / 110) * 100 = -4.55%

# Comparison
is_lower = -4.55 < -2.0
Result: True
```

---

## Migration Notes

### Breaking Change
`N_PERCENT_TO_TARGET` has been renamed to `N_PERCENT_TO_CURRENT_TARGET`.

**Impact:** Any existing rules using `N_PERCENT_TO_TARGET` will need to be updated.

**Migration Steps:**
1. Search for rules using `percent_to_target` in the database
2. Update event type to `percent_to_current_target`
3. Test rules to ensure they work as expected

**Database Query:**
```sql
-- Find affected rules
SELECT * FROM eventaction 
WHERE triggers LIKE '%percent_to_target%';

-- Update (example - adapt to your schema)
UPDATE eventaction 
SET triggers = REPLACE(triggers, '"event_type": "percent_to_target"', '"event_type": "percent_to_current_target"')
WHERE triggers LIKE '%percent_to_target%';
```

---

## Files Modified

- `ba2_trade_platform/core/types.py`
  - Renamed `N_PERCENT_TO_TARGET` → `N_PERCENT_TO_CURRENT_TARGET`
  - Added `N_PERCENT_TO_NEW_TARGET`
  - Added `F_NEW_TARGET_HIGHER`
  - Added `F_NEW_TARGET_LOWER`

- `ba2_trade_platform/core/TradeConditions.py`
  - Renamed `PercentToTargetCondition` → `PercentToCurrentTargetCondition`
  - Added `PercentToNewTargetCondition`
  - Added `NewTargetHigherCondition`
  - Added `NewTargetLowerCondition`
  - Updated `create_condition()` factory function

---

## Benefits

1. **✅ Intelligent TP Adjustment**: Automatically increase TP when expert becomes more optimistic
2. **✅ Early Exit Strategy**: Close positions when expert lowers expectations
3. **✅ Target Distance Tracking**: Monitor how close price is to both current and new targets
4. **✅ Tolerance-Based Logic**: 2% tolerance prevents unnecessary adjustments from minor fluctuations
5. **✅ Comprehensive Logging**: All calculations logged for debugging and verification
6. **✅ Open Positions Analysis**: Perfect for `open_positions` ruleset to manage existing trades
