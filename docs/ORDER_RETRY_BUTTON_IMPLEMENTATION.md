# Order Retry Button Implementation

## Summary
Added a "Retry Selected Orders" button to the pending orders table alongside the existing "Delete Selected Orders" button. This allows users to easily retry orders that have failed with ERROR status.

## Implementation Details

### UI Changes
- **Location**: `ba2_trade_platform/ui/pages/overview.py` in the `AccountOverviewTab` class
- **Button Position**: Added in the `top-left` slot of the pending orders table, next to the delete button
- **Icon**: `refresh` icon with orange color (`color=orange`)
- **Enabled State**: Only enabled when orders are selected (same binding as delete button)

### Functionality

#### `_handle_retry_selected_orders(self, selected_rows)`
- Filters selected orders to only include those with ERROR status
- Shows a confirmation dialog explaining what will happen
- Provides warning if non-ERROR orders are selected (they will be skipped)
- Calls `_confirm_retry_orders` to execute the retry operation

#### `_confirm_retry_orders(self, order_ids, dialog)`
- Iterates through ERROR orders and attempts to resubmit them
- Uses the account provider's `submit_order()` method (same as manual submission)
- Provides detailed feedback:
  - Success count for retried orders
  - Error messages for failed retries (shows first 3 errors)
- Refreshes the pending orders table after completion
- Closes the confirmation dialog

### Business Logic
1. **Order Status Check**: Only orders with `OrderStatus.ERROR` can be retried
2. **Account Provider**: Uses the same submission logic as the existing "Submit Order" functionality
3. **Error Handling**: Comprehensive error handling with user-friendly messages
4. **Logging**: All retry attempts are logged for debugging and audit purposes

### User Experience
1. User selects one or more orders from the pending orders table
2. Clicks "Retry Selected Orders" button
3. Confirmation dialog shows:
   - Number of ERROR orders that will be retried
   - Warning if non-ERROR orders are selected (will be skipped)
   - Clear explanation of what the retry will do
4. User confirms the retry operation
5. System attempts to resubmit each ERROR order
6. Success/error feedback is displayed via notifications
7. Table refreshes to show updated order statuses

### Error Scenarios Handled
- **Order not found**: Graceful handling with error message
- **Non-ERROR status**: Orders are skipped with explanation
- **Account not found**: Error message provided
- **Provider unavailable**: Error message provided
- **Broker submission failure**: Specific error details provided
- **Multiple failures**: Aggregated error reporting (first 3 errors shown)

### Integration
- **Account Providers**: Uses existing `submit_order()` method from account interfaces
- **Database**: Uses existing `get_instance()` helpers for data retrieval
- **UI Framework**: Integrates seamlessly with NiceGUI table component
- **Notifications**: Uses existing `ui.notify()` system for user feedback
- **Logging**: Uses existing logger for audit trail

## Usage
1. Navigate to the Overview page
2. Check the "Pending Orders" section
3. Select one or more orders (especially those with ERROR status)
4. Click the "Retry Selected Orders" button (orange refresh icon)
5. Confirm the retry operation in the dialog
6. Review the feedback notifications and refreshed table

## Testing
The implementation has been tested with a verification script that confirms:
- ✅ All methods are present in the correct class
- ✅ Method signatures are correct
- ✅ Required imports are available
- ✅ Integration points exist and are accessible

This feature addresses the common issue of orders failing due to temporary broker issues, insufficient quantities, or network problems, allowing users to easily retry them without manual intervention.