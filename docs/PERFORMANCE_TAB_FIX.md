# Performance Tab Model Mismatch Fix

**Date**: 2025-10-08  
**Status**: ✅ Fixed  
**Impact**: Performance analytics tab now works correctly

## Problem

The Performance tab (`ba2_trade_platform/ui/pages/performance.py`) was querying `TradingOrder` model but trying to access fields from the `Transaction` model, causing `AttributeError` when loading the page:

```
AttributeError: 'TradingOrder' object has no attribute 'close_time'
```

### Root Cause

The performance analytics should be based on **completed transactions** (Transaction model), not individual orders (TradingOrder model). A transaction represents the full lifecycle of a trade (open → close), while orders are individual buy/sell actions.

### Incorrect Implementation

**Before**:
```python
from ba2_trade_platform.core.models import TradingOrder
from ba2_trade_platform.core.types import OrderStatus

def _get_closed_transactions(self) -> List[TradingOrder]:
    query = session.query(TradingOrder).filter(
        TradingOrder.account_id == self.account_id,
        TradingOrder.status == OrderStatus.CLOSED,
        TradingOrder.close_time.isnot(None),  # ❌ TradingOrder has no close_time
        ...
    )
```

**Issues**:
1. `TradingOrder` doesn't have `close_time`, `open_time`, or `pnl` fields
2. `TradingOrder` uses `expert_instance_id`, not `expert_id`
3. `TradingOrder` represents individual orders, not complete trades

## Solution

### Changes Made

1. **Updated imports** to use `Transaction` model:
   ```python
   from ba2_trade_platform.core.models import Transaction, TradingOrder, ExpertInstance
   from ba2_trade_platform.core.types import TransactionStatus
   ```

2. **Fixed query** to use Transaction model:
   ```python
   def _get_closed_transactions(self) -> List[Transaction]:
       query = session.query(Transaction).filter(
           Transaction.status == TransactionStatus.CLOSED,
           Transaction.close_date.isnot(None),
           Transaction.close_date >= cutoff_date
       )
       
       if self.selected_experts:
           query = query.filter(Transaction.expert_id.in_(self.selected_experts))
   ```

3. **Updated field references** throughout the file:
   - `txn.close_time` → `txn.close_date`
   - `txn.open_time` → `txn.open_date`
   - `txn.expert_instance_id` → `txn.expert_id`

4. **Added P&L calculation** (Transaction model doesn't store pre-calculated P&L):
   ```python
   # Calculate P&L = (close_price - open_price) * quantity
   pnls = []
   for txn in txns:
       if txn.open_price and txn.close_price and txn.quantity:
           pnl = (txn.close_price - txn.open_price) * txn.quantity
           pnls.append(pnl)
   ```

### Transaction Model Fields (Reference)

```python
class Transaction(SQLModel, table=True):
    id: int | None
    symbol: str
    quantity: float
    open_price: float | None
    close_price: float | None
    stop_loss: float | None
    take_profit: float | None
    open_date: DateTime | None      # ✅ Use this, not open_time
    close_date: DateTime | None     # ✅ Use this, not close_time
    status: TransactionStatus       # ✅ Use TransactionStatus, not OrderStatus
    expert_id: int | None           # ✅ Use this, not expert_instance_id
    trading_orders: List["TradingOrder"]  # Relationship to individual orders
```

## Testing

After the fix, the Performance tab should:
1. ✅ Load without errors
2. ✅ Display closed transactions correctly
3. ✅ Calculate metrics (P&L, duration, win rate) accurately
4. ✅ Group by expert correctly using `expert_id`

## Related Files

- **Modified**: `ba2_trade_platform/ui/pages/performance.py`
- **Models Used**: `Transaction`, `ExpertInstance`, `AccountDefinition`
- **Types Used**: `TransactionStatus`

## Future Considerations

1. **Performance**: Consider adding a calculated `pnl` field to Transaction model to avoid repeated calculations
2. **Indexing**: May want to add database indexes on `Transaction.close_date` and `Transaction.expert_id` for faster queries
3. **Validation**: Ensure Transaction records have all required fields (open_price, close_price, quantity) before closing

## Key Takeaway

**Always use the `Transaction` model for trade performance analytics**, not `TradingOrder`. 

- `Transaction` = Complete trade lifecycle (open → close)
- `TradingOrder` = Individual order execution (buy/sell action)
