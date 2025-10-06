# Market Analysis Cleanup Feature

## Overview

The Cleanup feature allows users to safely remove old MarketAnalysis records and their associated data (outputs and recommendations) to keep the database clean and performant. The feature is accessible via a new "Cleanup" tab in the Expert Settings dialog.

---

## Key Features

### 1. **Safe Deletion**
- **Never deletes analyses with open transactions**: Analyses linked to open positions are automatically protected
- **Transactional**: All deletions happen in a database transaction (all-or-nothing)
- **Preview before delete**: See exactly what will be deleted before executing

### 2. **Flexible Filtering**
- **Age-based**: Configure how many days of history to keep (default: 30 days)
- **Status-based**: Select which analysis statuses to clean up (PENDING, RUNNING, COMPLETED, FAILED, CANCELLED)
- **Expert-scoped**: When editing an expert, cleanup only affects that expert's analyses

### 3. **Detailed Statistics**
- Real-time database statistics
- Breakdown by status and age
- Total counts of analyses, outputs, and recommendations

---

## User Interface

### Location
**Expert Settings → Cleanup Tab**

The Cleanup tab appears when creating or editing an expert instance in:
- **Settings Page → Expert Instances → Add/Edit Expert**

### Tab Layout

```
┌─────────────────────────────────────────────────────────┐
│ 🗑️ Cleanup Tab                                         │
├─────────────────────────────────────────────────────────┤
│                                                         │
│ [Current Database Statistics]                          │
│   Total Analyses: 156                                  │
│   Total Outputs: 2,340                                 │
│   Total Recommendations: 156                           │
│                                                         │
│   By Status:                                           │
│   COMPLETED: 120  FAILED: 30  PENDING: 6              │
│                                                         │
│   By Age:                                              │
│   < 7 days: 20    7-30 days: 45   30-90 days: 60      │
│   90-180 days: 25   > 180 days: 6                     │
│                                                         │
├─────────────────────────────────────────────────────────┤
│ [Cleanup Configuration]                                │
│                                                         │
│   Days to Keep: [30]                                   │
│                                                         │
│   Select statuses to clean up:                         │
│   ☐ PENDING    ☐ RUNNING    ☑ COMPLETED               │
│   ☑ FAILED     ☐ CANCELLED                            │
│                                                         │
│   ⚠️ Analyses with open transactions will never be     │
│      deleted.                                          │
│                                                         │
├─────────────────────────────────────────────────────────┤
│ [Preview]                                              │
│   Click "Preview Cleanup" to see what will be deleted. │
│                                                         │
│                     [Preview Cleanup]  [Execute (🔒)]  │
└─────────────────────────────────────────────────────────┘
```

### Workflow

1. **View Statistics**: See current database state
2. **Configure Cleanup**: Set days to keep and select statuses
3. **Preview**: Click "Preview Cleanup" to see what will be deleted
4. **Review**: Check the preview table and summary
5. **Execute**: Click "Execute Cleanup" (enabled after preview)
6. **Confirm**: Confirm the deletion in the browser dialog

---

## Technical Implementation

### Backend Module: `ba2_trade_platform/core/cleanup.py`

#### Functions

**1. `preview_cleanup(days_to_keep, statuses, expert_instance_id)`**
- Returns preview of what would be deleted without actually deleting
- Categorizes analyses as deletable or protected
- Provides counts and sample data

**2. `execute_cleanup(days_to_keep, statuses, expert_instance_id)`**
- Executes the actual cleanup operation
- Skips analyses with open transactions
- Returns detailed results

**3. `get_cleanup_statistics(expert_instance_id)`**
- Returns current database statistics
- Breakdown by status and age
- Useful for dashboard displays

**4. `_has_open_transaction(session, market_analysis_id)`**
- Internal function to check for linked open transactions
- Traverses: MarketAnalysis → ExpertRecommendation → TradeActionResult → Transaction
- Returns True if any transaction has status == OPENED

---

## Database Relationships

### Data Deleted

When cleaning up a MarketAnalysis, the following are deleted:

1. **AnalysisOutput records**
   - Tool outputs (API responses, calculations, etc.)
   - Agent messages and debates
   - Data visualizations

2. **ExpertRecommendation records**
   - BUY/SELL/HOLD recommendations
   - Confidence scores and risk levels
   - Expected profit calculations

3. **MarketAnalysis record**
   - The analysis metadata itself
   - Status, timestamps, state data

### Protection Logic

An analysis is **protected** (not deleted) if:

```
MarketAnalysis
  └─> ExpertRecommendation (one or more)
        └─> TradeActionResult (one or more)
              └─> Transaction (status == OPENED)
```

**Example Protected Scenario**:
- Analysis #123 for AAPL created 60 days ago
- Recommendation: BUY AAPL at $150
- TradeActionResult: Executed buy order
- Transaction #456: OPENED at $150 (position still open)
- **Result**: Analysis #123 is protected, won't be deleted

---

## Usage Examples

### Example 1: Clean Up Old Completed Analyses

**Goal**: Remove completed analyses older than 30 days

**Steps**:
1. Open Expert Settings → Edit expert
2. Go to Cleanup tab
3. Set "Days to Keep" = 30
4. Check only "COMPLETED"
5. Click "Preview Cleanup"
6. Review preview showing 45 analyses to delete
7. Click "Execute Cleanup"
8. Confirm deletion

**Result**:
```
✅ Cleanup completed!
Deleted: 45 analyses
Protected: 3 analyses with open transactions
Outputs deleted: 675
Recommendations deleted: 45
```

### Example 2: Clean Up All Failed Analyses

**Goal**: Remove all failed analyses regardless of age

**Steps**:
1. Set "Days to Keep" = 365 (effectively all)
2. Check only "FAILED"
3. Preview and execute

**Result**: All failed analyses deleted, keeping completed/running ones

### Example 3: Aggressive Cleanup

**Goal**: Keep only last 7 days of data

**Steps**:
1. Set "Days to Keep" = 7
2. Check all statuses except RUNNING (to preserve active jobs)
3. Preview and execute

**Result**: Database trimmed to only recent week's data

---

## Preview Output

### Summary Card (Orange Border)

```
Cleanup Summary
┌─────────────────────────────────────────────────────┐
│ Will delete: 45 analyses                            │
│ Protected: 3 analyses (have open transactions)      │
│                                                     │
│ Outputs to delete: 675                              │
│ Recommendations to delete: 45                       │
└─────────────────────────────────────────────────────┘
```

### Preview Table

| ID  | Symbol | Status    | Created            | Outputs | Recs |
|-----|--------|-----------|-------------------|---------|------|
| 101 | AAPL   | completed | 2025-09-01T10:00  | 15      | 1    |
| 102 | MSFT   | completed | 2025-09-02T14:30  | 15      | 1    |
| 103 | TSLA   | failed    | 2025-09-03T09:15  | 3       | 0    |
| ... | ...    | ...       | ...               | ...     | ...  |

*Table shows up to 100 sample analyses to be deleted*

---

## Error Handling

### Validation Errors

**No statuses selected**:
```
⚠️ Please select at least one status to clean up.
(Execute button remains disabled)
```

### Execution Errors

**Individual analysis deletion failure**:
```
✅ Cleanup completed!
Deleted: 42 analyses
Protected: 3 analyses with open transactions
⚠️ 3 errors occurred

(Errors are logged to app.log)
```

**Complete failure**:
```
❌ Cleanup failed:
Database connection error
```

### Browser Confirmation

Before executing cleanup:
```javascript
⚠️ This will permanently delete the previewed analyses 
and their data. Are you sure?

[Cancel] [OK]
```

---

## Safety Features

### 1. **Transaction Protection**
- Checks `Transaction.status == OPENED` before deletion
- Traverses relationship chain to find linked transactions
- Never deletes analyses backing open positions

### 2. **Preview-First Design**
- Execute button disabled until preview is run
- Preview shows exact data to be deleted
- Allows user to adjust settings before execution

### 3. **Transactional Deletion**
- All deletions in a single database transaction
- If any error occurs, entire operation rolls back
- Database remains consistent

### 4. **Confirmation Dialog**
- Browser-level confirmation required
- Prevents accidental clicks
- Clear warning about permanent deletion

### 5. **Detailed Logging**
```python
INFO - Cleanup: Found 48 analyses older than 30 days
DEBUG - Cleanup: Protecting analysis 123 (has open transaction)
DEBUG - Cleanup: Deleted analysis 101 (AAPL, completed)
INFO - Cleanup completed: 45 analyses deleted, 3 protected
```

---

## Performance Considerations

### Database Queries

**Preview Operation**:
- 1 query to fetch old analyses
- 1 query per analysis to check relationships
- Total: ~(N + 1) queries for N analyses

**Execute Operation**:
- Same as preview
- Plus deletion queries (cascading via foreign keys)

### Large Datasets

For databases with thousands of analyses:
- Preview limited to 100 sample items
- Statistics use COUNT queries (efficient)
- Consider running cleanup during off-hours

### Optimization Tips

1. **Run cleanup regularly**: Small batches are faster
2. **Use status filters**: Target specific types (e.g., only FAILED)
3. **Adjust days to keep**: Shorter retention = less data to scan

---

## Configuration Defaults

### Default Settings

```python
days_to_keep = 30  # Keep last month
statuses = [
    MarketAnalysisStatus.COMPLETED,  # ✓ Clean up completed
    MarketAnalysisStatus.FAILED      # ✓ Clean up failed
]
# NOT checked by default:
# - PENDING (might still run)
# - RUNNING (actively executing)
# - CANCELLED (user cancelled, might want to review)
```

### Recommended Configurations

**Conservative** (recommended for production):
- Days to keep: 60-90
- Statuses: COMPLETED, FAILED only

**Moderate** (good balance):
- Days to keep: 30
- Statuses: COMPLETED, FAILED, CANCELLED

**Aggressive** (development/testing):
- Days to keep: 7
- Statuses: All except RUNNING

---

## Testing Checklist

### Manual Testing

1. **Create test data**:
   - Run several analyses (completed, failed, running)
   - Create some analyses with linked transactions
   - Vary ages (recent, 30 days old, 60 days old)

2. **Test preview**:
   - Verify statistics are accurate
   - Check preview shows correct counts
   - Confirm protected analyses are excluded

3. **Test execution**:
   - Run cleanup with various filters
   - Verify correct data deleted
   - Check protected analyses remain
   - Confirm database integrity

4. **Test edge cases**:
   - No analyses to delete (empty result)
   - All analyses protected (nothing deleted)
   - Delete with only one status selected
   - Very old analyses (> 1 year)

### Database Verification

**Before cleanup**:
```sql
SELECT COUNT(*) FROM market_analysis WHERE created_at < '2025-09-01';
-- Result: 48

SELECT COUNT(*) FROM analysis_output;
-- Result: 720

SELECT COUNT(*) FROM expert_recommendation;
-- Result: 48
```

**After cleanup (preview showed 45 deletable)**:
```sql
SELECT COUNT(*) FROM market_analysis WHERE created_at < '2025-09-01';
-- Result: 3 (protected analyses)

SELECT COUNT(*) FROM analysis_output;
-- Result: 45 (45 * 15 outputs deleted)

SELECT COUNT(*) FROM expert_recommendation;
-- Result: 3 (45 recommendations deleted)
```

---

## Future Enhancements

### Possible Additions

1. **Automated Cleanup**
   - Schedule cleanup to run automatically (weekly, monthly)
   - Configure via cron or scheduler
   - Email report after execution

2. **Selective Deletion**
   - Choose individual analyses from preview
   - Bulk select/deselect
   - Delete by specific symbols

3. **Archive Instead of Delete**
   - Export to JSON/CSV before deleting
   - Restore archived analyses
   - Cold storage for historical data

4. **Cleanup Templates**
   - Save common configurations
   - Quick apply presets
   - Expert-specific defaults

5. **Advanced Filters**
   - Filter by symbol
   - Filter by confidence score
   - Filter by error type (for failed analyses)

6. **Dry Run Mode**
   - Test cleanup without confirmation
   - Generate reports
   - Compare different configurations

---

## Troubleshooting

### Issue: "No analyses to clean up"

**Cause**: All analyses are either too recent or have open transactions

**Solution**:
- Check "Days to Keep" value (increase to see older data)
- Verify status filters are checked
- Review statistics to see age distribution

### Issue: "Execute button stays disabled"

**Cause**: Preview not run yet or preview showed zero deletable analyses

**Solution**:
- Click "Preview Cleanup" first
- If preview shows 0 deletable, adjust settings
- Check at least one status is selected

### Issue: "Analyses with open transactions were deleted"

**Should never happen** - this would be a bug

**Mitigation**:
- Protection check runs for every analysis
- Transaction status check is explicit
- File bug report with reproduction steps

### Issue: "Cleanup takes too long"

**Cause**: Large database or many analyses to check

**Solution**:
- Use more specific status filters
- Reduce "Days to Keep" to target smaller subset
- Run cleanup during off-hours
- Consider database indexing on `created_at` column

---

## Code Examples

### Using Cleanup Functions Directly

```python
from ba2_trade_platform.core.cleanup import (
    preview_cleanup, 
    execute_cleanup,
    get_cleanup_statistics
)
from ba2_trade_platform.core.types import MarketAnalysisStatus

# Get statistics
stats = get_cleanup_statistics(expert_instance_id=5)
print(f"Total analyses: {stats['total_analyses']}")
print(f"Older than 180 days: {stats['analyses_by_age']['older']}")

# Preview cleanup
preview = preview_cleanup(
    days_to_keep=30,
    statuses=[MarketAnalysisStatus.COMPLETED, MarketAnalysisStatus.FAILED],
    expert_instance_id=5
)
print(f"Would delete: {preview['deletable_analyses']} analyses")
print(f"Protected: {preview['protected_analyses']} analyses")

# Execute cleanup
result = execute_cleanup(
    days_to_keep=30,
    statuses=[MarketAnalysisStatus.COMPLETED],
    expert_instance_id=None  # All experts
)
if result['success']:
    print(f"Deleted {result['analyses_deleted']} analyses")
else:
    print(f"Errors: {result['errors']}")
```

### Custom Cleanup Script

```python
#!/usr/bin/env python
"""
Weekly cleanup script - keeps 30 days of completed/failed analyses
"""
from ba2_trade_platform.core.cleanup import execute_cleanup
from ba2_trade_platform.core.types import MarketAnalysisStatus
from ba2_trade_platform.logger import logger

def weekly_cleanup():
    logger.info("Starting weekly cleanup job")
    
    result = execute_cleanup(
        days_to_keep=30,
        statuses=[
            MarketAnalysisStatus.COMPLETED,
            MarketAnalysisStatus.FAILED
        ],
        expert_instance_id=None  # All experts
    )
    
    if result['success']:
        logger.info(f"Cleanup completed: {result['analyses_deleted']} deleted")
    else:
        logger.error(f"Cleanup failed: {result['errors']}")
    
    return result

if __name__ == '__main__':
    weekly_cleanup()
```

---

## Conclusion

The Cleanup feature provides a safe, flexible way to maintain database hygiene:

✅ **Safe**: Never deletes analyses with open transactions
✅ **Flexible**: Configurable age and status filters
✅ **Transparent**: Preview before execution
✅ **Efficient**: Transactional deletion with detailed logging
✅ **User-friendly**: Clear UI with statistics and confirmation

Perfect for keeping your BA2 Trade Platform database clean and performant! 🗑️✨
