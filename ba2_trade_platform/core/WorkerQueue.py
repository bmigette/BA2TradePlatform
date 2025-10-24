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
from .models import AppSetting
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
                           market_analysis_id: Optional[int] = None) -> str:
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
                market_analysis_id=market_analysis_id
            )
            
            self._tasks[task_id] = task
            self._task_keys[task_key] = task_id
            
        # Add to priority queue with tiebreaker (priority, counter, task)
        # The counter ensures unique ordering when priorities are equal
        with self._task_lock:
            self._queue_counter += 1
            queue_entry = (priority, self._queue_counter, task)
        
        self._queue.put(queue_entry)
        
        logger.debug(f"Analysis task '{task_id}' submitted for expert {expert_instance_id}, symbol {symbol}, priority {priority}")
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
        
        logger.info(f"Smart Risk Manager task '{task_id}' submitted for expert {expert_instance_id}, priority {priority}")
        return task_id
        
    def submit_instrument_expansion_task(self, expert_instance_id: int, expansion_type: str, 
                                        subtype: str = "ENTER_MARKET", priority: int = 5, 
                                        task_id: Optional[str] = None) -> str:
        """
        Submit an instrument expansion task (DYNAMIC/EXPERT/OPEN_POSITIONS) to be processed by worker queue.
        
        Args:
            expert_instance_id: The expert instance ID to expand instruments for
            expansion_type: Type of expansion ("DYNAMIC", "EXPERT", or "OPEN_POSITIONS")
            subtype: Analysis use case subtype (default "ENTER_MARKET")
            priority: Task priority (lower numbers = higher priority, default 5)
            task_id: Optional custom task ID
            
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
                priority=priority
            )
            
            self._tasks[task_id] = task
            self._task_keys[task_key] = task_id
            
        # Add to priority queue with tiebreaker
        with self._task_lock:
            self._queue_counter += 1
            queue_entry = (priority, self._queue_counter, task)
        
        self._queue.put(queue_entry)
        
        logger.info(f"Instrument expansion task '{task_id}' ({expansion_type}) submitted for expert {expert_instance_id}, priority {priority}")
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
                        status=MarketAnalysisStatus.CANCELLED,
                        subtype=AnalysisUseCase(task.subtype),
                        state={"reason": "insufficient_balance", "message": "Skipped due to insufficient available balance"}
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
                        status=MarketAnalysisStatus.FAILED,  # Changed from CANCELLED to FAILED for visibility
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
            if task.subtype == AnalysisUseCase.ENTER_MARKET:
                self._check_and_process_expert_recommendations(task.expert_instance_id, AnalysisUseCase.ENTER_MARKET)
            elif task.subtype == AnalysisUseCase.OPEN_POSITIONS:
                self._check_and_process_expert_recommendations(task.expert_instance_id, AnalysisUseCase.OPEN_POSITIONS)
            
        except Exception as e:
            # Update task with failure
            with self._task_lock:
                task.status = WorkerTaskStatus.FAILED
                task.error = e
                task.completed_at = time.time()
                
            execution_time = task.completed_at - task.started_at
            logger.error(f"Analysis task '{task.id}' failed after {execution_time:.2f}s: {e}", exc_info=True)
        
        finally:
            # Clean up task key mapping for completed/failed tasks
            with self._task_lock:
                if task.status in [WorkerTaskStatus.COMPLETED, WorkerTaskStatus.FAILED]:
                    task_key = task.get_task_key()
                    if task_key in self._task_keys and self._task_keys[task_key] == task.id:
                        del self._task_keys[task_key]
    
    def _execute_smart_risk_manager_task(self, task: SmartRiskManagerTask, worker_name: str):
        """Execute a Smart Risk Manager task."""
        logger.debug(f"Worker {worker_name} executing Smart Risk Manager task '{task.id}' for expert {task.expert_instance_id}")
        
        # Update task status
        with self._task_lock:
            task.status = WorkerTaskStatus.RUNNING
            task.started_at = time.time()
        
        job_id = None
        try:
            # Import here to avoid circular imports
            from .db import get_instance, add_instance, update_instance
            from .models import ExpertInstance, SmartRiskManagerJob
            from .SmartRiskManagerGraph import run_smart_risk_manager
            
            # Get the expert instance to retrieve settings
            expert_instance = get_instance(ExpertInstance, task.expert_instance_id)
            if not expert_instance:
                raise ValueError(f"Expert instance {task.expert_instance_id} not found")
            
            # Get model and user instructions from settings
            settings = expert_instance.settings or {}
            model_used = settings.get("llm_model", "")
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
                    duration = (datetime.now(timezone.utc) - smart_risk_job.run_date).total_seconds()
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
                            duration = (datetime.now(timezone.utc) - smart_risk_job.run_date).total_seconds()
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
        
        finally:
            # Clean up task key mapping for completed/failed tasks
            with self._task_lock:
                if task.status in [WorkerTaskStatus.COMPLETED, WorkerTaskStatus.FAILED]:
                    task_key = task.get_task_key()
                    if task_key in self._task_keys and self._task_keys[task_key] == task.id:
                        del self._task_keys[task_key]
    
    def _execute_instrument_expansion_task(self, task: InstrumentExpansionTask, worker_name: str):
        """Execute an instrument expansion task (DYNAMIC/EXPERT/OPEN_POSITIONS)."""
        logger.debug(f"Worker {worker_name} executing instrument expansion task '{task.id}' ({task.expansion_type}) for expert {task.expert_instance_id}")
        
        # Update task status
        with self._task_lock:
            task.status = WorkerTaskStatus.RUNNING
            task.started_at = time.time()
        
        try:
            # Import JobManager to access expansion methods
            from .JobManager import get_job_manager
            
            # Get the JobManager singleton instance
            job_manager = get_job_manager()
            
            # Execute appropriate expansion method based on type
            if task.expansion_type == "DYNAMIC":
                job_manager._execute_dynamic_analysis(task.expert_instance_id, task.subtype)
                logger.info(f"Dynamic analysis expansion completed for expert {task.expert_instance_id}")
            elif task.expansion_type == "EXPERT":
                job_manager._execute_expert_driven_analysis(task.expert_instance_id, task.subtype)
                logger.info(f"Expert-driven analysis expansion completed for expert {task.expert_instance_id}")
            elif task.expansion_type == "OPEN_POSITIONS":
                job_manager._execute_open_positions_analysis(task.expert_instance_id, task.subtype)
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
            # Check if there are any pending tasks for this expert
            # Use lock to prevent race condition when multiple jobs complete simultaneously
            with self._risk_manager_lock:
                # Check if this expert is already being processed by another thread
                lock_key = f"expert_{expert_instance_id}_{use_case.value}"
                if lock_key in self._processing_experts:
                    logger.debug(f"Expert {expert_instance_id} ({use_case.value}) is already being processed, skipping")
                    return
                
                # Check for pending tasks
                has_pending = False
                with self._task_lock:
                    for task in self._tasks.values():
                        if (task.expert_instance_id == expert_instance_id and
                            task.subtype == use_case and
                            task.status in [WorkerTaskStatus.PENDING, WorkerTaskStatus.RUNNING]):
                            has_pending = True
                            break
                
                if not has_pending:
                    # Mark this expert as being processed
                    self._processing_experts.add(lock_key)
                    
                    try:
                        logger.info(f"All {use_case.value} analysis tasks completed for expert {expert_instance_id}, triggering automated processing")
                        
                        # Get expert instance with loaded settings (MarketExpertInterface)
                        from .utils import get_expert_instance_from_id
                        
                        expert = get_expert_instance_from_id(expert_instance_id)
                        if not expert:
                            logger.error(f"Expert instance {expert_instance_id} not found or invalid expert type")
                            return
                        
                        # Check risk_manager_mode setting
                        settings = expert.settings or {}
                        risk_manager_mode = settings.get("risk_manager_mode", "classic")
                        
                        if risk_manager_mode == "smart":
                            # Use Smart Risk Manager for automated processing
                            logger.info(f"Expert {expert_instance_id} using Smart Risk Manager mode, triggering SmartRiskManager")
                            
                            # Get account_id from expert instance database record
                            from .db import get_instance
                            from .models import ExpertInstance
                            expert_instance = get_instance(ExpertInstance, expert_instance_id)
                            if not expert_instance or not expert_instance.account_id:
                                logger.error(f"Expert instance {expert_instance_id} not found in database or has no account_id")
                                return
                            
                            account_id = expert_instance.account_id
                            
                            # Add Smart Risk Manager task to queue
                            try:
                                task_id = self.submit_smart_risk_manager_task(expert_instance_id, account_id)
                                logger.info(f"Queued Smart Risk Manager task {task_id} for expert {expert_instance_id}")
                            except Exception as e:
                                logger.error(f"Failed to queue Smart Risk Manager task for expert {expert_instance_id}: {e}", exc_info=True)
                        else:
                            # Use classic TradeManager for automated processing
                            logger.info(f"Expert {expert_instance_id} using classic risk manager mode, triggering TradeManager")
                            
                            from .TradeManager import get_trade_manager
                            trade_manager = get_trade_manager()
                            
                            if use_case == AnalysisUseCase.ENTER_MARKET:
                                created_orders = trade_manager.process_expert_recommendations_after_analysis(expert_instance_id)
                            elif use_case == AnalysisUseCase.OPEN_POSITIONS:
                                created_orders = trade_manager.process_open_positions_recommendations(expert_instance_id)
                            else:
                                logger.error(f"Unknown use case: {use_case}")
                                return
                            
                            if created_orders:
                                logger.info(f"Automated processing created {len(created_orders)} orders for expert {expert_instance_id}")
                            else:
                                logger.debug(f"No orders created by automated processing for expert {expert_instance_id}")
                    finally:
                        # Always remove from processing set, even if an error occurred
                        self._processing_experts.discard(lock_key)
                else:
                    logger.debug(f"Still has pending {use_case.value} tasks for expert {expert_instance_id}, skipping automated processing")
                
        except Exception as e:
            logger.error(f"Error checking and processing recommendations for expert {expert_instance_id} ({use_case.value}): {e}", exc_info=True)
    
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
                
                logger.debug(f"Transaction check for expert {expert_id}, symbol {symbol}: {'found' if has_transactions else 'not found'}")
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
        worker_queue.start()
        

def shutdown_worker_queue():
    """Shutdown the global worker queue."""
    global _worker_queue_instance
    if _worker_queue_instance and _worker_queue_instance.is_running():
        _worker_queue_instance.stop()