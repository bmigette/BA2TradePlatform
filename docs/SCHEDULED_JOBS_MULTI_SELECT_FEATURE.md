# Scheduled Jobs Multi-Selection Feature

## Overview
Added the ability to select multiple jobs in the "Current Week Scheduled Jobs" table and start them all at once with a single button click.

## Changes Made

### 1. ScheduledJobsTab Class Updates

#### Added Selection State Tracking (`__init__`)
**File:** `ba2_trade_platform/ui/pages/marketanalysis.py`
**Line:** ~837

```python
self.selected_jobs = []  # Track selected jobs for bulk operations
```

Added a new instance variable to track which jobs are currently selected by the user.

#### Updated Table with Multi-Selection (`_create_scheduled_jobs_table`)
**Line:** ~920-958

**Key Changes:**
1. **Added Header Row with Button:**
   ```python
   with ui.row().classes('w-full justify-between items-center mb-2'):
       ui.label('Current Week Scheduled Jobs').classes('text-md font-bold')
       ui.button('Start Selected Jobs', 
                on_click=self._start_selected_jobs, 
                icon='play_arrow',
                color='primary').props('outline').bind_enabled_from(self, 'selected_jobs', 
                                                                    lambda jobs: len(jobs) > 0)
   ```
   - Added "Start Selected Jobs" button at the top of the table
   - Button is dynamically enabled/disabled based on whether any jobs are selected
   - Uses `bind_enabled_from` to reactively update button state

2. **Enabled Multi-Selection on Table:**
   ```python
   self.scheduled_jobs_table = ui.table(
       columns=columns, 
       rows=scheduled_data, 
       row_key='id',
       selection='multiple'  # ← New parameter
   ).classes('w-full')
   ```
   - Added `selection='multiple'` parameter to enable checkbox selection
   - Users can now check/uncheck multiple rows

3. **Bind Selected Rows:**
   ```python
   self.scheduled_jobs_table.bind_value_to(self, 'selected_jobs')
   ```
   - Automatically syncs selected rows with `self.selected_jobs` list
   - NiceGUI handles the two-way binding

#### New Method: `_start_selected_jobs()`
**Line:** ~1176-1244

**Purpose:** Start all selected jobs with a single action.

**Implementation:**
```python
def _start_selected_jobs(self):
    """Start all selected jobs from the scheduled jobs table."""
    try:
        if not self.selected_jobs:
            ui.notify("No jobs selected", type='warning')
            return
        
        from ...core.JobManager import get_job_manager
        job_manager = get_job_manager()
        
        # Track submission results
        successful_submissions = 0
        duplicate_submissions = 0
        failed_submissions = 0
        
        # Submit each selected job
        for job in self.selected_jobs:
            try:
                expert_instance_id = int(job['expert_instance_id'])
                symbol = str(job['symbol'])
                subtype = str(job['subtype'])
                
                success = job_manager.submit_market_analysis(
                    expert_instance_id,
                    symbol,
                    subtype=subtype,
                    bypass_balance_check=True,
                    bypass_transaction_check=True
                )
                
                if success:
                    successful_submissions += 1
                else:
                    duplicate_submissions += 1
                    
            except Exception as e:
                failed_submissions += 1
                logger.error(f"Failed to submit analysis for job {job}: {e}", exc_info=True)
        
        # Show summary notification
        # ... notification logic ...
        
        # Clear selection after starting jobs
        self.selected_jobs = []
        if self.scheduled_jobs_table:
            self.scheduled_jobs_table.selected = []
            
    except Exception as e:
        logger.error(f"Error starting selected jobs: {e}", exc_info=True)
        ui.notify(f"Error starting jobs: {str(e)}", type='negative')
```

**Features:**
- Validates that jobs are selected before proceeding
- Iterates through all selected jobs and submits them to JobManager
- Tracks three outcomes:
  - **Successful**: Job submitted successfully
  - **Duplicate**: Job already pending in queue
  - **Failed**: Error occurred during submission
- Shows comprehensive summary notification with all three counts
- Automatically clears selection after starting jobs
- Full error handling with logging

## User Experience

### Before
- Users could only start jobs one at a time using individual "Run Now" buttons
- Required multiple clicks to start multiple jobs
- No visual indication of which jobs were being operated on

### After
1. **Select Jobs:**
   - Click checkboxes next to jobs in the table
   - Multiple jobs can be selected at once
   - Checkbox in table header selects/deselects all visible jobs

2. **Start Selected Jobs:**
   - "Start Selected Jobs" button appears at top of table
   - Button is disabled (grayed out) when no jobs are selected
   - Button becomes enabled when one or more jobs are selected

3. **Feedback:**
   - Summary notification shows:
     - Number of jobs successfully started
     - Number already pending (if any)
     - Number that failed (if any)
   - Selection automatically clears after operation
   - Individual job buttons remain for single-job operations

## Technical Details

### NiceGUI Table Selection
- Uses `selection='multiple'` parameter
- Adds checkboxes to each row automatically
- Header checkbox for select/deselect all
- Selected rows bound to instance variable

### Binding Pattern
```python
# Button enabled state bound to selected_jobs list
.bind_enabled_from(self, 'selected_jobs', lambda jobs: len(jobs) > 0)

# Table selection bound to instance variable
self.scheduled_jobs_table.bind_value_to(self, 'selected_jobs')
```

### Job Manager Integration
- Uses existing `job_manager.submit_market_analysis()` method
- Bypasses balance and transaction checks (manual submission)
- Passes required parameters:
  - `expert_instance_id`: Which expert to use
  - `symbol`: Instrument to analyze
  - `subtype`: Analysis type (ENTER_MARKET or OPEN_POSITIONS)

## Benefits

1. **Efficiency:** Start multiple jobs with single action
2. **Clarity:** Visual feedback on which jobs are selected
3. **Safety:** Disabled button prevents accidental empty submissions
4. **Feedback:** Comprehensive summary of operation results
5. **Flexibility:** Individual job buttons still available for single operations
6. **User-Friendly:** Automatic selection clearing after operation

## Compatibility

- **NiceGUI Version:** 3.0+
- **Python:** 3.11+
- **Dependencies:** No new dependencies
- **Backward Compatible:** Existing single-job "Run Now" functionality unchanged

## Testing Recommendations

1. **Basic Selection:**
   - Select single job → button enables
   - Deselect job → button disables
   - Start single selected job → verify it starts

2. **Multi-Selection:**
   - Select multiple jobs (2-5) → verify all selected
   - Click "Start Selected Jobs" → verify all start
   - Check notification shows correct counts

3. **Edge Cases:**
   - Select jobs that are already pending → verify duplicate count
   - Select mix of valid and pending jobs → verify separate counts
   - Clear selection → button disables
   - Start jobs, then refresh → verify selection cleared

4. **Error Handling:**
   - Select job with invalid data → verify graceful error
   - Start jobs with no expert instance → verify error notification
   - Network error during submission → verify error logged

## Future Enhancements

Potential improvements for future iterations:

1. **Bulk Filtering:** Filter jobs before selection (e.g., "Select all for Expert X")
2. **Schedule Modification:** Bulk edit schedules for selected jobs
3. **Export/Import:** Export selected job configurations
4. **Job Grouping:** Group similar jobs and operate on groups
5. **Pause/Resume:** Pause scheduled execution for selected jobs
6. **Priority Setting:** Set execution priority for selected jobs

## Related Files

- **Main File:** `ba2_trade_platform/ui/pages/marketanalysis.py`
- **Job Manager:** `ba2_trade_platform/core/JobManager.py`
- **Models:** `ba2_trade_platform/core/models.py`
- **Types:** `ba2_trade_platform/core/types.py`

## Author Notes

This feature follows the existing patterns in the codebase:
- Uses NiceGUI's reactive binding system
- Leverages existing JobManager infrastructure
- Consistent error handling and logging
- User-friendly notifications with clear feedback
- No breaking changes to existing functionality
