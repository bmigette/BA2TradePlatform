# Cleanup Page Async Fix - November 7, 2025

## Problem
The cleanup preview page in settings UI was **hanging when clicking "Preview Cleanup"** because the `preview_cleanup()` function was running synchronously on the main thread, blocking the UI.

**Symptoms:**
- UI freezes when clicking "Preview Cleanup" button
- No visual feedback while database is being analyzed
- No progress indication to user
- Same issue with "Execute Cleanup" operation

## Root Cause
In `ba2_trade_platform/ui/pages/settings.py`:
- `_preview_cleanup()` method called `preview_cleanup()` synchronously
- `_perform_cleanup()` method called `execute_cleanup()` synchronously
- Long-running database operations blocked the NiceGUI event loop
- No progress logging or status updates

## Solution
Converted both methods to use **async/await pattern** with `asyncio.run_in_executor()`:

### 1. **Async Preview Cleanup** (Lines 3103-3203)

**Changes:**
- ✅ Wrapped `preview_cleanup()` call in `loop.run_in_executor()` to run in thread pool
- ✅ Shows progress bar that updates during execution
- ✅ Displays status messages as operations progress
- ✅ Uses `asyncio.create_task()` to run async function without blocking
- ✅ Detailed logging at each step

**Progress Flow:**
```
0% → "Analyzing database for cleanup candidates..."
20% → "Fetching old analyses..."
80% → "Preparing preview display..."
100% → "✓ Preview ready" (with success indicator)
```

**Logging Added:**
```
Starting cleanup preview: days_to_keep=30, expert_id=1, statuses=[...]
Cleanup preview completed: 45 deletable, 10 protected analyses
Cleanup preview display completed successfully
```

### 2. **Async Execute Cleanup** (Lines 3218-3290)

**Changes:**
- ✅ Wrapped `execute_cleanup()` call in `loop.run_in_executor()` to run in thread pool
- ✅ Shows progress bar during execution
- ✅ Displays detailed status log of cleanup operations
- ✅ Logs each deletion milestone
- ✅ Shows error summaries if failures occur
- ✅ Auto-scrolling status log with all details

**Progress Flow:**
```
10% → "Validating cleanup parameters..."
20% → "Querying database for analyses to delete..."
80% → "Cleanup completed successfully!"
100% → Shows summary with counts
```

**Status Log Display:**
Each step is added to a scrollable log:
- "• Deleted 45 analyses"
- "• Protected 10 analyses with open transactions"
- "• Deleted 120 analysis outputs"
- "• Deleted 250 expert recommendations"
- "• ⚠️ 3 errors occurred during cleanup"

**Logging Added:**
```
Starting cleanup execution: days_to_keep=30, expert_id=1, statuses=[...]
[CLEANUP] Validating cleanup parameters...
[CLEANUP] Querying database for analyses to delete...
[CLEANUP] Deleted 45 analyses
[CLEANUP] Protected 10 analyses with open transactions
[CLEANUP] Deleted 120 analysis outputs
[CLEANUP] Deleted 250 expert recommendations
Cleanup operation completed successfully
```

## Technical Details

### Async Pattern Used
```python
async def run_preview():
    loop = asyncio.get_event_loop()
    
    # Update UI with progress
    progress_bar.set_value(0.2)
    progress_label.set_text('Fetching old analyses...')
    
    # Run blocking function in thread pool
    result = await loop.run_in_executor(
        None,
        preview_cleanup,
        day_to_keep,
        selected_statuses,
        expert_id
    )
    
    # Update UI with results
    progress_bar.set_value(1.0)

# Start without blocking main thread
asyncio.create_task(run_preview())
```

### Key Improvements
1. **Non-Blocking**: UI remains responsive during analysis/cleanup
2. **Progress Feedback**: Users see real-time progress with bar and status messages
3. **Detailed Logging**: Complete operation audit trail in logs and on-screen
4. **Error Handling**: Graceful error display with first 5 errors shown
5. **Status Updates**: Log container shows all operations as they complete
6. **Auto-Refresh**: Statistics automatically refresh after successful cleanup

## User Experience

### Before
- Click "Preview Cleanup" → **UI hangs for 5-30 seconds**
- No feedback until results appear or error shows
- No way to know what's happening

### After
- Click "Preview Cleanup" → Immediate progress bar starts
- Real-time status: "Analyzing database..." → "Fetching..." → "Ready"
- Can see exactly what's being processed
- Responsive UI throughout operation

## Testing

**Preview Cleanup Test:**
1. Go to Settings → Expert Instance → Cleanup tab
2. Configure cleanup parameters (days=30, select statuses)
3. Click "Preview Cleanup"
4. ✅ Progress bar appears and updates
5. ✅ Results display within 5-10 seconds (no UI hang)
6. ✅ Detailed info shown in preview table

**Execute Cleanup Test:**
1. After preview, click "Execute Cleanup"
2. Confirm in dialog
3. ✅ Progress bar appears
4. ✅ Status log shows each operation
5. ✅ Completion summary appears
6. ✅ Statistics refresh automatically

## Files Modified
- `ba2_trade_platform/ui/pages/settings.py`
  - `_preview_cleanup()` method (lines 3103-3203)
  - `_perform_cleanup()` method (lines 3218-3290)

## Dependencies
- `asyncio` (Python standard library)
- Existing `preview_cleanup()` and `execute_cleanup()` functions (unchanged)
- NiceGUI async support (already available)

## Future Enhancements
- Add cancel button to stop long-running cleanup
- Add estimated time remaining
- Add download option for detailed cleanup report
- Add batch cleanup scheduling
