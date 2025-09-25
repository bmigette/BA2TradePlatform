"""
Worker Queue System for BA2 Trade Platform

This module provides a configurable worker queue system for processing tasks
asynchronously using a thread pool. The number of workers is configurable
through the AppSettings system.
"""

import threading
import queue
import time
from typing import Callable, Any, Optional, Dict
from dataclasses import dataclass
from enum import Enum
from ..logger import logger
from .db import get_setting, add_instance, update_instance
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
    
    def __post_init__(self):
        if self.created_at is None:
            self.created_at = time.time()
    
    def get_task_key(self) -> str:
        """Get a unique key for this task based on expert instance and symbol."""
        return f"{self.expert_instance_id}_{self.symbol}"


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
            self._queue.put((0, None))
            
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
        
    def submit_analysis_task(self, expert_instance_id: int, symbol: str, subtype: str = AnalysisUseCase.ENTER_MARKET, priority: int = 0, task_id: Optional[str] = None) -> str:
        """
        Submit an analysis task to be processed by the worker queue.
        
        Args:
            expert_instance_id: The expert instance ID to run the analysis
            symbol: The symbol to analyze
            subtype: Analysis use case (AnalysisUseCase.ENTER_MARKET or AnalysisUseCase.OPEN_POSITIONS)
            priority: Task priority (lower numbers = higher priority)
            task_id: Optional custom task ID
            
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
                priority=priority
            )
            
            self._tasks[task_id] = task
            self._task_keys[task_key] = task_id
            
        # Add to priority queue (priority, task)
        self._queue.put((priority, task))
        
        logger.debug(f"Analysis task '{task_id}' submitted for expert {expert_instance_id}, symbol {symbol}, priority {priority}")
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
    
    def cancel_analysis_task(self, expert_instance_id: int, symbol: str) -> bool:
        """
        Cancel a pending analysis task by expert instance ID and symbol.
        
        Args:
            expert_instance_id: The expert instance ID
            symbol: The symbol
            
        Returns:
            True if task was cancelled, False if task not found or not pending
        """
        with self._task_lock:
            # Find task by expert instance and symbol
            for task_id, task in self._tasks.items():
                if (task.expert_instance_id == expert_instance_id and 
                    task.symbol == symbol and 
                    task.status == WorkerTaskStatus.PENDING):
                    
                    # Remove from task key mapping
                    task_key = task.get_task_key()
                    if task_key in self._task_keys and self._task_keys[task_key] == task_id:
                        del self._task_keys[task_key]
                        
                    # Update task status
                    task.status = WorkerTaskStatus.FAILED
                    task.error = Exception("Analysis task cancelled by user")
                    task.completed_at = time.time()
                    
                    logger.info(f"Analysis task for expert {expert_instance_id}, symbol {symbol} cancelled")
                    return True
        
        return False
        
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
            logger.error(f"Error ensuring worker_count setting: {e}")
            
    def _worker_loop(self):
        """Main loop for worker threads."""
        worker_name = threading.current_thread().name
        logger.info(f"Worker {worker_name} started")
        
        while self._running and not self._shutdown_event.is_set():
            try:
                # Get task from queue with timeout
                try:
                    priority, task = self._queue.get(timeout=1.0)
                    
                    # Check for sentinel value (shutdown signal)
                    if task is None:
                        break
                        
                except queue.Empty:
                    continue
                    
                # Execute the task
                self._execute_task(task, worker_name)
                
                # Mark task as done
                self._queue.task_done()
                
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
            
            # Create MarketAnalysis record with subtype
            from .types import AnalysisUseCase
            market_analysis = MarketAnalysis(
                symbol=task.symbol,
                source_expert_instance_id=task.expert_instance_id,
                status=MarketAnalysisStatus.PENDING,
                subtype=AnalysisUseCase(task.subtype)
            )
            market_analysis_id = add_instance(market_analysis)
            task.market_analysis_id = market_analysis_id
            
            # Reload the market analysis object to get the ID
            market_analysis = get_instance(MarketAnalysis, market_analysis_id)
            
            # Run the analysis - this updates the market_analysis object
            expert.run_analysis(task.symbol, market_analysis)
            
            # Update task with success
            with self._task_lock:
                task.status = WorkerTaskStatus.COMPLETED
                task.result = {"market_analysis_id": market_analysis_id, "status": "completed"}
                task.completed_at = time.time()
                
            execution_time = task.completed_at - task.started_at
            logger.debug(f"Analysis task '{task.id}' completed successfully in {execution_time:.2f}s")
            
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