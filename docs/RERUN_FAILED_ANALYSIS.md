# Re-run Failed Analysis Feature

## Overview

Added a "Re-run" button to the Job Monitoring page that allows users to retry failed market analyses without creating new database records. The feature clears existing outputs and re-queues the analysis for execution.

---

## Feature Description

### What It Does

When a market analysis fails (e.g., API errors, ChromaDB issues, LLM timeouts), users can now:

1. **Clear failed data**: Removes all analysis outputs and expert recommendations
2. **Reset status**: Changes analysis status from FAILED → PENDING
3. **Re-queue**: Submits the same analysis back to the worker queue
4. **Preserve record**: Uses the same MarketAnalysis database record (same ID)

### Why It's Useful

- **No duplicate records**: Keeps database clean by reusing existing analysis record
- **Quick recovery**: One-click retry for transient failures (network issues, rate limits, etc.)
- **Debug friendly**: Maintains analysis history while allowing fresh retry
- **Cost effective**: Avoids creating duplicate expert recommendations in database

---

## User Interface

### Location
**Market Analysis → Job Monitoring Tab**

### New Button
A **refresh icon button** appears in the Actions column for failed analyses:

```
Actions Column:
┌──────────────────────────────────────┐
│ 🔍 View Details                      │
│ 🔄 Re-run (orange, only for failed)  │ ← NEW
│ 🐛 Troubleshoot Ruleset              │
└──────────────────────────────────────┘
```

### Button Properties
- **Icon**: `refresh` (circular arrow)
- **Color**: Orange
- **Visibility**: Only shown when `status === 'failed'`
- **Tooltip**: "Re-run Failed Analysis"

---

## Implementation Details

### Files Modified

**`ba2_trade_platform/ui/pages/marketanalysis.py`**

1. **Updated action buttons slot** (Lines ~130-155)
   - Added conditional re-run button for failed analyses
   - Emits `rerun_analysis` event with analysis ID

2. **Registered event handler** (Line ~158)
   - `self.analysis_table.on('rerun_analysis', self.rerun_analysis)`

3. **New method: `rerun_analysis()`** (Lines ~475-555)
   - Validates analysis status is FAILED
   - Clears existing data
   - Re-queues analysis

### Code Flow

```python
def rerun_analysis(self, event_data):
    # 1. Extract analysis ID from event
    analysis_id = extract_id(event_data)
    
    # 2. Validate analysis exists and is FAILED
    analysis = get_instance(MarketAnalysis, analysis_id)
    if analysis.status != FAILED:
        return error("Can only re-run failed analyses")
    
    # 3. Clear existing data
    with get_db() as session:
        # Delete AnalysisOutputs
        delete_outputs(analysis_id)
        
        # Delete ExpertRecommendations
        delete_recommendations(analysis_id)
        
        # Reset analysis
        analysis.state = None
        analysis.status = PENDING
        session.commit()
    
    # 4. Re-queue for execution
    worker_queue.submit_analysis_task(
        expert_instance_id=analysis.expert_instance_id,
        symbol=analysis.symbol,
        subtype=analysis.subtype
    )
    
    # 5. Refresh UI
    refresh_data()
```

---

## Database Operations

### Data Cleared

1. **AnalysisOutput records**
   - All outputs associated with `market_analysis_id`
   - Includes tool outputs, agent messages, etc.
   - Query: `SELECT * FROM analysis_output WHERE market_analysis_id = ?`

2. **ExpertRecommendation records**
   - All recommendations associated with `market_analysis_id`
   - Includes BUY/SELL/HOLD decisions, confidence scores
   - Query: `SELECT * FROM expert_recommendation WHERE market_analysis_id = ?`

3. **MarketAnalysis.state**
   - Cleared to `None`
   - Removes cached agent states, tool results, etc.

4. **MarketAnalysis.status**
   - Changed from `FAILED` → `PENDING`

### Data Preserved

1. **MarketAnalysis record**
   - Same ID, created_at, symbol, expert_instance_id
   - Maintains audit trail

2. **Associated metadata**
   - Expert instance configuration
   - Analysis subtype (ENTER_MARKET, OPEN_POSITIONS)
   - Any custom parameters

---

## User Workflow

### Scenario: API Timeout Caused Failure

1. **User sees failed analysis**
   ```
   Symbol: AAPL
   Status: ❌ Failed
   Expert: TradingAgents - Default
   ```

2. **User clicks re-run button**
   - Orange refresh icon in Actions column

3. **System clears old data**
   - Deletes 15 tool outputs
   - Deletes 1 expert recommendation
   - Resets state to empty

4. **System re-queues**
   - Creates new worker task: `analysis_XYZ`
   - Status changes to ⏳ Pending

5. **Analysis runs again**
   - Fresh API calls
   - New agent execution
   - New outputs generated

6. **Success notification**
   ```
   ✓ Analysis 123 queued for re-run (Task: analysis_456)
   ```

---

## Error Handling

### Validation Errors

**Not a failed analysis**:
```
Can only re-run failed analyses
Type: warning (orange notification)
```

**Analysis not found**:
```
Analysis not found
Type: negative (red notification)
```

### Queue Errors

**Duplicate task** (shouldn't happen):
```
Analysis already queued: Analysis task for expert X and symbol Y is already pending
Type: warning (orange notification)
Still refreshes UI to show current state
```

**Queue submission failed**:
```
Failed to queue analysis for re-run: <error details>
Type: negative (red notification)
Status reverted to FAILED
```

### Database Errors

**Error clearing data**:
```
Error re-running analysis: <error details>
Type: negative (red notification)
Analysis state unchanged
```

---

## Testing Checklist

### Manual Testing

1. **Create a failed analysis**
   - Disable internet or API key
   - Run TradingAgents analysis
   - Wait for failure

2. **Verify re-run button appears**
   - Check orange refresh icon in Actions column
   - Hover to see tooltip: "Re-run Failed Analysis"

3. **Click re-run button**
   - Verify notification: "Analysis X queued for re-run"
   - Check status changes to Pending
   - Check table refreshes

4. **Monitor re-run**
   - Watch status change: Pending → Running → Completed/Failed
   - Check new outputs appear in database
   - Verify same analysis ID is used

5. **Test error conditions**
   - Try re-running completed analysis (should fail)
   - Try re-running pending analysis (should fail)
   - Check error messages are clear

### Database Verification

**Before re-run**:
```sql
-- Check existing data
SELECT COUNT(*) FROM analysis_output WHERE market_analysis_id = 123;
-- Result: 15

SELECT COUNT(*) FROM expert_recommendation WHERE market_analysis_id = 123;
-- Result: 1

SELECT status, state FROM market_analysis WHERE id = 123;
-- Result: status='failed', state={...large JSON...}
```

**After re-run (before execution)**:
```sql
-- Verify data cleared
SELECT COUNT(*) FROM analysis_output WHERE market_analysis_id = 123;
-- Result: 0

SELECT COUNT(*) FROM expert_recommendation WHERE market_analysis_id = 123;
-- Result: 0

SELECT status, state FROM market_analysis WHERE id = 123;
-- Result: status='pending', state=NULL
```

**After successful execution**:
```sql
-- New data generated
SELECT COUNT(*) FROM analysis_output WHERE market_analysis_id = 123;
-- Result: 15 (or different number)

SELECT COUNT(*) FROM expert_recommendation WHERE market_analysis_id = 123;
-- Result: 1

SELECT status FROM market_analysis WHERE id = 123;
-- Result: status='completed'
```

---

## Logging

### Success Path
```
INFO - Cleared 15 outputs and 1 recommendations for analysis 123
DEBUG - Analysis task 'analysis_456' submitted for expert 5, symbol AAPL, priority 0
```

### Error Path
```
ERROR - Error re-running analysis 123: Could not connect to database
Traceback (most recent call last):
  ...
```

### Debug Information
```
INFO - User re-running failed analysis 123 (AAPL, TradingAgents)
DEBUG - Deleting analysis outputs for market_analysis_id=123
DEBUG - Deleting expert recommendations for market_analysis_id=123
DEBUG - Resetting analysis 123 status: failed → pending
DEBUG - Submitting re-run task to worker queue
```

---

## Comparison: Re-run vs New Analysis

| Feature | Re-run Failed | Create New |
|---------|---------------|------------|
| Database Record | ✅ Reuses existing | ❌ Creates new |
| Analysis ID | ✅ Same ID | ❌ New ID |
| Created Timestamp | ✅ Original timestamp | ❌ New timestamp |
| Expert Configuration | ✅ Same config | ✅ Same config |
| Previous Data | ❌ Deleted | N/A (no previous) |
| Audit Trail | ✅ Maintains history | ❌ Separate record |
| Use Case | Transient failures | New analysis needed |

---

## Future Enhancements

### Possible Additions

1. **Retry Count Tracking**
   - Add `retry_count` field to MarketAnalysis
   - Increment on each re-run
   - Limit retries (e.g., max 3 attempts)

2. **Failure Reason Display**
   - Show error message from state
   - Help user decide if re-run will help

3. **Batch Re-run**
   - Select multiple failed analyses
   - Re-run all at once

4. **Smart Re-run**
   - Detect transient vs permanent failures
   - Auto-suggest re-run for transient (rate limits, timeouts)
   - Warn against re-run for permanent (invalid symbol, disabled expert)

5. **Partial Re-run**
   - Keep successful agent outputs
   - Re-run only failed portions
   - More efficient for large analyses

---

## Security Considerations

### Access Control
- Uses same permissions as viewing analysis
- No additional authorization needed
- Only affects analyses user can already view

### Data Integrity
- Transaction-based deletion (all-or-nothing)
- Status changes are atomic
- Queue submission failures restore original state

### Concurrency
- Worker queue handles duplicate task detection
- Status changes are database-level protected
- No race conditions with table refresh

---

## Conclusion

The re-run feature provides a clean, user-friendly way to recover from failed analyses:

✅ **One-click recovery** from transient failures
✅ **Database efficiency** by reusing records
✅ **Audit trail preservation** with same analysis ID
✅ **Error handling** for edge cases
✅ **UI feedback** with clear notifications

Perfect for handling API timeouts, rate limits, network issues, and other recoverable failures! 🔄
