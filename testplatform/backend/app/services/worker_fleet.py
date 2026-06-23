"""Shared helper to surface the TRUE state of the worker fleet in the UI.

The CLI/distributed optimization path (``distributed_eval``) talks to remote workers directly
over HTTP and never writes their status back to the serve's DB, so the dashboard/workers panels
would otherwise show a stale ``status`` ("offline" while a worker is actively running trials) and
a stale ``active_jobs_count`` ("0 jobs" while 10 remote slots are busy).

``refresh_remote_status`` live-probes each enabled remote worker's ``/health`` at request time and
persists the result, so the badge is always correct regardless of how the run was launched. The
per-worker *active slot* counts are written separately by ``distributed_eval`` while a run is in
flight (engaged slots = local consumers + each worker's capacity).
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Dict, Optional

logger = logging.getLogger(__name__)


def refresh_remote_status(db, timeout: float = 1.5) -> Dict[int, Optional[int]]:
    """Live-probe every enabled REMOTE worker's ``/health``, persist ``status`` + ``last_heartbeat``
    to the DB, and return ``{worker_id: capacity}`` (capacity None if unknown/offline).

    Local workers are skipped (always reachable from the serve). Never raises — a bad probe or
    commit must not break the dashboard.
    """
    from app.models import Worker
    from app.services import worker_client

    caps: Dict[int, Optional[int]] = {}
    try:
        remotes = db.query(Worker).filter(
            Worker.is_local == False,  # noqa: E712
            Worker.is_enabled == True,  # noqa: E712
        ).all()
    except Exception as e:  # noqa: BLE001
        logger.debug(f"refresh_remote_status: could not list workers: {e}")
        return caps

    changed = False
    for w in remotes:
        status, cap = worker_client.quick_status(
            {"url": w.url, "password": w.password}, timeout=timeout)
        caps[w.id] = cap
        if w.status != status:
            w.status = status
            changed = True
        if status == "online":
            w.last_heartbeat = datetime.utcnow()
            changed = True
    if changed:
        try:
            db.commit()
        except Exception as e:  # noqa: BLE001
            logger.debug(f"refresh_remote_status: commit failed: {e}")
            db.rollback()
    return caps
