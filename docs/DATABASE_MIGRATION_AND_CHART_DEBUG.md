# Database Migration & Balance Usage Chart Debug

## Summary
Fixed the database schema error and added comprehensive debugging to the Balance Usage chart to diagnose why it's not showing data.

## Issue 1: Database Schema Missing Columns

### Problem
```
sqlite3.OperationalError: no such column: tradingorder.filled_avg_price
```

The database was missing the newly added `filled_avg_price` and `open_price` columns.

### Solution
Created and applied Alembic migration to add the missing columns.

**Migration File**: `alembic/versions/648ce01dcd39_add_filled_avg_price_and_open_price_to_.py`

```python
def upgrade() -> None:
    """Upgrade schema."""
    # Add filled_avg_price and open_price columns to tradingorder table
    with op.batch_alter_table('tradingorder', schema=None) as batch_op:
        batch_op.add_column(sa.Column('filled_avg_price', sa.Float(), nullable=True))
        batch_op.add_column(sa.Column('open_price', sa.Float(), nullable=True))
```

**Migration Applied**:
```bash
.\.venv\Scripts\python.exe -m alembic upgrade head
# Output: Running upgrade 0d97964e8ad8 -> 648ce01dcd39
```

**Result**: ✅ Database now has the required columns

---

## Issue 2: Chart Showing "No Active Balance Usage"

### Problem
The chart displays "No active balance usage found (all orders closed/canceled)" even when there are active transactions.

### Root Cause Analysis
The chart queries for transactions with:
1. `expert_id IS NOT NULL`
2. `status IN (OPENED, WAITING)`

If either condition isn't met, no data is shown.

### Debugging Enhancements Added

#### 1. Enhanced Chart Logging

**File**: `ba2_trade_platform/ui/components/BalanceUsagePerExpertChart.py`

Added comprehensive diagnostic logging to `calculate_expert_balance_usage()`:

```python
# Log total transactions
all_transactions = session.exec(select(Transaction)).all()
logger.info(f"Total transactions in database: {len(all_transactions)}")

# Count by status
status_counts = {}
expert_counts = {'with_expert': 0, 'without_expert': 0}
for t in all_transactions:
    status_str = str(t.status)
    status_counts[status_str] = status_counts.get(status_str, 0) + 1
    if t.expert_id:
        expert_counts['with_expert'] += 1
    else:
        expert_counts['without_expert'] += 1

logger.info(f"Transaction status breakdown: {status_counts}")
logger.info(f"Expert attribution: {expert_counts}")

# Log filtered results
logger.info(f"Found {len(transactions)} active transactions with expert attribution (OPENED or WAITING status)")

# If none found, check if there are ANY transactions with experts (any status)
if len(transactions) == 0:
    transactions_any_status = session.exec(
        select(Transaction)
        .where(Transaction.expert_id.isnot(None))
    ).all()
    logger.info(f"Transactions with expert_id (any status): {len(transactions_any_status)}")
    if transactions_any_status:
        for t in transactions_any_status:
            logger.info(f"  - Transaction {t.id}: {t.symbol}, status={t.status}, expert_id={t.expert_id}")
```

**What This Reveals**:
- Total number of transactions in database
- How many have `expert_id` set vs not set
- Breakdown of transactions by status
- If transactions exist but have wrong status (e.g., CLOSING instead of OPENED)

---

**Per-Transaction Logging**:
```python
logger.info(f"Transaction {transaction.id} ({transaction.symbol}): Expert {expert_name}, Filled: ${filled_equity:.2f}, Pending: ${pending_equity:.2f}")

# Debug: Check if there are any orders for this transaction
order_count = session.exec(
    select(TradingOrder)
    .where(TradingOrder.transaction_id == transaction.id)
).all()
logger.debug(f"  - Transaction {transaction.id} has {len(order_count)} orders")
```

**What This Reveals**:
- Which transactions are being processed
- Whether they have orders
- Calculated equity values (might be $0 if prices are missing)

---

**Filtering Results**:
```python
before_filter_count = len(balance_usage)
balance_usage = {k: v for k, v in balance_usage.items() if v['pending'] > 0 or v['filled'] > 0}
filtered_count = before_filter_count - len(balance_usage)

if filtered_count > 0:
    logger.info(f"Filtered out {filtered_count} experts with zero balance usage")

logger.info(f"Final result: {len(balance_usage)} experts with active balance usage")
```

**What This Reveals**:
- If transactions are found but filtered out due to $0 equity
- How many experts had transactions but no balance

---

#### 2. Enhanced Transaction Method Logging

**File**: `ba2_trade_platform/core/models.py`

**Method**: `get_current_open_equity()`

Added detailed logging for filled order calculations:

```python
logger.debug(f"Transaction {self.id}.get_current_open_equity(): Found {len(orders)} orders")

for order in orders:
    if order.status in OrderStatus.get_executed_statuses() and order.filled_qty:
        price = order.open_price if order.open_price else order.filled_avg_price
        
        if price:
            equity = abs(order.filled_qty) * price
            total_equity += equity
            logger.debug(f"  Order {order.id}: filled_qty={order.filled_qty}, price={price}, equity=${equity:.2f}")
        else:
            logger.debug(f"  Order {order.id}: No price available (open_price={order.open_price}, filled_avg_price={order.filled_avg_price})")
    else:
        logger.debug(f"  Order {order.id}: Skipped (status={order.status}, filled_qty={order.filled_qty})")

logger.debug(f"Transaction {self.id}.get_current_open_equity(): Total = ${total_equity:.2f}")
```

**What This Reveals**:
- How many orders exist for the transaction
- Which orders are filled vs unfilled
- Whether price data is available (`open_price` or `filled_avg_price`)
- Individual order equity calculations
- Final total equity

---

**Method**: `get_pending_open_equity()`

Added detailed logging for pending order calculations:

```python
if account_interface:
    market_price = account_interface.get_instrument_current_price(self.symbol)
    logger.debug(f"Transaction {self.id}.get_pending_open_equity(): Market price for {self.symbol} = ${market_price}")
else:
    logger.debug(f"Transaction {self.id}.get_pending_open_equity(): No market price available, returning 0")

logger.debug(f"Transaction {self.id}.get_pending_open_equity(): Found {len(orders)} orders")

for order in orders:
    if order.status in OrderStatus.get_unfilled_statuses():
        if order.depends_on_order is not None:
            logger.debug(f"  Order {order.id}: Skipped (dependent order)")
            continue
        
        # ... calculation ...
        logger.debug(f"  Order {order.id}: remaining_qty={remaining_qty}, market_price=${market_price}, equity=${equity:.2f}")
    else:
        logger.debug(f"  Order {order.id}: Skipped (status={order.status})")

logger.debug(f"Transaction {self.id}.get_pending_open_equity(): Total = ${total_equity:.2f}")
```

**What This Reveals**:
- Whether market price is available
- How many pending orders exist
- Which orders are dependent (TP/SL) and skipped
- Remaining quantity calculations
- Individual pending order equity
- Final total pending equity

---

## Diagnostic Workflow

When the chart shows "No active balance usage", check the logs for:

### Step 1: Check if Transactions Exist
```
INFO - Total transactions in database: X
INFO - Transaction status breakdown: {...}
INFO - Expert attribution: {'with_expert': X, 'without_expert': Y}
```

**Possible Issues**:
- No transactions in database
- All transactions missing `expert_id`

---

### Step 2: Check Transaction Status
```
INFO - Found X active transactions with expert attribution (OPENED or WAITING status)
```

**If X = 0**:
```
INFO - Transactions with expert_id (any status): Y
INFO -   - Transaction 1: AAPL, status=CLOSING, expert_id=5
```

**Possible Issues**:
- Transactions have wrong status (CLOSING, CLOSED instead of OPENED/WAITING)
- Need to update transaction status query

---

### Step 3: Check Order Counts
```
DEBUG - Transaction 1 has X orders
```

**Possible Issues**:
- Transactions exist but have no orders
- Orders not properly linked to transactions

---

### Step 4: Check Equity Calculations
```
DEBUG - Transaction 1.get_current_open_equity(): Found X orders
DEBUG -   Order 1: filled_qty=10, price=100.0, equity=$1000.00
DEBUG - Transaction 1.get_current_open_equity(): Total = $1000.00
```

**Possible Issues**:
- Orders missing price data (NULL `open_price` and `filled_avg_price`)
- Orders not in FILLED status
- Calculation logic issue

---

### Step 5: Check Pending Calculations
```
DEBUG - Transaction 1.get_pending_open_equity(): Market price for AAPL = $150.0
DEBUG - Transaction 1.get_pending_open_equity(): Found X orders
DEBUG -   Order 2: remaining_qty=5, market_price=$150.0, equity=$750.00
```

**Possible Issues**:
- No account interface provided (market price unavailable)
- All orders are filled (no pending)
- Orders are dependent (TP/SL) and skipped

---

### Step 6: Check Final Filtering
```
INFO - Filtered out X experts with zero balance usage
INFO - Final result: Y experts with active balance usage
```

**Possible Issues**:
- Transactions found but all have $0 equity
- Price data missing for all orders
- All equity calculations returned 0

---

## Common Scenarios & Solutions

### Scenario 1: "No transactions with expert_id"
**Symptoms**:
```
INFO - Expert attribution: {'with_expert': 0, 'without_expert': 5}
```

**Solution**: Transactions need `expert_id` set. This should happen when:
- Expert creates a recommendation
- Order is created from expert recommendation
- Transaction is linked to the order

**Check**: Are expert recommendations being created? Are orders linked to experts?

---

### Scenario 2: "Transactions in wrong status"
**Symptoms**:
```
INFO - Found 0 active transactions with expert attribution (OPENED or WAITING status)
INFO - Transactions with expert_id (any status): 3
INFO -   - Transaction 5: AAPL, status=CLOSING, expert_id=2
```

**Solution**: Transaction status might not be updating correctly. Check:
- Is `refresh_orders()` being called?
- Are orders being filled but transaction status not updating to OPENED?
- Are transactions stuck in CLOSING state?

---

### Scenario 3: "Orders missing price data"
**Symptoms**:
```
DEBUG - Order 91: No price available (open_price=None, filled_avg_price=None)
DEBUG - Transaction 5.get_current_open_equity(): Total = $0.00
```

**Solution**: Price fields are NULL. This happens when:
- Orders were created before migration (old data)
- `refresh_orders()` hasn't run yet to populate prices
- Broker API not returning price data

**Fix**: Run `refresh_orders()` for the account or wait for next automatic refresh

---

### Scenario 4: "No market price for pending orders"
**Symptoms**:
```
DEBUG - Transaction 5.get_pending_open_equity(): No market price available, returning 0
```

**Solution**: Account interface not provided or market data unavailable. Check:
- Is account interface being passed to method?
- Can account interface fetch current price?
- Is market open? (might get NULL price if closed)

---

## Testing Checklist

After migration and debugging enhancements:

- [ ] Database schema updated (columns exist)
- [ ] Migration applied successfully
- [ ] No schema errors when querying transactions
- [ ] Chart logging shows transaction counts
- [ ] Chart logging shows status breakdown
- [ ] Chart logging shows expert attribution
- [ ] Transaction method logging shows order details
- [ ] Transaction method logging shows price data
- [ ] Can identify why chart shows no data from logs

---

## Next Steps

1. **Run the application** and navigate to the Overview page
2. **Check the logs** (`logs/app.log` and `logs/app.debug.log`)
3. **Look for the diagnostic output** from the chart
4. **Identify the specific issue**:
   - No transactions?
   - Wrong status?
   - Missing expert_id?
   - Missing price data?
   - Zero equity calculations?
5. **Fix the root cause** based on what the logs reveal

---

## Files Modified

| File | Changes | Purpose |
|------|---------|---------|
| `alembic/versions/648ce01dcd39_*.py` | Created migration | Add missing database columns |
| `ui/components/BalanceUsagePerExpertChart.py` | +40 lines logging | Diagnose why chart shows no data |
| `core/models.py` | +35 lines logging | Debug equity calculations |

---

## Additional Notes

### Why So Much Logging?

The chart relies on multiple layers:
1. Database query (transactions with right status + expert_id)
2. Order query (orders linked to transactions)
3. Price data (open_price, filled_avg_price, market price)
4. Status checking (FILLED, PENDING, etc.)
5. Calculation logic (equity formulas)

Any layer can fail silently, resulting in $0 equity. The detailed logging helps pinpoint exactly where the chain breaks.

### Logging Levels

- **INFO**: High-level status (transaction counts, final results)
- **DEBUG**: Detailed per-order calculations, SQL queries, individual equity values

Set logging level to DEBUG to see full diagnostic output:
```python
logger.setLevel(logging.DEBUG)
```

### Performance Impact

The additional logging adds minimal overhead:
- Only runs when chart is loaded
- Only affects DEBUG level logs
- Can be disabled by setting log level to INFO

---

## Summary

✅ **Database Migration**: Applied successfully, schema now has required columns
✅ **Debugging Added**: Comprehensive logging at every step
✅ **Ready for Diagnosis**: Logs will reveal exactly why chart shows no data

The chart should now either:
1. **Display data** (if transactions exist with correct status and prices)
2. **Show detailed logs** explaining why no data (check logs for root cause)
