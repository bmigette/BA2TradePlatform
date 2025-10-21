# TP/SL Percent Storage and Recalculation Architecture

**Status**: Implementation Complete (Phase 1)  
**Date**: October 21, 2025  
**Related Issue**: AMD Order 339 TP Price Discrepancy

## Problem Statement

Take Profit (TP) and Stop Loss (SL) orders were using stale market prices instead of the filled entry price when being triggered. This caused:
- TP order for order 339 to be created at $240.80 instead of $268.45
- $27.65 loss per share due to using market bid ($229.33) instead of filled price ($239.69)

## Root Cause

The `AdjustTakeProfitAction` and related classes used `ReferenceValue.CURRENT_PRICE` (market price) as the default reference instead of `ReferenceValue.ORDER_OPEN_PRICE` (filled price).

## Solution Architecture

### 1. Data Field Addition (✅ COMPLETED)

**File**: `ba2_trade_platform/core/models.py` (TradingOrder model)

Added optional JSON field to store TP/SL metadata:
```python
data: dict | None = Field(
    sa_column=Column(JSON), 
    default=None, 
    description="Optional order metadata (e.g., TP/SL percent for WAITING_TRIGGER orders)"
)
```

**Data Structure**:
```python
{
    "type": "tp" | "sl",           # Order type marker
    "tp_percent": float,           # TP percentage from filled price
    "sl_percent": float,           # SL percentage from filled price
    "parent_filled_price": float,  # Parent order's filled price at creation time
    "recalculated_at_trigger": bool,  # Whether price was recalculated when triggered
    "calculation_timestamp": str   # ISO 8601 timestamp of calculation
}
```

### 2. Percent Calculation at Action Execution (✅ IMPLEMENTED)

**Files**: 
- `ba2_trade_platform/modules/accounts/AlpacaAccount.py` - `_set_order_tp_impl` and `_create_tp_order_object`
- `ba2_trade_platform/modules/accounts/AlpacaAccount.py` - `_set_order_sl_impl` and `_create_sl_order_object`

When TradeActionEvaluator calls `_set_order_tp_impl` or `_set_order_sl_impl`:
1. Calculate percent: `(tp_price - parent.open_price) / parent.open_price * 100`
2. Pass percent to `_create_tp_order_object` or `_create_sl_order_object`
3. Store percent in order.data field

**Example**:
```
TP calculation: $239.69 (filled) × (1 + 12.0%) = $268.45 (target)
Stored percent: 12.0%
```

### 3. Fallback Percent Calculation at Submission (✅ IMPLEMENTED)

**File**: `ba2_trade_platform/core/interfaces/AccountInterface.py`

New method: `_ensure_tp_sl_percent_stored(tp_or_sl_order, parent_order)`

**When called**: From `_submit_pending_tp_sl_orders` before creating TP/SL orders

**Logic**:
1. Check if percent already stored in order.data
2. If NOT stored (e.g., old orders or orders created before this feature):
   - Calculate percent from current limit_price/stop_price
   - Store in order.data with parent_filled_price
   - Log as "FALLBACK calculation"
3. If already stored: Skip (use existing value)

**Log Example**:
```
Calculated and stored TP percent for order 123: 12.00% 
(parent filled $239.69 → TP target $268.45) - FALLBACK calculation
```

### 4. Price Recalculation on Trigger (✅ IMPLEMENTED)

**Files**:
- `ba2_trade_platform/core/TradeManager.py` - `_check_all_waiting_trigger_orders`
- `ba2_trade_platform/core/interfaces/AccountInterface.py` - `_submit_pending_tp_sl_orders` (fallback)

**When triggered**:
1. Parent order reaches FILLED status
2. WAITING_TRIGGER order detected
3. Check order.data for tp_percent or sl_percent
4. If found:
   - Recalculate: `new_price = parent.open_price * (1 ± percent/100)`
   - Update limit_price (for TP) or stop_price (for SL)
   - Update order.data["recalculated_at_trigger"] = True
   - Log with before/after prices
5. If NOT found:
   - Log debug message (will be calculated during submission)

**Log Example**:
```
Recalculated TP price for order 123: 
parent filled $239.69 * (1 + 12.00%) = $268.45 (was $240.80)
```

## Data Flow

### Scenario 1: Normal Flow (Percent Stored During Action Evaluation)

```
TradeActionEvaluator.evaluate()
    ↓
AdjustTakeProfitAction.execute()
    ↓
account._set_order_tp_impl(order, tp_price)
    ├─ Calculate percent from tp_price and order.open_price
    └─ Pass percent to _create_tp_order_object(order, tp_price, percent)
        └─ Store percent in tp_order.data["tp_percent"]
            ↓
            [Order saved to database with percent stored]
            ↓
Parent order FILLED
    ↓
TradeManager._check_all_waiting_trigger_orders()
    ├─ Find WAITING_TRIGGER TP order
    ├─ Get percent from order.data["tp_percent"]
    ├─ Recalculate: new_price = parent.open_price * (1 + percent/100)
    ├─ Update tp_order.limit_price
    └─ Submit to broker with correct price
```

### Scenario 2: Fallback Flow (Percent Calculated at Submission)

```
Old order exists without percent stored
    ↓
Transaction.take_profit set
    ↓
AccountInterface._submit_pending_tp_sl_orders()
    ├─ Call _ensure_tp_sl_percent_stored(order, order)
    │   ├─ Check if "tp_percent" in order.data → NOT FOUND
    │   ├─ Calculate from current limit_price
    │   └─ Store in order.data as FALLBACK
    │
    └─ Call _set_order_tp_impl() with percent now available
        └─ TP order created/updated with percent in data
            ↓
            [Order saved with percent now populated]
            ↓
            [Recalculation will work on next trigger]
```

## Code Changes Summary

### Modified Files

1. **`ba2_trade_platform/core/models.py`**
   - Added `data: dict | None` field to TradingOrder

2. **`ba2_trade_platform/modules/accounts/AlpacaAccount.py`**
   - Updated `_set_order_tp_impl()` to calculate tp_percent
   - Updated `_create_tp_order_object()` to accept and store tp_percent
   - Added `_set_order_sl_impl()` (NEW) for SL orders
   - Added `_create_sl_order_object()` (NEW) for SL orders
   - Added `_find_existing_sl_order()` (NEW) helper

3. **`ba2_trade_platform/core/interfaces/AccountInterface.py`**
   - Added `_ensure_tp_sl_percent_stored()` (NEW) - fallback calculation
   - Updated `_submit_pending_tp_sl_orders()` to call _ensure_tp_sl_percent_stored

4. **`ba2_trade_platform/core/TradeManager.py`**
   - Added price recalculation logic in `_check_all_waiting_trigger_orders()`
   - Recalculates TP/SL prices from stored percent when triggering

## Logging

Comprehensive logging at each step:

1. **Percent Calculation** (during action evaluation):
   ```
   Calculated TP percent: 12.00% from filled price $239.69 to target $268.45 for AMD
   ```

2. **Percent Storage** (in _create_tp_order_object):
   ```
   Created WAITING_TRIGGER TP order 123 at $268.45 with metadata: tp_percent=12.00%
   ```

3. **Fallback Calculation** (during submission):
   ```
   Calculated and stored TP percent for order 123: 12.00% 
   (parent filled $239.69 → TP target $268.45) - FALLBACK calculation
   ```

4. **Price Recalculation** (on trigger):
   ```
   Recalculated TP price for order 123: parent filled $239.69 * (1 + 12.00%) = $268.45 (was $240.80)
   ```

## Key Benefits

1. **Account-Agnostic**: Logic in AccountInterface applies to all account implementations
2. **Resilient**: Works even if percent not stored initially (fallback calculation)
3. **Immutable Reference**: Parent filled price stored at creation, not subject to market changes
4. **Auditable**: Full history in logs and order.data field
5. **Extensible**: Easy to add more metadata in the future
6. **Bug-Proof**: Cannot use wrong reference value (stored percent enforces it)

## Testing Checklist

- [ ] Create TP order → verify percent stored in order.data
- [ ] Create SL order → verify percent stored in order.data
- [ ] Trigger WAITING_TRIGGER TP order → verify limit_price recalculated
- [ ] Trigger WAITING_TRIGGER SL order → verify stop_price recalculated
- [ ] Old order without percent → verify fallback calculation works
- [ ] Log output shows all calculation steps
- [ ] Multiple TP/SL orders on same transaction work correctly
- [ ] Percent recalculation uses parent.open_price, not current market price

## Future Improvements

1. **Store in TradeActionEvaluator**: Instead of recalculating in _set_order_tp_impl, calculate once in TradeActionEvaluator and pass directly
2. **Configuration Option**: Allow per-account or per-expert override of recalculation behavior
3. **Historical Analysis**: Query order.data to analyze TP/SL slippage patterns
4. **Notification**: Alert when TP/SL would have triggered at original price but market moved

## References

- Issue: AMD Order 339 TP Price Discrepancy ($27.65 loss)
- Root Cause: ReferenceValue.CURRENT_PRICE default instead of ORDER_OPEN_PRICE
- Documentation: docs/AMD_ORDER_339_TP_PRICE_BUG_FIXED.md
