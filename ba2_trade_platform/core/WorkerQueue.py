"""
Worker Queue System for BA2 Trade Platform

This module provides a configurable worker queue system for processing tasks
asynchronously using a thread pool. The number of workers is configurable
through the AppSettings system.
"""

import threading
import queue
import time
from typing import Callable, Any, Optional, Dict, Set
from dataclasses import dataclass
from enum import Enum
from datetime import datetime, timezone
from sqlmodel import Session
from ..logger import logger
from .db import get_setting, add_instance, update_instance, get_db
from .models import AppSetting, PersistedQueueTask
from .types import WorkerTaskStatus, AnalysisUseCase




@dataclass
class AnalysisTask:
    """Represents an analysis task to be executed by a worker."""
    id: str
    expert_instance_id: int
    symbol: str
    subtype: str = AnalysisUseCase.ENTER_MARKET  # Analysis use case
    priority: int = 0  # Lower numbers = higher priority
    status: WorkerTaskStatus = WorkerTaskStatus.PENDING
    result: Any = None
    error: Optional[Exception] = None
    created_at: float = None
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    market_analysis_id: Optional[int] = None  # Reference to MarketAnalysis record
    bypass_balance_check: bool = False  # If True, skip balance verification for this task
    bypass_transaction_check: bool = False  # If True, skip existing transaction checks for this task
    batch_id: Optional[str] = None  # Batch ID for grouping related analysis jobs (e.g., "expertid_HHmm_YYYYMMDD" for scheduled, timestamp-based for manual)
    
    def __post_init__(self):
        if self.created_at is None:
            self.created_at = time.time()
    
    def get_task_key(self) -> str:
        """Get a unique key for this task based on expert instance and symbol."""
        return f"{self.expert_instance_id}_{self.symbol}"


@dataclass
class SmartRiskManagerTask:
    """Represents a Smart Risk Manager task to be executed by a worker."""
    id: str
    expert_instance_id: int
    account_id: int
    priority: int = -10  # Higher priority than analysis tasks (negative = higher priority)
    status: WorkerTaskStatus = WorkerTaskStatus.PENDING
    result: Any = None
    error: Optional[Exception] = None
    created_at: float = None
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    job_id: Optional[int] = None  # Reference to SmartRiskManagerJob record
    
    def __post_init__(self):
        if self.created_at is None:
            self.created_at = time.time()
    
    def get_task_key(self) -> str:
        """Get a unique key for this task based on expert instance."""
        return f"smart_risk_{self.expert_instance_id}"


@dataclass
class InstrumentExpansionTask:
    """Represents an instrument expansion task (DYNAMIC/EXPERT/OPEN_POSITIONS) to be executed by a worker."""
    id: str
    expert_instance_id: int
    expansion_type: str  # "DYNAMIC", "EXPERT", or "OPEN_POSITIONS"
    subtype: str = AnalysisUseCase.ENTER_MARKET  # Analysis use case for expanded instruments
    priority: int = 5  # Lower priority than individual analysis to allow processing
    status: WorkerTaskStatus = WorkerTaskStatus.PENDING
    result: Any = None
    error: Optional[Exception] = None
    created_at: float = None
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    batch_id: Optional[str] = None  # Batch ID for grouping related expansion tasks
    
    def __post_init__(self):
        if self.created_at is None:
            self.created_at = time.time()
    
    def get_task_key(self) -> str:
        """Get a unique key for this task."""
        return f"expansion_{self.expansion_type}_{self.expert_instance_id}_{self.subtype}"



class WorkerQueue:
    """
    A configurable worker queue system that manages a pool of worker threads
    to process tasks asynchronously.
    """
    
    def __init__(self):
        """Initialize the WorkerQueue system."""
        self._queue = queue.PriorityQueue()
        self._workers = []
        self._worker_count = 0
        self._running = False
        self._shutdown_event = threading.Event()
        self._task_counter = 0
        self._task_lock = threading.Lock()
        self._tasks: Dict[str, AnalysisTask] = {}
        self._task_keys: Dict[str, str] = {}  # Maps task_key to task_id for duplicate checking
        self._queue_counter = 0  # Counter for tiebreaking in priority queue
        self._risk_manager_lock = threading.Lock()  # Lock for risk manager processing per expert
        self._processing_experts: Set[int] = set()  # Track which experts are currently being processed
        
        # Batch analysis tracking for activity logging
        self._batch_start_times: Dict[str, float] = {}  # Maps batch_id to start timestamp
        self._batch_task_counts: Dict[str, int] = {}  # Maps batch_id to total job count
        self._batch_completed_counts: Dict[str, int] = {}  # Maps batch_id to completed job count
        self._batch_lock = threading.RLock()  # Lock for batch tracking (reentrant for nested calls)
        
        logger.info("WorkerQueue system initialized")
        
    def start(self):
        """Start the worker queue system with the configured number of workers."""
        if self._running:
            logger.warning("WorkerQueue is already running")
            return
            
        # Get worker count from AppSettings
        worker_count = self._get_worker_count()
        
        logger.info(f"Starting WorkerQueue with {worker_count} workers")
        
        self._running = True
        self._shutdown_event.clear()
        
        # Create and start worker threads
        for i in range(worker_count):
            worker_thread = threading.Thread(
                target=self._worker_loop,
                name=f"Worker-{i+1}",
                daemon=True
            )
            worker_thread.start()
            self._workers.append(worker_thread)
            
        self._worker_count = worker_count
        logger.info(f"WorkerQueue started successfully with {worker_count} workers")
        
    def stop(self, timeout: float = 10.0):
        """
        Stop the worker queue system.
        
        Args:
            timeout: Maximum time to wait for workers to finish (seconds)
        """
        if not self._running:
            logger.warning("WorkerQueue is not running")
            return
            
        logger.info("Stopping WorkerQueue...")
        
        self._running = False
        self._shutdown_event.set()
        
        # Signal all workers to stop by adding sentinel values
        for _ in self._workers:
            self._queue.put((0, 0, None))  # (priority, counter, task)
            
        # Wait for workers to finish
        start_time = time.time()
        for worker in self._workers:
            remaining_time = timeout - (time.time() - start_time)
            if remaining_time > 0:
                worker.join(timeout=remaining_time)
                
        # Check if all workers stopped
        alive_workers = [w for w in self._workers if w.is_alive()]
        if alive_workers:
            logger.warning(f"{len(alive_workers)} workers did not stop within timeout")
        else:
            logger.info("All workers stopped successfully")
            
        self._workers.clear()
        self._worker_count = 0
        logger.info("WorkerQueue stopped")
        
    def submit_analysis_task(self, expert_instance_id: int, symbol: str, 
                           subtype: str = AnalysisUseCase.ENTER_MARKET, 
                           priority: int = 0, task_id: Optional[str] = None,
                           bypass_balance_check: bool = False,
                           bypass_transaction_check: bool = False,
                           market_analysis_id: Optional[int] = None,
                           batch_id: Optional[str] = None) -> str:
        """
        Submit an analysis task to be processed by the worker queue.
        
        Args:
            expert_instance_id: The expert instance ID to run the analysis
            symbol: The symbol to analyze
            subtype: Analysis use case (AnalysisUseCase.ENTER_MARKET or AnalysisUseCase.OPEN_POSITIONS)
            priority: Task priority (lower numbers = higher priority)
            task_id: Optional custom task ID
            bypass_balance_check: If True, skip balance verification for this task
            bypass_transaction_check: If True, skip existing transaction checks for this task
            market_analysis_id: Optional existing MarketAnalysis ID to reuse (for retries)
            batch_id: Optional batch identifier for grouping related analysis jobs (e.g., "expertid_HHmm_YYYYMMDD" for scheduled)
            
        Returns:
            Task ID that can be used to track the task
            
        Raises:
            RuntimeError: If WorkerQueue is not running
            ValueError: If a task with the same expert_instance_id and symbol is already pending/running
        """
        if not self._running:
            raise RuntimeError("WorkerQueue is not running. Call start() first.")
            
        with self._task_lock:
            # Check for duplicate task
            task_key = f"{expert_instance_id}_{symbol}"
            if task_key in self._task_keys:
                existing_task_id = self._task_keys[task_key]
                existing_task = self._tasks[existing_task_id]
                if existing_task.status in [WorkerTaskStatus.PENDING, WorkerTaskStatus.RUNNING]:
                    raise ValueError(f"Analysis task for expert {expert_instance_id} and symbol {symbol} is already {existing_task.status.value}")
            
            if task_id is None:
                self._task_counter += 1
                task_id = f"analysis_{self._task_counter}"
            elif task_id in self._tasks:
                raise ValueError(f"Task ID '{task_id}' already exists")
                
            task = AnalysisTask(
                id=task_id,
                expert_instance_id=expert_instance_id,
                symbol=symbol,
                subtype=subtype,
                priority=priority,
                bypass_balance_check=bypass_balance_check,
                bypass_transaction_check=bypass_transaction_check,
                market_analysis_id=market_analysis_id,
                batch_id=batch_id
            )
            
            self._tasks[task_id] = task
            self._task_keys[task_key] = task_id
            
        # Add to priority queue with tiebreaker (priority, counter, task)
        # The counter ensures unique ordering when priorities are equal
        with self._task_lock:
            self._queue_counter += 1
            queue_entry = (priority, self._queue_counter, task)
        
        self._queue.put(queue_entry)
        
        # Persist task for recovery after restart
        self._persist_task(task, self._queue_counter)
        
        logger.debug(f"Analysis task '{task_id}' submitted for expert {expert_instance_id}, symbol {symbol}, priority {priority}, batch_id={batch_id}")
        return task_id
    
    def submit_smart_risk_manager_task(self, expert_instance_id: int, account_id: int, 
                                      priority: int = -10, task_id: Optional[str] = None) -> str:
        """
        Submit a Smart Risk Manager task to be processed by the worker queue.
        Smart Risk Manager tasks have higher priority than regular analysis tasks (default priority = -10).
        
        Args:
            expert_instance_id: The expert instance ID to run Smart Risk Manager for
            account_id: The account ID associated with the expert
            priority: Task priority (lower numbers = higher priority, default -10 for high priority)
            task_id: Optional custom task ID
            
        Returns:
            Task ID that can be used to track the task
            
        Raises:
            RuntimeError: If WorkerQueue is not running
            ValueError: If a Smart Risk Manager task for the same expert is already pending/running
        """
        if not self._running:
            raise RuntimeError("WorkerQueue is not running. Call start() first.")
            
        with self._task_lock:
            # Check for duplicate task
            task_key = f"smart_risk_{expert_instance_id}"
            if task_key in self._task_keys:
                existing_task_id = self._task_keys[task_key]
                existing_task = self._tasks[existing_task_id]
                if existing_task.status in [WorkerTaskStatus.PENDING, WorkerTaskStatus.RUNNING]:
                    raise ValueError(f"Smart Risk Manager task for expert {expert_instance_id} is already {existing_task.status.value}")
            
            if task_id is None:
                self._task_counter += 1
                task_id = f"smart_risk_{self._task_counter}"
            elif task_id in self._tasks:
                raise ValueError(f"Task ID '{task_id}' already exists")
                
            task = SmartRiskManagerTask(
                id=task_id,
                expert_instance_id=expert_instance_id,
                account_id=account_id,
                priority=priority
            )
            
            self._tasks[task_id] = task
            self._task_keys[task_key] = task_id
            
        # Add to priority queue with tiebreaker (priority, counter, task)
        # Lower priority numbers are processed first (higher priority)
        with self._task_lock:
            self._queue_counter += 1
            queue_entry = (priority, self._queue_counter, task)
        
        self._queue.put(queue_entry)
        
        # Persist task for recovery after restart
        self._persist_task(task, self._queue_counter)
        
        logger.info(f"Smart Risk Manager task '{task_id}' submitted for expert {expert_instance_id}, priority {priority}")
        return task_id
        
    def submit_instrument_expansion_task(self, expert_instance_id: int, expansion_type: str, 
                                        subtype: str = "ENTER_MARKET", priority: int = 5, 
                                        task_id: Optional[str] = None, batch_id: Optional[str] = None) -> str:
        """
        Submit an instrument expansion task (DYNAMIC/EXPERT/OPEN_POSITIONS) to be processed by worker queue.
        
        Args:
            expert_instance_id: The expert instance ID to expand instruments for
            expansion_type: Type of expansion ("DYNAMIC", "EXPERT", or "OPEN_POSITIONS")
            subtype: Analysis use case subtype (default "ENTER_MARKET")
            priority: Task priority (lower numbers = higher priority, default 5)
            task_id: Optional custom task ID
            batch_id: Optional batch identifier for grouping related expansion tasks
            
        Returns:
            Task ID that can be used to track the task
            
        Raises:
            RuntimeError: If WorkerQueue is not running
            ValueError: If expansion task for same expert/type/subtype is already pending/running
        """
        if not self._running:
            raise RuntimeError("WorkerQueue is not running. Call start() first.")
            
        with self._task_lock:
            # Check for duplicate task using expansion-specific key
            task_key = f"expansion_{expansion_type}_{expert_instance_id}_{subtype}"
            if task_key in self._task_keys:
                existing_task_id = self._task_keys[task_key]
                existing_task = self._tasks[existing_task_id]
                if existing_task.status in [WorkerTaskStatus.PENDING, WorkerTaskStatus.RUNNING]:
                    raise ValueError(f"Expansion task ({expansion_type}) for expert {expert_instance_id} is already {existing_task.status.value}")
            
            # Generate task ID if not provided
            if task_id is None:
                self._task_counter += 1
                task_id = f"expansion_{expansion_type.lower()}_{self._task_counter}"
            elif task_id in self._tasks:
                raise ValueError(f"Task ID '{task_id}' already exists")
                
            task = InstrumentExpansionTask(
                id=task_id,
                expert_instance_id=expert_instance_id,
                expansion_type=expansion_type,
                subtype=subtype,
                priority=priority,
                batch_id=batch_id
            )
            
            self._tasks[task_id] = task
            self._task_keys[task_key] = task_id
            
        # Add to priority queue with tiebreaker
        with self._task_lock:
            self._queue_counter += 1
            queue_entry = (priority, self._queue_counter, task)
        
        self._queue.put(queue_entry)
        
        # Persist task for recovery after restart
        self._persist_task(task, self._queue_counter)
        
        logger.info(f"Instrument expansion task '{task_id}' ({expansion_type}) submitted for expert {expert_instance_id}, priority {priority}, batch_id={batch_id}")
        return task_id
        
    def get_task_status(self, task_id: str) -> Optional[AnalysisTask]:
        """
        Get the status of a specific task.
        
        Args:
            task_id: The ID of the task to check
            
        Returns:
            AnalysisTask object or None if task not found
        """
        with self._task_lock:
            return self._tasks.get(task_id)
            
    def get_queue_size(self) -> int:
        """Get the current number of tasks in the queue."""
        return self._queue.qsize()
        
    def get_worker_count(self) -> int:
        """Get the current number of worker threads."""
        return self._worker_count
        
    def is_running(self) -> bool:
        """Check if the worker queue is running."""
        return self._running
    
    def track_batch_start(self, batch_id: str, total_tasks: int) -> float:
        """
        Track the start of a batch of analysis jobs.
        
        Args:
            batch_id: Unique batch identifier
            total_tasks: Total number of tasks in the batch
            
        Returns:
            Start timestamp
        """
        with self._batch_lock:
            start_time = time.time()
            self._batch_start_times[batch_id] = start_time
            self._batch_task_counts[batch_id] = total_tasks
            self._batch_completed_counts[batch_id] = 0
            logger.debug(f"Batch {batch_id} tracking started with {total_tasks} tasks")
            return start_time
    
    def track_batch_job_completion(self, batch_id: str) -> Optional[tuple[float, int, int]]:
        """
        Track completion of a job in a batch. Returns batch end info if this was the last job.
        
        Args:
            batch_id: Unique batch identifier
            
        Returns:
            Tuple of (start_time, elapsed_seconds, total_tasks) if batch is complete, None otherwise
        """
        with self._batch_lock:
            if batch_id not in self._batch_completed_counts:
                # Batch not found - initialize it automatically
                logger.debug(f"Batch {batch_id} not found in tracking, initializing")
                self._batch_completed_counts[batch_id] = 0
                self._batch_task_counts[batch_id] = 1  # Will be updated as tasks complete
                self._batch_start_times[batch_id] = time.time()
            
            self._batch_completed_counts[batch_id] += 1
            completed = self._batch_completed_counts[batch_id]
            total = self._batch_task_counts.get(batch_id, completed)
            
            if completed >= total:
                # Batch is complete
                start_time = self._batch_start_times.pop(batch_id, time.time())
                elapsed = time.time() - start_time
                self._batch_task_counts.pop(batch_id, None)
                self._batch_completed_counts.pop(batch_id, None)
                logger.debug(f"Batch {batch_id} completed. Total tasks: {total}, Elapsed: {elapsed:.2f}s")
                return (start_time, int(elapsed), total)
            
            return None
    
    def cleanup_stale_batches(self, max_age_hours: int = 24) -> int:
        """
        Clean up stale batch tracking entries that haven't been completed.
        Useful for clearing incomplete batches from failed/cancelled jobs.
        
        Args:
            max_age_hours: Maximum age in hours before a batch is considered stale
            
        Returns:
            Number of batches cleaned up
        """
        with self._batch_lock:
            current_time = time.time()
            max_age_seconds = max_age_hours * 3600
            cleaned = 0
            
            stale_batches = []
            for batch_id, start_time in self._batch_start_times.items():
                if current_time - start_time > max_age_seconds:
                    stale_batches.append(batch_id)
            
            for batch_id in stale_batches:
                self._batch_start_times.pop(batch_id, None)
                self._batch_task_counts.pop(batch_id, None)
                self._batch_completed_counts.pop(batch_id, None)
                cleaned += 1
                logger.info(f"Cleaned up stale batch {batch_id}")
            
            return cleaned
        
    def get_all_tasks(self) -> Dict[str, AnalysisTask]:
        """Get all tasks (pending, running, completed, failed)."""
        with self._task_lock:
            return self._tasks.copy()
            
    def get_pending_tasks(self) -> Dict[str, AnalysisTask]:
        """Get all pending tasks."""
        with self._task_lock:
            return {tid: task for tid, task in self._tasks.items() 
                   if task.status == WorkerTaskStatus.PENDING}
                   
    def get_running_tasks(self) -> Dict[str, AnalysisTask]:
        """Get all running tasks."""
        with self._task_lock:
            return {tid: task for tid, task in self._tasks.items() 
                   if task.status == WorkerTaskStatus.RUNNING}
                   
    def cancel_task(self, task_id: str) -> bool:
        """
        Cancel a pending task. Running tasks cannot be cancelled.
        
        Args:
            task_id: The ID of the task to cancel
            
        Returns:
            True if task was cancelled, False if task not found or not pending
        """
        with self._task_lock:
            task = self._tasks.get(task_id)
            if not task:
                return False
                
            if task.status != WorkerTaskStatus.PENDING:
                return False
                
            # Remove from task key mapping
            task_key = task.get_task_key()
            if task_key in self._task_keys and self._task_keys[task_key] == task_id:
                del self._task_keys[task_key]
                
            # Update task status
            task.status = WorkerTaskStatus.FAILED
            task.error = Exception("Task cancelled by user")
            task.completed_at = time.time()
            
            logger.info(f"Task '{task_id}' cancelled")
            return True
    
    def cancel_analysis_task(self, expert_instance_id: int, symbol: str) -> tuple[bool, str]:
        """
        Cancel a pending analysis task by expert instance ID and symbol.
        
        Args:
            expert_instance_id: The expert instance ID
            symbol: The symbol
            
        Returns:
            Tuple of (success: bool, message: str) - success indicates if cancelled, message explains why
        """
        with self._task_lock:
            # Find task by expert instance and symbol
            for task_id, task in self._tasks.items():
                if (task.expert_instance_id == expert_instance_id and 
                    task.symbol == symbol):
                    
                    # Check if task is already running
                    if task.status == WorkerTaskStatus.RUNNING:
                        return False, "Task is currently running and cannot be cancelled"
                    
                    # Check if task is not pending
                    if task.status != WorkerTaskStatus.PENDING:
                        return False, f"Task is in '{task.status.value}' status and cannot be cancelled"
                    
                    # Remove from task key mapping
                    task_key = task.get_task_key()
                    if task_key in self._task_keys and self._task_keys[task_key] == task_id:
                        del self._task_keys[task_key]
                        
                    # Update task status
                    task.status = WorkerTaskStatus.FAILED
                    task.error = Exception("Analysis task cancelled by user")
                    task.completed_at = time.time()
                    
                    logger.info(f"Analysis task for expert {expert_instance_id}, symbol {symbol} cancelled")
                    return True, "Task cancelled successfully"
        
        # Task not found - update MarketAnalysis to FAILED if it exists
        try:
            from .db import get_instance, update_instance
            from .models import MarketAnalysis
            from .types import MarketAnalysisStatus
            
            # Try to find MarketAnalysis by expert_instance_id and symbol
            with Session(get_db().bind) as session:
                from sqlmodel import select
                statement = select(MarketAnalysis).where(
                    MarketAnalysis.expert_instance_id == expert_instance_id,
                    MarketAnalysis.symbol == symbol,
                    MarketAnalysis.status.in_([
                        MarketAnalysisStatus.PENDING,
                        MarketAnalysisStatus.RUNNING
                    ])
                ).order_by(MarketAnalysis.id.desc()).limit(1)
                
                market_analysis = session.exec(statement).first()
                if market_analysis:
                    market_analysis.status = MarketAnalysisStatus.FAILED
                    if market_analysis.state is None:
                        market_analysis.state = {}
                    market_analysis.state["cancelled"] = True
                    market_analysis.state["cancel_reason"] = "Task not found in queue (may have already started or completed)"
                    session.add(market_analysis)
                    session.commit()
                    logger.info(f"Updated MarketAnalysis to FAILED for expert {expert_instance_id}, symbol {symbol} (task not found)")
        except Exception as e:
            logger.error(f"Error updating MarketAnalysis to FAILED for expert {expert_instance_id}, symbol {symbol}: {e}", exc_info=True)
        
        return False, "Task not found in queue - it may have already started or completed"
    
    def cancel_analysis_by_market_analysis_id(self, market_analysis_id: int) -> tuple[bool, str]:
        """
        Cancel a pending analysis task by market analysis ID.
        
        Args:
            market_analysis_id: The market analysis ID
            
        Returns:
            Tuple of (success: bool, message: str) - success indicates if cancelled, message explains why
        """
        with self._task_lock:
            # Find task by market_analysis_id
            for task_id, task in self._tasks.items():
                if task.market_analysis_id == market_analysis_id:
                    
                    # Check if task is already running
                    if task.status == WorkerTaskStatus.RUNNING:
                        return False, "Task is currently running and cannot be cancelled"
                    
                    # Check if task is not pending
                    if task.status != WorkerTaskStatus.PENDING:
                        return False, f"Task is in '{task.status.value}' status and cannot be cancelled"
                    
                    # Remove from task key mapping
                    task_key = task.get_task_key()
                    if task_key in self._task_keys and self._task_keys[task_key] == task_id:
                        del self._task_keys[task_key]
                        
                    # Update task status
                    task.status = WorkerTaskStatus.FAILED
                    task.error = Exception("Analysis task cancelled by user")
                    task.completed_at = time.time()
                    
                    logger.info(f"Analysis task with market_analysis_id {market_analysis_id} cancelled")
                    return True, "Task cancelled successfully"
        
        # Task not found - update MarketAnalysis to FAILED if it exists
        try:
            from .db import get_instance, update_instance
            from .models import MarketAnalysis
            from .types import MarketAnalysisStatus
            
            market_analysis = get_instance(MarketAnalysis, market_analysis_id)
            if market_analysis:
                if market_analysis.status not in [MarketAnalysisStatus.FAILED, MarketAnalysisStatus.COMPLETED]:
                    market_analysis.status = MarketAnalysisStatus.FAILED
                    if market_analysis.state is None:
                        market_analysis.state = {}
                    market_analysis.state["cancelled"] = True
                    market_analysis.state["cancel_reason"] = "Task not found in queue (may have already started or completed)"
                    update_instance(market_analysis)
                    logger.info(f"Updated MarketAnalysis {market_analysis_id} to FAILED (task not found)")
        except Exception as e:
            logger.error(f"Error updating MarketAnalysis {market_analysis_id} to FAILED: {e}", exc_info=True)
        
        return False, "Task not found in queue - it may have already started or completed"
        
    def _get_worker_count(self) -> int:
        """Get the configured worker count from AppSettings."""
        try:
            worker_count_str = get_setting("worker_count")
            if worker_count_str:
                worker_count = int(worker_count_str)
                if worker_count > 0:
                    return worker_count
        except (ValueError, TypeError) as e:
            logger.warning(f"Invalid worker_count setting: {e}")
            
        # Default to 4 workers and create the setting if it doesn't exist
        default_count = 4
        self._ensure_worker_count_setting(default_count)
        return default_count
        
    def _ensure_worker_count_setting(self, default_value: int):
        """Ensure the worker_count AppSetting exists with a default value."""
        try:
            existing_setting = get_setting("worker_count")
            if existing_setting is None:
                # Create the setting
                setting = AppSetting(
                    key="worker_count",
                    value_str=str(default_value)
                )
                add_instance(setting)
                logger.info(f"Created worker_count AppSetting with default value: {default_value}")
        except Exception as e:
            logger.error(f"Error ensuring worker_count setting: {e}", exc_info=True)
            
    def _worker_loop(self):
        """Main loop for worker threads."""
        worker_name = threading.current_thread().name
        logger.info(f"Worker {worker_name} started")
        
        while self._running and not self._shutdown_event.is_set():
            try:
                # Get task from queue with timeout (priority, counter, task)
                priority, counter, task = self._queue.get(timeout=1.0)
                
                # Check for sentinel value (shutdown signal)
                if task is None:
                    self._queue.task_done()  # Mark sentinel task as done
                    break
                
                # Handle different task types
                if isinstance(task, SmartRiskManagerTask):
                    # Execute Smart Risk Manager task
                    try:
                        self._execute_smart_risk_manager_task(task, worker_name)
                    except Exception as e:
                        logger.error(f"Error executing Smart Risk Manager task in worker {worker_name}: {e}", exc_info=True)
                    finally:
                        self._queue.task_done()
                    continue
                
                if isinstance(task, InstrumentExpansionTask):
                    # Execute instrument expansion task
                    try:
                        self._execute_instrument_expansion_task(task, worker_name)
                    except Exception as e:
                        logger.error(f"Error executing instrument expansion task in worker {worker_name}: {e}", exc_info=True)
                    finally:
                        self._queue.task_done()
                    continue
                
                # For AnalysisTask, track batch start if this is first task in a batch
                if isinstance(task, AnalysisTask) and task.batch_id:
                    with self._batch_lock:
                        if task.batch_id not in self._batch_start_times:
                            # This is the first task in this batch
                            self.track_batch_start(task.batch_id, 1)  # We'll update count in JobManager
                            
                            # Log batch start activity
                            try:
                                from .utils import log_analysis_batch_start
                                log_analysis_batch_start(
                                    batch_id=task.batch_id,
                                    expert_instance_id=task.expert_instance_id,
                                    total_jobs=1,  # Will be corrected in final log
                                    analysis_type=task.subtype,
                                    is_scheduled="_" in task.batch_id  # Scheduled batches have format: expertid_HHmm_YYYYMMDD
                                )
                            except Exception as e:
                                logger.warning(f"Failed to log batch start for {task.batch_id}: {e}")
                
                # For AnalysisTask, check if we should skip based on existing transactions
                should_skip = self._should_skip_task(task)
                if should_skip:
                    # Create or update MarketAnalysis record for skipped analysis
                    try:
                        from .db import get_instance, add_instance, update_instance
                        from .models import MarketAnalysis
                        from .types import MarketAnalysisStatus, AnalysisUseCase
                        
                        if task.market_analysis_id:
                            # Update existing MarketAnalysis record
                            market_analysis = get_instance(MarketAnalysis, task.market_analysis_id)
                            if market_analysis:
                                market_analysis.status = MarketAnalysisStatus.FAILED
                                if market_analysis.state is None:
                                    market_analysis.state = {}
                                market_analysis.state["skipped"] = True
                                market_analysis.state["skip_reason"] = should_skip
                                market_analysis.state["skip_type"] = "transaction_check"
                                update_instance(market_analysis)
                                logger.debug(f"Updated MarketAnalysis {task.market_analysis_id} status to FAILED due to skip")
                        else:
                            # Create new MarketAnalysis record for pre-check skips
                            market_analysis = MarketAnalysis(
                                symbol=task.symbol,
                                expert_instance_id=task.expert_instance_id,
                                status=MarketAnalysisStatus.FAILED,
                                subtype=AnalysisUseCase(task.subtype),
                                state={
                                    "skipped": True,
                                    "skip_reason": should_skip,
                                    "skip_type": "transaction_check"
                                }
                            )
                            market_analysis_id = add_instance(market_analysis)
                            task.market_analysis_id = market_analysis_id
                            logger.debug(f"Created MarketAnalysis {market_analysis_id} with FAILED status due to skip")
                    except Exception as e:
                        logger.error(f"Error creating/updating MarketAnalysis for skipped task: {e}", exc_info=True)
                    
                    # Mark task as completed since we're skipping it
                    with self._task_lock:
                        task.status = WorkerTaskStatus.COMPLETED
                        task.result = {"status": "skipped", "reason": should_skip, "market_analysis_id": task.market_analysis_id}
                        task.completed_at = time.time()
                        
                        # Clean up task key mapping
                        task_key = task.get_task_key()
                        if task_key in self._task_keys and self._task_keys[task_key] == task.id:
                            del self._task_keys[task_key]
                    
                    self._queue.task_done()
                    continue
                
                # Execute the task (with error handling inside)
                try:
                    self._execute_task(task, worker_name)
                except Exception as e:
                    logger.error(f"Error executing task in worker {worker_name}: {e}", exc_info=True)
                finally:
                    # Always mark task as done after getting it from queue
                    self._queue.task_done()
                    
            except queue.Empty:
                continue
            except Exception as e:
                logger.error(f"Unexpected error in worker {worker_name}: {e}", exc_info=True)
                
        logger.info(f"Worker {worker_name} stopped")
        
    def _execute_task(self, task: AnalysisTask, worker_name: str):
        """Execute a single analysis task."""
        logger.debug(f"Worker {worker_name} executing analysis task '{task.id}' for expert {task.expert_instance_id}, symbol {task.symbol}")
        
        # Update task status
        with self._task_lock:
            task.status = WorkerTaskStatus.RUNNING
            task.started_at = time.time()
        
        # Update persisted task status
        self._update_persisted_task_status(task.id, "running", datetime.fromtimestamp(task.started_at, tz=timezone.utc))
            
        try:
            # Import here to avoid circular imports
            from .db import get_instance, add_instance
            from .models import ExpertInstance, MarketAnalysis
            from .types import MarketAnalysisStatus
            from .utils import get_expert_instance_from_id
            
            # Get the expert instance with appropriate class
            expert = get_expert_instance_from_id(task.expert_instance_id)
            if not expert:
                raise ValueError(f"Expert instance {task.expert_instance_id} not found or invalid expert type")
            
            # Check available balance for ENTER_MARKET analysis before proceeding (unless bypassed)
            from .types import AnalysisUseCase
            if task.subtype == AnalysisUseCase.ENTER_MARKET.value and not task.bypass_balance_check:
                if not expert.has_sufficient_balance_for_entry():
                    # Skip analysis due to insufficient balance
                    logger.info(f"Skipping ENTER_MARKET analysis for expert {task.expert_instance_id}, symbol {task.symbol}: "
                              f"Insufficient available balance below threshold")
                    
                    # Create analysis record marked as skipped
                    market_analysis = MarketAnalysis(
                        symbol=task.symbol,
                        expert_instance_id=task.expert_instance_id,
                        status=MarketAnalysisStatus.SKIPPED,
                        subtype=AnalysisUseCase(task.subtype),
                        state={
                            "skipped": True,
                            "skip_reason": "Insufficient available balance below threshold",
                            "skip_type": "insufficient_balance"
                        }
                    )
                    market_analysis_id = add_instance(market_analysis)
                    task.market_analysis_id = market_analysis_id
                    
                    # Update task as completed (but skipped)
                    with self._task_lock:
                        task.status = WorkerTaskStatus.COMPLETED
                        task.result = {"market_analysis_id": market_analysis_id, "status": "skipped", "reason": "insufficient_balance"}
                        task.completed_at = time.time()
                    
                    execution_time = task.completed_at - task.started_at
                    logger.debug(f"Analysis task '{task.id}' skipped due to insufficient balance in {execution_time:.2f}s")
                    return

            # Check symbol price and balance constraints for ENTER_MARKET analysis (unless bypassed)
            if task.subtype == AnalysisUseCase.ENTER_MARKET.value and not task.bypass_balance_check:
                should_skip, skip_reason = expert.should_skip_analysis_for_symbol(task.symbol)
                if should_skip:
                    # Skip analysis due to symbol price/balance constraints
                    logger.info(f"Skipping ENTER_MARKET analysis for expert {task.expert_instance_id}, symbol {task.symbol}: {skip_reason}")
                    
                    # Create analysis record marked as skipped
                    market_analysis = MarketAnalysis(
                        symbol=task.symbol,
                        expert_instance_id=task.expert_instance_id,
                        status=MarketAnalysisStatus.SKIPPED,
                        subtype=AnalysisUseCase(task.subtype),
                        state={
                            "skipped": True,
                            "skip_reason": skip_reason,  # Store actual skip reason
                            "skip_type": "symbol_price_balance_check",
                            "analysis_timestamp": datetime.now(timezone.utc).isoformat()
                        }
                    )
                    market_analysis_id = add_instance(market_analysis)
                    task.market_analysis_id = market_analysis_id
                    
                    # Update task as completed (but skipped)
                    with self._task_lock:
                        task.status = WorkerTaskStatus.COMPLETED
                        task.result = {"market_analysis_id": market_analysis_id, "status": "skipped", "reason": "symbol_price_balance_check"}
                        task.completed_at = time.time()
                    
                    execution_time = task.completed_at - task.started_at
                    logger.debug(f"Analysis task '{task.id}' skipped due to symbol price/balance constraints in {execution_time:.2f}s")
                    return

            # Create or reuse MarketAnalysis record
            if task.market_analysis_id:
                # Reusing existing MarketAnalysis for retry
                market_analysis_id = task.market_analysis_id
                market_analysis = get_instance(MarketAnalysis, market_analysis_id)
                if not market_analysis:
                    raise ValueError(f"MarketAnalysis {market_analysis_id} not found for retry")
                # Ensure state is initialized (protect against NULL in database)
                if market_analysis.state is None:
                    market_analysis.state = {}
                # Update status to PENDING for retry
                market_analysis.status = MarketAnalysisStatus.PENDING
                from .db import update_instance
                update_instance(market_analysis)
                logger.info(f"Reusing MarketAnalysis {market_analysis_id} for retry of {task.symbol}")
            else:
                # Create new MarketAnalysis record with subtype
                market_analysis = MarketAnalysis(
                    symbol=task.symbol,
                    expert_instance_id=task.expert_instance_id,
                    status=MarketAnalysisStatus.PENDING,
                    subtype=AnalysisUseCase(task.subtype)
                )
                market_analysis_id = add_instance(market_analysis)
                task.market_analysis_id = market_analysis_id
                
                # Reload the market analysis object to get the ID
                market_analysis = get_instance(MarketAnalysis, market_analysis_id)
            
            # Run the analysis - this updates the market_analysis object
            expert.run_analysis(task.symbol, market_analysis)
            
            # Add 2-second sleep after analysis for all experts except TradingAgents
            expert_instance_record = get_instance(ExpertInstance, task.expert_instance_id)
            if expert_instance_record and expert_instance_record.expert != "TradingAgents":
                logger.debug(f"Adding 2-second sleep after {expert_instance_record.expert} analysis")
                time.sleep(2)
            
            # Update task with success
            with self._task_lock:
                task.status = WorkerTaskStatus.COMPLETED
                task.result = {"market_analysis_id": market_analysis_id, "status": "completed"}
                task.completed_at = time.time()
                
            execution_time = task.completed_at - task.started_at
            logger.debug(f"Analysis task '{task.id}' completed successfully in {execution_time:.2f}s")
            
            # Check if all analysis tasks are completed for this expert
            # If so, trigger automated order processing
            logger.debug(f"[RISK_MGR_TRIGGER] Task '{task.id}' (expert {task.expert_instance_id}, {task.subtype}) completed. Checking for SmartRiskManager trigger...")
            if task.subtype == AnalysisUseCase.ENTER_MARKET:
                logger.debug(f"[RISK_MGR_TRIGGER] Calling _check_and_process_expert_recommendations for ENTER_MARKET")
                self._check_and_process_expert_recommendations(task.expert_instance_id, AnalysisUseCase.ENTER_MARKET)
            elif task.subtype == AnalysisUseCase.OPEN_POSITIONS:
                logger.debug(f"[RISK_MGR_TRIGGER] Calling _check_and_process_expert_recommendations for OPEN_POSITIONS")
                self._check_and_process_expert_recommendations(task.expert_instance_id, AnalysisUseCase.OPEN_POSITIONS)
            
        except Exception as e:
            # Update task with failure
            with self._task_lock:
                task.status = WorkerTaskStatus.FAILED
                task.error = e
                task.completed_at = time.time()
            
            # Get detailed task information for error reporting
            expert_info = "Unknown"
            model_info = "Unknown"
            account_id = "Unknown"
            try:
                expert_instance_record = get_instance(ExpertInstance, task.expert_instance_id)
                if expert_instance_record:
                    expert_name = expert_instance_record.expert
                    expert_alias = expert_instance_record.alias or f"{expert_name}-{task.expert_instance_id}"
                    expert_info = f"{expert_alias} (ID: {task.expert_instance_id}, Type: {expert_name})"
                    account_id = expert_instance_record.account_id
                    
                    # Try to get model information from settings
                    if hasattr(expert_instance_record, 'settings') and expert_instance_record.settings:
                        settings = expert_instance_record.settings
                        if isinstance(settings, str):
                            import json
                            try:
                                settings = json.loads(settings)
                            except:
                                pass
                        
                        if isinstance(settings, dict):
                            # Try different possible keys for model information
                            model_info = (
                                settings.get('quick_think_llm') or 
                                settings.get('deep_think_llm') or 
                                settings.get('model') or 
                                settings.get('llm_model') or
                                "Not specified"
                            )
            except:
                pass  # Don't fail error logging if we can't get expert details
                
            execution_time = task.completed_at - task.started_at
            logger.error(
                f"Analysis task '{task.id}' failed after {execution_time:.2f}s\n"
                f"  Symbol: {task.symbol}\n"
                f"  Expert: {expert_info}\n"
                f"  Account ID: {account_id}\n"
                f"  Analysis Type: {task.subtype}\n"
                f"  Model: {model_info}\n"
                f"  Error: {e}",
                exc_info=True
            )
        
        finally:
            # Handle batch completion logging if this task belongs to a batch
            if hasattr(task, 'batch_id') and task.batch_id:
                try:
                    batch_completion = self.track_batch_job_completion(task.batch_id)
                    if batch_completion:
                        # This was the last job in the batch
                        start_time, elapsed_seconds, total_jobs = batch_completion
                        try:
                            from .utils import log_analysis_batch_end
                            log_analysis_batch_end(
                                batch_id=task.batch_id,
                                expert_instance_id=task.expert_instance_id,
                                total_jobs=total_jobs,
                                elapsed_seconds=elapsed_seconds,
                                analysis_type=task.subtype,
                                is_scheduled="_" in task.batch_id  # Scheduled batches have format: expertid_HHmm_YYYYMMDD
                            )
                        except Exception as e:
                            logger.warning(f"Failed to log batch end for {task.batch_id}: {e}")
                except Exception as e:
                    logger.warning(f"Error tracking batch completion for {task.batch_id}: {e}")
            
            # Clean up task key mapping for completed/failed tasks
            with self._task_lock:
                if task.status in [WorkerTaskStatus.COMPLETED, WorkerTaskStatus.FAILED]:
                    task_key = task.get_task_key()
                    if task_key in self._task_keys and self._task_keys[task_key] == task.id:
                        del self._task_keys[task_key]
            
            # Remove from persistence when task completes or fails
            if task.status in [WorkerTaskStatus.COMPLETED, WorkerTaskStatus.FAILED]:
                self._remove_persisted_task(task.id)
    
    def _execute_smart_risk_manager_task(self, task: SmartRiskManagerTask, worker_name: str):
        """Execute a Smart Risk Manager task."""
        logger.debug(f"Worker {worker_name} executing Smart Risk Manager task '{task.id}' for expert {task.expert_instance_id}")
        
        # Update task status
        with self._task_lock:
            task.status = WorkerTaskStatus.RUNNING
            task.started_at = time.time()
        
        # Update persisted task status
        self._update_persisted_task_status(task.id, "running", datetime.fromtimestamp(task.started_at, tz=timezone.utc))
        
        job_id = None
        try:
            # Import here to avoid circular imports
            from .db import get_instance, add_instance, update_instance
            from .models import ExpertInstance, SmartRiskManagerJob
            from .SmartRiskManagerGraph import run_smart_risk_manager
            from .utils import get_expert_instance_from_id
            
            # Get the expert instance to retrieve settings
            expert_instance = get_instance(ExpertInstance, task.expert_instance_id)
            if not expert_instance:
                raise ValueError(f"Expert instance {task.expert_instance_id} not found")
            
            # Get the full expert interface to access settings properly
            expert_interface = get_expert_instance_from_id(task.expert_instance_id)
            if not expert_interface:
                raise ValueError(f"Expert interface for instance {task.expert_instance_id} could not be loaded")
            
            # Get model and user instructions from settings
            settings = expert_interface.settings
            model_used = settings.get("risk_manager_model", "")
            user_instructions = settings.get("user_instructions", "")
            
            # Create SmartRiskManagerJob record with RUNNING status
            smart_risk_job = SmartRiskManagerJob(
                expert_instance_id=task.expert_instance_id,
                account_id=task.account_id,
                status="RUNNING",
                model_used=model_used,
                user_instructions=user_instructions,
                run_date=datetime.now(timezone.utc)
            )
            job_id = add_instance(smart_risk_job)
            task.job_id = job_id
            
            logger.info(f"Created SmartRiskManagerJob {job_id} for expert {task.expert_instance_id}")
            
            # Run the Smart Risk Manager with the existing job_id
            result = run_smart_risk_manager(task.expert_instance_id, task.account_id, job_id=job_id)
            
            # Reload the job to update it
            smart_risk_job = get_instance(SmartRiskManagerJob, job_id)
            if not smart_risk_job:
                raise ValueError(f"SmartRiskManagerJob {job_id} not found after execution")
            
            # Update job with results
            if result["success"]:
                smart_risk_job.status = "COMPLETED"
                smart_risk_job.iteration_count = result.get("iterations", 0)
                smart_risk_job.actions_taken_count = result.get("actions_count", 0)
                smart_risk_job.actions_summary = result.get("summary", "")
                
                # Store actions log if available
                if "actions" in result and result["actions"]:
                    smart_risk_job.actions_log = result["actions"]
                
                # Update duration
                if smart_risk_job.run_date:
                    # Ensure run_date is timezone-aware (handle legacy data)
                    if smart_risk_job.run_date.tzinfo is None:
                        run_date_utc = smart_risk_job.run_date.replace(tzinfo=timezone.utc)
                    else:
                        run_date_utc = smart_risk_job.run_date
                    duration = (datetime.now(timezone.utc) - run_date_utc).total_seconds()
                    smart_risk_job.duration_seconds = int(duration)
                
                logger.info(f"SmartRiskManagerJob {job_id} completed successfully: {result.get('iterations', 0)} iterations, {result.get('actions_count', 0)} actions")
            else:
                smart_risk_job.status = "FAILED"
                error_message = result.get("error", "Unknown error")
                smart_risk_job.error_message = error_message
                logger.error(f"SmartRiskManagerJob {job_id} failed: {error_message}")
            
            update_instance(smart_risk_job)
            
            # Update task with success
            with self._task_lock:
                task.status = WorkerTaskStatus.COMPLETED
                task.result = {
                    "job_id": job_id,
                    "status": "completed" if result["success"] else "failed",
                    "iterations": result.get("iterations", 0),
                    "actions_count": result.get("actions_count", 0),
                    "summary": result.get("summary", ""),
                    "error": result.get("error") if not result["success"] else None
                }
                task.completed_at = time.time()
            
            execution_time = task.completed_at - task.started_at
            logger.debug(f"Smart Risk Manager task '{task.id}' completed in {execution_time:.2f}s")
            
        except Exception as e:
            # Update job with failure if job was created
            if job_id:
                try:
                    from .db import get_instance, update_instance
                    from .models import SmartRiskManagerJob
                    smart_risk_job = get_instance(SmartRiskManagerJob, job_id)
                    if smart_risk_job:
                        smart_risk_job.status = "FAILED"
                        smart_risk_job.error_message = str(e)
                        
                        # Update duration
                        if smart_risk_job.run_date:
                            # Ensure run_date is timezone-aware (handle legacy data)
                            if smart_risk_job.run_date.tzinfo is None:
                                run_date_utc = smart_risk_job.run_date.replace(tzinfo=timezone.utc)
                            else:
                                run_date_utc = smart_risk_job.run_date
                            duration = (datetime.now(timezone.utc) - run_date_utc).total_seconds()
                            smart_risk_job.duration_seconds = int(duration)
                        
                        update_instance(smart_risk_job)
                except Exception as update_error:
                    logger.error(f"Failed to update SmartRiskManagerJob {job_id} with error status: {update_error}")
            
            # Update task with failure
            with self._task_lock:
                task.status = WorkerTaskStatus.FAILED
                task.error = e
                task.completed_at = time.time()
            
            execution_time = task.completed_at - task.started_at
            logger.error(f"Smart Risk Manager task '{task.id}' failed after {execution_time:.2f}s: {e}", exc_info=True)
            
            # Log activity for task failure
            try:
                from .db import log_activity
                from .types import ActivityLogSeverity, ActivityLogType
                
                log_activity(
                    severity=ActivityLogSeverity.FAILURE,
                    activity_type=ActivityLogType.TASK_FAILED,
                    description=f"Smart Risk Manager task '{task.id}' failed: {str(e)}",
                    data={
                        "task_id": task.id,
                        "task_type": "smart_risk_manager",
                        "expert_instance_id": task.expert_instance_id,
                        "job_id": job_id,
                        "execution_time": execution_time,
                        "error": str(e)
                    },
                    source_expert_id=task.expert_instance_id,
                    source_account_id=None  # No account context for SRM tasks
                )
            except Exception as log_error:
                logger.warning(f"Failed to log Smart Risk Manager task failure activity: {log_error}")
        
        finally:
            # Clean up task key mapping for completed/failed tasks
            with self._task_lock:
                if task.status in [WorkerTaskStatus.COMPLETED, WorkerTaskStatus.FAILED]:
                    task_key = task.get_task_key()
                    if task_key in self._task_keys and self._task_keys[task_key] == task.id:
                        del self._task_keys[task_key]
            
            # Remove from persistence when task completes or fails
            if task.status in [WorkerTaskStatus.COMPLETED, WorkerTaskStatus.FAILED]:
                self._remove_persisted_task(task.id)
    
    def _execute_instrument_expansion_task(self, task: InstrumentExpansionTask, worker_name: str):
        """Execute an instrument expansion task (DYNAMIC/EXPERT/OPEN_POSITIONS)."""
        logger.debug(f"Worker {worker_name} executing instrument expansion task '{task.id}' ({task.expansion_type}) for expert {task.expert_instance_id}")
        
        # Update task status
        with self._task_lock:
            task.status = WorkerTaskStatus.RUNNING
            task.started_at = time.time()
        
        # Update persisted task status
        self._update_persisted_task_status(task.id, "running", datetime.fromtimestamp(task.started_at, tz=timezone.utc))
        
        try:
            # Import JobManager to access expansion methods
            from .JobManager import get_job_manager
            
            # Get the JobManager singleton instance
            job_manager = get_job_manager()
            
            # Execute appropriate expansion method based on type
            if task.expansion_type == "DYNAMIC":
                job_manager._execute_dynamic_analysis(task.expert_instance_id, task.subtype, batch_id=task.batch_id)
                logger.info(f"Dynamic analysis expansion completed for expert {task.expert_instance_id}")
            elif task.expansion_type == "EXPERT":
                job_manager._execute_expert_driven_analysis(task.expert_instance_id, task.subtype, batch_id=task.batch_id)
                logger.info(f"Expert-driven analysis expansion completed for expert {task.expert_instance_id}")
            elif task.expansion_type == "OPEN_POSITIONS":
                job_manager._execute_open_positions_analysis(task.expert_instance_id, task.subtype, batch_id=task.batch_id)
                logger.info(f"Open positions analysis expansion completed for expert {task.expert_instance_id}")
            else:
                raise ValueError(f"Unknown expansion type: {task.expansion_type}")
            
            # Update task with success
            with self._task_lock:
                task.status = WorkerTaskStatus.COMPLETED
                task.result = {
                    "expansion_type": task.expansion_type,
                    "expert_instance_id": task.expert_instance_id,
                    "subtype": task.subtype,
                    "status": "completed"
                }
                task.completed_at = time.time()
            
            execution_time = task.completed_at - task.started_at
            logger.debug(f"Instrument expansion task '{task.id}' ({task.expansion_type}) completed in {execution_time:.2f}s")
            
        except Exception as e:
            # Update task with failure
            with self._task_lock:
                task.status = WorkerTaskStatus.FAILED
                task.error = e
                task.completed_at = time.time()
            
            execution_time = task.completed_at - task.started_at
            logger.error(f"Instrument expansion task '{task.id}' ({task.expansion_type}) failed after {execution_time:.2f}s: {e}", exc_info=True)
        
        finally:
            # Clean up task key mapping for completed/failed tasks
            with self._task_lock:
                if task.status in [WorkerTaskStatus.COMPLETED, WorkerTaskStatus.FAILED]:
                    task_key = task.get_task_key()
                    if task_key in self._task_keys and self._task_keys[task_key] == task.id:
                        del self._task_keys[task_key]
            
            # Remove from persistence when task completes or fails
            if task.status in [WorkerTaskStatus.COMPLETED, WorkerTaskStatus.FAILED]:
                self._remove_persisted_task(task.id)
    
    def _check_and_process_expert_recommendations(self, expert_instance_id: int, use_case: AnalysisUseCase = AnalysisUseCase.ENTER_MARKET) -> None:
        """
        Check if there are any pending analysis tasks for an expert.
        If not, trigger automated order processing based on expert's risk_manager_mode setting:
        - "smart": Queue SmartRiskManager task for agentic portfolio management
        - "classic": Use TradeManager for direct recommendation processing
        
        Args:
            expert_instance_id: The expert instance ID to check
            use_case: The analysis use case (ENTER_MARKET or OPEN_POSITIONS)
        """
        try:
            logger.debug(f"[RISK_MGR_TRIGGER] ===== START _check_and_process_expert_recommendations for expert {expert_instance_id}, use_case={use_case.value} =====")
            
            # Check if there are any pending tasks for this expert
            # Use lock to prevent race condition when multiple jobs complete simultaneously
            with self._risk_manager_lock:
                # Check if this expert is already being processed by another thread
                lock_key = f"expert_{expert_instance_id}_{use_case.value}"
                if lock_key in self._processing_experts:
                    logger.debug(f"[RISK_MGR_TRIGGER] Expert {expert_instance_id} ({use_case.value}) is already being processed, skipping")
                    return
                
                logger.debug(f"[RISK_MGR_TRIGGER] Checking for pending tasks for expert {expert_instance_id}, use_case={use_case.value}")
                
                # Check for pending tasks
                has_pending = False
                pending_tasks = []
                with self._task_lock:
                    for task in self._tasks.values():
                        if (task.expert_instance_id == expert_instance_id and
                            task.subtype == use_case and
                            task.status in [WorkerTaskStatus.PENDING, WorkerTaskStatus.RUNNING]):
                            has_pending = True
                            pending_tasks.append((task.id, task.symbol, task.status))
                
                if has_pending:
                    logger.debug(f"[RISK_MGR_TRIGGER] Still has {len(pending_tasks)} pending {use_case.value} tasks for expert {expert_instance_id}: {pending_tasks}")
                    logger.debug(f"[RISK_MGR_TRIGGER] ===== END (pending tasks found, skipping) =====")
                    return
                
                logger.info(f"[RISK_MGR_TRIGGER] No pending {use_case.value} tasks for expert {expert_instance_id}, proceeding with risk manager check")
                
                if not has_pending:
                    # Mark this expert as being processed
                    self._processing_experts.add(lock_key)
                    
                    try:
                        logger.info(f"[RISK_MGR_TRIGGER] All {use_case.value} analysis tasks completed for expert {expert_instance_id}, triggering automated processing")
                        
                        # Get expert instance with loaded settings (MarketExpertInterface)
                        from .utils import get_expert_instance_from_id
                        from .db import get_instance
                        from .models import ExpertInstance
                        
                        expert = get_expert_instance_from_id(expert_instance_id)
                        if not expert:
                            logger.error(f"[RISK_MGR_TRIGGER] Expert instance {expert_instance_id} not found or invalid expert type")
                            logger.debug(f"[RISK_MGR_TRIGGER] ===== END (expert not found) =====")
                            return
                        
                        logger.debug(f"[RISK_MGR_TRIGGER] Got expert instance: {type(expert).__name__}")
                        
                        # Get the ExpertInstance database record (already cached by get_expert_instance_from_id)
                        expert_instance_record = get_instance(ExpertInstance, expert_instance_id)
                        if not expert_instance_record:
                            error_msg = f"ExpertInstance {expert_instance_id} not found in database"
                            logger.error(f"[RISK_MGR_TRIGGER] {error_msg}")
                            logger.debug(f"[RISK_MGR_TRIGGER] ===== END (expert instance record not found) =====")
                            return
                        
                        # Check risk_manager_mode setting with validation and error logging
                        from .utils import get_risk_manager_mode
                        from .db import log_activity
                        from .types import ActivityLogSeverity, ActivityLogType
                        
                        settings = expert.settings or {}
                        risk_manager_mode = get_risk_manager_mode(settings)
                        
                        logger.debug(f"[RISK_MGR_TRIGGER] Retrieved risk_manager_mode: {risk_manager_mode}, settings keys: {list(settings.keys())}")
                        
                        # Validate risk_manager_mode is properly configured
                        if not risk_manager_mode:
                            error_msg = f"Risk manager mode not configured for expert {expert_instance_id}"
                            logger.error(f"[RISK_MGR_TRIGGER] {error_msg}")
                            try:
                                log_activity(
                                    severity=ActivityLogSeverity.FAILURE,
                                    activity_type=ActivityLogType.RISK_MANAGER_EXECUTION,
                                    description=error_msg,
                                    data={"expert_id": expert_instance_id, "issue": "missing_risk_manager_mode"},
                                    source_expert_id=expert_instance_id
                                )
                            except Exception as e:
                                logger.warning(f"Failed to log activity for missing risk_manager_mode: {e}")
                            logger.debug(f"[RISK_MGR_TRIGGER] ===== END (risk_manager_mode not configured) =====")
                            return
                        
                        if risk_manager_mode == "classic":
                            logger.debug(f"[RISK_MGR_TRIGGER] Using CLASSIC risk manager mode")
                            
                            # Get ruleset ID from ExpertInstance database record
                            enter_market_ruleset_id = expert_instance_record.enter_market_ruleset_id
                            # Check if ruleset ID is actually set (not None, not empty string)
                            if not enter_market_ruleset_id or (isinstance(enter_market_ruleset_id, str) and not enter_market_ruleset_id.strip()):
                                error_msg = f"Classic risk manager mode enabled but no enter_market_ruleset_id configured for expert {expert_instance_id}"
                                logger.error(f"[RISK_MGR_TRIGGER] {error_msg}")
                                logger.error(f"Settings keys: {list(settings.keys()) if settings else 'None'}")
                                logger.error(f"enter_market_ruleset_id value: {repr(enter_market_ruleset_id)}")
                                try:
                                    log_activity(
                                        severity=ActivityLogSeverity.FAILURE,
                                        activity_type=ActivityLogType.RISK_MANAGER_EXECUTION,
                                        description=error_msg,
                                        data={"expert_id": expert_instance_id, "issue": "missing_classic_rules", "settings_keys": list(settings.keys()) if settings else []},
                                        source_expert_id=expert_instance_id
                                    )
                                except Exception as e:
                                    logger.warning(f"Failed to log activity for missing classic rules: {e}")
                                logger.debug(f"[RISK_MGR_TRIGGER] ===== END (classic mode but no ruleset) =====")
                                return
                        
                        if risk_manager_mode == "smart":
                            # Use Smart Risk Manager for automated processing
                            logger.info(f"[RISK_MGR_TRIGGER] Expert {expert_instance_id} using SMART risk manager mode, triggering SmartRiskManager")
                            
                            # Get account_id from expert instance database record
                            from .db import get_instance
                            from .models import ExpertInstance
                            expert_instance = get_instance(ExpertInstance, expert_instance_id)
                            if not expert_instance or not expert_instance.account_id:
                                logger.error(f"[RISK_MGR_TRIGGER] Expert instance {expert_instance_id} not found in database or has no account_id")
                                logger.debug(f"[RISK_MGR_TRIGGER] ===== END (no expert instance or account_id) =====")
                                return
                            
                            logger.debug(f"[RISK_MGR_TRIGGER] Found expert instance with account_id={expert_instance.account_id}")
                            
                            account_id = expert_instance.account_id
                            
                            # Add Smart Risk Manager task to queue
                            try:
                                logger.debug(f"[RISK_MGR_TRIGGER] Submitting SmartRiskManager task for expert {expert_instance_id}, account {account_id}")
                                task_id = self.submit_smart_risk_manager_task(expert_instance_id, account_id)
                                logger.info(f"[RISK_MGR_TRIGGER] ✓ Queued Smart Risk Manager task {task_id} for expert {expert_instance_id}")
                                logger.debug(f"[RISK_MGR_TRIGGER] ===== END (SmartRiskManager queued successfully) =====")
                            except Exception as e:
                                logger.error(f"[RISK_MGR_TRIGGER] ✗ Failed to queue Smart Risk Manager task for expert {expert_instance_id}: {e}", exc_info=True)
                                logger.debug(f"[RISK_MGR_TRIGGER] ===== END (error queuing SmartRiskManager) =====")
                        else:
                            # Use classic TradeManager for automated processing
                            logger.info(f"[RISK_MGR_TRIGGER] Expert {expert_instance_id} using CLASSIC risk manager mode, triggering TradeManager")
                            
                            from .TradeManager import get_trade_manager
                            trade_manager = get_trade_manager()
                            
                            if use_case == AnalysisUseCase.ENTER_MARKET:
                                logger.debug(f"[RISK_MGR_TRIGGER] Processing ENTER_MARKET recommendations")
                                created_orders = trade_manager.process_expert_recommendations_after_analysis(expert_instance_id)
                            elif use_case == AnalysisUseCase.OPEN_POSITIONS:
                                logger.debug(f"[RISK_MGR_TRIGGER] Processing OPEN_POSITIONS recommendations")
                                created_orders = trade_manager.process_open_positions_recommendations(expert_instance_id)
                            else:
                                logger.error(f"[RISK_MGR_TRIGGER] Unknown use case: {use_case}")
                                logger.debug(f"[RISK_MGR_TRIGGER] ===== END (unknown use case) =====")
                                return
                            
                            if created_orders:
                                logger.info(f"[RISK_MGR_TRIGGER] Automated processing created {len(created_orders)} orders for expert {expert_instance_id}")
                            else:
                                logger.debug(f"[RISK_MGR_TRIGGER] No orders created by automated processing for expert {expert_instance_id}")
                            logger.debug(f"[RISK_MGR_TRIGGER] ===== END (classic mode completed) =====")
                    finally:
                        # Always remove from processing set, even if an error occurred
                        logger.debug(f"[RISK_MGR_TRIGGER] Removing from processing set for {lock_key}")
                        self._processing_experts.discard(lock_key)
                else:
                    logger.debug(f"[RISK_MGR_TRIGGER] Still has pending {use_case.value} tasks for expert {expert_instance_id}, skipping automated processing")
                    logger.debug(f"[RISK_MGR_TRIGGER] ===== END (has pending tasks) =====")
                
        except Exception as e:
            logger.error(f"[RISK_MGR_TRIGGER] ✗ Error checking and processing recommendations for expert {expert_instance_id} ({use_case.value}): {e}", exc_info=True)
            logger.debug(f"[RISK_MGR_TRIGGER] ===== END (exception) =====")
    
    def has_existing_transactions(self, expert_id: int, symbol: str) -> bool:
        """
        Check if there are existing transactions for the given expert_id and symbol.
        
        Args:
            expert_id: The expert instance ID
            symbol: The trading symbol
            
        Returns:
            True if existing transactions are found, False otherwise
        """
        try:
            from sqlmodel import select, Session
            from .db import get_db
            from .models import Transaction
            from .types import TransactionStatus
            
            with Session(get_db().bind) as session:
                # Check for transactions with this expert_id and symbol
                # that are not closed or failed
                statement = select(Transaction).where(
                    Transaction.expert_id == expert_id,
                    Transaction.symbol == symbol,
                    Transaction.status.in_([
                        TransactionStatus.WAITING,
                        TransactionStatus.OPENED
                    ])
                )
                
                transaction = session.exec(statement).first()
                has_transactions = transaction is not None
                
                logger.debug(f"Existing Transactions check for expert {expert_id}, symbol {symbol}: {'found' if has_transactions else 'no transactions found found'}")
                return has_transactions
                
        except Exception as e:
            logger.error(f"Error checking for existing transactions for expert {expert_id}, symbol {symbol}: {e}", exc_info=True)
            return False  # Default to False if error occurs
    
    def _should_skip_task(self, task: AnalysisTask) -> Optional[str]:
        """
        Determine if a task should be skipped based on existing transactions and analysis type.
        
        For OPEN_POSITIONS analysis:
        - If symbol is "EXPERT" or "DYNAMIC": Skip only if NO open positions exist (any symbol)
        - If symbol is a regular symbol: Skip only if NO existing transactions exist
        
        Args:
            task: The analysis task to check
            
        Returns:
            String explaining why task should be skipped, or None if task should proceed
        """
        try:
            # If bypass_transaction_check is True, skip transaction validation
            if task.bypass_transaction_check:
                logger.debug(f"Bypassing transaction check for expert {task.expert_instance_id}, symbol {task.symbol} (manual analysis)")
                return None
            
            if task.subtype == AnalysisUseCase.ENTER_MARKET:
                # ENTER_MARKET: Check if there are existing transactions for this specific symbol
                has_transactions = self.has_existing_transactions(task.expert_instance_id, task.symbol)
                if has_transactions:
                    logger.info(f"Skipping ENTER_MARKET analysis for expert {task.expert_instance_id}, symbol {task.symbol}: existing transactions found (OPENED or WAITING)")
                    return "existing transactions found for enter_market analysis"
            
            elif task.subtype == AnalysisUseCase.OPEN_POSITIONS:
                # OPEN_POSITIONS: Check based on symbol type
                if task.symbol in ("EXPERT", "DYNAMIC"):
                    # For special symbols, check if ANY open positions exist for this expert
                    has_any_open_positions = self._has_any_open_positions(task.expert_instance_id)
                    if not has_any_open_positions:
                        logger.info(f"Skipping OPEN_POSITIONS analysis for expert {task.expert_instance_id}, symbol {task.symbol}: no open positions found for any symbol")
                        return "no open positions found for open_positions analysis"
                else:
                    # For regular symbols, check if transactions exist for this specific symbol
                    has_transactions = self.has_existing_transactions(task.expert_instance_id, task.symbol)
                    if not has_transactions:
                        logger.info(f"Skipping OPEN_POSITIONS analysis for expert {task.expert_instance_id}, symbol {task.symbol}: no existing transactions found")
                        return "no existing transactions found for open_positions analysis"
            
            return None  # Task should proceed
            
        except Exception as e:
            logger.error(f"Error checking if task should be skipped: {e}", exc_info=True)
            return None  # Default to proceeding if error occurs
    
    def _has_any_open_positions(self, expert_id: int) -> bool:
        """
        Check if there are ANY open positions for the given expert (regardless of symbol).
        Used for EXPERT and DYNAMIC symbol analysis.
        
        Args:
            expert_id: The expert instance ID
            
        Returns:
            True if any open positions exist, False otherwise
        """
        try:
            from sqlmodel import select, Session
            from .db import get_db
            from .models import Transaction
            from .types import TransactionStatus
            
            with Session(get_db().bind) as session:
                # Check for ANY transactions with this expert_id that are open
                statement = select(Transaction).where(
                    Transaction.expert_id == expert_id,
                    Transaction.status.in_([
                        TransactionStatus.WAITING,
                        TransactionStatus.OPENED
                    ])
                )
                
                transaction = session.exec(statement).first()
                has_positions = transaction is not None
                
                logger.debug(f"Open positions check for expert {expert_id}: {'found' if has_positions else 'not found'}")
                return has_positions
                
        except Exception as e:
            logger.error(f"Error checking for open positions for expert {expert_id}: {e}", exc_info=True)
            return False  # Default to False if error occurs

    # ==================== PERSISTENCE METHODS ====================
    
    def _persist_task(self, task, queue_counter: int) -> bool:
        """
        Persist a task to the database for recovery after restart.
        
        Args:
            task: AnalysisTask, SmartRiskManagerTask, or InstrumentExpansionTask
            queue_counter: The queue counter value for ordering
            
        Returns:
            True if persisted successfully, False otherwise
        """
        try:
            from sqlmodel import select
            
            # Determine task type and extract fields
            if isinstance(task, SmartRiskManagerTask):
                task_type = "smart_risk_manager"
                persisted = PersistedQueueTask(
                    task_id=task.id,
                    task_type=task_type,
                    status=task.status.value if hasattr(task.status, 'value') else str(task.status),
                    priority=task.priority,
                    expert_instance_id=task.expert_instance_id,
                    account_id=task.account_id,
                    queue_counter=queue_counter
                )
            elif isinstance(task, InstrumentExpansionTask):
                task_type = "instrument_expansion"
                persisted = PersistedQueueTask(
                    task_id=task.id,
                    task_type=task_type,
                    status=task.status.value if hasattr(task.status, 'value') else str(task.status),
                    priority=task.priority,
                    expert_instance_id=task.expert_instance_id,
                    subtype=task.subtype,
                    expansion_type=task.expansion_type,
                    batch_id=task.batch_id,
                    queue_counter=queue_counter
                )
            elif isinstance(task, AnalysisTask):
                task_type = "analysis"
                persisted = PersistedQueueTask(
                    task_id=task.id,
                    task_type=task_type,
                    status=task.status.value if hasattr(task.status, 'value') else str(task.status),
                    priority=task.priority,
                    expert_instance_id=task.expert_instance_id,
                    symbol=task.symbol,
                    subtype=task.subtype,
                    market_analysis_id=task.market_analysis_id,
                    batch_id=task.batch_id,
                    bypass_balance_check=task.bypass_balance_check,
                    bypass_transaction_check=task.bypass_transaction_check,
                    queue_counter=queue_counter
                )
            else:
                logger.warning(f"Unknown task type: {type(task)}")
                return False
            
            # Check if task already exists and update, otherwise insert
            with Session(get_db().bind) as session:
                existing = session.exec(
                    select(PersistedQueueTask).where(PersistedQueueTask.task_id == task.id)
                ).first()
                
                if existing:
                    # Update existing record
                    existing.status = persisted.status
                    existing.priority = persisted.priority
                    existing.queue_counter = queue_counter
                    session.add(existing)
                else:
                    session.add(persisted)
                    
                session.commit()
                
            logger.debug(f"Persisted task {task.id} (type={task_type})")
            return True
            
        except Exception as e:
            logger.error(f"Failed to persist task {task.id}: {e}", exc_info=True)
            return False
    
    def _update_persisted_task_status(self, task_id: str, status: str, started_at: datetime = None) -> bool:
        """
        Update the status of a persisted task.
        
        Args:
            task_id: The task ID
            status: New status value
            started_at: Optional timestamp when task started
            
        Returns:
            True if updated successfully, False otherwise
        """
        try:
            from sqlmodel import select
            
            with Session(get_db().bind) as session:
                persisted = session.exec(
                    select(PersistedQueueTask).where(PersistedQueueTask.task_id == task_id)
                ).first()
                
                if persisted:
                    persisted.status = status
                    if started_at:
                        persisted.started_at = started_at
                    session.add(persisted)
                    session.commit()
                    logger.debug(f"Updated persisted task {task_id} status to {status}")
                    return True
                    
            return False
            
        except Exception as e:
            logger.error(f"Failed to update persisted task {task_id}: {e}", exc_info=True)
            return False
    
    def _remove_persisted_task(self, task_id: str) -> bool:
        """
        Remove a task from persistence (called when task completes or fails).
        
        Args:
            task_id: The task ID to remove
            
        Returns:
            True if removed successfully, False otherwise
        """
        try:
            from sqlmodel import select, delete
            
            with Session(get_db().bind) as session:
                statement = delete(PersistedQueueTask).where(PersistedQueueTask.task_id == task_id)
                session.exec(statement)
                session.commit()
                
            logger.debug(f"Removed persisted task {task_id}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to remove persisted task {task_id}: {e}", exc_info=True)
            return False
    
    def save_queue_state(self) -> int:
        """
        Save all pending and running tasks to the database.
        Called on shutdown and periodically for safety.
        
        Returns:
            Number of tasks saved
        """
        saved_count = 0
        try:
            with self._task_lock:
                for task_id, task in self._tasks.items():
                    if task.status in [WorkerTaskStatus.PENDING, WorkerTaskStatus.RUNNING]:
                        # Get the queue_counter from the task (we'll use _queue_counter as fallback)
                        queue_counter = getattr(task, '_queue_counter', self._queue_counter)
                        if self._persist_task(task, queue_counter):
                            saved_count += 1
            
            logger.info(f"Saved {saved_count} queue tasks to database")
            return saved_count
            
        except Exception as e:
            logger.error(f"Failed to save queue state: {e}", exc_info=True)
            return saved_count
    
    def get_persisted_tasks_count(self) -> Dict[str, int]:
        """
        Get counts of persisted tasks by status.
        
        Returns:
            Dict with 'pending', 'running', and 'total' counts
        """
        try:
            from sqlmodel import select, func
            
            with Session(get_db().bind) as session:
                pending_count = session.exec(
                    select(func.count()).select_from(PersistedQueueTask).where(
                        PersistedQueueTask.status == "pending"
                    )
                ).one()
                
                running_count = session.exec(
                    select(func.count()).select_from(PersistedQueueTask).where(
                        PersistedQueueTask.status == "running"
                    )
                ).one()
                
                return {
                    'pending': pending_count or 0,
                    'running': running_count or 0,
                    'total': (pending_count or 0) + (running_count or 0)
                }
                
        except Exception as e:
            logger.error(f"Failed to get persisted tasks count: {e}", exc_info=True)
            return {'pending': 0, 'running': 0, 'total': 0}
    
    def get_persisted_tasks(self) -> list:
        """
        Get all persisted tasks (pending and running).
        
        Returns:
            List of PersistedQueueTask objects
        """
        try:
            from sqlmodel import select
            
            with Session(get_db().bind) as session:
                tasks = session.exec(
                    select(PersistedQueueTask).where(
                        PersistedQueueTask.status.in_(["pending", "running"])
                    ).order_by(PersistedQueueTask.priority, PersistedQueueTask.queue_counter)
                ).all()
                
                # Detach from session to prevent lazy loading issues
                return [
                    PersistedQueueTask(
                        id=t.id,
                        task_id=t.task_id,
                        task_type=t.task_type,
                        status=t.status,
                        priority=t.priority,
                        expert_instance_id=t.expert_instance_id,
                        account_id=t.account_id,
                        symbol=t.symbol,
                        subtype=t.subtype,
                        market_analysis_id=t.market_analysis_id,
                        batch_id=t.batch_id,
                        expansion_type=t.expansion_type,
                        bypass_balance_check=t.bypass_balance_check,
                        bypass_transaction_check=t.bypass_transaction_check,
                        created_at=t.created_at,
                        started_at=t.started_at,
                        queue_counter=t.queue_counter
                    )
                    for t in tasks
                ]
                
        except Exception as e:
            logger.error(f"Failed to get persisted tasks: {e}", exc_info=True)
            return []
    
    def restore_persisted_tasks(self) -> Dict[str, int]:
        """
        Restore persisted tasks to the queue.
        Previously running tasks are treated as failed and need to be restarted.
        
        Returns:
            Dict with 'restored', 'failed' counts
        """
        restored_count = 0
        failed_count = 0
        
        try:
            persisted_tasks = self.get_persisted_tasks()
            
            if not persisted_tasks:
                logger.info("No persisted tasks to restore")
                return {'restored': 0, 'failed': 0}
            
            logger.info(f"Restoring {len(persisted_tasks)} persisted tasks...")
            
            for pt in persisted_tasks:
                try:
                    # If task was running, it needs to be restarted (like a failed task)
                    # For analysis tasks, we clear the market_analysis_id to create a new one
                    if pt.status == "running":
                        pt.market_analysis_id = None  # Clear to create fresh analysis
                    
                    if pt.task_type == "analysis":
                        task_id = self.submit_analysis_task(
                            expert_instance_id=pt.expert_instance_id,
                            symbol=pt.symbol,
                            subtype=pt.subtype or AnalysisUseCase.ENTER_MARKET,
                            priority=pt.priority,
                            bypass_balance_check=pt.bypass_balance_check,
                            bypass_transaction_check=pt.bypass_transaction_check,
                            market_analysis_id=pt.market_analysis_id,
                            batch_id=pt.batch_id
                        )
                        restored_count += 1
                        logger.debug(f"Restored analysis task {pt.task_id} as {task_id}")
                        
                    elif pt.task_type == "smart_risk_manager":
                        task_id = self.submit_smart_risk_manager_task(
                            expert_instance_id=pt.expert_instance_id,
                            account_id=pt.account_id,
                            priority=pt.priority
                        )
                        restored_count += 1
                        logger.debug(f"Restored smart risk manager task {pt.task_id} as {task_id}")
                        
                    elif pt.task_type == "instrument_expansion":
                        task_id = self.submit_instrument_expansion_task(
                            expert_instance_id=pt.expert_instance_id,
                            expansion_type=pt.expansion_type,
                            subtype=pt.subtype or "ENTER_MARKET",
                            priority=pt.priority,
                            batch_id=pt.batch_id
                        )
                        restored_count += 1
                        logger.debug(f"Restored expansion task {pt.task_id} as {task_id}")
                    
                    # Remove the old persisted task since we created a new one
                    self._remove_persisted_task(pt.task_id)
                    
                except ValueError as e:
                    # Task already exists (duplicate), skip and remove persisted entry
                    logger.debug(f"Skipping duplicate task {pt.task_id}: {e}")
                    self._remove_persisted_task(pt.task_id)
                    
                except Exception as e:
                    logger.error(f"Failed to restore task {pt.task_id}: {e}", exc_info=True)
                    failed_count += 1
            
            logger.info(f"Restored {restored_count} tasks, {failed_count} failed")
            return {'restored': restored_count, 'failed': failed_count}
            
        except Exception as e:
            logger.error(f"Failed to restore persisted tasks: {e}", exc_info=True)
            return {'restored': restored_count, 'failed': failed_count}
    
    def clear_persisted_tasks(self) -> int:
        """
        Clear all persisted tasks from database.
        
        Returns:
            Number of tasks cleared
        """
        try:
            from sqlmodel import delete
            
            with Session(get_db().bind) as session:
                result = session.exec(delete(PersistedQueueTask))
                count = result.rowcount
                session.commit()
                
            logger.info(f"Cleared {count} persisted tasks")
            return count
            
        except Exception as e:
            logger.error(f"Failed to clear persisted tasks: {e}", exc_info=True)
            return 0
    
    def clear_stale_persisted_tasks(self, max_age_hours: int = 24) -> int:
        """
        Clear persisted tasks older than the specified age.
        Called at startup to remove stale tasks that are unlikely to be valid.
        
        Args:
            max_age_hours: Maximum age in hours before a task is considered stale (default 24)
            
        Returns:
            Number of tasks cleared
        """
        try:
            from sqlmodel import delete
            from datetime import timedelta
            
            cutoff_time = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
            
            with Session(get_db().bind) as session:
                result = session.exec(
                    delete(PersistedQueueTask).where(
                        PersistedQueueTask.created_at < cutoff_time
                    )
                )
                count = result.rowcount
                session.commit()
            
            if count > 0:
                logger.info(f"Cleared {count} stale persisted tasks older than {max_age_hours} hours")
            else:
                logger.debug(f"No stale persisted tasks to clear (max age: {max_age_hours} hours)")
            
            return count
            
        except Exception as e:
            logger.error(f"Failed to clear stale persisted tasks: {e}", exc_info=True)
            return 0


# Global worker queue instance
_worker_queue_instance: Optional[WorkerQueue] = None


def get_worker_queue() -> WorkerQueue:
    """Get the global worker queue instance."""
    global _worker_queue_instance
    if _worker_queue_instance is None:
        _worker_queue_instance = WorkerQueue()
    return _worker_queue_instance


def initialize_worker_queue():
    """Initialize and start the global worker queue."""
    worker_queue = get_worker_queue()
    if not worker_queue.is_running():
        # Clear stale persisted tasks older than 24 hours
        worker_queue.clear_stale_persisted_tasks(max_age_hours=24)
        worker_queue.start()
        

def shutdown_worker_queue():
    """Shutdown the global worker queue and save pending tasks for later resumption."""
    global _worker_queue_instance
    if _worker_queue_instance and _worker_queue_instance.is_running():
        # Save queue state before stopping
        logger.info("Saving queue state before shutdown...")
        _worker_queue_instance.save_queue_state()
        _worker_queue_instance.stop()