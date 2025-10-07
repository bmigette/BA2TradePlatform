# Retry Close Transaction - Quick Reference

## What Was Added

A **Retry Close** button for transactions stuck in `CLOSING` status.

## Visual Changes

**Before**: Transactions in CLOSING status showed a disabled hourglass icon ⏳

**After**: Transactions in CLOSING status show a clickable refresh icon 🔄

## How It Works

1. **User sees stuck transaction** with CLOSING status
2. **Clicks retry button** (🔄 icon)
3. **Confirmation dialog appears** explaining the action
4. **User confirms** the retry
5. **Status resets** to OPENED or WAITING (based on order state)
6. **User can try closing again** by clicking the normal Close button

## When to Use

- Close operation failed due to network error
- Broker API timeout during close
- Application crashed while closing
- Transaction stuck in CLOSING for extended period

## Safety Features

✅ Confirmation dialog required
✅ Only works on CLOSING status
✅ Smart status detection (OPENED vs WAITING)
✅ Full error handling
✅ Comprehensive logging
✅ Table auto-refreshes after reset

## Code Changes

### File: `ba2_trade_platform/ui/pages/overview.py`

**3 Changes**:
1. Button template updated (line ~1957)
2. Event handler registered (line ~2041)
3. Two new methods added (lines ~2154-2238):
   - `_show_retry_close_dialog()` - Shows confirmation
   - `_retry_close_position()` - Resets status

## Implementation Status

✅ **Complete** - Feature is fully functional

## Documentation

Full documentation: `docs/RETRY_CLOSE_TRANSACTION_FEATURE.md`
