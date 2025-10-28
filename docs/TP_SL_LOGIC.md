# Take Profit / Stop Loss Order Management Logic

## Architecture Principles

1. **Transaction as Source of Truth**: `transaction.take_profit` and `transaction.stop_loss` are the authoritative TP/SL prices
2. **Stateless Interface**: `adjust_tp()`, `adjust_sl()`, `adjust_tp_sl()` are stateless - they determine actions based on current state
3. **Provider-Level Implementation**: AccountInterface defines generic stubs; all logic implemented at provider level (e.g., AlpacaAccount)
4. **Mandatory Interface Usage**: All code adjusting TP/SL MUST call account interface methods (no direct order creation)

## Alpaca-Specific Implementation

### Order Types
- **OCO (One-Cancels-Other)**: Used when BOTH TP and SL are defined
  - Single order with `limit_price` (TP) and `stop_price` (SL)
  - When one fills, the other cancels automatically
  
- **OTO (One-Triggers-Other)**: Used when ONLY TP or ONLY SL is defined
  - Single order with either `limit_price` (TP only) or `stop_price` (SL only)

Reference: [Alpaca Bracket Orders Documentation](https://docs.alpaca.markets/docs/orders-at-alpaca#bracket-orders)

### Trigger Mechanism
- TP/SL orders are ALWAYS created as separate OTO/OCO orders (never as bracket legs)
- These orders are triggered AFTER entry order execution
- Triggered orders should also call `adjust_tp()` / `adjust_sl()` / `adjust_tp_sl()` methods

## Decision Flow Logic

### Scenario 1: NO Existing TP/SL Orders

#### Entry Order NOT Yet Sent to Broker (`get_unsent_statuses()`)
**Action**: Create PENDING OTO/OCO order in database
- Set `depends_on_order` = entry_order.id
- Set `status = OrderStatus.PENDING`
- Order will be submitted when entry order fills (via order update monitoring)
- Order type determined by current transaction state: OCO if both TP and SL exist, OTO if only one

#### Entry Order Already Executed (`get_executed_statuses()`)
**Action**: Create OTO/OCO order and immediately submit to broker
- Create order in database
- Call `submit_order()` to send to Alpaca API
- Broker order ID will be assigned upon successful submission
- Order type determined by current transaction state: OCO if both TP and SL exist, OTO if only one

### Scenario 2: Existing TP/SL Orders Present

#### Entry Order NOT Yet Sent to Broker (`get_unsent_statuses()`)
**Action**: Modify existing PENDING order in database
- Update `limit_price` and/or `stop_price` in database
- Update `order_type` if transitioning between OTO ↔ OCO
- Update `data` field with new percent targets
- No broker interaction needed (order not submitted yet)

#### Entry Order Already Executed (`get_executed_statuses()`)
**Action**: Attempt to REPLACE existing TP/SL order via broker API

##### **OTO ↔ OCO Transitions** (CRITICAL):
If order type needs to change (e.g., OTO → OCO when adding SL, or OCO → OTO when removing TP/SL):
- **Cannot use replace API** (Alpaca doesn't support changing order type via replace)
- Mark existing order `status = OrderStatus.PENDING_CANCEL`
- Create new order with correct type (OTO or OCO)
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

### Order Type Determination (OTO vs OCO)

**CRITICAL RULE**: Order type is ALWAYS determined by the CURRENT state of `transaction.take_profit` and `transaction.stop_loss`:

- **OCO (One-Cancels-Other)**: Both `take_profit` AND `stop_loss` are set (not None, > 0)
  - Single order with both `limit_price` (TP) and `stop_price` (SL)
  - When one fills, the other cancels automatically
  
- **OTO (One-Triggers-Other)**: Only `take_profit` OR only `stop_loss` is set
  - Single order with either `limit_price` (TP only) or `stop_price` (SL only)

**Examples of transitions**:
1. **OTO → OCO**: User has TP-only order, then calls `adjust_sl()` to add SL
   - System checks transaction: TP exists → Creates OCO with both prices
   - Old OTO order must be canceled (can't replace with different type)
   
2. **OCO → OTO**: User has TP+SL order, removes SL by setting `transaction.stop_loss = None`
   - When `adjust_tp()` is called, system checks transaction: only TP exists → Creates OTO
   - Old OCO order must be canceled (can't replace with different type)
   
3. **OTO → OTO**: User has TP-only order, adjusts TP price (SL still None)
   - System checks transaction: only TP exists → Stays OTO
   - Can use replace API (same order type)

## Implementation Checklist

- [x] Add `CoreOrderType.OCO` and `CoreOrderType.OTO` to order type enum
- [x] Implement `adjust_tp()` method (stateless, handles all scenarios)
- [x] Implement `adjust_sl()` method (stateless, handles all scenarios)  
- [x] Implement `adjust_tp_sl()` method (creates single OCO order, not two separate orders)
- [x] Implement OTO ↔ OCO transitions in replace methods
- [x] Handle order type changes by canceling old and creating new order
- [ ] Implement order replace logic with fallback to cancel-then-create (partially done - needs testing)
- [ ] Add `OrderStatus.PENDING_CANCEL` state handling in order monitoring
- [ ] Ensure triggered orders call adjust methods (not direct order creation)

## Common Pitfalls

1. ❌ **Creating two separate orders for TP+SL**: Should create ONE OCO order
2. ❌ **Bypassing account interface**: Always use `adjust_tp()` / `adjust_sl()` / `adjust_tp_sl()`
3. ❌ **Hardcoding default prices**: Never use fallback values for None prices (fail explicitly)
4. ❌ **Ignoring replace failures**: Must implement cancel-then-create fallback
5. ❌ **Not handling PENDING_CANCEL**: Status monitoring must respect this state
6. ❌ **Not handling OTO ↔ OCO transitions**: Order type must be determined from current transaction state
7. ❌ **Using replace API for order type changes**: Must cancel old and create new when type changes