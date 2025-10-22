# TP/SL Nested Structure - Quick Reference Guide

## The Problem

❌ **Before**: TP/SL data in flat structure causes conflicts with expert data
```json
{
  "tp_percent": 12.0,
  "expert_signal": "BUY",  // Data collision risk!
  "confidence": 85.5
}
```

✅ **After**: TP/SL data in dedicated namespace prevents conflicts
```json
{
  "TP_SL": {
    "tp_percent": 12.0
  },
  "expert_signal": "BUY",
  "confidence": 85.5
}
```

---

## Usage Patterns

### Writing TP/SL Data

```python
# Step 1: Ensure TP_SL key exists
if not order.data:
    order.data = {}
if "TP_SL" not in order.data:
    order.data["TP_SL"] = {}

# Step 2: Write TP/SL values
order.data["TP_SL"]["tp_percent"] = 12.5
order.data["TP_SL"]["parent_filled_price"] = 100.0
order.data["TP_SL"]["type"] = "tp"
```

### Reading TP/SL Data

```python
# Option 1: Safe read with explicit checks
if order.data and "TP_SL" in order.data:
    tp_percent = order.data["TP_SL"].get("tp_percent")

# Option 2: Safe read with helper function
def safe_get_tp_percent(order):
    if not order.data:
        return None
    tp_sl = order.data.get("TP_SL")
    return tp_sl.get("tp_percent") if tp_sl else None

# Option 3: Inline safe access
tp_percent = order.data and order.data.get("TP_SL", {}).get("tp_percent")
```

### Checking for TP/SL Data

```python
# Check if order has TP
has_tp = (order.data and 
          "TP_SL" in order.data and 
          "tp_percent" in order.data["TP_SL"])

# Check if order has SL
has_sl = (order.data and 
          "TP_SL" in order.data and 
          "sl_percent" in order.data["TP_SL"])
```

---

## TP_SL Object Fields

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `tp_percent` | float | No | Take-profit percentage (1-100 scale) |
| `sl_percent` | float | No | Stop-loss percentage (1-100 scale, typically negative) |
| `parent_filled_price` | float | No | Parent order's filled price |
| `type` | str | No | "tp" for take-profit, "sl" for stop-loss |
| `recalculated_at_trigger` | bool | No | True if recalculated when parent order filled |

---

## Real-World Examples

### Example 1: Creating a TP Order

```python
def _create_tp_order(self, original_order, tp_price, tp_percent):
    # Create order with nested TP data
    tp_order = TradingOrder(
        account_id=self.id,
        symbol=original_order.symbol,
        quantity=0,  # Set when triggered
        side=opposite_side,
        order_type=OrderType.SELL_LIMIT,
        limit_price=tp_price,
        # Store TP metadata in nested structure
        data={
            "TP_SL": {
                "tp_percent": round(tp_percent, 2),
                "parent_filled_price": None,
                "type": "tp"
            }
        }
    )
    return tp_order
```

### Example 2: Recalculating Prices at Trigger

```python
def trigger_dependent_orders(self, parent_order):
    for dependent_order in parent_order.dependent_orders:
        # Safe check for TP
        if (dependent_order.data and 
            "TP_SL" in dependent_order.data and 
            "tp_percent" in dependent_order.data["TP_SL"]):
            
            tp_percent = dependent_order.data["TP_SL"]["tp_percent"]
            # Recalculate price from parent's filled price
            new_limit_price = parent_order.open_price * (1 + tp_percent / 100)
            dependent_order.limit_price = new_limit_price
            
            # Update parent filled price in metadata
            dependent_order.data["TP_SL"]["parent_filled_price"] = parent_order.open_price
            dependent_order.data["TP_SL"]["recalculated_at_trigger"] = True
```

### Example 3: Fallback Calculation

```python
def ensure_tp_sl_stored(self, tp_or_sl_order, parent_order):
    """Fallback calculation if TP/SL wasn't stored during order creation"""
    
    # Initialize nested structure
    if not tp_or_sl_order.data:
        tp_or_sl_order.data = {}
    if "TP_SL" not in tp_or_sl_order.data:
        tp_or_sl_order.data["TP_SL"] = {}
    
    # Calculate and store if missing
    if "tp_percent" not in tp_or_sl_order.data["TP_SL"]:
        if parent_order.open_price > 0 and tp_or_sl_order.limit_price:
            tp_percent = ((tp_or_sl_order.limit_price - parent_order.open_price) 
                          / parent_order.open_price) * 100
            tp_or_sl_order.data["TP_SL"]["tp_percent"] = round(tp_percent, 2)
            tp_or_sl_order.data["TP_SL"]["parent_filled_price"] = parent_order.open_price
```

---

## Common Mistakes ❌

```python
# ❌ WRONG: Storing in flat structure
order.data["tp_percent"] = 12.0

# ❌ WRONG: No safety checks
tp_percent = order.data["tp_percent"]

# ❌ WRONG: Silent fallback values
tp_percent = order.data.get("tp_percent", 0)  # Hides data errors!

# ❌ WRONG: Checking wrong location
if "tp_percent" in order.data:  # Missing TP_SL check
    ...

# ❌ WRONG: Not initializing parent dict
order.data["TP_SL"]["tp_percent"] = 12.0  # Will error if TP_SL doesn't exist
```

---

## Best Practices ✅

```python
# ✅ CORRECT: Initialize before writing
if not order.data:
    order.data = {}
if "TP_SL" not in order.data:
    order.data["TP_SL"] = {}
order.data["TP_SL"]["tp_percent"] = 12.0

# ✅ CORRECT: Safe reads with checks
if order.data and "TP_SL" in order.data:
    tp_percent = order.data["TP_SL"].get("tp_percent")

# ✅ CORRECT: Helper functions for repeated access
def get_tp_percent(order) -> Optional[float]:
    if not order.data or not order.data.get("TP_SL"):
        return None
    return order.data["TP_SL"].get("tp_percent")

# ✅ CORRECT: Explicit structure checks
has_tp = (order.data and 
          "TP_SL" in order.data and 
          "tp_percent" in order.data["TP_SL"])
```

---

## Files Modified

| File | Changes | Impact |
|------|---------|--------|
| `AccountInterface.py` | Updated `_ensure_tp_sl_percent_stored()` | Fallback calculations |
| `AlpacaAccount.py` | Updated 4 methods for nested structure | TP/SL order creation |
| `TradeManager.py` | Updated trigger calculations | Order price recalculation |

---

## Testing

Run the comprehensive test suite:
```bash
.venv\Scripts\python.exe -c "import sys; sys.path.insert(0, '.'); exec(open('test_files/test_tp_sl_nested_structure.py').read())"
```

All 5 tests should pass:
- ✅ Nested TP data structure
- ✅ Nested SL data structure
- ✅ Coexistence with expert data
- ✅ Safe access patterns
- ✅ Proper initialization

---

## Key Takeaway

**Always use `order.data["TP_SL"]` namespace for TP/SL data.**

This ensures:
- 🔒 Data isolation from expert recommendations
- 🛡️ Clear intent and maintainability
- 🔄 Extensibility for future enhancements
- 🧪 Testable and predictable behavior
