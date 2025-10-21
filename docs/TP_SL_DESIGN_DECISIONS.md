# TP/SL Percent Storage: Design Decisions

## Key Decision: Move Logic to AccountInterface (Not Account-Specific)

**Decision**: Implement `_ensure_tp_sl_percent_stored()` in `AccountInterface` base class, not in `AlpacaAccount`.

**Rationale**:
1. **Account-Agnostic**: Logic applies identically to all account implementations (Alpaca, other brokers)
2. **DRY Principle**: Don't duplicate fallback calculation logic across multiple account classes
3. **Centralized**: Single source of truth for TP/SL percent handling
4. **Extensible**: New account implementations automatically get this behavior

## Key Decision: Fallback Calculation Strategy

**Decision**: If percent not stored in `order.data`, calculate it during submission from current `limit_price`/`stop_price`.

**Rationale**:
1. **Backward Compatibility**: Handles orders created before this feature existed
2. **Resilience**: Works even if TradeActionEvaluator didn't store the percent
3. **Audit Trail**: Logs "FALLBACK calculation" so we know when this happened
4. **No Data Loss**: Better to recalculate than to fail silently

## Key Decision: Three-Layer Calculation Approach

**Layers** (in order of preference):
1. **TradeActionEvaluator** → Stores percent when action is evaluated (PRIMARY)
2. **_submit_pending_tp_sl_orders** → Fallback calculation if not stored (SECONDARY)
3. **_check_all_waiting_trigger_orders** → Uses stored percent, logs if missing (TERTIARY)

**Benefit**: Guaranteed percent is available by the time order is submitted, regardless of where it came from.

## Code Locations

### _ensure_tp_sl_percent_stored()
- **Location**: `AccountInterface.py` (line ~314)
- **Called From**: 
  - `_submit_pending_tp_sl_orders()` (before _set_order_tp_impl)
  - `_submit_pending_tp_sl_orders()` (before _set_order_sl_impl)
- **Logic**: Calculate from limit_price/stop_price if not in data

### _set_order_tp_impl() / _set_order_sl_impl()
- **Location**: `AlpacaAccount.py` (lines ~860 and ~1060)
- **Logic**: Calculate percent from tp_price/sl_price, store in order.data

### _check_all_waiting_trigger_orders()
- **Location**: `TradeManager.py` (line ~390)
- **Logic**: Recalculate price from stored percent when parent order is FILLED
- **Fallback**: If no percent in data, log debug message

## Percent Storage Format

```python
# For TP orders
order.data = {
    "type": "tp",
    "tp_percent": 12.0,           # Stored as 1-100 scale (not 0-1)
    "parent_filled_price": 239.69,
    "recalculated_at_trigger": False
}

# For SL orders
order.data = {
    "type": "sl",
    "sl_percent": -5.0,           # Negative for stop-loss (below entry)
    "parent_filled_price": 239.69,
    "recalculated_at_trigger": False
}
```

## Example: End-to-End Flow

### Scenario: AMD Order 339 (Fixed)

```
Step 1: TradeActionEvaluator evaluates recommendation
  - TP recommendation: 12% gain
  
Step 2: AdjustTakeProfitAction._set_order_tp_impl() called
  - Parent order.open_price = $239.69 (filled)
  - tp_price = $268.45 (calculated: 239.69 * 1.12)
  - tp_percent = 12.0
  - Store in tp_order.data["tp_percent"] = 12.0
  - Log: "Calculated TP percent: 12.00% from filled price $239.69 to target $268.45"
  
Step 3: TP order saved as WAITING_TRIGGER
  - tp_order.limit_price = $268.45
  - tp_order.data = {"type": "tp", "tp_percent": 12.0, "parent_filled_price": 239.69}
  
Step 4: Parent order FILLED at $239.69
  
Step 5: TradeManager._check_all_waiting_trigger_orders() runs
  - Finds TP order in WAITING_TRIGGER status
  - Reads tp_order.data["tp_percent"] = 12.0 ✓
  - Recalculates: 239.69 * (1 + 0.12) = $268.45
  - TP price is CORRECT, submits to broker
  - Log: "Recalculated TP price: parent filled $239.69 * (1 + 12.00%) = $268.45"
  
Result: ✓ TP executes at correct $268.45, not $240.80
```

## Testing the Implementation

### Test 1: Percent Stored During Action Execution
```python
# Create TP order through AdjustTakeProfitAction
# Verify: order.data["tp_percent"] contains correct value
# Verify: Log shows "Calculated TP percent"
```

### Test 2: Fallback Calculation at Submission
```python
# Create order without percent (old order scenario)
# Call _submit_pending_tp_sl_orders()
# Verify: order.data["tp_percent"] is populated
# Verify: Log shows "FALLBACK calculation"
```

### Test 3: Recalculation on Trigger
```python
# Set TP with percent stored
# Trigger parent order to FILLED
# Verify: TP order.limit_price recalculated from percent
# Verify: Log shows "Recalculated TP price"
```

### Test 4: Market Price Independence
```python
# Create TP at $268.45 (12% above filled $239.69)
# Market price drops to $200
# Trigger parent order
# Verify: TP still $268.45 (not affected by market price drop)
```

## Common Issues & Solutions

### Issue: Percent not stored in order.data
**Cause**: TradeActionEvaluator didn't calculate it  
**Solution**: _ensure_tp_sl_percent_stored() fallback will calculate it  
**Log**: "Calculated and stored TP percent... - FALLBACK calculation"

### Issue: TP price wrong when triggered
**Cause**: Using market price instead of stored percent  
**Solution**: Verify percent is stored, check TradeManager recalculation logic  
**Debug**: Look for "Recalculated TP price" log

### Issue: Fallback calculation says "Cannot calculate TP percent"
**Cause**: parent.open_price or tp_order.limit_price is None  
**Solution**: Debug why these prices aren't set  
**Check**: Order status, transaction state, broker fill data

## Migration Path

1. **Phase 1** (Current): Store percent in data field, fallback calculation ✓ DONE
2. **Phase 2** (Next): Update TradeActionEvaluator to store percent at evaluation time
3. **Phase 3** (Future): Remove AlpacaAccount-specific calculation, use only AccountInterface
4. **Phase 4** (Future): Historical analysis of TP/SL slippage using order.data
