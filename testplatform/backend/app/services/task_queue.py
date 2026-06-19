"""
Database-backed Task Queue Service

Provides task queue functionality using the database instead of Redis/Celery.
Tasks are processed by background threads within the same application.
"""

import logging
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Dict, Any, List, Callable
from sqlalchemy.orm import Session
from sqlalchemy import and_, or_

from app.models.database import SessionLocal
from app.models.task_queue import TaskQueue, TaskStatus, TaskPriority

logger = logging.getLogger(__name__)


class TaskQueueService:
    """
    Database-backed task queue service.

    Provides:
    - Task creation and queuing
    - Background task processing
    - Progress tracking
    - Retry logic
    - Task cancellation

    Usage:
        # Initialize service
        task_service = TaskQueueService()
        task_service.start()

        # Register task handlers
        task_service.register_handler('training', training_handler)

        # Queue a task
        task_id = task_service.queue_task(
            task_type='training',
            name='Train LSTM Model',
            payload={'dataset_id': 1, 'model_type': 'lstm'}
        )

        # Check task status
        status = task_service.get_task_status(task_id)
    """

    def __init__(self, max_workers: int = 2, poll_interval: float = 1.0, task_types: Optional[List[str]] = None, exclude_task_types: Optional[List[str]] = None, name: str = "TaskQueue", use_subprocess: bool = False, worker_script: str = None):
        """
        Initialize task queue service.

        Args:
            max_workers: Maximum concurrent task workers
            poll_interval: Seconds between queue polls
            task_types: If set, ONLY process tasks of these types (whitelist)
            exclude_task_types: If set, NEVER process tasks of these types (blacklist)
                                Ignored when task_types is also set.
            name: Queue name for logging
            use_subprocess: If True, run task handlers in separate processes
                            to avoid GIL contention. The subprocess handles its
                            own DB updates; the worker thread just monitors it.
            worker_script: Name of the worker script in the backend dir
                           (e.g. "training_worker.py", "backtest_worker.py").
                           Required when use_subprocess=True.
        """
        self.max_workers = max_workers
        self.poll_interval = poll_interval
        self.task_types = task_types
        self.exclude_task_types = exclude_task_types
        self.name = name
        self.use_subprocess = use_subprocess
        self.worker_script = worker_script
        self._handlers: Dict[str, Callable] = {}
        self._running = False
        self._workers: List[threading.Thread] = []
        self._lock = threading.Lock()
        self._active_tasks: Dict[str, threading.Thread] = {}
        self._active_processes: Dict[str, 'subprocess.Popen'] = {}

    def register_handler(self, task_type: str, handler: Callable):
        """
        Register a handler function for a task type.

        Args:
            task_type: Type of task (e.g., 'training', 'backtest')
            handler: Function to handle the task. Should accept (task_id, payload) and return result dict.
        """
        self._handlers[task_type] = handler
        logger.info(f"Registered handler for task type: {task_type}")

    def start(self):
        """Start the task queue worker threads."""
        if self._running:
            logger.warning("Task queue already running")
            return

        self._running = True

        # Start worker threads
        for i in range(self.max_workers):
            worker = threading.Thread(
                target=self._worker_loop,
                name=f"{self.name}-Worker-{i}",
                daemon=True
            )
            worker.start()
            self._workers.append(worker)

        logger.info(f"Started {self.name} with {self.max_workers} workers")

    def stop(self):
        """Stop the task queue."""
        self._running = False
        logger.info(f"Stopping {self.name}...")

        # Terminate active subprocesses
        for task_id, proc in list(self._active_processes.items()):
            if proc.poll() is None:
                logger.info(f"Terminating subprocess for task {task_id}")
                proc.terminate()

        # Wait for workers to finish
        for worker in self._workers:
            worker.join(timeout=5.0)

        self._workers.clear()
        logger.info(f"{self.name} stopped")

    def resize_workers(self, max_workers: int):
        """
        Resize the worker pool.

        Adding workers takes effect immediately. Reducing workers is advisory —
        the new limit is respected by idle workers on their next poll cycle,
        and excess daemon threads will exit gracefully.

        Args:
            max_workers: New desired worker count (must be >= 1)
        """
        max_workers = max(1, max_workers)
        if max_workers == self.max_workers:
            return

        old_max = self.max_workers
        self.max_workers = max_workers

        if max_workers > old_max and self._running:
            for i in range(old_max, max_workers):
                worker = threading.Thread(
                    target=self._worker_loop,
                    name=f"{self.name}-Worker-{i}",
                    daemon=True
                )
                worker.start()
                self._workers.append(worker)
            logger.info(f"{self.name}: added {max_workers - old_max} workers, now {max_workers} total")
        else:
            logger.info(f"{self.name}: reduced max_workers from {old_max} to {max_workers} (excess threads will idle)")

    def queue_task(
        self,
        task_type: str,
        name: str,
        payload: Optional[Dict[str, Any]] = None,
        description: Optional[str] = None,
        priority: TaskPriority = TaskPriority.NORMAL,
        scheduled_at: Optional[datetime] = None,
        max_retries: int = 3,
        timeout_seconds: int = 3600
    ) -> str:
        """
        Queue a new task for processing.

        Args:
            task_type: Type of task
            name: Task name/title
            payload: Task parameters
            description: Optional description
            priority: Task priority
            scheduled_at: Optional delayed execution time
            max_retries: Maximum retry attempts
            timeout_seconds: Task timeout

        Returns:
            Task ID
        """
        task_id = str(uuid.uuid4())[:12]

        db = SessionLocal()
        try:
            task = TaskQueue(
                task_id=task_id,
                task_type=task_type,
                name=name,
                description=description,
                payload=payload or {},
                status=TaskStatus.QUEUED.value,
                priority=priority.value if isinstance(priority, TaskPriority) else priority,
                scheduled_at=scheduled_at,
                queued_at=datetime.now(),
                max_retries=max_retries,
                timeout_seconds=timeout_seconds
            )
            db.add(task)
            db.commit()

            logger.info(f"Queued task {task_id}: {name} (type={task_type})")
            return task_id

        except Exception as e:
            db.rollback()
            logger.error(f"Failed to queue task: {e}")
            raise
        finally:
            db.close()

    def get_task_status(self, task_id: str) -> Optional[Dict[str, Any]]:
        """Get task status and details."""
        db = SessionLocal()
        try:
            task = db.query(TaskQueue).filter(TaskQueue.task_id == task_id).first()
            if task:
                return task.to_dict()
            return None
        finally:
            db.close()

    def get_task_progress(self, task_id: str) -> Optional[Dict[str, Any]]:
        """Get task progress information."""
        db = SessionLocal()
        try:
            task = db.query(TaskQueue).filter(TaskQueue.task_id == task_id).first()
            if task:
                return {
                    "task_id": task.task_id,
                    "status": task.status,
                    "progress": task.progress,
                    "progress_message": task.progress_message,
                    "started_at": task.started_at.isoformat() if task.started_at else None,
                    "completed_at": task.completed_at.isoformat() if task.completed_at else None,
                }
            return None
        finally:
            db.close()

    def cancel_task(self, task_id: str) -> bool:
        """
        Cancel a pending or running task.

        Returns:
            True if task was cancelled
        """
        db = SessionLocal()
        try:
            task = db.query(TaskQueue).filter(TaskQueue.task_id == task_id).first()
            if not task:
                return False

            if task.status in [TaskStatus.COMPLETED.value, TaskStatus.FAILED.value, TaskStatus.CANCELLED.value]:
                return False

            task.status = TaskStatus.CANCELLED.value
            task.completed_at = datetime.now()
            db.commit()

            # Terminate subprocess if running in subprocess mode
            proc = self._active_processes.get(task_id)
            if proc and proc.poll() is None:
                logger.info(f"Terminating subprocess for cancelled task {task_id}")
                proc.terminate()

            logger.info(f"Cancelled task {task_id}")
            return True

        except Exception as e:
            db.rollback()
            logger.error(f"Failed to cancel task {task_id}: {e}")
            return False
        finally:
            db.close()

    def pause_task(self, task_id: str) -> bool:
        """
        Pause a running task.

        The task handler should check for pause status and save checkpoint.

        Returns:
            True if task was paused
        """
        db = SessionLocal()
        try:
            task = db.query(TaskQueue).filter(TaskQueue.task_id == task_id).first()
            if not task:
                logger.warning(f"Task {task_id} not found for pause")
                return False

            if task.status != TaskStatus.RUNNING.value:
                logger.warning(f"Task {task_id} is not running (status={task.status}), cannot pause")
                return False

            task.status = TaskStatus.PAUSED.value
            db.commit()

            logger.info(f"Paused task {task_id}")
            return True

        except Exception as e:
            db.rollback()
            logger.error(f"Failed to pause task {task_id}: {e}")
            return False
        finally:
            db.close()

    def resume_task(self, task_id: str) -> bool:
        """
        Resume a paused or stopped (crashed) task.

        The task will be re-queued and picked up by a worker,
        which should resume from the last checkpoint.

        Returns:
            True if task was resumed
        """
        db = SessionLocal()
        try:
            task = db.query(TaskQueue).filter(TaskQueue.task_id == task_id).first()
            if not task:
                logger.warning(f"Task {task_id} not found for resume")
                return False

            # Allow resuming paused or stopped (crashed) tasks
            resumable_statuses = [TaskStatus.PAUSED.value, TaskStatus.STOPPED.value]
            if task.status not in resumable_statuses:
                logger.warning(f"Task {task_id} is not resumable (status={task.status})")
                return False

            # Re-queue the task
            task.status = TaskStatus.QUEUED.value
            task.queued_at = datetime.now()
            db.commit()

            logger.info(f"Resumed task {task_id} - re-queued for processing")
            return True

        except Exception as e:
            db.rollback()
            logger.error(f"Failed to resume task {task_id}: {e}")
            return False
        finally:
            db.close()

    def is_task_paused(self, task_id: str) -> bool:
        """
        Check if a task is paused or pause requested.

        Task handlers should call this periodically to check if they should pause.

        Returns:
            True if task should pause
        """
        db = SessionLocal()
        try:
            task = db.query(TaskQueue).filter(TaskQueue.task_id == task_id).first()
            if task:
                return task.status == TaskStatus.PAUSED.value
            return False
        finally:
            db.close()

    def update_progress(self, task_id: str, progress: float, message: Optional[str] = None):
        """Update task progress."""
        db = SessionLocal()
        try:
            task = db.query(TaskQueue).filter(TaskQueue.task_id == task_id).first()
            if task:
                task.progress = min(100.0, max(0.0, progress))
                if message:
                    task.progress_message = message
                db.commit()
        except Exception as e:
            db.rollback()
            logger.error(f"Failed to update progress for {task_id}: {e}")
        finally:
            db.close()

    def list_tasks(
        self,
        status: Optional[str] = None,
        task_type: Optional[str] = None,
        limit: int = 100
    ) -> List[Dict[str, Any]]:
        """List tasks with optional filters."""
        db = SessionLocal()
        try:
            query = db.query(TaskQueue)

            if status:
                query = query.filter(TaskQueue.status == status)
            if task_type:
                query = query.filter(TaskQueue.task_type == task_type)

            query = query.order_by(TaskQueue.created_at.desc()).limit(limit)

            return [task.to_dict() for task in query.all()]
        finally:
            db.close()

    def get_queue_stats(self) -> Dict[str, Any]:
        """Get queue statistics."""
        db = SessionLocal()
        try:
            total = db.query(TaskQueue).count()
            pending = db.query(TaskQueue).filter(TaskQueue.status == TaskStatus.PENDING.value).count()
            queued = db.query(TaskQueue).filter(TaskQueue.status == TaskStatus.QUEUED.value).count()
            running = db.query(TaskQueue).filter(TaskQueue.status == TaskStatus.RUNNING.value).count()
            completed = db.query(TaskQueue).filter(TaskQueue.status == TaskStatus.COMPLETED.value).count()
            failed = db.query(TaskQueue).filter(TaskQueue.status == TaskStatus.FAILED.value).count()
            paused = db.query(TaskQueue).filter(TaskQueue.status == TaskStatus.PAUSED.value).count()

            return {
                "total": total,
                "pending": pending,
                "queued": queued,
                "running": running,
                "completed": completed,
                "failed": failed,
                "paused": paused,
                "workers": self.max_workers,
                "active_workers": len(self._active_tasks)
            }
        finally:
            db.close()

    def clear_completed_results(self, days: int = 1) -> int:
        """
        Clear the result column for completed/failed tasks older than N days.

        The result data is typically already persisted in domain tables (backtests,
        trained_models), so keeping it in the task queue is redundant and wastes
        significant space (can be hundreds of MB for backtests with equity curves).

        Returns:
            Number of tasks cleared
        """
        db = SessionLocal()
        try:
            cutoff = datetime.now() - timedelta(days=days)
            tasks = db.query(TaskQueue).filter(
                and_(
                    TaskQueue.status.in_([TaskStatus.COMPLETED.value, TaskStatus.FAILED.value, TaskStatus.CANCELLED.value]),
                    TaskQueue.completed_at < cutoff,
                    TaskQueue.result.isnot(None)
                )
            ).all()

            count = 0
            for task in tasks:
                task.result = None
                count += 1

            if count > 0:
                db.commit()
                logger.info(f"Cleared result data from {count} completed tasks (space reclaimed)")
            return count

        except Exception as e:
            db.rollback()
            logger.error(f"Failed to clear completed results: {e}")
            return 0
        finally:
            db.close()

    def cleanup_old_tasks(self, days: int = 30) -> int:
        """
        Remove completed/failed tasks older than specified days.

        Returns:
            Number of tasks removed
        """
        db = SessionLocal()
        try:
            cutoff = datetime.now() - timedelta(days=days)
            result = db.query(TaskQueue).filter(
                and_(
                    TaskQueue.status.in_([TaskStatus.COMPLETED.value, TaskStatus.FAILED.value, TaskStatus.CANCELLED.value]),
                    TaskQueue.completed_at < cutoff
                )
            ).delete(synchronize_session=False)
            db.commit()

            if result > 0:
                logger.info(f"Cleaned up {result} old tasks")
            return result

        except Exception as e:
            db.rollback()
            logger.error(f"Failed to cleanup old tasks: {e}")
            return 0
        finally:
            db.close()

    def recover_stuck_tasks(self):
        """
        On startup, mark any 'running' tasks as 'stopped' since no worker
        is actually processing them (workers died on restart).
        """
        db = SessionLocal()
        try:
            stuck = db.query(TaskQueue).filter(
                TaskQueue.status == TaskStatus.RUNNING.value
            ).all()
            for task in stuck:
                logger.warning(
                    f"Recovering stuck task {task.task_id} ({task.name}) - "
                    f"was running, marking as stopped"
                )
                task.status = TaskStatus.STOPPED.value
                task.error_message = "Task interrupted by server restart"
            if stuck:
                db.commit()
                logger.info(f"Recovered {len(stuck)} stuck tasks")
        except Exception as e:
            db.rollback()
            logger.error(f"Failed to recover stuck tasks: {e}")
        finally:
            db.close()

    def _worker_loop(self):
        """Worker thread main loop."""
        worker_name = threading.current_thread().name
        logger.debug(f"{worker_name} started")

        worker_idx = int(worker_name.rsplit("-", 1)[-1]) if worker_name[-1].isdigit() else 0
        while self._running:
            # Exit gracefully if this worker is above the current max_workers limit
            if worker_idx >= self.max_workers:
                logger.debug(f"{worker_name} exiting (excess worker, max_workers={self.max_workers})")
                break
            try:
                task = self._claim_next_task(worker_name)
                if task:
                    self._process_task(task, worker_name)
                else:
                    time.sleep(self.poll_interval)

            except Exception as e:
                logger.error(f"{worker_name} error: {e}")
                time.sleep(self.poll_interval)

        logger.debug(f"{worker_name} stopped")

    def _claim_next_task(self, worker_name: str) -> Optional[TaskQueue]:
        """Claim the next available task from the queue."""
        db = SessionLocal()
        try:
            with self._lock:
                # Find next queued task ordered by priority and queue time
                now = datetime.now()
                filters = [
                    TaskQueue.status == TaskStatus.QUEUED.value,
                    or_(
                        TaskQueue.scheduled_at.is_(None),
                        TaskQueue.scheduled_at <= now
                    )
                ]
                # Whitelist: only claim these task types
                if self.task_types:
                    filters.append(TaskQueue.task_type.in_(self.task_types))
                # Blacklist: never claim these task types (ignored when whitelist is set)
                elif self.exclude_task_types:
                    filters.append(TaskQueue.task_type.notin_(self.exclude_task_types))
                task = db.query(TaskQueue).filter(
                    and_(*filters)
                ).order_by(
                    TaskQueue.priority.desc(),
                    TaskQueue.queued_at.asc()
                ).first()

                if task:
                    # Claim the task
                    task.status = TaskStatus.RUNNING.value
                    task.started_at = datetime.now()
                    task.worker_name = worker_name
                    db.commit()
                    db.refresh(task)
                    return task

                return None

        except Exception as e:
            db.rollback()
            logger.error(f"Error claiming task: {e}")
            return None
        finally:
            db.close()

    def _process_task(self, task: TaskQueue, worker_name: str):
        """Process a claimed task."""
        task_id = task.task_id
        task_type = task.task_type

        logger.info(f"{worker_name} processing task {task_id}: {task.name}")

        # Track active task
        self._active_tasks[task_id] = threading.current_thread()

        if self.use_subprocess:
            self._process_task_subprocess(task, worker_name)
        else:
            self._process_task_inline(task, worker_name)

    def _process_task_subprocess(self, task: TaskQueue, worker_name: str):
        """Process a task by spawning a separate Python process.

        This avoids GIL contention — the worker thread sleeps while the
        subprocess does the heavy lifting in its own interpreter.
        The subprocess handles DB updates (progress, status) directly.
        """
        task_id = task.task_id

        # Find the worker script
        backend_dir = Path(__file__).resolve().parent.parent.parent
        script_name = self.worker_script or "training_worker.py"
        worker_script = backend_dir / script_name

        if not worker_script.exists():
            logger.error(f"Worker script not found: {worker_script}")
            self._fail_task(task_id, f"Worker script not found: {worker_script}")
            return

        # Spawn subprocess using the same Python interpreter
        cmd = [sys.executable, str(worker_script), task_id]
        logger.info(f"{worker_name} spawning subprocess for task {task_id}: {' '.join(cmd)}")

        try:
            # Redirect stdout/stderr to DEVNULL — all logging goes through the
            # logging module to files. Using PIPE would deadlock when the buffer
            # fills (subprocess blocks on write, parent never reads).
            proc = subprocess.Popen(
                cmd,
                cwd=str(backend_dir),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self._active_processes[task_id] = proc

            # Wait for the subprocess, sleeping to release GIL
            while proc.poll() is None:
                time.sleep(2.0)
                # Check if we should stop
                if not self._running:
                    logger.warning(f"Queue stopping, terminating subprocess for task {task_id}")
                    proc.terminate()
                    proc.wait(timeout=10)
                    break

            exit_code = proc.returncode

            if exit_code != 0:
                logger.error(f"Subprocess for task {task_id} exited with code {exit_code}")
                # Check if the subprocess already updated the status
                db = SessionLocal()
                try:
                    db_task = db.query(TaskQueue).filter(TaskQueue.task_id == task_id).first()
                    if db_task and db_task.status == 'running':
                        # Subprocess crashed without updating status
                        db_task.status = TaskStatus.FAILED.value
                        db_task.error_message = f"Worker process crashed (exit code {exit_code}): {stderr[-200:]}"
                        db_task.completed_at = datetime.now()
                        db.commit()
                finally:
                    db.close()
            else:
                logger.info(f"Subprocess for task {task_id} completed (exit code 0)")

        except Exception as e:
            logger.error(f"Failed to spawn/monitor subprocess for task {task_id}: {e}")
            self._fail_task(task_id, f"Subprocess error: {str(e)}")

        finally:
            self._active_processes.pop(task_id, None)
            self._active_tasks.pop(task_id, None)

    def _fail_task(self, task_id: str, error_message: str):
        """Mark a task as failed in the database."""
        db = SessionLocal()
        try:
            db_task = db.query(TaskQueue).filter(TaskQueue.task_id == task_id).first()
            if db_task:
                db_task.status = TaskStatus.FAILED.value
                db_task.error_message = error_message
                db_task.completed_at = datetime.now()
                db.commit()
        finally:
            db.close()

    def _process_task_inline(self, task: TaskQueue, worker_name: str):
        """Process a task inline in the current thread (original behavior)."""
        task_id = task.task_id
        task_type = task.task_type

        db = SessionLocal()
        try:
            # Get handler
            handler = self._handlers.get(task_type)
            if not handler:
                raise ValueError(f"No handler registered for task type: {task_type}")

            # Execute handler
            result = handler(task_id, task.payload or {})

            # Check if the result indicates failure
            result_status = result.get('status', 'completed') if isinstance(result, dict) else 'completed'

            db_task = db.query(TaskQueue).filter(TaskQueue.task_id == task_id).first()
            if db_task:
                db_task.result = result
                db_task.completed_at = datetime.now()
                db_task.progress = 100.0

                if result_status == 'failed':
                    # Handler returned a failure status - mark task as failed
                    db_task.status = TaskStatus.FAILED.value
                    error_msg = result.get('error', 'Task handler returned failed status')
                    db_task.error_message = error_msg
                    logger.warning(f"Task {task_id} marked as failed: {error_msg}")
                elif result_status == 'partial':
                    # Partial success - still mark as completed but with warning
                    db_task.status = TaskStatus.COMPLETED.value
                    logger.warning(f"Task {task_id} partially completed")
                else:
                    # Success
                    db_task.status = TaskStatus.COMPLETED.value
                    logger.info(f"Task {task_id} completed successfully")

                db.commit()

        except Exception as e:
            logger.error(f"Task {task_id} failed: {e}")

            # Handle failure
            db_task = db.query(TaskQueue).filter(TaskQueue.task_id == task_id).first()
            if db_task:
                db_task.error_message = str(e)

                # Check for retry
                if db_task.retry_count < db_task.max_retries:
                    db_task.retry_count += 1
                    db_task.status = TaskStatus.QUEUED.value
                    db_task.scheduled_at = datetime.now() + timedelta(seconds=db_task.retry_delay_seconds)
                    logger.info(f"Task {task_id} scheduled for retry {db_task.retry_count}/{db_task.max_retries}")
                else:
                    db_task.status = TaskStatus.FAILED.value
                    db_task.completed_at = datetime.now()

                db.commit()

        finally:
            db.close()
            # Remove from active tasks
            self._active_tasks.pop(task_id, None)


# Global task queue instance
_task_queue: Optional[TaskQueueService] = None


def get_task_queue() -> TaskQueueService:
    """Get the global task queue instance."""
    global _task_queue
    if _task_queue is None:
        _task_queue = TaskQueueService()
    return _task_queue


def init_task_queue(max_workers: int = 2, exclude_task_types: Optional[List[str]] = None):
    """Initialize and start the task queue."""
    import os
    global _task_queue
    _task_queue = TaskQueueService(max_workers=max_workers, exclude_task_types=exclude_task_types, name="MainTaskQueue")
    # Skip starting workers in test mode to avoid race conditions with table creation
    if os.getenv('PYTEST_CURRENT_TEST') is None:
        _task_queue.recover_stuck_tasks()
        _task_queue.start()
    else:
        logger.info("Test mode detected - skipping task queue worker startup")
    return _task_queue


# Dedicated training task queue — limited to 2 workers to avoid saturating GPU.
_training_task_queue: Optional[TaskQueueService] = None


def get_training_task_queue() -> TaskQueueService:
    """Get the dedicated training task queue instance."""
    global _training_task_queue
    if _training_task_queue is None:
        _training_task_queue = TaskQueueService(
            max_workers=2,
            task_types=['training_job'],
            name="TrainingTaskQueue"
        )
    return _training_task_queue


def init_training_task_queue(max_workers: int = 2):
    """Initialize and start the dedicated training task queue.

    Uses subprocess mode to run training jobs in separate Python processes,
    avoiding GIL contention that would block the API event loop during
    CPU/GPU-intensive training.
    """
    import os
    global _training_task_queue
    _training_task_queue = TaskQueueService(
        max_workers=max_workers,
        task_types=['training_job'],
        name="TrainingTaskQueue",
        use_subprocess=True,
        worker_script="training_worker.py",
    )
    if os.getenv('PYTEST_CURRENT_TEST') is None:
        _training_task_queue.start()
    else:
        logger.info("Test mode detected - skipping training task queue worker startup")
    return _training_task_queue


# Dedicated backtest task queue — subprocess mode to avoid GIL contention.
_backtest_task_queue: Optional[TaskQueueService] = None


def get_backtest_task_queue() -> TaskQueueService:
    """Get the dedicated backtest task queue instance."""
    global _backtest_task_queue
    if _backtest_task_queue is None:
        _backtest_task_queue = TaskQueueService(
            max_workers=2,
            task_types=['backtest'],
            name="BacktestTaskQueue",
            use_subprocess=True,
            worker_script="backtest_worker.py",
        )
    return _backtest_task_queue


def init_backtest_task_queue(max_workers: int = 2):
    """Initialize and start the dedicated backtest task queue.

    Uses subprocess mode so CPU-intensive backtests don't block the API.
    """
    import os
    global _backtest_task_queue
    _backtest_task_queue = TaskQueueService(
        max_workers=max_workers,
        task_types=['backtest'],
        name="BacktestTaskQueue",
        use_subprocess=True,
        worker_script="backtest_worker.py",
    )
    if os.getenv('PYTEST_CURRENT_TEST') is None:
        _backtest_task_queue.start()
    else:
        logger.info("Test mode detected - skipping backtest task queue worker startup")
    return _backtest_task_queue


# Dedicated OHLCV task queue — isolated so it can be resized without affecting
# training jobs, backtests, or other task types.
_ohlcv_task_queue: Optional[TaskQueueService] = None


def get_ohlcv_task_queue() -> TaskQueueService:
    """Get the dedicated OHLCV task queue instance."""
    global _ohlcv_task_queue
    if _ohlcv_task_queue is None:
        _ohlcv_task_queue = TaskQueueService(
            max_workers=3,
            task_types=['ohlcv_cache_fetch'],
            name="OHLCVTaskQueue"
        )
    return _ohlcv_task_queue


def init_ohlcv_task_queue(max_workers: int = 3):
    """Initialize and start the dedicated OHLCV task queue."""
    import os
    global _ohlcv_task_queue
    _ohlcv_task_queue = TaskQueueService(
        max_workers=max_workers,
        task_types=['ohlcv_cache_fetch'],
        name="OHLCVTaskQueue"
    )
    if os.getenv('PYTEST_CURRENT_TEST') is None:
        _ohlcv_task_queue.start()
    else:
        logger.info("Test mode detected - skipping OHLCV task queue worker startup")
    return _ohlcv_task_queue
