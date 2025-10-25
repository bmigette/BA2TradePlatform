# Take Profit / Stop Loss Order Management Logic

## Overview

Take Profit (TP) and Stop Loss (SL) orders are protective orders that automatically close positions when price reaches target levels. This document explains the complete logic for creating, updating, and managing these orders.

## Order Types by Position Direction

### For BUY Positions (Long)
- **TP Order**: `SELL_LIMIT` (closes position at profit target above entry price)
- **SL Order**: `SELL_STOP` (closes position at loss limit below entry price)
- **Combined**: `SELL_STOP_LIMIT` (single order with both stop trigger and limit execution price)

### For SELL Positions (Short)
- **TP Order**: `BUY_LIMIT` (closes position at profit target below entry price)
- **SL Order**: `BUY_STOP` (closes position at loss limit above entry price)
- **Combined**: `BUY_STOP_LIMIT` (single order with both stop trigger and limit execution price)

## Alpaca Broker Constraint

**CRITICAL**: Alpaca allows **only ONE order in the opposite direction** per position at a time.

This means:
- A BUY order can have either ONE SELL_LIMIT **OR** ONE SELL_STOP **OR** ONE SELL_STOP_LIMIT
- A SELL order can have either ONE BUY_LIMIT **OR** ONE BUY_STOP **OR** ONE BUY_STOP_LIMIT
- You **CANNOT** have both SELL_LIMIT and SELL_STOP active simultaneously

## Order State Management

### Order Lifecycle States

1. **WAITING_TRIGGER**: Order exists only in local database, not yet submitted to broker
   - Parent entry order has not been FILLED yet
   - Stored with `quantity=0` to indicate it's not active
   - Has `depends_on_order` pointing to parent order ID
   - Has `depends_order_status_trigger=FILLED` to indicate when to submit

2. **PENDING**: Order created in database and ready to be submitted to broker
   - Parent order is FILLED
   - Order has been submitted to broker and received `broker_order_id`
   - Waiting for broker acceptance

3. **ACCEPTED**: Order is active at the broker
   - Broker has acknowledged the order
   - Order is live and monitoring for trigger conditions

4. **FILLED**: Order has executed
   - Position has been closed at TP or SL price
   - Transaction moves to CLOSED status

5. **REPLACED**: Order has been replaced by a newer order
   - Old order marked REPLACED in database
   - New order created with new `broker_order_id`
   - Used when updating TP/SL requires changing order type

## Implementation Scenarios

### Scenario 1: No Existing TP/SL - Creating First Order

**Case 1A: Create TP only**
```python
# Order: BUY position with no TP/SL yet
# Action: Set TP at $110

Result:
- Create SELL_LIMIT order with limit_price=$110
- If parent FILLED: Submit immediately (status=PENDING)
- If parent NOT FILLED: Store as WAITING_TRIGGER
```

**Case 1B: Create SL only**
```python
# Order: BUY position with no TP/SL yet
# Action: Set SL at $90

Result:
- Create SELL_STOP order with stop_price=$90
- If parent FILLED: Submit immediately (status=PENDING)
- If parent NOT FILLED: Store as WAITING_TRIGGER
```

**Case 1C: Create both TP and SL (PREFERRED)**
```python
# Order: BUY position with no TP/SL yet
# Action: Set TP=$110 and SL=$90

Result:
- Create SELL_STOP_LIMIT order with:
  - stop_price=$90 (SL trigger)
  - limit_price=$110 (TP target)
- Single order satisfies Alpaca's constraint
- If parent FILLED: Submit immediately
- If parent NOT FILLED: Store as WAITING_TRIGGER
```

### Scenario 2: Existing TP Order - Adding SL

**Problem**: Already have SELL_LIMIT (TP), cannot add SELL_STOP (SL) due to Alpaca constraint

**Solution**: Replace existing TP order with STOP_LIMIT order
```python
# Current: SELL_LIMIT at $110 (TP only)
# Action: Add SL at $90

Steps:
1. Check if TP order has broker_order_id
2. If YES (broker-submitted):
   a. Call Alpaca replace_order_by_id(existing_tp.broker_order_id)
   b. Create SELL_STOP_LIMIT with stop=$90, limit=$110
   c. Mark old TP order as REPLACED in database
   d. Create new database record with new broker_order_id
3. If NO (WAITING_TRIGGER):
   a. Update database record directly
   b. Change order_type to SELL_STOP_LIMIT
   c. Set stop_price=$90, limit_price=$110
```

### Scenario 3: Existing SL Order - Adding TP

**Problem**: Already have SELL_STOP (SL), cannot add SELL_LIMIT (TP) due to Alpaca constraint

**Solution**: Replace existing SL order with STOP_LIMIT order
```python
# Current: SELL_STOP at $90 (SL only)
# Action: Add TP at $110

Steps:
1. Check if SL order has broker_order_id
2. If YES (broker-submitted):
   a. Call Alpaca replace_order_by_id(existing_sl.broker_order_id)
   b. Create SELL_STOP_LIMIT with stop=$90, limit=$110
   c. Mark old SL order as REPLACED in database
   d. Create new database record with new broker_order_id
3. If NO (WAITING_TRIGGER):
   a. Update database record directly
   b. Change order_type to SELL_STOP_LIMIT
   c. Set stop_price=$90, limit_price=$110
```

### Scenario 4: Existing STOP_LIMIT - Updating Prices

**Case 4A: Update TP only (keep SL same)**
```python
# Current: SELL_STOP_LIMIT with stop=$90, limit=$110
# Action: Change TP to $115

Steps:
1. If broker-submitted:
   a. Call replace_order_by_id
   b. New SELL_STOP_LIMIT with stop=$90, limit=$115
   c. Mark old as REPLACED
2. If WAITING_TRIGGER:
   a. Update database: limit_price=$115
```

**Case 4B: Update SL only (keep TP same)**
```python
# Current: SELL_STOP_LIMIT with stop=$90, limit=$110
# Action: Change SL to $88

Steps:
1. If broker-submitted:
   a. Call replace_order_by_id
   b. New SELL_STOP_LIMIT with stop=$88, limit=$110
   c. Mark old as REPLACED
2. If WAITING_TRIGGER:
   a. Update database: stop_price=$88
```

**Case 4C: Update both TP and SL**
```python
# Current: SELL_STOP_LIMIT with stop=$90, limit=$110
# Action: Change TP=$120, SL=$85

Steps:
1. If broker-submitted:
   a. Call replace_order_by_id
   b. New SELL_STOP_LIMIT with stop=$85, limit=$120
   c. Mark old as REPLACED
2. If WAITING_TRIGGER:
   a. Update database: stop_price=$85, limit_price=$120
```

### Scenario 5: Removing TP (Keep SL)

**Problem**: Have STOP_LIMIT, want to remove TP and keep only SL

**Solution**: Replace with pure STOP order
```python
# Current: SELL_STOP_LIMIT with stop=$90, limit=$110
# Action: Remove TP, keep SL=$90

Steps:
1. If broker-submitted:
   a. Call replace_order_by_id
   b. New SELL_STOP with stop=$90 (no limit_price)
   c. Mark old as REPLACED
2. If WAITING_TRIGGER:
   a. Update database: order_type=SELL_STOP, limit_price=None
```

### Scenario 6: Removing SL (Keep TP)

**Problem**: Have STOP_LIMIT, want to remove SL and keep only TP

**Solution**: Replace with pure LIMIT order
```python
# Current: SELL_STOP_LIMIT with stop=$90, limit=$110
# Action: Remove SL, keep TP=$110

Steps:
1. If broker-submitted:
   a. Call replace_order_by_id
   b. New SELL_LIMIT with limit=$110 (no stop_price)
   c. Mark old as REPLACED
2. If WAITING_TRIGGER:
   a. Update database: order_type=SELL_LIMIT, stop_price=None
```

## Implementation Functions

### Core Methods in AccountInterface

1. **`set_order_tp_sl(trading_order, tp_price, sl_price)`**
   - Sets both TP and SL together (PREFERRED method)
   - Enforces minimum TP/SL percentages
   - Updates transaction's take_profit and stop_loss values
   - Calls `_replace_tp_order()` or `_replace_sl_order()` for broker-submitted orders
   - Updates WAITING_TRIGGER orders directly in database
   - Returns tuple of (tp_order, sl_order)

2. **`set_order_tp(trading_order, tp_price)`**
   - Sets only TP
   - May need to merge with existing SL into STOP_LIMIT
   - Returns tp_order object

3. **`set_order_sl(trading_order, sl_price)`**
   - Sets only SL
   - May need to merge with existing TP into STOP_LIMIT
   - Returns sl_order object

4. **`_replace_tp_order(existing_tp, new_tp_price)`** (broker-specific)
   - Uses Alpaca's replace_order_by_id API
   - Creates new order with new broker_order_id
   - Marks old order as REPLACED
   - Returns new order object

5. **`_replace_sl_order(existing_sl, new_sl_price)`** (broker-specific)
   - Uses Alpaca's replace_order_by_id API
   - Creates new order with new broker_order_id
   - Marks old order as REPLACED
   - Returns new order object

## Database Schema Fields

### TradingOrder Model
```python
order_type: OrderType  # SELL_LIMIT, SELL_STOP, SELL_STOP_LIMIT, etc.
limit_price: float | None  # Execution price for LIMIT orders
stop_price: float | None  # Trigger price for STOP orders
broker_order_id: str | None  # Alpaca's order ID (None if WAITING_TRIGGER)
status: OrderStatus  # WAITING_TRIGGER, PENDING, ACCEPTED, FILLED, REPLACED, etc.
depends_on_order: int | None  # Parent order ID (entry order)
depends_order_status_trigger: OrderStatus | None  # Usually FILLED
transaction_id: int  # Link to Transaction
```

### Transaction Model
```python
take_profit: float | None  # TP price target
stop_loss: float | None  # SL price target
```

## Order Identification Logic

To identify existing TP/SL orders for a transaction:

```python
# Find TP order: has limit_price, opposite side of entry
tp_order = orders.filter(
    side != entry_order.side,
    limit_price is not None,
    status not in terminal_statuses
).first()

# Find SL order: has stop_price (and no limit_price for pure STOP)
sl_order = orders.filter(
    side != entry_order.side,
    stop_price is not None,
    limit_price is None,  # Pure STOP (not STOP_LIMIT)
    status not in terminal_statuses
).first()

# Find STOP_LIMIT order: has both stop_price and limit_price
stop_limit_order = orders.filter(
    side != entry_order.side,
    stop_price is not None,
    limit_price is not None,
    status not in terminal_statuses
).first()
```

## Wash Trade Prevention

**Problem**: Submitting TP/SL orders immediately after entry order causes wash trade errors

**Solution**: Use WAITING_TRIGGER status
- Entry order submits immediately with status=PENDING
- TP/SL orders created with status=WAITING_TRIGGER
- When entry order reaches status=FILLED, auto-submit mechanism triggers
- TP/SL orders change status to PENDING and submit to broker
- Prevents opposite-side orders from being active simultaneously

## Best Practices

1. **Always use STOP_LIMIT when setting both TP and SL together**
   - Single order satisfies Alpaca's constraint
   - Cleaner order management
   - Fewer API calls

2. **Check for existing orders before creating new ones**
   - Prevents duplicate orders
   - Enables proper replace logic

3. **Always update Transaction.take_profit and Transaction.stop_loss**
   - Keeps transaction records in sync
   - Enables portfolio analysis

4. **Use replace_order_by_id for live broker orders**
   - Never cancel and create manually
   - Broker handles the transition atomically

5. **Update WAITING_TRIGGER orders directly in database**
   - No broker API call needed
   - Changes take effect when order submits

6. **Mark replaced orders as REPLACED, never delete**
   - Maintains audit trail
   - Enables order history tracking

## Error Handling

1. **Replace fails**: Fall back to cancel + create new
2. **Minimum TP/SL enforcement**: Adjust prices to meet minimums, log warning
3. **Broker API errors**: Rollback database changes, restore original values
4. **Missing parent order**: Raise ValueError
5. **Missing transaction**: Raise ValueError

## Logging

Use consistent logging patterns:
```python
logger.info(f"Creating WAITING_TRIGGER TP order {order.id} at ${tp_price} (will submit when order {parent_id} is FILLED)")
logger.info(f"Replacing TP order {old_id} with new order {new_id} at broker")
logger.warning(f"TP enforcement (LONG): Profit {actual}% below minimum {min}%. Adjusting from ${original} to ${adjusted}")
```