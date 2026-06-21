"""Distributed-worker runtime API (master side) — fan GA trials out to remote workers.

A remote worker (``ba2-test worker``) drives this:
  ``POST /api/worker/register``     -> upsert its Worker row, return ``{worker_id, version}``
  ``POST /api/worker/heartbeat``    -> liveness + active-job count, return master ``version``
  ``POST /api/worker/claim-trial``  -> atomically claim one pending trial (or 204 No Content)
  ``POST /api/worker/trial-result`` -> return a finished trial's fitness to the broker

The ``version`` in register/heartbeat lets the worker detect a code mismatch and self-update
(distributed trials MUST run identical code). Claim/result are pure broker ops (no DB) so the
hot path stays fast. All endpoints share the worker token.
"""

import logging
from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, Depends, Header, Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.models import Worker, get_db
from app.services import self_update
from app.services.trial_broker import get_broker
from app.services.worker_auth import verify_worker_token

logger = logging.getLogger(__name__)

router = APIRouter()


class RegisterReq(BaseModel):
    name: str
    url: Optional[str] = None
    capabilities: Optional[dict] = None
    cpu_info: Optional[dict] = None
    gpu_info: Optional[dict] = None


class HeartbeatReq(BaseModel):
    worker_id: int
    active_jobs: int = 0


class TrialResultReq(BaseModel):
    trial_id: str
    ok: bool
    fitness: float = 0.0
    trades: int = 0
    error: Optional[str] = None
    fatal: bool = False


@router.post("/register")
async def register_worker(req: RegisterReq, authorization: str = Header(default=None),
                          db: Session = Depends(get_db)):
    """Upsert this remote worker's row; return its id + the master's code version."""
    verify_worker_token(authorization)
    worker = (db.query(Worker)
              .filter(Worker.name == req.name, Worker.is_local == False)  # noqa: E712
              .first())
    if worker is None:
        worker = Worker(
            name=req.name, url=(req.url or req.name), worker_type="remote",
            capabilities=req.capabilities or {"backtest": True}, is_enabled=True, is_local=False,
        )
        db.add(worker)
    worker.status = "online"
    worker.last_heartbeat = datetime.utcnow()
    if req.url:
        worker.url = req.url
    if req.cpu_info:
        worker.cpu_info = req.cpu_info
    if req.gpu_info:
        worker.gpu_info = req.gpu_info
    db.commit()
    db.refresh(worker)
    logger.info(f"worker registered: {worker.name} (id={worker.id})")
    return {"worker_id": worker.id, "version": self_update.get_version_info()}


@router.post("/heartbeat")
async def worker_heartbeat(req: HeartbeatReq, authorization: str = Header(default=None),
                           db: Session = Depends(get_db)):
    """Refresh liveness + active-job count; return the master version (for mismatch detection)."""
    verify_worker_token(authorization)
    worker = db.query(Worker).filter(Worker.id == req.worker_id).first()
    if worker is not None:
        worker.status = "online"
        worker.last_heartbeat = datetime.utcnow()
        worker.active_jobs_count = req.active_jobs
        db.commit()
    return {"ok": True, "version": self_update.get_version_info()}


@router.post("/claim-trial")
async def claim_trial(worker_id: int = 0, authorization: str = Header(default=None)):
    """Atomically claim the next pending trial, or 204 No Content if the queue is empty."""
    verify_worker_token(authorization)
    job = get_broker().claim(worker_id=f"remote:{worker_id}")
    if job is None:
        return Response(status_code=204)
    return job


@router.post("/trial-result")
async def trial_result(req: TrialResultReq, authorization: str = Header(default=None)):
    """Hand a finished trial's result back to the broker (the GA coordinator is waiting on it)."""
    verify_worker_token(authorization)
    out: dict[str, Any] = {
        "ok": req.ok, "fitness": float(req.fitness), "trades": int(req.trades),
        "error": req.error, "fatal": req.fatal,
    }
    accepted = get_broker().post_result(req.trial_id, out)
    return {"ok": True, "accepted": accepted}
