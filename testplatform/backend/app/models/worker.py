"""
Worker model for training and inference workers.

Workers are processes that train and infer models. The backend host can act
as a local worker, and remote workers can be configured via the API.
"""

from sqlalchemy import Column, Integer, String, Boolean, Text, DateTime, JSON
from sqlalchemy.sql import func
from .database import Base


class Worker(Base):
    """Worker configuration and status."""

    __tablename__ = "workers"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), nullable=False)
    url = Column(String(500), nullable=False)  # URL for remote workers, "local" for local worker
    description = Column(Text, nullable=True)

    # Worker type and capabilities
    worker_type = Column(String(20), default="remote")  # "local" or "remote"
    capabilities = Column(JSON, default={"train": True, "infer": True})

    # Per-worker auth password — the master sends it (bearer) to authenticate to this worker's
    # HTTP server. Write-only via the API: to_dict() exposes only ``hasPassword``, never the value.
    password = Column(String(255), nullable=True)

    # Status
    is_enabled = Column(Boolean, default=True)
    is_local = Column(Boolean, default=False)
    status = Column(String(20), default="offline")  # "online", "offline", "busy"

    # Hardware info (populated by worker heartbeat)
    gpu_info = Column(JSON, nullable=True)  # {name, memory, count}
    cpu_info = Column(JSON, nullable=True)  # {cores, model}

    # Metrics
    last_heartbeat = Column(DateTime, nullable=True)
    active_jobs_count = Column(Integer, default=0)
    total_jobs_completed = Column(Integer, default=0)

    # Timestamps
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    def to_dict(self):
        """Convert worker to dictionary."""
        return {
            "id": self.id,
            "name": self.name,
            "url": self.url,
            "description": self.description,
            "workerType": self.worker_type,
            "capabilities": self.capabilities,
            "hasPassword": bool(self.password),
            "isEnabled": self.is_enabled,
            "isLocal": self.is_local,
            "status": self.status,
            "gpuInfo": self.gpu_info,
            "cpuInfo": self.cpu_info,
            "lastHeartbeat": self.last_heartbeat.isoformat() if self.last_heartbeat else None,
            "activeJobsCount": self.active_jobs_count,
            "totalJobsCompleted": self.total_jobs_completed,
            "createdAt": self.created_at.isoformat() if self.created_at else None,
            "updatedAt": self.updated_at.isoformat() if self.updated_at else None
        }
