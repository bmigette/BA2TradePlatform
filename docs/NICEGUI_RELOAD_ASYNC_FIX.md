# NiceGUI Navigate Reload Async Fix

## Problem
RuntimeWarning appearing during application execution:

```
c:\Users\basti\Documents\BA2TradePlatform\.venv\Lib\site-packages\nicegui\functions\navigate.py:45: RuntimeWarning: coroutine 'new_run_javascript' was never awaited
  run_javascript('history.go(0)')
RuntimeWarning: Enable tracemalloc to get the object allocation traceback
```

## Root Cause Analysis

### Investigation
The warning originates from **inside NiceGUI's own code** at `navigate.py:45`, specifically when calling `history.go(0)` for page reload functionality.

### Root Cause
In newer versions of NiceGUI, the `ui.navigate.reload()` method has become **async** (returns a coroutine). When called from synchronous contexts or non-async callbacks, the coroutine is created but never awaited, triggering the RuntimeWarning.

**Problematic Pattern**:
```python
# Direct call in sync context - WRONG
ui.navigate.reload()

# Lambda in timer callback - ALSO WRONG
ui.timer(10.0, lambda: ui.navigate.reload())
```

The lambda callback is synchronous, so it cannot await the async `reload()` method, leaving the coroutine unawaited.

## Solution

### Pattern: Async Timer Callbacks
To properly await `ui.navigate.reload()`, timer callbacks must be **async functions**:

**Correct Pattern**:
```python
async def reload_page():
    await ui.navigate.reload()

ui.timer(10.0, reload_page)
```

### Files Modified

**1. `ba2_trade_platform/ui/pages/overview.py` (Line 318-320)**

**Before**:
```python
# Refresh the page to show updated data
ui.navigate.reload()
```

**After**:
```python
# Refresh the page to show updated data
async def reload_page():
    await ui.navigate.reload()
ui.timer(0.1, reload_page, once=True)
```

**Changes**:
- Wrapped reload in async function
- Used `ui.timer` with `once=True` for immediate async execution
- Timer callback properly awaits the coroutine

**2. `ba2_trade_platform/ui/pages/market_analysis_detail.py` (Line 178-180)**

**Before**:
```python
# Auto-refresh for pending analyses
ui.timer(10.0, lambda: ui.navigate.reload())
```

**After**:
```python
# Auto-refresh for pending analyses
async def reload_page():
    await ui.navigate.reload()
ui.timer(10.0, reload_page)
```

**Changes**:
- Replaced lambda with async function
- Timer callback properly awaits the coroutine
- Maintains 10-second auto-refresh interval

## Technical Details

### Why ui.timer with async callbacks?
NiceGUI's `ui.timer` **automatically handles async callbacks** by:
1. Detecting if the callback is a coroutine function
2. Using `asyncio.create_task()` to schedule the coroutine
3. Ensuring the coroutine is awaited within the event loop

This makes `ui.timer` the **recommended pattern** for executing async UI operations.

### Alternative: Direct await in async context
If you're already in an async function, you can directly await:

```python
async def some_handler():
    # ... do something ...
    await ui.navigate.reload()
```

However, for button handlers and other UI callbacks that might be synchronous, using `ui.timer` is safer.

## Impact

### Before Fix
- ✗ RuntimeWarning on every page reload
- ✗ Coroutine created but never executed
- ✗ Potential memory leaks from unawaited coroutines
- ✗ Console pollution with warnings

### After Fix
- ✅ No RuntimeWarnings
- ✅ Coroutines properly awaited
- ✅ Clean async execution
- ✅ Proper error handling in event loop

## Testing

1. **Overview Page - Transaction Update**:
   - Navigate to Overview page
   - Edit transaction quantities
   - Apply changes
   - Verify page reloads without warnings

2. **Market Analysis Detail - Pending Analysis**:
   - Start a market analysis
   - Navigate to detail page while pending
   - Wait for auto-refresh (10 seconds)
   - Verify page reloads without warnings

3. **Console Check**:
   - Monitor terminal/console output
   - Verify no "coroutine was never awaited" warnings appear

## Best Practices

### When using ui.navigate.reload()

**✅ DO**:
```python
# In async function - direct await
async def my_handler():
    await ui.navigate.reload()

# In sync context - use timer with async callback
async def reload_page():
    await ui.navigate.reload()
ui.timer(0.1, reload_page, once=True)
```

**❌ DON'T**:
```python
# Direct call in sync context
def my_handler():
    ui.navigate.reload()  # WARNING!

# Lambda without await
ui.timer(10.0, lambda: ui.navigate.reload())  # WARNING!
```

### General Async UI Pattern

For any NiceGUI async methods (indicated by `async def` in their signature):

1. **Check method signature** - Is it async?
2. **Check your context** - Are you in an async function?
3. **Use appropriate pattern**:
   - Async context → direct `await`
   - Sync context → `ui.timer` with async callback

## Related Issues

- NiceGUI version-specific async migration
- Python asyncio best practices
- Event loop integration in web frameworks

## Notes

- This fix aligns with NiceGUI's async-first architecture
- The pattern is reusable for other async UI operations
- Using `ui.timer(0.1, callback, once=True)` effectively defers execution to the event loop, allowing async operations in sync contexts
- No performance impact - reload happens asynchronously as intended
