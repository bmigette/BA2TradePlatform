# Zero-Quantity Order Submission Feature

**Date**: 2025-01-24  
**Status**: ✅ Complete  
**Feature**: Interactive quantity input dialog for zero-quantity pending orders

## Problem Summary

When orders are created with `quantity=0` (e.g., due to risk management calculation issues), users could not submit them to the broker. Clicking the submit button would either fail silently or show errors, with no way to correct the quantity before submission.

## Solution Implemented

### 1. Quantity Input Dialog for Zero-Quantity Orders

When a user attempts to submit an order with `quantity=0`, a dialog now appears asking for the quantity:

**Dialog Features:**
- Displays order details (Order ID, Symbol, Side)
- Shows current market price for reference (if available)
- Provides numeric input field for quantity (min=1, integer only)
- Includes note that both order and linked transaction will be updated
- "Update & Submit" button to proceed with submission

### 2. Automatic Updates

When the user enters a quantity and clicks "Update & Submit":
1. **Order is updated**: `order.quantity` is set to the new value
2. **Linked transaction is updated**: If a `Transaction` record exists for this order, its `quantity` is also updated
3. **Order is submitted**: After updates, the order is automatically submitted to the broker

### 3. Expanded Submit Button Availability

**Before:** Only `PENDING` orders could be submitted via the submit button  
**After:** Orders with `PENDING`, `WAITING_TRIGGER`, or `ERROR` status can now be submitted

This aligns with the user's requirement to handle all three statuses.

### 4. Retry Orders Protection

When using "Retry Selected Orders" for ERROR orders:
- Orders with `quantity=0` are now skipped with a warning
- User is notified: "X order(s) with quantity=0 skipped. Please update quantity manually and submit."
- Prevents automatic retry failures for zero-quantity orders

## Code Changes

### File Modified
`ba2_trade_platform/ui/pages/overview.py`

### Changes Summary

1. **Added Transaction import** (line 11):
   ```python
   from ...core.models import AccountDefinition, MarketAnalysis, ExpertRecommendation, ExpertInstance, AppSetting, TradingOrder, Transaction
   ```

2. **Updated `can_submit` logic** (line 1948):
   ```python
   'can_submit': order.status in [OrderStatus.PENDING, OrderStatus.WAITING_TRIGGER, OrderStatus.ERROR] and not order.broker_order_id
   ```

3. **Enhanced `_handle_submit_order()` method** (lines 2044-2075):
   - Added check for allowed statuses (PENDING, WAITING_TRIGGER, ERROR)
   - Added zero-quantity check before submission
   - Shows quantity input dialog if `order.quantity == 0`

4. **New `_show_quantity_input_dialog()` method** (lines 2077-2115):
   - Creates interactive dialog with quantity input
   - Fetches and displays current market price
   - Validates input (must be >= 1)
   - Calls update and submit methods

5. **New `_update_quantity_and_submit()` method** (lines 2117-2149):
   - Updates order quantity
   - Finds and updates linked transaction
   - Logs all updates
   - Submits order after successful update

6. **Refactored `_submit_order_to_broker()` method** (lines 2151-2189):
   - Extracted submission logic from `_handle_submit_order()`
   - Reusable for both direct submission and post-update submission
   - Handles account provider interaction

7. **Enhanced `_confirm_retry_orders()` method** (lines 2230-2279):
   - Added check for zero-quantity orders
   - Skips retry for orders with `quantity=0`
   - Notifies user about skipped orders

## User Experience

### Scenario 1: Submit Zero-Quantity Order
1. User navigates to "Account Overview" → "Pending Orders" tab
2. User sees order with quantity=0 and clicks submit button (📤 icon)
3. Dialog appears: "Order has Quantity = 0"
4. Dialog shows: Order #407 - CHTR (BUY)
5. Dialog shows: Current Price: $243.97
6. User enters: Quantity = 2
7. User clicks "Update & Submit"
8. Order quantity updated to 2, transaction updated to 2
9. Order submitted to broker
10. Success notification: "Order 407 submitted successfully to Alpaca"
11. Table refreshes automatically

### Scenario 2: Retry ERROR Orders with Zero-Quantity
1. User selects multiple ERROR orders (some with qty=0, some with qty>0)
2. User clicks "Retry Selected Orders"
3. System processes:
   - Orders with qty>0: Submitted automatically
   - Orders with qty=0: Skipped with warning
4. Notification: "Successfully retried 5 order(s)"
5. Notification: "2 order(s) with quantity=0 skipped. Please update quantity manually and submit."
6. User can then individually submit zero-qty orders using the submit button

### Scenario 3: Submit WAITING_TRIGGER Order with Zero-Quantity
1. Order is in WAITING_TRIGGER status (e.g., TP order waiting for position to open)
2. User wants to manually submit it
3. User clicks submit button
4. Dialog appears to input quantity (same as Scenario 1)
5. After update, order is submitted

## Technical Details

### Database Updates
Both `TradingOrder` and `Transaction` models are updated in the database:

```python
# Update order
order.quantity = new_quantity
update_instance(order)

# Update linked transaction
transaction.quantity = new_quantity
update_instance(transaction)
```

### Current Price Fetching
Attempts to fetch current market price for reference:
```python
provider_obj = get_account_instance_from_id(account.id)
current_price = provider_obj.get_instrument_current_price(order.symbol)
```

If price fetch fails, the dialog still works but without the price reference.

### Validation
- Quantity must be greater than 0
- Quantity is converted to integer (no fractional shares)
- Order status must be PENDING, WAITING_TRIGGER, or ERROR
- Order must not already have a broker_order_id

## Benefits

1. **User Empowerment**: Users can now manually correct quantity=0 orders
2. **No Lost Orders**: Orders that failed risk management allocation can be recovered
3. **Transparency**: Shows current price to help users make informed decisions
4. **Data Consistency**: Both order and transaction records stay synchronized
5. **Error Prevention**: Retry function won't fail on zero-quantity orders
6. **Flexible Status Handling**: Works for PENDING, WAITING_TRIGGER, and ERROR orders

## Edge Cases Handled

1. **Missing Price**: Dialog works even if current price cannot be fetched
2. **No Transaction**: If no linked transaction exists, only order is updated
3. **Invalid Quantity**: Validation ensures quantity >= 1
4. **Already Submitted**: Checks for existing broker_order_id before submission
5. **Wrong Status**: Only allows submission for appropriate statuses

## Future Enhancements

1. Consider adding bulk quantity update for multiple selected orders
2. Add quantity validation against available balance
3. Show recommended quantity based on current risk management settings
4. Add option to run risk management calculation before submission
5. Support fractional shares if broker allows it

## Testing Recommendations

1. Test with Expert 9's zero-quantity orders (transactions 190-203)
2. Verify quantity update persists in database
3. Verify linked transaction gets updated
4. Test with orders that have no linked transaction
5. Test retry function with mix of zero and non-zero quantity orders
6. Verify submit button appears for all three statuses (PENDING, WAITING_TRIGGER, ERROR)
7. Test price fetching failure scenario
8. Verify order submission success after quantity update
