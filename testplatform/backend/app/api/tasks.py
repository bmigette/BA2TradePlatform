"""
Task Queue API endpoints.

Provides REST API for managing background tasks using the database-backed queue.
"""

import logging
from typing import Optional, List
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from datetime import datetime

from app.services.task_queue import get_task_queue, TaskQueueService
from app.models.task_queue import TaskStatus, TaskPriority

logger = logging.getLogger(__name__)

router = APIRouter()


class TaskCreateRequest(BaseModel):
    """Request model for creating a task."""
    task_type: str
    name: str
    description: Optional[str] = None
    payload: Optional[dict] = None
    priority: Optional[int] = TaskPriority.NORMAL.value
    scheduled_at: Optional[datetime] = None
    max_retries: Optional[int] = 3
    timeout_seconds: Optional[int] = 3600


class TaskResponse(BaseModel):
    """Response model for a task."""
    task_id: str
    task_type: str
    name: str
    description: Optional[str]
    status: str
    priority: int
    progress: float
    progress_message: Optional[str]
    result: Optional[dict]
    error_message: Optional[str]
    retry_count: int
    max_retries: int
    created_at: Optional[str]
    queued_at: Optional[str]
    started_at: Optional[str]
    completed_at: Optional[str]


class QueueStatsResponse(BaseModel):
    """Response model for queue statistics."""
    total: int
    pending: int
    queued: int
    running: int
    completed: int
    failed: int
    paused: int = 0
    workers: int
    active_workers: int


@router.post("", response_model=dict)
async def create_task(request: TaskCreateRequest):
    """
    Create and queue a new task.

    Args:
        request: Task creation parameters

    Returns:
        Task ID and status
    """
    try:
        queue = get_task_queue()
        task_id = queue.queue_task(
            task_type=request.task_type,
            name=request.name,
            description=request.description,
            payload=request.payload,
            priority=TaskPriority(request.priority) if request.priority else TaskPriority.NORMAL,
            scheduled_at=request.scheduled_at,
            max_retries=request.max_retries or 3,
            timeout_seconds=request.timeout_seconds or 3600
        )

        return {
            "task_id": task_id,
            "status": "queued",
            "message": f"Task '{request.name}' queued successfully"
        }

    except Exception as e:
        logger.error(f"Failed to create task: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{task_id}")
async def get_task(task_id: str):
    """
    Get task details by ID.

    Args:
        task_id: Task identifier

    Returns:
        Task details
    """
    queue = get_task_queue()
    task = queue.get_task_status(task_id)

    if not task:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

    return task


@router.get("/{task_id}/progress")
async def get_task_progress(task_id: str):
    """
    Get task progress.

    Args:
        task_id: Task identifier

    Returns:
        Progress information
    """
    queue = get_task_queue()
    progress = queue.get_task_progress(task_id)

    if not progress:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

    return progress


@router.post("/{task_id}/cancel")
async def cancel_task(task_id: str):
    """
    Cancel a pending or running task.

    Args:
        task_id: Task identifier

    Returns:
        Cancellation status
    """
    queue = get_task_queue()
    success = queue.cancel_task(task_id)

    if not success:
        raise HTTPException(
            status_code=400,
            detail=f"Task {task_id} cannot be cancelled (not found or already completed)"
        )

    return {
        "task_id": task_id,
        "status": "cancelled",
        "message": "Task cancelled successfully"
    }


@router.post("/{task_id}/pause")
async def pause_task(task_id: str):
    """
    Pause a running task.

    The task handler will save a checkpoint and stop processing.
    Use resume to continue from the checkpoint.

    Args:
        task_id: Task identifier

    Returns:
        Pause status
    """
    queue = get_task_queue()
    success = queue.pause_task(task_id)

    if not success:
        raise HTTPException(
            status_code=400,
            detail=f"Task {task_id} cannot be paused (not found or not running)"
        )

    return {
        "task_id": task_id,
        "status": "paused",
        "message": "Task paused successfully. A checkpoint has been saved."
    }


@router.post("/{task_id}/resume")
async def resume_task(task_id: str):
    """
    Resume a paused task.

    The task will be re-queued and continue from the last checkpoint.

    Args:
        task_id: Task identifier

    Returns:
        Resume status
    """
    queue = get_task_queue()
    success = queue.resume_task(task_id)

    if not success:
        raise HTTPException(
            status_code=400,
            detail=f"Task {task_id} cannot be resumed (not found or not paused)"
        )

    return {
        "task_id": task_id,
        "status": "queued",
        "message": "Task resumed and re-queued for processing"
    }


@router.get("/{task_id}/checkpoints")
async def list_task_checkpoints(task_id: str):
    """
    List all checkpoints for a task.

    Args:
        task_id: Task identifier

    Returns:
        List of checkpoints
    """
    from app.services.training_checkpoint import get_checkpoint_service

    checkpoint_service = get_checkpoint_service()
    checkpoints = checkpoint_service.list_checkpoints(task_id)

    return {
        "task_id": task_id,
        "checkpoints": checkpoints,
        "count": len(checkpoints)
    }


@router.get("/{task_id}/checkpoints/latest")
async def get_latest_checkpoint(task_id: str):
    """
    Get the latest checkpoint for a task.

    Args:
        task_id: Task identifier

    Returns:
        Latest checkpoint info
    """
    from app.services.training_checkpoint import get_checkpoint_service

    checkpoint_service = get_checkpoint_service()
    checkpoint = checkpoint_service.get_latest_checkpoint_info(task_id)

    if not checkpoint:
        raise HTTPException(
            status_code=404,
            detail=f"No checkpoint found for task {task_id}"
        )

    return checkpoint


@router.delete("/{task_id}/checkpoints/{epoch}")
async def delete_checkpoint(task_id: str, epoch: int):
    """
    Delete a specific checkpoint.

    Args:
        task_id: Task identifier
        epoch: Epoch number of checkpoint to delete

    Returns:
        Deletion status
    """
    from app.services.training_checkpoint import get_checkpoint_service

    checkpoint_service = get_checkpoint_service()
    success = checkpoint_service.delete_checkpoint(task_id, epoch)

    if not success:
        raise HTTPException(
            status_code=404,
            detail=f"Checkpoint not found for task {task_id} at epoch {epoch}"
        )

    return {
        "task_id": task_id,
        "epoch": epoch,
        "message": "Checkpoint deleted successfully"
    }


@router.post("/{task_id}/checkpoints/cleanup")
async def cleanup_checkpoints(
    task_id: str,
    keep_latest: bool = Query(True, description="Keep the latest checkpoint"),
    keep_best: bool = Query(True, description="Keep the best checkpoint")
):
    """
    Clean up old checkpoints for a task, keeping only essential ones.

    Args:
        task_id: Task identifier
        keep_latest: Whether to keep the latest checkpoint
        keep_best: Whether to keep the best checkpoint

    Returns:
        Cleanup result
    """
    from app.services.training_checkpoint import get_checkpoint_service

    checkpoint_service = get_checkpoint_service()
    deleted = checkpoint_service.cleanup_task_checkpoints(
        task_id,
        keep_latest=keep_latest,
        keep_best=keep_best
    )

    return {
        "task_id": task_id,
        "deleted": deleted,
        "message": f"Deleted {deleted} checkpoints"
    }


@router.get("")
async def list_tasks(
    status: Optional[str] = Query(None, description="Filter by status"),
    task_type: Optional[str] = Query(None, description="Filter by task type"),
    limit: int = Query(100, ge=1, le=1000, description="Maximum tasks to return")
):
    """
    List tasks with optional filters.

    Args:
        status: Filter by status (pending, queued, running, completed, failed)
        task_type: Filter by task type
        limit: Maximum number of tasks to return

    Returns:
        List of tasks
    """
    queue = get_task_queue()
    tasks = queue.list_tasks(status=status, task_type=task_type, limit=limit)

    return {
        "tasks": tasks,
        "count": len(tasks)
    }


@router.get("/stats/summary", response_model=QueueStatsResponse)
async def get_queue_stats():
    """
    Get task queue statistics.

    Returns:
        Queue statistics including counts by status
    """
    queue = get_task_queue()
    stats = queue.get_queue_stats()
    return QueueStatsResponse(**stats)


@router.post("/cleanup")
async def cleanup_old_tasks(days: int = Query(30, ge=1, le=365)):
    """
    Remove old completed/failed tasks.

    Args:
        days: Remove tasks older than this many days

    Returns:
        Cleanup result
    """
    queue = get_task_queue()
    removed = queue.cleanup_old_tasks(days=days)

    return {
        "removed": removed,
        "message": f"Removed {removed} tasks older than {days} days"
    }


# Health check endpoint for the queue
@router.get("/health/status")
async def queue_health():
    """
    Check task queue health.

    Returns:
        Queue health status
    """
    try:
        queue = get_task_queue()
        stats = queue.get_queue_stats()

        return {
            "status": "healthy",
            "workers": stats["workers"],
            "active_workers": stats["active_workers"],
            "queued_tasks": stats["queued"],
            "running_tasks": stats["running"]
        }

    except Exception as e:
        return {
            "status": "unhealthy",
            "error": str(e)
        }
