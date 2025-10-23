"""
Smart Risk Manager Queue - Dedicated worker queue for Smart Risk Manager jobs.

Provides a separate thread pool and queue for Smart Risk Manager execution,
independent from market analysis tasks. This ensures Smart Risk Manager jobs
don't compete for resources with analysis tasks.
"""

import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Optional, Dict, Any
from enum import Enum
from datetime import datetime, timezone

from ..logger import logger


class SmartRiskManagerTaskStatus(Enum):
    """Status of a Smart Risk Manager task."""
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class SmartRiskManagerTask:
    """Task for Smart Risk Manager execution."""
    id: str
    expert_instance_id: int
    account_id: int
    status: SmartRiskManagerTaskStatus = SmartRiskManagerTaskStatus.PENDING
    job_id: Optional[int] = None  # Linked SmartRiskManagerJob ID
    result: Optional[Dict[str, Any]] = None
    error: Optional[Exception] = None
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    
    def get_task_key(self) -> str:
        """Generate unique key for task deduplication."""
        return f"smart_risk_manager_{self.expert_instance_id}"


class SmartRiskManagerQueue:
    """
    Dedicated worker queue for Smart Risk Manager jobs.
    
    Runs independently from the analysis WorkerQueue with its own thread pool.
    """
    
    def __init__(self, num_workers: int = 2):
        """
        Initialize the Smart Risk Manager queue.
        
        Args:
            num_workers: Number of worker threads (default 2, since Smart Risk Manager is CPU-intensive)
        """
        self._queue: queue.Queue = queue.Queue()
        self._num_workers = num_workers
        self._workers: list[threading.Thread] = []
        self._running = False
        self._task_lock = threading.Lock()
        self._tasks: Dict[str, SmartRiskManagerTask] = {}  # task_id -> task
        self._task_keys: Dict[str, str] = {}  # task_key -> task_id (for deduplication)
        self._task_counter = 0
        
        logger.info(f"SmartRiskManagerQueue initialized with {num_workers} workers")
    
    def start(self):
        """Start the worker threads."""
        if self._running:
            logger.warning("SmartRiskManagerQueue is already running")
            return
        
        self._running = True
        
        # Start worker threads
        for i in range(self._num_workers):
            worker = threading.Thread(
                target=self._worker_loop,
                args=(f"SmartRiskManagerWorker-{i+1}",),
                daemon=True
            )
            worker.start()
            self._workers.append(worker)
        
        logger.info(f"SmartRiskManagerQueue started with {self._num_workers} worker threads")
    
    def stop(self, timeout: float = 5.0):
        """
        Stop the worker threads.
        
        Args:
            timeout: Maximum time to wait for workers to finish (seconds)
        """
        if not self._running:
            logger.warning("SmartRiskManagerQueue is not running")
            return
        
        logger.info("Stopping SmartRiskManagerQueue...")
        self._running = False
        
        # Send sentinel values to wake up all workers
        for _ in range(self._num_workers):
            self._queue.put(None)
        
        # Wait for workers to finish
        for worker in self._workers:
            worker.join(timeout=timeout)
            if worker.is_alive():
                logger.warning(f"Worker {worker.name} did not stop gracefully")
        
        self._workers.clear()
        logger.info("SmartRiskManagerQueue stopped")
    
    def is_running(self) -> bool:
        """Check if the queue is running."""
        return self._running
    
    def submit_task(self, expert_instance_id: int, account_id: int) -> Optional[str]:
        """
        Submit a Smart Risk Manager task to the queue.
        
        Args:
            expert_instance_id: Expert instance ID
            account_id: Account ID for the expert
            
        Returns:
            Task ID if successfully submitted, None if duplicate exists
        """
        task_key = f"smart_risk_manager_{expert_instance_id}"
        
        with self._task_lock:
            # Check for existing pending/running task for this expert
            if task_key in self._task_keys:
                existing_task_id = self._task_keys[task_key]
                existing_task = self._tasks.get(existing_task_id)
                if existing_task and existing_task.status in [
                    SmartRiskManagerTaskStatus.PENDING,
                    SmartRiskManagerTaskStatus.RUNNING
                ]:
                    logger.info(f"Task already exists for expert {expert_instance_id}: {existing_task_id}")
                    return None
            
            # Create new task
            self._task_counter += 1
            task_id = f"srm_task_{self._task_counter}"
            
            task = SmartRiskManagerTask(
                id=task_id,
                expert_instance_id=expert_instance_id,
                account_id=account_id
            )
            
            self._tasks[task_id] = task
            self._task_keys[task_key] = task_id
            
            # Enqueue task
            self._queue.put(task)
            
            logger.info(f"Submitted Smart Risk Manager task {task_id} for expert {expert_instance_id}")
            return task_id
    
    def get_task_status(self, task_id: str) -> Optional[Dict[str, Any]]:
        """
        Get status of a specific task.
        
        Args:
            task_id: Task ID
            
        Returns:
            Dict with task status info, or None if not found
        """
        with self._task_lock:
            task = self._tasks.get(task_id)
            if not task:
                return None
            
            return {
                "id": task.id,
                "expert_instance_id": task.expert_instance_id,
                "account_id": task.account_id,
                "status": task.status.value,
                "job_id": task.job_id,
                "created_at": task.created_at,
                "started_at": task.started_at,
                "completed_at": task.completed_at,
                "result": task.result,
                "error": str(task.error) if task.error else None
            }
    
    def get_all_tasks(self) -> list[Dict[str, Any]]:
        """
        Get status of all tasks.
        
        Returns:
            List of task status dicts
        """
        with self._task_lock:
            return [
                {
                    "id": task.id,
                    "expert_instance_id": task.expert_instance_id,
                    "account_id": task.account_id,
                    "status": task.status.value,
                    "job_id": task.job_id,
                    "created_at": task.created_at,
                    "started_at": task.started_at,
                    "completed_at": task.completed_at,
                    "result": task.result,
                    "error": str(task.error) if task.error else None
                }
                for task in self._tasks.values()
            ]
    
    def get_queue_status(self) -> Dict[str, Any]:
        """
        Get current queue status.
        
        Returns:
            Dict with queue statistics
        """
        with self._task_lock:
            pending = sum(1 for t in self._tasks.values() if t.status == SmartRiskManagerTaskStatus.PENDING)
            running = sum(1 for t in self._tasks.values() if t.status == SmartRiskManagerTaskStatus.RUNNING)
            completed = sum(1 for t in self._tasks.values() if t.status == SmartRiskManagerTaskStatus.COMPLETED)
            failed = sum(1 for t in self._tasks.values() if t.status == SmartRiskManagerTaskStatus.FAILED)
        
        return {
            "num_workers": self._num_workers,
            "pending_tasks": pending,
            "running_tasks": running,
            "completed_tasks": completed,
            "failed_tasks": failed,
            "total_tasks": len(self._tasks),
            "queue_size": self._queue.qsize()
        }
    
    def _worker_loop(self, worker_name: str):
        """
        Worker thread loop - processes tasks from the queue.
        
        Args:
            worker_name: Name of this worker thread
        """
        logger.info(f"{worker_name} started")
        
        while self._running:
            try:
                # Get task from queue (blocking with timeout)
                task = self._queue.get(timeout=1.0)
                
                # Check for sentinel value (shutdown signal)
                if task is None:
                    logger.info(f"{worker_name} received shutdown signal")
                    break
                
                # Execute task
                try:
                    self._execute_task(task, worker_name)
                except Exception as e:
                    logger.error(f"{worker_name} error executing task {task.id}: {e}", exc_info=True)
                finally:
                    self._queue.task_done()
                    
            except queue.Empty:
                # Timeout - continue loop
                continue
            except Exception as e:
                logger.error(f"{worker_name} error in worker loop: {e}", exc_info=True)
        
        logger.info(f"{worker_name} stopped")
    
    def _execute_task(self, task: SmartRiskManagerTask, worker_name: str):
        """
        Execute a Smart Risk Manager task.
        
        Args:
            task: Task to execute
            worker_name: Name of the worker executing the task
        """
        logger.debug(f"{worker_name} executing task {task.id} for expert {task.expert_instance_id}")
        
        # Update task status to RUNNING
        with self._task_lock:
            task.status = SmartRiskManagerTaskStatus.RUNNING
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
            
            logger.info(f"{worker_name} created SmartRiskManagerJob {job_id} for expert {task.expert_instance_id}")
            
            # Run the Smart Risk Manager
            result = run_smart_risk_manager(task.expert_instance_id, task.account_id)
            
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
                
                logger.info(f"{worker_name} SmartRiskManagerJob {job_id} completed: {result.get('iterations', 0)} iterations, {result.get('actions_count', 0)} actions")
            else:
                smart_risk_job.status = "FAILED"
                error_message = result.get("error", "Unknown error")
                smart_risk_job.error_message = error_message
                logger.error(f"{worker_name} SmartRiskManagerJob {job_id} failed: {error_message}")
            
            update_instance(smart_risk_job)
            
            # Update task with success
            with self._task_lock:
                task.status = SmartRiskManagerTaskStatus.COMPLETED
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
            logger.debug(f"{worker_name} task {task.id} completed in {execution_time:.2f}s")
            
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
                    logger.error(f"{worker_name} failed to update SmartRiskManagerJob {job_id} with error status: {update_error}")
            
            # Update task with failure
            with self._task_lock:
                task.status = SmartRiskManagerTaskStatus.FAILED
                task.error = e
                task.completed_at = time.time()
            
            execution_time = task.completed_at - task.started_at if task.started_at else 0
            logger.error(f"{worker_name} task {task.id} failed after {execution_time:.2f}s: {e}", exc_info=True)
        
        finally:
            # Clean up task key mapping for completed/failed tasks
            with self._task_lock:
                if task.status in [SmartRiskManagerTaskStatus.COMPLETED, SmartRiskManagerTaskStatus.FAILED]:
                    task_key = task.get_task_key()
                    if task_key in self._task_keys and self._task_keys[task_key] == task.id:
                        del self._task_keys[task_key]


# Global Smart Risk Manager queue instance
_smart_risk_manager_queue_instance: Optional[SmartRiskManagerQueue] = None


def get_smart_risk_manager_queue() -> SmartRiskManagerQueue:
    """Get the global Smart Risk Manager queue instance."""
    global _smart_risk_manager_queue_instance
    if _smart_risk_manager_queue_instance is None:
        _smart_risk_manager_queue_instance = SmartRiskManagerQueue()
    return _smart_risk_manager_queue_instance


def initialize_smart_risk_manager_queue():
    """Initialize and start the global Smart Risk Manager queue."""
    queue = get_smart_risk_manager_queue()
    if not queue.is_running():
        queue.start()
        logger.info("Smart Risk Manager queue initialized and started")


def shutdown_smart_risk_manager_queue():
    """Shutdown the global Smart Risk Manager queue."""
    global _smart_risk_manager_queue_instance
    if _smart_risk_manager_queue_instance and _smart_risk_manager_queue_instance.is_running():
        _smart_risk_manager_queue_instance.stop()
        logger.info("Smart Risk Manager queue shut down")
