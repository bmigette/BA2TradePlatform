"""
Database-backed Task Queue Model

Provides a simple task queue using the database instead of Redis/Celery.
Tasks are stored in SQLite/PostgreSQL and processed by background threads.
"""

from sqlalchemy import Column, Integer, String, Text, DateTime, JSON, Enum, Float
from sqlalchemy.sql import func
from datetime import datetime
import enum

from .database import Base


class TaskStatus(str, enum.Enum):
    """Task status enumeration."""
    PENDING = "pending"
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    PAUSED = "paused"
    STOPPED = "stopped"  # Crashed/interrupted, can be resumed


class TaskPriority(int, enum.Enum):
    """Task priority levels."""
    LOW = 1
    NORMAL = 5
    HIGH = 10
    CRITICAL = 100


class TaskQueue(Base):
    """
    Database model for task queue.

    Stores tasks that need to be processed asynchronously.
    Replaces the need for Celery/Redis with a simple database-backed queue.
    """
    __tablename__ = "task_queue"

    id = Column(Integer, primary_key=True, index=True)

    # Task identification
    task_id = Column(String(50), unique=True, index=True, nullable=False)
    task_type = Column(String(50), nullable=False, index=True)  # e.g., 'training', 'backtest', 'data_fetch'

    # Task configuration
    name = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    payload = Column(JSON, nullable=True)  # Task-specific parameters

    # Status and priority
    status = Column(String(20), default=TaskStatus.PENDING.value, index=True)
    priority = Column(Integer, default=TaskPriority.NORMAL.value, index=True)

    # Progress tracking
    progress = Column(Float, default=0.0)  # 0-100
    progress_message = Column(String(500), nullable=True)

    # Results
    result = Column(JSON, nullable=True)  # Task output/result
    error_message = Column(Text, nullable=True)

    # Checkpoint for resumability
    checkpoint_data = Column(JSON, nullable=True)  # GA state for crash recovery

    # Worker assignment
    worker_id = Column(Integer, nullable=True, index=True)
    worker_name = Column(String(100), nullable=True)

    # Retry configuration
    max_retries = Column(Integer, default=3)
    retry_count = Column(Integer, default=0)
    retry_delay_seconds = Column(Integer, default=60)

    # Timestamps
    created_at = Column(DateTime, default=func.now(), nullable=False)
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())
    queued_at = Column(DateTime, nullable=True)
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)

    # Scheduling
    scheduled_at = Column(DateTime, nullable=True)  # For delayed execution
    timeout_seconds = Column(Integer, default=3600)  # 1 hour default timeout

    def __repr__(self):
        return f"<TaskQueue(id={self.id}, task_id={self.task_id}, type={self.task_type}, status={self.status})>"

    def to_dict(self):
        """Convert to dictionary."""
        return {
            "id": self.id,
            "task_id": self.task_id,
            "task_type": self.task_type,
            "name": self.name,
            "description": self.description,
            "payload": self.payload,
            "status": self.status,
            "priority": self.priority,
            "progress": self.progress,
            "progress_message": self.progress_message,
            "result": self.result,
            "error_message": self.error_message,
            "worker_id": self.worker_id,
            "worker_name": self.worker_name,
            "retry_count": self.retry_count,
            "max_retries": self.max_retries,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "queued_at": self.queued_at.isoformat() if self.queued_at else None,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "checkpoint_data": self.checkpoint_data,
        }
