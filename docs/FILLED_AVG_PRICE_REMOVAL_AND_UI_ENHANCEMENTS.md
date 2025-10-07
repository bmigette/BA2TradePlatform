# Filled Average Price Removal and UI Enhancements

**Date**: 2025-10-07  
**Status**: ✅ Completed

## Summary

This document describes the removal of the `filled_avg_price` field from the `TradingOrder` model and the enhancement of the rules evaluation UI to display more detailed information about conditions and actions.

## Changes Made

### 1. Database Schema Changes

#### Removed Field
- **Field**: `filled_avg_price` from `TradingOrder` model
- **Reason**: Redundant field - `open_price` serves the same purpose and is more semantically accurate
- **Migration**: `39406a9ffef1_remove_filled_avg_price_from_tradingorder.py`

#### Updated Field Usage
- All references to `filled_avg_price` replaced with `open_price`
- `open_price` now stores the broker's `filled_avg_price` directly (no duplication)

### 2. Code Changes

#### models.py (TradingOrder)
**Before**:
```python
filled_avg_price: float | None = Field(default=None, description="Average price at which the order was filled")
open_price: float | None = Field(default=None, description="Price at which the order opened (for filled orders)")
```

**After**:
```python
open_price: float | None = Field(default=None, description="Price at which the order opened (for filled orders)")
```

#### models.py (Transaction.get_current_open_equity)
**Before**:
```python
# Use open_price if available, otherwise use filled_avg_price
price = order.open_price if order.open_price else order.filled_avg_price
```

**After**:
```python
# Use open_price for filled orders
price = order.open_price
```

#### AlpacaAccount.py (alpaca_order_to_trading_order)
**Before**:
```python
filled_avg_price=getattr(order, "filled_avg_price", None),
open_price=getattr(order, "filled_avg_price", None),  # Use filled_avg_price as open_price for filled orders
```

**After**:
```python
open_price=getattr(order, "filled_avg_price", None),  # Use broker's filled_avg_price as open_price
```

#### AlpacaAccount.py (refresh_orders)
**Before**:
```python
# Update filled_avg_price if it changed
if alpaca_order.filled_avg_price and (db_order.filled_avg_price is None or float(db_order.filled_avg_price) != float(alpaca_order.filled_avg_price)):
    logger.debug(f"Order {db_order.id} filled_avg_price changed: {db_order.filled_avg_price} -> {alpaca_order.filled_avg_price}")
    db_order.filled_avg_price = alpaca_order.filled_avg_price
    has_changes = True

# Update open_price if it changed (use filled_avg_price)
if alpaca_order.open_price and (db_order.open_price is None or float(db_order.open_price) != float(alpaca_order.open_price)):
    logger.debug(f"Order {db_order.id} open_price changed: {db_order.open_price} -> {alpaca_order.open_price}")
    db_order.open_price = alpaca_order.open_price
    has_changes = True
```

**After**:
```python
# Update open_price if it changed (use broker's filled_avg_price)
if alpaca_order.filled_avg_price and (db_order.open_price is None or float(db_order.open_price) != float(alpaca_order.filled_avg_price)):
    logger.debug(f"Order {db_order.id} open_price changed: {db_order.open_price} -> {alpaca_order.filled_avg_price}")
    db_order.open_price = alpaca_order.filled_avg_price
    has_changes = True
```

#### AccountInterface.py (refresh_transactions)
**Before**:
```python
# Set open_price from the first filled market entry order
if not transaction.open_price:
    for order in market_entry_orders:
        if order.status in executed_statuses and order.limit_price:
            transaction.open_price = order.limit_price
            has_changes = True
            break
        # For market orders, try to get current price from broker
        elif order.status in executed_statuses:
            # We don't have exact fill price, but we can try to get current price
            # This is a fallback - ideally filled_avg_price would be stored
            try:
                current_price = self.get_instrument_current_price(order.symbol)
                if current_price:
                    transaction.open_price = current_price
                    has_changes = True
                    break
            except:
                pass
```

**After**:
```python
# Set open_price from the oldest filled market entry order's open_price
if not transaction.open_price:
    # Sort market entry orders by created_at to get the oldest one
    filled_entry_orders = [
        order for order in market_entry_orders 
        if order.status in executed_statuses
    ]
    if filled_entry_orders:
        # Sort by created_at to get the oldest filled order
        oldest_order = min(filled_entry_orders, key=lambda o: o.created_at or datetime.min.replace(tzinfo=timezone.utc))
        if oldest_order.open_price:
            transaction.open_price = oldest_order.open_price
            has_changes = True
            logger.debug(f"Transaction {transaction.id} open_price set to {oldest_order.open_price} from oldest filled order {oldest_order.id}")
```

**Key Improvement**: Transaction `open_price` now matches the `open_price` of the **oldest** filled market entry order, providing a more accurate entry price for the position.

#### AccountInterface.py (close_price logic)
**Before**:
```python
# Set close_price from the first filled closing order
if not transaction.close_price:
    closing_order = filled_closing_orders[0]
    if closing_order.limit_price:
        transaction.close_price = closing_order.limit_price
    else:
        # Fallback to current price for market orders
        try:
            current_price = self.get_instrument_current_price(closing_order.symbol)
            if current_price:
                transaction.close_price = current_price
        except:
            pass
```

**After**:
```python
# Set close_price from the first filled closing order's open_price
if not transaction.close_price:
    closing_order = filled_closing_orders[0]
    if closing_order.open_price:
        transaction.close_price = closing_order.open_price
```

**Key Improvement**: Uses actual execution price (`open_price`) instead of fallback methods.

### 3. UI Enhancements - Rules Evaluation Page

#### Enhanced Condition Display
**Added**:
- Display of `reference_value` in condition labels (e.g., "ref: 80.5")
- More comprehensive condition summary showing operator, value, and reference value

**Before**:
```python
condition_label = f'{trigger_key}: {event_type}'
if operator and value is not None:
    condition_label += f' {operator} {value}'
```

**After**:
```python
condition_label = f'{trigger_key}: {event_type}'
if operator and value is not None:
    condition_label += f' {operator} {value}'
if reference_value:
    condition_label += f' (ref: {reference_value})'
```

**Example Output**: 
- Before: `confidence: value >= 70`
- After: `confidence: value >= 70 (ref: 80.5)`

#### Enhanced Action Display
**Added**:
- Prominent display of Take Profit (TP) and Stop Loss (SL) percentages
- Display of quantity percentage, order type, limit price, and stop price
- Formatted as readable summary line below action description

**New Feature**:
```python
# Display action-specific values (TP/SL, etc.)
action_config = result.get('action_config', {})
if action_config:
    # Build a readable summary of important parameters
    params = []
    if 'take_profit_percent' in action_config:
        params.append(f"TP: {action_config['take_profit_percent']}%")
    if 'stop_loss_percent' in action_config:
        params.append(f"SL: {action_config['stop_loss_percent']}%")
    if 'quantity_percent' in action_config:
        params.append(f"Qty: {action_config['quantity_percent']}%")
    if 'order_type' in action_config:
        order_type = action_config['order_type']
        if hasattr(order_type, 'value'):
            order_type = order_type.value
        params.append(f"Type: {order_type}")
    if 'limit_price' in action_config:
        params.append(f"Limit: ${action_config['limit_price']}")
    if 'stop_price' in action_config:
        params.append(f"Stop: ${action_config['stop_price']}")
    
    if params:
        ui.label(' | '.join(params)).classes('text-sm font-medium text-blue-700 mt-1')
```

**Example Output**: `TP: 15.0% | SL: 5.0% | Qty: 100.0% | Type: MARKET`

## Benefits

### 1. Database Simplification
- ✅ Eliminated redundant field (`filled_avg_price`)
- ✅ Single source of truth for execution prices (`open_price`)
- ✅ Cleaner schema with less confusion

### 2. Improved Accuracy
- ✅ Transaction `open_price` now uses the **oldest** filled market entry order
- ✅ More accurate position entry price tracking
- ✅ Direct use of broker's execution price (no fallbacks to current market price)

### 3. Better UI/UX
- ✅ Conditions show operators, values, and reference values
- ✅ Actions display TP/SL percentages prominently
- ✅ Clear visual summary of action parameters
- ✅ Easier to understand rule evaluation results

## Migration

### Database Migration
```bash
# Migration applied successfully
alembic upgrade head
```

**Migration File**: `alembic/versions/39406a9ffef1_remove_filled_avg_price_from_.py`

**Operations**:
- **Upgrade**: Drops `filled_avg_price` column from `tradingorder` table
- **Downgrade**: Re-adds `filled_avg_price` column if rollback needed

### Data Preservation
- ✅ No data loss - `open_price` already contains the same data as `filled_avg_price`
- ✅ All existing orders retain their execution prices in `open_price` field
- ✅ Backward compatible with existing transactions

## Testing Recommendations

### 1. Order Execution Flow
- [ ] Create new market order and verify `open_price` is populated from broker's `filled_avg_price`
- [ ] Verify `refresh_orders()` updates `open_price` correctly
- [ ] Check that transaction `open_price` matches oldest filled market entry order

### 2. Transaction Lifecycle
- [ ] Create transaction with multiple entry orders
- [ ] Verify transaction `open_price` uses the oldest filled order
- [ ] Verify transaction `close_price` uses closing order's `open_price`

### 3. UI Verification
- [ ] Test ruleset evaluation page with various conditions
- [ ] Verify condition operators, values, and reference values display correctly
- [ ] Verify action TP/SL parameters display prominently
- [ ] Test with different action types (BUY, SELL, CLOSE, etc.)

## Files Modified

1. `ba2_trade_platform/core/models.py`
2. `ba2_trade_platform/modules/accounts/AlpacaAccount.py`
3. `ba2_trade_platform/core/AccountInterface.py`
4. `ba2_trade_platform/ui/pages/rulesettest.py`
5. `alembic/versions/39406a9ffef1_remove_filled_avg_price_from_.py` (NEW)

## Completion Status

- ✅ Database schema updated
- ✅ Migration created and applied
- ✅ All code references updated
- ✅ UI enhancements implemented
- ✅ No compilation errors
- ✅ Documentation created

## Future Considerations

1. **Price Accuracy**: Consider storing execution timestamp with `open_price` for better audit trail
2. **Average Price Calculation**: If multiple partial fills, consider weighted average calculation
3. **UI Enhancements**: Add charts/graphs to visualize rule evaluation results
4. **Export Functionality**: Allow exporting rule evaluation results to CSV/JSON
