# TP/SL Refactoring: Account-Agnostic Architecture

## Overview
Refactored the Take Profit and Stop Loss order management logic to be account-agnostic by moving enforcement and order creation from broker-specific implementations into the base `AccountInterface` class. This ensures consistency across all broker implementations and simplifies their responsibilities.

## Architecture Changes

### Before: Broker-Heavy Architecture
- **AlpacaAccount**: Handled all TP/SL logic including:
  - Minimum percent enforcement
  - Order creation and metadata storage
  - Database session management
  - Transaction updates
  - Helper methods for finding existing orders

- **AccountInterface**: Only provided abstract methods (`_set_order_tp_impl`, `_set_order_sl_impl`)

### After: Base-Class Authority Architecture
- **AccountInterface**: Now handles ALL business logic for TP/SL:
  - ✅ Enforces minimum TP/SL percent based on `get_min_tp_sl_percent()`
  - ✅ Calculates profit/loss percent from open price to target price
  - ✅ Creates/updates WAITING_TRIGGER orders in database
  - ✅ Stores TP/SL percent metadata in `order.data`
  - ✅ Updates linked transaction's `take_profit` and `stop_loss` values
  - ✅ Calls broker implementation for any broker-specific operations
  - ✅ Returns the created/updated order object

- **AlpacaAccount** (and other brokers): Now only implement broker-specific hooks:
  - `_set_order_tp_impl()`: No-op for Alpaca (database order creation is sufficient)
  - `_set_order_sl_impl()`: No-op for Alpaca (database order creation is sufficient)
  - Removed helper methods (no longer needed): 
    - `_ensure_order_in_session()`
    - `_find_existing_tp_order()` 
    - `_create_tp_order_object()`
    - `_find_existing_sl_order()`
    - `_create_sl_order_object()`

## Key Methods

### AccountInterface.set_order_tp()
```python
def set_order_tp(self, trading_order: TradingOrder, tp_price: float) -> TradingOrder:
    """
    Set take profit for an existing order.
    
    Responsibilities:
    1. Validate inputs (trading_order and tp_price not None, tp_price > 0)
    2. Enforce minimum TP percent based on open_price:
       - For LONG: TP > open, profit% = (TP - Open) / Open * 100
       - For SHORT: TP < open, profit% = (Open - TP) / Open * 100
       - If profit% < min_tp_sl_percent, adjust TP upward/downward
    3. Create/update WAITING_TRIGGER TP order in database with metadata
    4. Update transaction's take_profit value
    5. Call _set_order_tp_impl() for broker-specific operations
    6. Return the created/updated TP order
    """
```

### AccountInterface.set_order_sl()
```python
def set_order_sl(self, trading_order: TradingOrder, sl_price: float) -> TradingOrder:
    """
    Set stop loss for an existing order.
    
    Responsibilities:
    1. Validate inputs (trading_order and sl_price not None, sl_price > 0)
    2. Enforce minimum SL percent based on open_price:
       - For LONG: SL < open, loss% = (Open - SL) / Open * 100
       - For SHORT: SL > open, loss% = (SL - Open) / Open * 100
       - If loss% < min_tp_sl_percent, adjust SL outward (larger loss)
    3. Create/update WAITING_TRIGGER SL order in database with metadata
    4. Update transaction's stop_loss value
    5. Call _set_order_sl_impl() for broker-specific operations
    6. Return the created/updated SL order
    """
```

### Broker Implementations
```python
def _set_order_tp_impl(self, trading_order: TradingOrder, tp_price: float) -> None:
    """
    Broker-specific hook called AFTER base class handles all TP logic.
    For most brokers (Alpaca), this is a no-op since database order creation
    is sufficient and actual TP/SL orders are submitted when parent fills.
    
    Override only if broker needs special handling.
    """
    pass  # Alpaca: no-op, database order creation handles TP logic

def _set_order_sl_impl(self, trading_order: TradingOrder, sl_price: float) -> None:
    """
    Broker-specific hook called AFTER base class handles all SL logic.
    For most brokers (Alpaca), this is a no-op.
    """
    pass  # Alpaca: no-op, database order creation handles SL logic
```

## Order Metadata Storage

### TP/SL Percent Stored in order.data
When creating TP/SL orders, the base class stores:
```python
{
    'tp_percent_target': 5.0,      # Profit % from open price to TP
    'tp_reference_price': 100.00,  # Open price when TP was set
    'sl_percent_target': 3.0,      # Max loss % from open price to SL  
    'sl_reference_price': 100.00   # Open price when SL was set
}
```

This enables recalculation of TP/SL prices if market gaps overnight or order fills at different price.

## TP/SL Percent Enforcement Examples

### Long Position TP
- Order opens at $100 (open_price)
- User wants 5% profit TP
- TP requested: $105
- Minimum TP/SL: 3%
- Actual profit: (105 - 100) / 100 = 5% ✅ (≥ 3%, no change)
- Final TP: $105

### Long Position TP (Below Minimum)
- Order opens at $100
- User wants 2% profit TP (below minimum)
- TP requested: $102
- Minimum TP/SL: 3%
- Actual profit: (102 - 100) / 100 = 2% ❌ (< 3%, enforce)
- **Enforced TP: $103** (to meet 3% minimum)
- Log: "TP enforcement (LONG): Profit 2.00% below minimum 3%. Adjusting TP from $102.00 to $103.00"

### Short Position SL
- Order opens at $100 (short/sell)
- User wants 3% max loss SL
- SL requested: $97
- Minimum TP/SL: 3%
- Actual loss: (100 - 97) / 100 = 3% ✅ (≥ 3%, no change)
- Final SL: $97

### Short Position SL (Below Minimum)
- Order opens at $100 (short)
- User wants 2% max loss SL (too close)
- SL requested: $98
- Minimum TP/SL: 3%
- Actual loss: (100 - 98) / 100 = 2% ❌ (< 3%, enforce)
- **Enforced SL: $97** (to meet 3% minimum loss)
- Log: "SL enforcement (SHORT): Risk 2.00% below minimum 3%. Adjusting SL from $98.00 to $97.00"

## TradeActions Integration

### AdjustTakeProfitAction
Now uses: `account.set_order_tp(existing_order, tp_price)`
- Calculates TP price based on reference (ORDER_OPEN_PRICE, CURRENT_PRICE, EXPERT_TARGET_PRICE)
- Calls `set_order_tp()` which handles all enforcement and order creation
- Stores percent target metadata in main order's `data` field for trigger logic
- Returns with TP order ID and new TP price

### AdjustStopLossAction
Now uses: `account.set_order_sl(existing_order, sl_price)`
- Calculates SL price based on reference (ORDER_OPEN_PRICE, CURRENT_PRICE, EXPERT_TARGET_PRICE)
- Calls `set_order_sl()` which handles all enforcement and order creation
- Stores percent target metadata in main order's `data` field for trigger logic
- Returns with SL order ID and new SL price

## Files Modified

1. **ba2_trade_platform/core/interfaces/AccountInterface.py**
   - Added `set_order_sl()` method with full SL logic
   - Refactored `set_order_tp()` to handle order creation (not just validation)
   - Updated `_set_order_tp_impl()` and `_set_order_sl_impl()` documentation

2. **ba2_trade_platform/modules/accounts/AlpacaAccount.py**
   - Simplified `_set_order_tp_impl()` to no-op (pass statement)
   - Simplified `_set_order_sl_impl()` to no-op (pass statement)
   - Removed helper methods:
     - `_ensure_order_in_session()`
     - `_find_existing_tp_order()`
     - `_create_tp_order_object()`
     - `_find_existing_sl_order()`
     - `_create_sl_order_object()`

3. **ba2_trade_platform/core/TradeActions.py**
   - Updated `AdjustStopLossAction.execute()` to use `account.set_order_sl()`
   - Removed manual order creation and submission logic
   - Now stores SL percent metadata in order.data (matching TP logic)

## Benefits

### 1. **Consistency Across Brokers**
- All brokers enforce same minimum TP/SL percent
- All brokers store same metadata format
- No variation in business logic

### 2. **Simplified Broker Implementations**
- Brokers only implement broker-specific operations
- No need to understand order creation, enforcement, or metadata
- Easier to add new brokers

### 3. **Maintainability**
- Single source of truth for TP/SL enforcement
- Changes to enforcement logic affect all brokers automatically
- No risk of inconsistent enforcement between brokers

### 4. **Testability**
- Can test TP/SL logic independently from broker implementations
- Base class tests apply to all brokers

### 5. **Separation of Concerns**
- **Base class**: Business logic (TP/SL enforcement, validation, order creation)
- **Broker classes**: Technical implementation (API calls, session management)

## Future Enhancements

1. **Dynamic Minimum TP/SL**
   - Currently fixed at config level
   - Could vary by symbol, market condition, or account tier

2. **TP/SL Recalculation on Trigger**
   - Use stored percent metadata to recalculate prices if order gaps
   - Prevent slippage from violating minimum protection

3. **Multi-Broker Testing**
   - Test with multiple broker implementations to validate abstraction
   - Ensure consistency across Alpaca, Interactive Brokers, etc.

## Testing Recommendations

1. **Unit Tests**
   - Test `set_order_tp()` with various TP prices below/above minimum
   - Test `set_order_sl()` with various SL prices below/above minimum
   - Test for both LONG and SHORT positions

2. **Integration Tests**
   - Test end-to-end flow: AdjustTakeProfitAction → set_order_tp()
   - Test end-to-end flow: AdjustStopLossAction → set_order_sl()
   - Verify order.data metadata is stored correctly

3. **Trade Simulation**
   - Simulate trades with various TP/SL scenarios
   - Verify minimum enforcement prevents trades at poor profit/loss ratios
