# Order Mapping Dialog Fix - NiceGUI Select Value Error

## Problem
The order mapping dialog was throwing a `ValueError: Invalid value: <uuid>` error when trying to create a NiceGUI select element. This happened when:

1. A database order had a `broker_order_id` (UUID)
2. That `broker_order_id` was not found in the list of available broker orders
3. The code tried to set the UUID as the default value without ensuring it was in the options list

## Root Cause
The original logic had a flaw in the conditional structure:

```python
# ORIGINAL PROBLEMATIC CODE
default_value = suggested_match['value'] if suggested_match else None
if db_order.broker_order_id and not any(opt['value'] == db_order.broker_order_id for opt in broker_options[1:]):
    # Add missing broker ID to options
    broker_options.insert(1, {...})
    default_value = db_order.broker_order_id
elif db_order.broker_order_id:
    # BUG: This sets default_value to broker_order_id even if it's not in options!
    default_value = db_order.broker_order_id
```

The `elif` clause would execute when `broker_order_id` existed but was already in the options list. However, if the broker order existed but wasn't found due to filtering or other issues, it could still set a value that wasn't in the options.

## Solution
Restructured the logic to be more explicit and added safety checks:

```python
# FIXED CODE
default_value = suggested_match['value'] if suggested_match else None
if db_order.broker_order_id:
    # Check if current broker ID exists in options
    broker_id_exists = any(opt['value'] == db_order.broker_order_id for opt in broker_options[1:])
    if not broker_id_exists:
        # Current broker ID not in list, add it
        broker_options.insert(1, {
            'label': f'🔴 {db_order.broker_order_id} (CURRENT - NOT FOUND)',
            'value': db_order.broker_order_id,
            'disabled': True
        })
    # Always set current broker ID as default if it exists
    default_value = db_order.broker_order_id

# Final safety check: ensure default_value is in options
if default_value is not None and not any(opt['value'] == default_value for opt in broker_options):
    logger.warning(f"Default value '{default_value}' not found in broker options, falling back to None")
    default_value = None
```

## Key Improvements

### 1. Explicit Existence Check
- Separated the logic for checking if broker ID exists in options
- Always adds missing broker IDs to the options list when they exist

### 2. Safety Net
- Added final validation before creating the select widget
- Falls back to `None` (valid option) if default value is invalid
- Logs warnings for debugging when fallback occurs

### 3. Clearer Logic Flow
- Simplified conditional structure
- More predictable behavior in edge cases
- Better error handling and logging

## Test Coverage
Created comprehensive test cases covering:
- ✅ Broker ID exists in options
- ✅ Broker ID doesn't exist in options (gets added)
- ✅ No broker ID, uses suggested match
- ✅ No broker ID, no suggested match (falls back to None)

## Files Modified
- **`ba2_trade_platform/ui/pages/overview.py`**: Fixed the order mapping dialog logic around line 1920-1940
- **`test_files/test_order_mapping_fix.py`**: Added comprehensive test coverage

## Error Prevention
This fix prevents the `ValueError: Invalid value` error by ensuring:
1. Any existing broker order ID is always present in the options list
2. The default value is always a valid option before creating the select widget
3. Graceful fallback to `None` if validation fails

The order mapping dialog should now work reliably even with orphaned or missing broker order IDs.