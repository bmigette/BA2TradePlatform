# FMPSenateTrade and Database Locking Fixes

**Date**: 2025-01-10  
**Issues Fixed**: 
1. FMPSenateTrade returning negative expected profit for SELL signals
2. Database locking error showing full stack trace on every retry attempt

## Issue 1: Negative Expected Profit for SELL Signals

### Problem
The FMPSenateTrade expert was returning negative expected profit percentages for SELL signals, which doesn't make sense since profit should always be positive regardless of the trade direction.

### Root Cause
Two locations in the code were explicitly negating expected profit for SELL signals:

1. **Copy Trade Mode** (line 696):
```python
expected_profit = 50.0 if signal == OrderRecommendation.BUY else -50.0
```

2. **Normal Analysis Mode** (lines 914-918):
```python
expected_profit = min(100.0, max(0.0, 50.0 + avg_symbol_focus_pct * growth_multiplier))

# For sell signals, negate expected profit
if signal == OrderRecommendation.SELL:
    expected_profit = -expected_profit
```

### Solution
**File**: `ba2_trade_platform/modules/experts/FMPSenateTrade.py`

1. **Fixed Copy Trade Mode**:
```python
# OLD: expected_profit = 50.0 if signal == OrderRecommendation.BUY else -50.0
# NEW: 
expected_profit = 50.0  # Always positive regardless of BUY/SELL
```

2. **Fixed Normal Analysis Mode**:
```python
# OLD:
expected_profit = min(100.0, max(0.0, 50.0 + avg_symbol_focus_pct * growth_multiplier))
if signal == OrderRecommendation.SELL:
    expected_profit = -expected_profit

# NEW:
expected_profit = min(100.0, max(0.0, 50.0 + avg_symbol_focus_pct * growth_multiplier))
# Removed the negation - profit is always positive
```

### Impact
- ✅ All FMPSenateTrade recommendations now have positive expected profit values
- ✅ Consistent with the principle that "profit" is always a positive expectation
- ✅ Fixes potential issues with rules/conditions that check for positive profit thresholds

## Issue 2: Database Locking Stack Trace Spam

### Problem
When database locking occurred, the system would show the full stack trace on every retry attempt (1/5, 2/5, 3/5, 4/5), creating log spam and making it hard to distinguish between retry attempts and actual failures.

Example of problematic output:
```
2025-10-10 16:21:55,826 - ba2_trade_platform - db - ERROR - Error updating instance: (sqlite3.OperationalError) database is locked
[... full stack trace ...]
2025-10-10 16:21:55,832 - ba2_trade_platform - db - WARNING - Database locked, retrying in 0.10s (attempt 1/5)
```

### Solution
**File**: `ba2_trade_platform/core/db.py`

1. **Updated retry_on_lock decorator** to only show full stack trace on final attempt:

```python
def retry_on_lock(func):
    """Decorator to retry database operations on lock errors with exponential backoff."""
    def wrapper(*args, **kwargs):
        max_retries = 5
        base_delay = 0.1  # Start with 100ms
        
        for attempt in range(max_retries):
            try:
                return func(*args, **kwargs)
            except Exception as e:
                if "database is locked" in str(e).lower():
                    if attempt < max_retries - 1:
                        delay = base_delay * (2 ** attempt)
                        # Only show warning without stack trace for retry attempts
                        logger.warning(f"Database locked, retrying in {delay:.2f}s (attempt {attempt + 1}/{max_retries})")
                        time.sleep(delay)
                    else:
                        # Show full error with stack trace only on final attempt (5/5)
                        logger.error(f"Database locked after {max_retries} attempts", exc_info=True)
                        raise
                else:
                    # Not a lock error, raise immediately with stack trace
                    logger.error(f"Error in {func.__name__}: {e}", exc_info=True)
                    raise
```

2. **Removed duplicate error logging** from `update_instance` and `add_instance` functions since the decorator now handles all logging appropriately.

### New Behavior
- **Attempts 1-4**: Only show warning message: `"Database locked, retrying in 0.20s (attempt 2/5)"`
- **Attempt 5 (final)**: Show full error with stack trace: `"Database locked after 5 attempts"` + traceback
- **Non-lock errors**: Show full stack trace immediately

### Impact
- ✅ Reduces log noise during normal database contention
- ✅ Still provides full diagnostic information when retries are exhausted
- ✅ Makes it easier to distinguish between transient lock issues and real problems
- ✅ Maintains exponential backoff retry logic (0.1s, 0.2s, 0.4s, 0.8s, 1.6s)

## Files Modified

1. **ba2_trade_platform/modules/experts/FMPSenateTrade.py**
   - Removed negative expected profit for SELL signals in two locations
   - Added comments explaining the change

2. **ba2_trade_platform/core/db.py**
   - Updated `retry_on_lock` decorator to control stack trace visibility
   - Removed duplicate error logging from `update_instance` and `add_instance`

3. **test_files/test_fmp_expected_profit.py** (NEW)
   - Created test to verify all FMP recommendations have positive expected profit

## Testing

### FMP Expected Profit Test
Run: `.venv\Scripts\python.exe test_files\test_fmp_expected_profit.py`

Expected output:
```
✓ Recommendation X: BUY signal has positive expected profit: 45.2%
✓ Recommendation Y: SELL signal has positive expected profit: 52.8%
✅ All recommendations have positive expected profit values
✅ TEST PASSED: All expected profit values are positive
```

### Database Locking Test
To test the improved logging, trigger database contention and observe:
- Attempts 1-4: Only warning messages without stack traces
- Attempt 5: Full error with stack trace if all retries fail

## Verification Steps

1. **Expected Profit Fix**:
   - Run FMPSenateTrade analysis on any symbol
   - Verify all recommendations (BUY/SELL/HOLD) have expected_profit_percent >= 0
   - Check both copy trade mode and normal analysis mode

2. **Database Locking Fix**:
   - Monitor logs during high-concurrency database operations
   - Verify retry attempts show only warning messages
   - Verify final failure (if occurs) shows full stack trace

## Notes

- **Expected Profit Logic**: The change maintains the same calculation formula but removes the arbitrary negation for SELL signals. The profit expectation represents the potential gain from the trade regardless of direction.

- **Database Retry Logic**: The retry mechanism was already in place and working correctly. This change only improves the logging to reduce noise while preserving diagnostic information when needed.

- **Backward Compatibility**: Both changes are backward compatible and don't affect the core functionality, only improving the user experience and data consistency.