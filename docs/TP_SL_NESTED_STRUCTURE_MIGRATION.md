# TP/SL Nested Data Structure Migration

**Date**: 2025-10-22  
**Status**: COMPLETE  
**Purpose**: Ensure TP/SL data uses a dedicated "TP_SL" namespace in `order.data` to avoid conflicts with expert recommendation data

---

## Problem Statement

Previously, TP/SL metadata was stored directly in `order.data`:
```json
{
  "tp_percent": 12.0,
  "sl_percent": -5.0,
  "parent_filled_price": 239.69,
  "type": "tp"
}
```

This flat structure could conflict with expert data stored in the same `order.data` field, causing data collisions and unpredictable behavior.

---

## Solution: Nested "TP_SL" Structure

All TP/SL data now uses a dedicated `"TP_SL"` namespace:

```json
{
  "TP_SL": {
    "tp_percent": 12.0,
    "sl_percent": -5.0,
    "parent_filled_price": 239.69,
    "type": "tp"
  },
  "expert_data": { }  // Other expert data can coexist safely
}
```

### TP_SL Object Schema

```typescript
interface TP_SL_Data {
  tp_percent?: number;              // TP percentage from filled price (1-100 scale)
  sl_percent?: number;              // SL percentage from filled price (1-100 scale, typically negative)
  parent_filled_price?: number;     // The parent order's filled price for recalculation
  type?: "tp" | "sl";               // Indicator: "tp" for take profit, "sl" for stop loss
  recalculated_at_trigger?: boolean; // True if prices were recalculated when parent filled
}
```

---

## Files Modified

### 1. `ba2_trade_platform/core/interfaces/AccountInterface.py`

#### `_ensure_tp_sl_percent_stored()`
- **Change**: Updated to use nested `order.data["TP_SL"]` structure
- **What it does**: Fallback calculation for TP/SL percentages when not stored initially
- **Checks**: Looks for data in `order.data["TP_SL"]["tp_percent"]` and `order.data["TP_SL"]["sl_percent"]`
- **Stores**: Writes to `order.data["TP_SL"][key]`

```python
# Now stores like this:
if "TP_SL" not in tp_or_sl_order.data:
    tp_or_sl_order.data["TP_SL"] = {}
tp_or_sl_order.data["TP_SL"]["tp_percent"] = round(tp_percent, 2)
```

### 2. `ba2_trade_platform/modules/accounts/AlpacaAccount.py`

#### `_create_tp_order_object()`
- **Change**: Order data now uses nested structure
- **Was**: `{"tp_percent": 12.0, "parent_filled_price": None, "type": "tp"}`
- **Now**: `{"TP_SL": {"tp_percent": 12.0, "parent_filled_price": None, "type": "tp"}}`

#### `_create_sl_order_object()`
- **Change**: Order data now uses nested structure  
- **Was**: `{"sl_percent": -5.0, "parent_filled_price": None, "type": "sl"}`
- **Now**: `{"TP_SL": {"sl_percent": -5.0, "parent_filled_price": None, "type": "sl"}}`

#### `_set_order_tp_impl()`
- **Change**: Nested structure when updating existing TP order metadata
- **Code**:
```python
if "TP_SL" not in existing_tp_order.data:
    existing_tp_order.data["TP_SL"] = {}
existing_tp_order.data["TP_SL"]["tp_percent"] = round(tp_percent, 2)
```

#### `_set_order_sl_impl()`
- **Change**: Nested structure when updating existing SL order metadata
- **Code**:
```python
if "TP_SL" not in existing_sl_order.data:
    existing_sl_order.data["TP_SL"] = {}
existing_sl_order.data["TP_SL"]["sl_percent"] = round(sl_percent, 2)
```

### 3. `ba2_trade_platform/core/TradeManager.py`

#### Trigger calculation for TP orders (lines ~398-420)
- **Change**: Reads from nested `order.data["TP_SL"]["tp_percent"]`
- **Check**: Verifies `"TP_SL" in dependent_order.data and "tp_percent" in dependent_order.data["TP_SL"]`
- **Access**: `dependent_order.data["TP_SL"].get("tp_percent")`

#### Trigger calculation for SL orders (lines ~425-450)
- **Change**: Reads from nested `order.data["TP_SL"]["sl_percent"]`
- **Check**: Verifies `"TP_SL" in dependent_order.data and "sl_percent" in dependent_order.data["TP_SL"]`
- **Access**: `dependent_order.data["TP_SL"].get("sl_percent")`

#### Transaction update section (lines ~474-479)
- **Change**: Checks for nested structure when updating Transaction TP/SL
- **TP Check**: `dependent_order.data and "TP_SL" in dependent_order.data and "tp_percent" in dependent_order.data["TP_SL"]`
- **SL Check**: `dependent_order.data and "TP_SL" in dependent_order.data and "sl_percent" in dependent_order.data["TP_SL"]`

---

## Migration Notes

### Backward Compatibility
- **No migration needed**: Database contains 0 orders with TP/SL data, so no legacy data conversion required
- **All new orders**: Use the nested structure from this point forward
- **If legacy orders exist**: Could implement a migration utility, but not currently needed

### Access Pattern Examples

#### Writing TP/SL data:
```python
# ✅ CORRECT - Nested structure
order.data["TP_SL"] = {
    "tp_percent": 12.0,
    "parent_filled_price": 239.69,
    "type": "tp"
}

# ❌ WRONG - Flat structure (outdated)
order.data["tp_percent"] = 12.0
```

#### Reading TP/SL data:
```python
# ✅ CORRECT - Nested access with safety checks
if order.data and "TP_SL" in order.data:
    tp_percent = order.data["TP_SL"].get("tp_percent")
    
# ❌ WRONG - Direct access (may fail or read expert data)
tp_percent = order.data.get("tp_percent")
```

#### Checking if order has TP:
```python
# ✅ CORRECT
has_tp = (order.data and "TP_SL" in order.data and 
          "tp_percent" in order.data["TP_SL"])

# ✅ ALSO OK - Safer alternative
has_tp = bool(order.data and 
              order.data.get("TP_SL", {}).get("tp_percent"))
```

---

## Testing Checklist

- [x] **Import validation**: All Python modules import without errors
- [x] **Database check**: Verify no existing orders need migration (0 orders with data)
- [x] **Code consistency**: All access patterns use nested structure
- [x] **Type safety**: No flat structure access patterns remain in code

### Manual Testing (Recommended)
1. Create a new trading order with TP target
2. Verify `order.data` contains `{"TP_SL": {"tp_percent": ...}}`
3. Verify TP order triggers correctly with recalculated price
4. Create SL order and verify nested structure
5. Test that expert recommendation data coexists without conflicts

---

## Future Considerations

### When to extend this further:
- **Data schema versioning**: Add `"_schema_version": 1` to enable future migrations
- **Expert-specific data**: Store separately as `order.data["expert_<expert_name>"]`
- **Audit trail**: Add timestamps `"tp_created_at"`, `"tp_triggered_at"` within `TP_SL` namespace

### Potential future schema evolution:
```json
{
  "_schema_version": 1,
  "TP_SL": {
    "tp_percent": 12.0,
    "tp_created_at": "2025-10-22T11:00:00Z",
    "tp_triggered_at": null,
    "modifications": [
      {"timestamp": "...", "from": 12.0, "to": 15.0, "reason": "batch_adjust"}
    ]
  },
  "expert_recommendation": { },
  "user_notes": { }
}
```

---

## Key Principles

1. **Namespace isolation**: Each data provider/feature gets its own top-level key in `order.data`
2. **Safety-first reads**: Always check for nested key existence before accessing
3. **Clean writes**: Initialize nested objects on first write
4. **No flat structure**: Reject any pull requests attempting to write flat `tp_percent` or `sl_percent` to root `order.data`

---

## References

- `AccountInterface.py` - `_ensure_tp_sl_percent_stored()` method (line 331)
- `AlpacaAccount.py` - `_create_tp_order_object()` (line 1021), `_create_sl_order_object()` (line 1177)
- `TradeManager.py` - Trigger calculations (lines 398-450, 474-479)
- Database schema: `core/models.py` - `TradingOrder` model (line 421)
