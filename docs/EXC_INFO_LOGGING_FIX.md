# exc_info Parameter Fix - Logging Best Practices

## Issue

The error `KeyError('exc_info')` was occurring because `logger.error()` was being called with `exc_info=True` **outside of exception handlers** (not inside an `except` block).

## Root Cause

The `exc_info=True` parameter tells the logger to capture the current exception context and stack trace. However, this parameter **only works when there is an active exception** - i.e., when the code is executing inside an `except` block.

When `exc_info=True` is used outside an exception handler, Python's logging system tries to access exception information that doesn't exist, causing `KeyError('exc_info')`.

## The Fix

### ❌ INCORRECT Usage (Causes KeyError)

```python
# NOT in an exception handler - no exception context available
if recommendation is None:
    logger.error("Recommendation not found", exc_info=True)  # ❌ WRONG - KeyError!
    return

# Validation check - not an exception
if container is None:
    logger.error("Container creation failed", exc_info=True)  # ❌ WRONG - KeyError!
    return
```

### ✅ CORRECT Usage

**Case 1: Inside Exception Handler**
```python
try:
    result = risky_operation()
except Exception as e:
    logger.error(f"Operation failed: {e}", exc_info=True)  # ✅ Correct - has exception context
```

**Case 2: Simple Validation (No Exception)**
```python
if result is None:
    logger.error("Result is None")  # ✅ Correct - no exc_info parameter
    return
```

**Case 3: Expected Condition (No Exception)**
```python
if recommendation is None:
    logger.error("Recommendation not found")  # ✅ Correct - just log the error
    return
```

## Files Fixed

### 1. `ba2_trade_platform/modules/experts/TradingAgents.py`

**Line ~1071**: Removed `exc_info=True` from non-exception error
```python
# BEFORE (WRONG)
if not recommendation:
    logger.error(f"[TRADE MANAGER] ExpertRecommendation {recommendation_id} not found", exc_info=True)
    return

# AFTER (CORRECT)
if not recommendation:
    logger.error(f"[TRADE MANAGER] ExpertRecommendation {recommendation_id} not found")
    return
```

### 2. `ba2_trade_platform/ui/pages/settings.py`

**Lines ~1009, 1019, 1045, 1055, 1250, 1257**: Removed `exc_info=True` from UI validation checks

```python
# BEFORE (WRONG)
if self.enter_market_times_container is None:
    logger.error("ui.column() returned None for enter_market_times_container", exc_info=True)

# AFTER (CORRECT)
if self.enter_market_times_container is None:
    logger.error("ui.column() returned None for enter_market_times_container")
```

**Note**: Lines 1449 and 1463 were kept unchanged because they ARE inside `except json.JSONDecodeError:` blocks - those are correct!

## Updated Guidelines

### When to Use `exc_info=True`

✅ **ALWAYS use when**:
- Inside an `except` block catching an exception
- You want to capture the full stack trace for debugging
- Diagnosing production errors where you need complete context

```python
try:
    data = fetch_api_data()
except requests.RequestException as e:
    logger.error(f"API fetch failed: {e}", exc_info=True)  # ✅
```

### When NOT to Use `exc_info=True`

❌ **NEVER use when**:
- Outside exception handlers (`except` blocks)
- Simple validation checks (`if x is None`)
- Expected error conditions (e.g., "record not found")
- Any error logging where you don't have an active exception

```python
# Validation - NOT an exception
if user is None:
    logger.error("User not found")  # ✅ Correct - no exc_info

# Expected condition - NOT an exception  
if balance < min_balance:
    logger.error(f"Insufficient balance: {balance}")  # ✅ Correct - no exc_info
```

## Testing the Fix

To verify the fix works:

1. **Test Validation Errors**: Trigger conditions that log non-exception errors (e.g., missing records)
   - Should log error message WITHOUT stack trace
   - Should NOT throw `KeyError('exc_info')`

2. **Test Exception Handling**: Trigger actual exceptions
   - Should log error message WITH full stack trace
   - `exc_info=True` should work correctly in `except` blocks

## Summary

| Scenario | Use `exc_info=True`? | Example |
|----------|---------------------|---------|
| Inside `except` block | ✅ YES | `except Exception as e: logger.error("...", exc_info=True)` |
| Validation check (`if x is None`) | ❌ NO | `if x is None: logger.error("...")` |
| Expected error condition | ❌ NO | `if not found: logger.error("...")` |
| UI element check | ❌ NO | `if element is None: logger.error("...")` |

## Documentation Updated

The `.github/copilot-instructions.md` file has been updated with these guidelines to prevent future occurrences of this issue.

## Impact

- **7 instances fixed** where `exc_info=True` was incorrectly used outside exception handlers
- **0 breaking changes** - only removed invalid `exc_info=True` parameters
- **All exception handlers preserved** - `exc_info=True` still used correctly in try/except blocks
- **No functional changes** - only fixed logging behavior to prevent KeyError
