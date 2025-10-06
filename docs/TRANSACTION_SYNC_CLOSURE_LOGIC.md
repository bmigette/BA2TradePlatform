# Transaction Sync - Enhanced Closure Logic

## Overview
Enhanced the `refresh_transactions()` method in `AccountInterface` to properly detect and close transactions based on comprehensive order state analysis.

## Problem
Previously, transactions could remain in `OPENED` or `WAITING` status even when:
1. All orders were canceled/rejected/expired (no execution ever occurred)
2. Filled buy and sell orders balanced out (position was closed via offsetting orders)

This led to stale transaction records that didn't reflect the actual position state.

## Solution
Added two new closure scenarios to the transaction sync logic:

### 1. All Orders Canceled/Rejected
**Scenario**: All orders associated with a transaction are in non-executed terminal states
- `CANCELED`, `REJECTED`, `ERROR`, `EXPIRED`

**Logic**:
```python
canceled_statuses = {OrderStatus.CANCELED, OrderStatus.REJECTED, OrderStatus.ERROR, OrderStatus.EXPIRED}
all_orders_canceled = (
    len(orders) > 0 and
    all(order.status in canceled_statuses for order in orders)
)
```

**Action**:
- Mark transaction as `CLOSED`
- Set `close_date` to current timestamp
- Works for any transaction status (WAITING or OPENED)
- Logs: `"Transaction {id} all orders canceled/rejected, marking as CLOSED"`

**Use Cases**:
- Market entry order rejected by broker due to insufficient funds
- Limit order expired before execution
- User manually canceled all pending orders
- System error prevented order execution

### 2. Balanced Buy/Sell Orders
**Scenario**: The sum of ALL filled buy orders equals the sum of ALL filled sell orders

**Logic**:
```python
# Sum ALL filled buy orders (market entry, limit, TP, SL, etc.)
total_filled_buy = sum of all executed BUY orders' quantities

# Sum ALL filled sell orders (market entry, limit, TP, SL, etc.)
total_filled_sell = sum of all executed SELL orders' quantities

# Check if balanced (within 0.0001 tolerance for floating point)
position_balanced = abs(total_filled_buy - total_filled_sell) < 0.0001
```

**Action**:
- Mark transaction as `CLOSED` (only if currently `OPENED`)
- Set `close_date` to current timestamp
- Set `close_price` from the last filled order (chronologically)
- Logs: `"Transaction {id} filled buy/sell orders balanced (buy={x}, sell={y}), marking as CLOSED"`

**Use Cases**:
- Position opened with BUY 100, then closed with SELL 100 (via any order type)
- Position opened with BUY_LIMIT 100 (filled), closed with SELL_LIMIT 100 (filled)
- Multiple fills accumulated: BUY 50 + BUY 50, then SELL 100
- TP/SL orders that filled: BUY 100, then SELL 100 (TP)
- Manual position close via broker interface
- Any combination where total buys = total sells

## Implementation Details

### Order Classification
Orders are classified into two categories:
1. **Market Entry Orders**: Orders without `depends_on_order` (initial position opening orders)
2. **Dependent Orders**: Orders with `depends_on_order` set (TP/SL orders)

### Quantity Calculation
Transaction quantity is calculated from filled market entry orders:
- BUY orders: Add quantity
- SELL orders: Subtract quantity (for short positions)

For balanced position detection, **ALL filled orders are summed** (market entry, dependent, limit, market, TP, SL - everything):
- If `total_filled_buy == total_filled_sell` → position is balanced → transaction closed
- This includes all order types: BUY, SELL, BUY_LIMIT, SELL_LIMIT, TP orders, SL orders, etc.

### Close Price Logic
When closing due to balanced orders:
1. Sort all filled orders by `created_at` timestamp
2. Take the last (most recent) filled order
3. Use its `limit_price` if available
4. Fallback to current market price for market orders

### Priority Order
The closure checks are evaluated in this order (first match wins):

1. **Filled Closing Order** (existing logic)
   - If TP or SL order is FILLED → CLOSED

2. **All Orders Canceled** (NEW)
   - If all orders are canceled/rejected/error/expired → CLOSED

3. **Balanced Buy/Sell** (NEW)
   - If filled buy/sell quantities match → CLOSED

4. **Waiting to Closed** (existing logic)
   - If all entry orders terminal without execution → CLOSED

5. **Opened to Closed** (existing logic)
   - If all entry orders terminal after opening (no active TP/SL) → CLOSED

## Examples

### Example 1: All Orders Canceled
```
Transaction #123 (WAITING)
├─ Order #1: BUY 100 AAPL @ $150 → CANCELED
└─ Order #2: SELL 100 AAPL @ $155 (TP) → CANCELED

Result: Transaction #123 → CLOSED
Reason: All orders canceled, no execution occurred
```

### Example 2: Balanced Position
```
Transaction #456 (OPENED, quantity=100)
├─ Order #10: BUY_LIMIT 100 TSLA @ $200 → FILLED (entry)
├─ Order #11: SELL_LIMIT 50 TSLA @ $210 (TP) → FILLED
└─ Order #12: SELL 50 TSLA @ $205 → FILLED (manual close)

Calculations:
- total_filled_buy = 100
- total_filled_sell = 50 + 50 = 100
- position_balanced = |100 - 100| < 0.0001 → TRUE

Result: Transaction #456 → CLOSED
Close Price: $205 (from Order #12, the last filled)
Reason: Buy/sell orders balanced (all order types counted)
```

### Example 3: Partial Position
```
Transaction #789 (OPENED, quantity=100)
├─ Order #20: BUY 100 NVDA @ $300 → FILLED
└─ Order #21: SELL 50 NVDA @ $310 (TP) → FILLED

Calculations:
- total_filled_buy = 100
- total_filled_sell = 50
- position_balanced = |100 - 50| < 0.0001 → FALSE

Result: Transaction #789 → OPENED (no change)
Reason: Position still open with 50 shares
```

## Testing Checklist

### Test Case 1: All Orders Canceled
- [ ] Create transaction with BUY order
- [ ] Cancel the BUY order via broker
- [ ] Run `refresh_transactions()`
- [ ] Verify transaction is CLOSED
- [ ] Verify `close_date` is set

### Test Case 2: Balanced Position - Simple
- [ ] Create transaction with BUY 100 shares (FILLED)
- [ ] Transaction should be OPENED
- [ ] Create manual SELL 100 shares order (FILLED)
- [ ] Run `refresh_transactions()`
- [ ] Verify transaction is CLOSED
- [ ] Verify `close_price` is set from SELL order

### Test Case 3: Balanced Position - Multiple Orders
- [ ] Create transaction with BUY 50 shares (FILLED)
- [ ] Add another BUY 50 shares (FILLED)
- [ ] Transaction should be OPENED with quantity=100
- [ ] Create SELL 60 shares (FILLED)
- [ ] Create SELL 40 shares (FILLED)
- [ ] Run `refresh_transactions()`
- [ ] Verify transaction is CLOSED
- [ ] Verify `close_price` is from the last SELL order

### Test Case 4: Partial Position (Should NOT Close)
- [ ] Create transaction with BUY 100 shares (FILLED)
- [ ] Create SELL 50 shares (FILLED)
- [ ] Run `refresh_transactions()`
- [ ] Verify transaction is still OPENED
- [ ] Verify quantity reflects remaining position

### Test Case 5: Mixed Canceled and Filled
- [ ] Create transaction with BUY 100 (FILLED)
- [ ] Add TP SELL order (CANCELED)
- [ ] Add SL SELL order (CANCELED)
- [ ] Run `refresh_transactions()`
- [ ] Verify transaction is still OPENED (not all orders canceled, position exists)

## Logging
Enhanced logging provides clear visibility into closure decisions:

```
INFO: Transaction 123 all orders canceled/rejected, marking as CLOSED
INFO: Transaction 456 filled buy/sell orders balanced (buy=100.0, sell=100.0), marking as CLOSED
DEBUG: Transaction 789 quantity updated to 100.0
```

## Migration Notes
No database schema changes required. This is a pure logic enhancement that works with existing models.

## Related Files
- `ba2_trade_platform/core/AccountInterface.py` - Main implementation
- `ba2_trade_platform/core/types.py` - OrderStatus enum with terminal/executed status sets
- `ba2_trade_platform/core/models.py` - Transaction and TradingOrder models

## Future Enhancements
Potential improvements for future iterations:
1. Track partial fill history for more accurate close price determination
2. Add transaction closure reason field to database (e.g., "BALANCED", "ALL_CANCELED", "TP_FILLED")
3. Support for averaging close prices across multiple closing orders
4. Configurable tolerance for position balancing (currently 0.0001)
