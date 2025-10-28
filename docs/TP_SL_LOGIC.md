# Take Profit / Stop Loss Order Management Logic

## Architecture Principles

1. **Transaction as Source of Truth**: `transaction.take_profit` and `transaction.stop_loss` are the authoritative TP/SL prices
2. **Stateless Interface**: `adjust_tp()`, `adjust_sl()`, `adjust_tp_sl()` are stateless - they determine actions based on current state
3. **Provider-Level Implementation**: AccountInterface defines generic stubs; all logic implemented at provider level (e.g., AlpacaAccount)
4. **Mandatory Interface Usage**: All code adjusting TP/SL MUST call account interface methods (no direct order creation)

## Alpaca-Specific Implementation

### Order Types
- **OCO (One-Cancels-Other)**: Used when BOTH TP and SL are defined
  - MARKET order (no `limit_price` on main order) with both `take_profit` and `stop_loss` legs
  - `take_profit` leg has `limit_price` (TP price)
  - `stop_loss` leg has both `stop_price` (trigger) and `limit_price` (execution limit)
  - When either the take_profit fills or the stop_loss triggers, the other cancels automatically
  - **CRITICAL**: Main order has no limit_price - prices are only in the legs
  
- **SELL_LIMIT / BUY_LIMIT**: Used when ONLY TP is defined (no SL)
  - Simple limit order with `limit_price` for the take-profit target
  - Direction determined by entry side (SELL for BUY entries, BUY for SELL entries)
  
- **SELL_STOP / BUY_STOP**: Used when ONLY SL is defined (no TP)
  - Simple stop order with `stop_price` for the stop-loss target
  - Direction determined by entry side (SELL for BUY entries, BUY for SELL entries)

Reference: [Alpaca Orders Documentation](https://docs.alpaca.markets/docs/orders-at-alpaca)

### Trigger Mechanism
- TP/SL orders are ALWAYS created as separate OTO/OCO orders (never as bracket legs)
- These orders are triggered AFTER entry order execution
- Triggered orders should also call `adjust_tp()` / `adjust_sl()` / `adjust_tp_sl()` methods

## Decision Flow Logic

### Scenario 1: NO Existing TP/SL Orders

#### Entry Order NOT Yet Sent to Broker (`get_unsent_statuses()`)
**Action**: Create PENDING OCO/SELL_LIMIT/BUY_LIMIT/SELL_STOP/BUY_STOP order in database
- Set `depends_on_order` = entry_order.id
- Set `status = OrderStatus.PENDING`
- Order will be submitted when entry order fills (via order update monitoring)
- Order type determined by current transaction state: OCO if both TP and SL exist, direction-specific LIMIT if only TP, direction-specific STOP if only SL

#### Entry Order Already Executed (`get_executed_statuses()`)
**Action**: Create OCO/direction-specific LIMIT/STOP order and immediately submit to broker
- Create order in database
- Call `submit_order()` to send to Alpaca API
- Broker order ID will be assigned upon successful submission
- Order type determined by current transaction state: OCO if both TP and SL exist, direction-specific LIMIT if only TP, direction-specific STOP if only SL

### Scenario 2: Existing TP/SL Orders Present

#### Entry Order NOT Yet Sent to Broker (`get_unsent_statuses()`)
**Action**: Modify existing PENDING order in database
- Update `limit_price` and/or `stop_price` in database
- Update `order_type` if transitioning between direction-specific types ↔ OCO
- Update `data` field with new percent targets
- No broker interaction needed (order not submitted yet)

#### Entry Order Already Executed (`get_executed_statuses()`)
**Action**: Attempt to REPLACE existing TP/SL order via broker API

##### **Direction-Specific Types ↔ OCO Transitions** (CRITICAL):
If order type needs to change (e.g., SELL_LIMIT → OCO when adding SL, or OCO → BUY_LIMIT when removing SL):
- **Cannot use replace API** (Alpaca doesn't support changing order type via replace)
- Mark existing order `status = OrderStatus.PENDING_CANCEL`
- Create new order with correct type (direction-specific LIMIT/STOP or OCO)
- Set new order `depends_on_order` = existing_order.id
- Submit cancel request for existing order
- New order will be submitted when cancel confirms

##### If Replace Succeeds (Same Order Type):
- Alpaca API accepts replacement
- Update database with new broker order ID and prices
- Old order automatically canceled by broker

##### If Replace Fails (Same Order Type):
This requires a **two-step cancel-then-create workflow**:

1. **Mark existing order for cancellation**:
   - Set existing order `status = OrderStatus.PENDING_CANCEL`
   - Submit cancel request to broker
   
2. **Create new pending OTO/OCO order**:
   - Create new order in database with `status = OrderStatus.PENDING`
   - Set `depends_on_order` = existing_order.id (to trigger after cancel)
   - Order will be submitted when existing order reaches `CANCELED` state

3. **Order status monitoring rules** (in order update loop):
   - Orders with `status = PENDING_CANCEL` can ONLY transition to `CANCELED`
   - Ignore broker status updates showing `FILLED` for `PENDING_CANCEL` orders
   - When `CANCELED` confirmed, submit the waiting pending order

### Order Type Determination (Direction-Specific Types vs OCO)

**CRITICAL RULE**: Order type is ALWAYS determined by the CURRENT state of `transaction.take_profit` and `transaction.stop_loss`:

- **OCO (One-Cancels-Other)**: Both `take_profit` AND `stop_loss` are set (not None, > 0)
  - MARKET order (no limit_price on main) with `take_profit` and `stop_loss` legs
  - `take_profit` leg: `limit_price` at TP target
  - `stop_loss` leg: `stop_price` (trigger) and `limit_price` (execution limit, slightly worse than stop)
  - When either the take_profit fills or the stop_loss triggers, the other cancels automatically
  - **CRITICAL**: Main order is MarketOrderRequest, prices only in legs (Alpaca API requirement)
  
- **SELL_LIMIT / BUY_LIMIT**: Only `take_profit` is set (no `stop_loss`)
  - Simple limit order with `limit_price` for the take-profit target
  - SELL_LIMIT for closing BUY entries, BUY_LIMIT for closing SELL entries
  
- **SELL_STOP / BUY_STOP**: Only `stop_loss` is set (no `take_profit`)
  - Simple stop order with `stop_price` for the stop-loss target
  - SELL_STOP for closing BUY entries, BUY_STOP for closing SELL entries

**Examples of transitions**:
1. **SELL_LIMIT → OCO**: User has TP-only order on BUY entry, then calls `adjust_sl()` to add SL
   - System checks transaction: both TP and SL exist → Creates OCO with both prices
   - Old SELL_LIMIT order must be canceled (can't replace with different type)
   
2. **OCO → BUY_LIMIT**: User has TP+SL order on SELL entry, removes SL by setting `transaction.stop_loss = None`
   - When `adjust_tp()` is called, system checks transaction: only TP exists → Creates BUY_LIMIT
   - Old OCO order must be canceled (can't replace with different type)
   
3. **SELL_LIMIT → SELL_LIMIT**: User has TP-only order, adjusts TP price (SL still None)
   - System checks transaction: only TP exists → Stays SELL_LIMIT
   - Can use replace API (same order type)

## Implementation Checklist

- [x] Add `CoreOrderType.OCO` to order type enum
- [x] Implement `adjust_tp()` method (stateless, handles all scenarios)
- [x] Implement `adjust_sl()` method (stateless, handles all scenarios)  
- [x] Implement `adjust_tp_sl()` method (creates single OCO order, not two separate orders)
- [x] Implement SELL_LIMIT/BUY_LIMIT/SELL_STOP/BUY_STOP ↔ OCO transitions in replace methods
- [x] Handle order type changes by canceling old and creating new order
- [x] Implement order replace logic with fallback to cancel-then-create
- [x] Add `OrderStatus.PENDING_CANCEL` state handling in order monitoring
- [x] Ensure triggered orders call adjust methods (implemented via `_check_and_submit_dependent_orders`)

## Common Pitfalls

1. ❌ **Creating two separate orders for TP+SL**: Should create ONE OCO order
2. ❌ **Bypassing account interface**: Always use `adjust_tp()` / `adjust_sl()` / `adjust_tp_sl()`
3. ❌ **Hardcoding default prices**: Never use fallback values for None prices (fail explicitly)
4. ❌ **Ignoring replace failures**: Must implement cancel-then-create fallback
5. ❌ **Not handling PENDING_CANCEL**: Status monitoring must respect this state
6. ❌ **Not handling direction-specific ↔ OCO transitions**: Order type must be determined from current transaction state
7. ❌ **Using replace API for order type changes**: Must cancel old and create new when type changes
8. ❌ **Using OTO for single TP/SL**: Should use direction-specific LIMIT (TP only) or STOP (SL only), not OTO