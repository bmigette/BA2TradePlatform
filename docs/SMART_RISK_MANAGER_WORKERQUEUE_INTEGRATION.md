# Smart Risk Manager WorkerQueue Integration

**Date**: 2025-01-22  
**Status**: Phase 1 Complete (Background Processing), Phase 2 Pending (UI Pages)

## Overview

This document describes the integration of the Smart Risk Manager with the WorkerQueue system to enable non-blocking background execution of AI-powered risk management workflows.

## Problem Statement

The Smart Risk Manager workflow (`run_smart_risk_manager`) takes 1-2 minutes to complete due to:
- LLM API calls for analysis and decision-making
- Portfolio data retrieval and processing
- Market data research
- Action execution and validation

Running this synchronously in the UI thread caused:
- UI freezing for 1-2 minutes
- Poor user experience
- No ability to track job progress
- No ability to view job history

## Solution Architecture

### 1. WorkerQueue Task Type

Added `SmartRiskManagerTask` dataclass to `ba2_trade_platform/core/WorkerQueue.py`:

```python
@dataclass
class SmartRiskManagerTask:
    """Task for Smart Risk Manager execution."""
    id: str
    expert_instance_id: int
    account_id: int
    priority: int = -10  # Higher priority than analysis tasks (0)
    status: WorkerTaskStatus = WorkerTaskStatus.PENDING
    job_id: Optional[int] = None  # Linked SmartRiskManagerJob ID
    result: Optional[Dict[str, Any]] = None
    error: Optional[Exception] = None
    created_at: float = 0.0
    started_at: float = 0.0
    completed_at: float = 0.0
    
    def __post_init__(self):
        if self.created_at == 0.0:
            self.created_at = time.time()
    
    def get_task_key(self) -> str:
        """Generate unique key for task deduplication."""
        return f"smart_risk_manager_{self.expert_instance_id}"
```

**Key Features**:
- **Priority=-10**: Processed before analysis tasks (priority=0)
- **Deduplication**: Prevents multiple simultaneous jobs for same expert via `get_task_key()`
- **Job Tracking**: Links to `SmartRiskManagerJob` database record via `job_id`
- **Status Lifecycle**: PENDING → RUNNING → COMPLETED/FAILED

### 2. Task Submission Method

Added `submit_smart_risk_manager_task()` method to WorkerQueue:

```python
def submit_smart_risk_manager_task(self, expert_instance_id: int, account_id: int) -> Optional[str]:
    """
    Submit a Smart Risk Manager task to the queue.
    
    Args:
        expert_instance_id: Expert instance ID
        account_id: Account ID for the expert
        
    Returns:
        Task ID if successfully submitted, None if duplicate exists
    """
```

**Behavior**:
- Checks for existing PENDING/RUNNING tasks for same expert
- Returns `None` if duplicate found (prevents duplicate jobs)
- Returns task_id if successfully enqueued
- Uses priority queue with priority=-10 (high priority)

### 3. Task Execution Method

Added `_execute_smart_risk_manager_task()` method to WorkerQueue:

**Execution Flow**:

1. **Initialize Task**:
   ```python
   task.status = RUNNING
   task.started_at = time.time()
   ```

2. **Create Job Record**:
   ```python
   smart_risk_job = SmartRiskManagerJob(
       expert_instance_id=task.expert_instance_id,
       account_id=task.account_id,
       status="RUNNING",
       model_used=settings["llm_model"],
       user_instructions=settings["user_instructions"],
       run_date=datetime.now(timezone.utc)
   )
   job_id = add_instance(smart_risk_job)
   task.job_id = job_id
   ```

3. **Execute Smart Risk Manager**:
   ```python
   result = run_smart_risk_manager(task.expert_instance_id, task.account_id)
   ```

4. **Update Job with Results**:
   ```python
   if result["success"]:
       smart_risk_job.status = "COMPLETED"
       smart_risk_job.iteration_count = result["iterations"]
       smart_risk_job.actions_taken_count = result["actions_count"]
       smart_risk_job.actions_summary = result["summary"]
       smart_risk_job.actions_log = result["actions"]
   else:
       smart_risk_job.status = "FAILED"
       smart_risk_job.error_message = result["error"]
   ```

5. **Update Task**:
   ```python
   task.status = COMPLETED
   task.result = {"job_id": job_id, "status": "completed", ...}
   task.completed_at = time.time()
   ```

6. **Error Handling**:
   - Updates job status to "FAILED"
   - Sets error_message in job
   - Updates task status to FAILED
   - Logs error with exc_info=True

### 4. Worker Loop Integration

Modified `_worker_loop()` to detect and route SmartRiskManagerTask:

```python
if isinstance(task, SmartRiskManagerTask):
    # Execute Smart Risk Manager task
    try:
        self._execute_smart_risk_manager_task(task, worker_name)
    except Exception as e:
        logger.error(f"Error executing Smart Risk Manager task in worker {worker_name}: {e}", exc_info=True)
    finally:
        self._queue.task_done()
    continue
```

**Benefits**:
- Type-based routing (no need for task type string)
- Dedicated execution method for Smart Risk Manager
- Separate error handling path
- Falls through to AnalysisTask handling for other task types

## UI Changes

### 1. marketanalysis.py

**Before** (Blocking):
```python
def _run_smart_risk_manager(self, expert_id: int, expert_instance):
    # Show processing dialog
    processing_dialog.open()
    
    # BLOCKS UI for 1-2 minutes
    result = run_smart_risk_manager(expert_id, account_id)
    
    processing_dialog.close()
    # Show results
```

**After** (Non-Blocking):
```python
def _run_smart_risk_manager(self, expert_id: int, expert_instance):
    # Enqueue job to WorkerQueue
    worker_queue = get_worker_queue()
    task_id = worker_queue.submit_smart_risk_manager_task(expert_id, account_id)
    
    if task_id:
        # Show success notification
        ui.notify(
            f'Smart Risk Manager job enqueued (Task ID: {task_id}). Check Job Monitoring page for progress.',
            type='positive'
        )
        # Navigate to job monitoring
        ui.navigate.to('/jobmonitoring')
    else:
        # Duplicate job warning
        ui.notify('Smart Risk Manager job already running for this expert', type='warning')
```

**Benefits**:
- UI remains responsive
- User can navigate away while job runs
- Clear feedback via notification
- Redirects to monitoring page

### 2. overview.py

**Changes**:
- Modified `_handle_risk_management_from_overview()` to enqueue jobs instead of direct execution
- Changed result tracking from `smart_manager_results` with job_id/actions_count to task_id/status
- Updated result dialog to show "Jobs Enqueued" with link to Job Monitoring page

**Multi-Expert Batch Processing**:
```python
for expert_id, expert_orders in orders_by_expert.items():
    risk_manager_mode = expert_instance.settings.get("risk_manager_mode", "classic")
    
    if risk_manager_mode == "smart":
        # Enqueue Smart Risk Manager job
        task_id = worker_queue.submit_smart_risk_manager_task(expert_id, account_id)
        if task_id:
            smart_manager_results.append({
                "expert_id": expert_id,
                "task_id": task_id,
                "status": "enqueued"
            })
    else:
        # Run classic risk management (still blocking for now)
        updated_orders = risk_management.review_and_prioritize_pending_orders(expert_id)
```

**Result Dialog**:
- Shows Task ID instead of Job ID (Job ID created later in background)
- Shows "enqueued" status
- Provides "Job Monitoring" button to navigate to monitoring page

## Priority System

WorkerQueue now processes tasks in this order:

1. **SmartRiskManagerTask** (priority=-10): AI-powered risk management
2. **AnalysisTask** (priority=0): Market analysis

**Rationale**:
- Risk management is more time-sensitive than routine analysis
- Risk management affects open positions (financial risk)
- Analysis can wait a few seconds for risk management to complete

## Database Schema

### SmartRiskManagerJob Model

Already exists in `ba2_trade_platform/core/models.py`:

```python
class SmartRiskManagerJob(SQLModel, table=True):
    __tablename__ = "smart_risk_manager_jobs"
    
    id: Optional[int] = Field(default=None, primary_key=True)
    expert_instance_id: int
    account_id: int
    run_date: datetime
    status: str  # RUNNING, COMPLETED, FAILED
    model_used: Optional[str] = None
    user_instructions: Optional[str] = None
    duration_seconds: Optional[int] = None
    iteration_count: Optional[int] = None
    actions_taken_count: Optional[int] = None
    actions_summary: Optional[str] = None
    actions_log: Optional[str] = None
    initial_portfolio_value: Optional[float] = None
    final_portfolio_value: Optional[float] = None
    error_message: Optional[str] = None
    graph_state: Optional[str] = None  # JSON snapshot of final state
```

**Status Lifecycle**:
1. **RUNNING**: Job created, graph execution in progress
2. **COMPLETED**: Graph execution succeeded, actions taken
3. **FAILED**: Graph execution failed, error captured

## Testing Workflow

### Manual Test Steps

1. **Configure Expert**:
   ```python
   expert.settings["risk_manager_mode"] = "smart"
   expert.settings["llm_model"] = "OpenAI/gpt-4o"
   ```

2. **Trigger from Market Analysis Page**:
   - Select expert with risk_manager_mode="smart"
   - Click "Run Risk Management" button
   - Verify notification shows Task ID
   - Verify navigation to Job Monitoring page

3. **Trigger from Overview Page**:
   - Click "Run Risk Management" for multiple experts
   - Verify batch enqueueing for smart experts
   - Verify classic experts still process synchronously
   - Verify result dialog shows "Jobs Enqueued"

4. **Monitor Execution** (Job Monitoring Page - NOT YET IMPLEMENTED):
   - View SmartRiskManagerJob records
   - Check status updates: RUNNING → COMPLETED/FAILED
   - View duration, iterations, actions count

5. **View Details** (smartriskmanagerdetail.py - NOT YET IMPLEMENTED):
   - Click job detail link
   - View full actions_log
   - View graph_state snapshot
   - View consulted market analyses

## Pending Work

### Phase 2: UI Pages (NOT YET IMPLEMENTED)

#### 1. smartriskmanagerdetail.py

**Location**: `ba2_trade_platform/ui/pages/smartriskmanagerdetail.py`

**Structure** (similar to market analysis detail):
```python
@ui.page('/smartriskmanagerdetail/{job_id}')
def smart_risk_manager_detail_page(job_id: int):
    # Load SmartRiskManagerJob
    job = get_instance(SmartRiskManagerJob, job_id)
    
    # Display job metadata
    # - Expert Instance, Account
    # - Run Date, Duration
    # - Model Used, User Instructions
    
    # Display portfolio snapshot
    # - Initial Portfolio Value
    # - Final Portfolio Value
    # - Difference (profit/loss)
    
    # Display execution summary
    # - Status (COMPLETED/FAILED)
    # - Iteration Count
    # - Actions Taken Count
    # - Actions Summary (formatted text)
    
    # Display actions log (collapsible)
    # - Parse actions_log JSON
    # - Show each action: type, symbol, quantity, price, timestamp
    
    # Display graph state (collapsible JSON viewer)
    # - Parse graph_state JSON
    # - Show in formatted tree view
    
    # Display consulted analyses (via junction table)
    # - Query SmartRiskManagerJobAnalysis
    # - Show linked MarketAnalysis records with links
```

**Fields to Display**:
- Header: Job ID, Expert Name, Account Name, Status Badge
- Metadata: Run Date, Duration, Model Used
- Portfolio: Initial Value, Final Value, Change (% and $)
- Execution: Iterations, Actions Count, Summary Text
- Actions: Table with columns: Type, Symbol, Direction, Quantity, Price, Timestamp
- Graph State: JSON viewer (collapsible)
- Consulted Analyses: Table with links to MarketAnalysis detail pages

#### 2. Job Monitoring Page Integration

**Location**: `ba2_trade_platform/ui/pages/jobmonitoring.py`

**Changes Needed**:

1. **Query SmartRiskManagerJob**:
   ```python
   from ...core.models import SmartRiskManagerJob
   
   # Add to existing query logic
   smart_jobs = session.exec(
       select(SmartRiskManagerJob)
       .order_by(SmartRiskManagerJob.run_date.desc())
       .limit(100)
   ).all()
   ```

2. **Add to Display Table**:
   ```python
   # Combine with existing MarketAnalysis display
   # Or create separate tab for Smart Risk Manager jobs
   
   columns = [
       {'name': 'id', 'label': 'Job ID', 'field': 'id'},
       {'name': 'expert', 'label': 'Expert', 'field': 'expert'},
       {'name': 'run_date', 'label': 'Run Date', 'field': 'run_date'},
       {'name': 'status', 'label': 'Status', 'field': 'status'},
       {'name': 'duration', 'label': 'Duration', 'field': 'duration'},
       {'name': 'actions', 'label': 'Actions', 'field': 'actions'},
       {'name': 'detail', 'label': '', 'field': 'detail'}  # Action icon
   ]
   ```

3. **Add Action Icon**:
   ```python
   # Detail button to open smartriskmanagerdetail.py
   ui.button(icon='visibility', on_click=lambda job_id=job.id: ui.navigate.to(f'/smartriskmanagerdetail/{job_id}'))
   ```

4. **Add Filters**:
   - Filter by expert (dropdown with expert names)
   - Filter by status (RUNNING/COMPLETED/FAILED)
   - Filter by date range (date picker)

#### 3. Testing Checklist

- [ ] Enqueue Smart Risk Manager job from Market Analysis page
- [ ] Enqueue multiple jobs from Overview page (batch)
- [ ] Verify duplicate prevention (try to enqueue same expert twice)
- [ ] Verify priority processing (Smart Risk Manager before Analysis)
- [ ] Monitor job status updates in Job Monitoring page
- [ ] Click detail icon to open smartriskmanagerdetail.py
- [ ] Verify all job fields display correctly in detail page
- [ ] Verify actions_log parsing and display
- [ ] Verify graph_state JSON viewer
- [ ] Verify consulted analyses links
- [ ] Test error handling (simulate LLM API failure)
- [ ] Verify error_message display in detail page
- [ ] Test navigation between pages (Monitoring → Detail → Back)

## Benefits

### User Experience
- ✅ No UI freezing during 1-2 minute Smart Risk Manager execution
- ✅ Clear feedback with Task ID notification
- ✅ Ability to navigate away while job runs
- ✅ Centralized job monitoring and history

### System Architecture
- ✅ Consistent with existing WorkerQueue pattern (AnalysisTask)
- ✅ High-priority processing for time-sensitive risk management
- ✅ Duplicate prevention via task_key system
- ✅ Database tracking with SmartRiskManagerJob records

### Maintainability
- ✅ Separation of concerns (UI vs execution)
- ✅ Centralized error handling in WorkerQueue
- ✅ Reusable task execution pattern
- ✅ Type-based routing (isinstance check)

## Configuration

### Expert Settings

To enable Smart Risk Manager for an expert:

```python
expert.settings = {
    "risk_manager_mode": "smart",  # "smart" or "classic"
    "llm_model": "OpenAI/gpt-4o",
    "user_instructions": "Focus on reducing risk for volatile stocks"
}
```

### WorkerQueue Settings

No configuration changes needed. WorkerQueue automatically:
- Starts on application initialization
- Creates thread pool workers
- Processes SmartRiskManagerTask with priority=-10

## Troubleshooting

### Issue: Task not processing

**Symptoms**: Job stuck in PENDING status

**Checks**:
1. Verify WorkerQueue is running: `worker_queue.is_running()`
2. Check worker thread count: `worker_queue._thread_pool`
3. Check queue size: `worker_queue._queue.qsize()`
4. Check for exceptions in logs: `logs/app.debug.log`

### Issue: Duplicate prevention not working

**Symptoms**: Multiple jobs for same expert

**Checks**:
1. Verify `get_task_key()` implementation
2. Check `_task_keys` dictionary in WorkerQueue
3. Verify task cleanup in `finally` block

### Issue: Job status not updating

**Symptoms**: Job stuck in RUNNING status

**Checks**:
1. Check for exceptions in `_execute_smart_risk_manager_task()`
2. Verify database transaction commit
3. Check for database locking issues
4. Review `exc_info=True` logging in exception handlers

## Future Enhancements

### Priority Queue Tuning
- Dynamic priority based on portfolio value at risk
- Time-based priority boosting (older tasks get higher priority)
- Expert-based priority (some experts more important)

### Progress Tracking
- Real-time status updates during graph execution
- WebSocket push notifications to UI
- Progress percentage based on iteration count

### Job Scheduling
- Scheduled automatic risk management runs
- Configurable frequency per expert
- Time-of-day restrictions (e.g., only during market hours)

### Performance Optimization
- Parallel execution of multiple Smart Risk Manager jobs
- Dedicated worker pool for Smart Risk Manager (separate from analysis)
- LLM response caching for repeated queries

## Related Documentation

- `SMART_RISK_MANAGER.md`: Complete Smart Risk Manager specification
- `WORKER_QUEUE.md`: WorkerQueue architecture documentation (if exists)
- `DATABASE_MODELS.md`: SmartRiskManagerJob schema documentation (if exists)

## Change Log

### 2025-01-22 - Phase 1 Complete
- ✅ Added SmartRiskManagerTask dataclass to WorkerQueue
- ✅ Implemented submit_smart_risk_manager_task() method
- ✅ Implemented _execute_smart_risk_manager_task() method
- ✅ Modified _worker_loop() to route SmartRiskManagerTask
- ✅ Updated marketanalysis.py to enqueue jobs
- ✅ Updated overview.py to enqueue jobs
- ❌ smartriskmanagerdetail.py NOT YET CREATED
- ❌ Job Monitoring page integration NOT YET COMPLETE

### Next Steps
1. Create smartriskmanagerdetail.py page
2. Integrate SmartRiskManagerJob display in Job Monitoring page
3. Add action icon to open detail page
4. Test complete workflow end-to-end
5. Document testing results
