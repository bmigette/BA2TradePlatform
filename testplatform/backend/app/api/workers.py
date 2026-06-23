"""
Workers API endpoints.

Manages training and inference workers for distributed job processing.
"""

import logging
import platform
import psutil
from datetime import datetime
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.models import get_db, Worker

logger = logging.getLogger(__name__)

router = APIRouter()


# Pydantic models for request/response
class WorkerCapabilities(BaseModel):
    train: bool = True
    infer: bool = True


class WorkerCreate(BaseModel):
    name: str
    url: str
    description: Optional[str] = None
    workerType: str = "remote"
    capabilities: WorkerCapabilities = WorkerCapabilities()
    password: Optional[str] = None  # write-only; the master sends it to authenticate to the worker


class WorkerUpdate(BaseModel):
    name: Optional[str] = None
    url: Optional[str] = None
    description: Optional[str] = None
    capabilities: Optional[WorkerCapabilities] = None
    isEnabled: Optional[bool] = None
    password: Optional[str] = None  # write-only; omit to leave unchanged


class GpuInfo(BaseModel):
    name: str
    memory: int  # MB
    count: int


class CpuInfo(BaseModel):
    cores: int
    model: str


class WorkerResponse(BaseModel):
    id: int
    name: str
    url: str
    description: Optional[str]
    workerType: str
    capabilities: dict
    hasPassword: bool = False
    isEnabled: bool
    isLocal: bool
    status: str
    gpuInfo: Optional[dict]
    cpuInfo: Optional[dict]
    lastHeartbeat: Optional[str]
    activeJobsCount: int
    totalJobsCompleted: int
    capacity: Optional[int] = None  # remote: trial-slot count from live /health (None if offline)
    createdAt: Optional[str]
    updatedAt: Optional[str]

    class Config:
        from_attributes = True


class WorkerExport(BaseModel):
    workers: List[dict]
    exportedAt: str


def get_local_hardware_info():
    """Get hardware info for the local worker."""
    cpu_info = {
        "cores": psutil.cpu_count(logical=True),
        "model": platform.processor() or "Unknown"
    }

    # Try to get GPU info
    gpu_info = None
    try:
        import torch
        if torch.cuda.is_available():
            gpu_count = torch.cuda.device_count()
            if gpu_count > 0:
                gpu_name = torch.cuda.get_device_name(0)
                gpu_memory = torch.cuda.get_device_properties(0).total_memory // (1024 * 1024)
                gpu_info = {
                    "name": gpu_name,
                    "memory": gpu_memory,
                    "count": gpu_count
                }
    except ImportError:
        pass

    return cpu_info, gpu_info


def _worker_dict(worker: Worker) -> dict:
    """Plain dict the master-side ``worker_client`` uses (carries the per-worker password)."""
    return {"id": worker.id, "name": worker.name, "url": worker.url, "password": worker.password}


def ensure_local_worker(db: Session):
    """Ensure the local worker exists in the database."""
    local_worker = db.query(Worker).filter(Worker.is_local == True).first()

    if not local_worker:
        cpu_info, gpu_info = get_local_hardware_info()
        local_worker = Worker(
            name="Local Worker",
            url="local",
            description="Worker running on the backend host",
            worker_type="local",
            capabilities={"train": True, "infer": True},
            is_enabled=True,
            is_local=True,
            status="online",
            cpu_info=cpu_info,
            gpu_info=gpu_info,
            last_heartbeat=datetime.utcnow()
        )
        db.add(local_worker)
        db.commit()
        db.refresh(local_worker)
        logger.info("Created local worker")

    return local_worker


@router.get("", response_model=List[WorkerResponse])
async def list_workers(db: Session = Depends(get_db)):
    """List all configured workers."""
    # Ensure local worker exists
    ensure_local_worker(db)

    # Live-probe remote workers so the status badge is accurate (the CLI/distributed path never
    # writes status back to the DB) and surface each worker's true slot capacity.
    from app.services.worker_fleet import refresh_remote_status
    caps = refresh_remote_status(db)

    workers = db.query(Worker).order_by(Worker.is_local.desc(), Worker.name).all()
    out = []
    for w in workers:
        d = w.to_dict()
        cores = w.cpu_info.get("cores") if isinstance(w.cpu_info, dict) else None
        d["capacity"] = cores if w.is_local else caps.get(w.id)
        out.append(WorkerResponse(**d))
    return out


@router.post("", response_model=WorkerResponse, status_code=201)
async def create_worker(worker: WorkerCreate, db: Session = Depends(get_db)):
    """Add a new remote worker configuration."""
    if worker.workerType == "local":
        raise HTTPException(status_code=400, detail="Cannot create additional local workers")

    db_worker = Worker(
        name=worker.name,
        url=worker.url,
        description=worker.description,
        worker_type=worker.workerType,
        capabilities=worker.capabilities.dict(),
        password=worker.password,
        is_enabled=True,
        is_local=False,
        status="offline"
    )

    db.add(db_worker)
    db.commit()
    db.refresh(db_worker)

    logger.info(f"Created worker: {db_worker.name}")
    return WorkerResponse(**db_worker.to_dict())


@router.get("/{worker_id}", response_model=WorkerResponse)
async def get_worker(worker_id: int, db: Session = Depends(get_db)):
    """Get worker details."""
    worker = db.query(Worker).filter(Worker.id == worker_id).first()
    if not worker:
        raise HTTPException(status_code=404, detail="Worker not found")
    return WorkerResponse(**worker.to_dict())


@router.put("/{worker_id}", response_model=WorkerResponse)
async def update_worker(worker_id: int, updates: WorkerUpdate, db: Session = Depends(get_db)):
    """Update worker configuration."""
    worker = db.query(Worker).filter(Worker.id == worker_id).first()
    if not worker:
        raise HTTPException(status_code=404, detail="Worker not found")

    if updates.name is not None:
        worker.name = updates.name
    if updates.url is not None:
        if worker.is_local:
            raise HTTPException(status_code=400, detail="Cannot change URL of local worker")
        worker.url = updates.url
    if updates.description is not None:
        worker.description = updates.description
    if updates.capabilities is not None:
        worker.capabilities = updates.capabilities.dict()
    if updates.isEnabled is not None:
        worker.is_enabled = updates.isEnabled
    if updates.password is not None:
        # Empty string clears the password; any other value sets it.
        worker.password = updates.password or None

    db.commit()
    db.refresh(worker)

    logger.info(f"Updated worker: {worker.name}")
    return WorkerResponse(**worker.to_dict())


@router.delete("/{worker_id}")
async def delete_worker(worker_id: int, db: Session = Depends(get_db)):
    """Delete a worker configuration."""
    worker = db.query(Worker).filter(Worker.id == worker_id).first()
    if not worker:
        raise HTTPException(status_code=404, detail="Worker not found")

    if worker.is_local:
        raise HTTPException(status_code=400, detail="Cannot delete the local worker")

    db.delete(worker)
    db.commit()

    logger.info(f"Deleted worker: {worker.name}")
    return {"message": "Worker deleted"}


@router.get("/{worker_id}/status")
async def get_worker_status(worker_id: int, db: Session = Depends(get_db)):
    """Get worker status and metrics."""
    worker = db.query(Worker).filter(Worker.id == worker_id).first()
    if not worker:
        raise HTTPException(status_code=404, detail="Worker not found")

    # For local worker, get real-time metrics
    if worker.is_local:
        cpu_percent = psutil.cpu_percent()
        memory = psutil.virtual_memory()
        gpu_utilization = None
        gpu_memory_used = None
        gpu_memory_total = None

        # Try pynvml for accurate NVIDIA GPU stats
        try:
            import pynvml
            pynvml.nvmlInit()
            device_count = pynvml.nvmlDeviceGetCount()
            if device_count > 0:
                handle = pynvml.nvmlDeviceGetHandleByIndex(0)
                util = pynvml.nvmlDeviceGetUtilizationRates(handle)
                gpu_utilization = util.gpu
                mem_info = pynvml.nvmlDeviceGetMemoryInfo(handle)
                gpu_memory_used = mem_info.used // (1024 * 1024)
                gpu_memory_total = mem_info.total // (1024 * 1024)
            pynvml.nvmlShutdown()
        except Exception:
            # Fallback to torch for basic detection
            try:
                import torch
                if torch.cuda.is_available():
                    gpu_utilization = 0  # Can't get real utilization without pynvml
            except ImportError:
                pass

        return {
            "id": worker.id,
            "status": "online",
            "cpuUtilization": cpu_percent,
            "memoryUsed": memory.used // (1024 * 1024),
            "memoryTotal": memory.total // (1024 * 1024),
            "gpuUtilization": gpu_utilization,
            "gpuMemoryUsed": gpu_memory_used,
            "gpuMemoryTotal": gpu_memory_total,
            "activeJobs": worker.active_jobs_count,
            "lastHeartbeat": datetime.utcnow().isoformat()
        }

    # For remote workers, return stored status
    return {
        "id": worker.id,
        "status": worker.status,
        "activeJobs": worker.active_jobs_count,
        "lastHeartbeat": worker.last_heartbeat.isoformat() if worker.last_heartbeat else None
    }


@router.post("/{worker_id}/enable")
async def enable_worker(worker_id: int, db: Session = Depends(get_db)):
    """Enable a worker."""
    worker = db.query(Worker).filter(Worker.id == worker_id).first()
    if not worker:
        raise HTTPException(status_code=404, detail="Worker not found")

    worker.is_enabled = True
    db.commit()

    logger.info(f"Enabled worker: {worker.name}")
    return {"message": "Worker enabled"}


@router.post("/{worker_id}/disable")
async def disable_worker(worker_id: int, db: Session = Depends(get_db)):
    """Disable a worker."""
    worker = db.query(Worker).filter(Worker.id == worker_id).first()
    if not worker:
        raise HTTPException(status_code=404, detail="Worker not found")

    worker.is_enabled = False
    db.commit()

    logger.info(f"Disabled worker: {worker.name}")
    return {"message": "Worker disabled"}


@router.post("/{worker_id}/health-check")
async def health_check_worker(worker_id: int, db: Session = Depends(get_db)):
    """Run health check on a worker."""
    worker = db.query(Worker).filter(Worker.id == worker_id).first()
    if not worker:
        raise HTTPException(status_code=404, detail="Worker not found")

    if worker.is_local:
        # Local worker is always online if we can respond
        worker.status = "online"
        worker.last_heartbeat = datetime.utcnow()
        cpu_info, gpu_info = get_local_hardware_info()
        worker.cpu_info = cpu_info
        worker.gpu_info = gpu_info
        db.commit()

        return {
            "status": "online",
            "message": "Local worker is healthy",
            "gpuInfo": gpu_info,
            "cpuInfo": cpu_info
        }

    # For remote workers, call the worker's /health with its password (push-model worker server).
    from app.services import worker_client
    try:
        data = worker_client.health(_worker_dict(worker), timeout=5.0)
        worker.status = "online"
        worker.last_heartbeat = datetime.utcnow()
        # The worker reports {cpu, gpu, capacity, version}; mirror hardware onto the row.
        if data.get("cpu"):
            worker.cpu_info = data["cpu"]
        if data.get("gpu"):
            worker.gpu_info = data["gpu"]
        db.commit()
        return {"status": "online", "message": "Worker is healthy", **data}
    except Exception as e:
        worker.status = "offline"
        db.commit()
        return {"status": "offline", "message": str(e)}


@router.post("/export", response_model=WorkerExport)
async def export_workers(db: Session = Depends(get_db)):
    """Export all worker configurations."""
    workers = db.query(Worker).filter(Worker.is_local == False).all()

    export_data = []
    for w in workers:
        export_data.append({
            "name": w.name,
            "url": w.url,
            "description": w.description,
            "workerType": w.worker_type,
            "capabilities": w.capabilities,
            "isEnabled": w.is_enabled
        })

    return WorkerExport(
        workers=export_data,
        exportedAt=datetime.utcnow().isoformat()
    )


@router.post("/import")
async def import_workers(data: WorkerExport, db: Session = Depends(get_db)):
    """Import worker configurations."""
    imported = 0

    for worker_data in data.workers:
        # Check if worker with same URL already exists
        existing = db.query(Worker).filter(Worker.url == worker_data["url"]).first()
        if existing:
            continue

        new_worker = Worker(
            name=worker_data["name"],
            url=worker_data["url"],
            description=worker_data.get("description"),
            worker_type=worker_data.get("workerType", "remote"),
            capabilities=worker_data.get("capabilities", {"train": True, "infer": True}),
            is_enabled=worker_data.get("isEnabled", True),
            is_local=False,
            status="offline"
        )
        db.add(new_worker)
        imported += 1

    db.commit()
    logger.info(f"Imported {imported} workers")

    return {"message": f"Imported {imported} workers", "count": imported}


@router.post("/{worker_id}/sync-cache")
async def sync_worker_cache(worker_id: int, db: Session = Depends(get_db)):
    """Push the master's cache (diff, as one tar stream) to a remote worker."""
    worker = db.query(Worker).filter(Worker.id == worker_id).first()
    if not worker:
        raise HTTPException(status_code=404, detail="Worker not found")
    if worker.is_local:
        return {"status": "skipped", "message": "Local worker shares the master cache"}
    from app.services import worker_client
    try:
        res = worker_client.push_cache(_worker_dict(worker))
        return {"status": "ok", **res}
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"cache push failed: {e}")


@router.post("/{worker_id}/update")
async def update_worker_code(worker_id: int, db: Session = Depends(get_db)):
    """Bring a remote worker's code up to the master's version (git pull + reinstall + restart)."""
    worker = db.query(Worker).filter(Worker.id == worker_id).first()
    if not worker:
        raise HTTPException(status_code=404, detail="Worker not found")
    if worker.is_local:
        return {"status": "skipped", "message": "Local worker updates with the master"}
    from app.services import worker_client, self_update
    master_version = self_update.get_version_info().get("app_version")
    ok = worker_client.ensure_synced(_worker_dict(worker), master_version, max_wait=180.0)
    return {"status": "ok" if ok else "failed", "synced": ok, "masterVersion": master_version}
