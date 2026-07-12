"""Plain HTTP (non-UI) API routes, registered onto the NiceGUI app's underlying FastAPI
instance (see ``ui/main.py``: ``app`` there is the FastAPI app NiceGUI wraps -- the same
object ``app.on_shutdown`` already hooks into).

Currently just the DB reload callback: an external caller (a script, the DB-editing UI in
another process, an ops action) can hit this after changing expert/account settings directly
in the database to force the running platform to drop its in-memory singleton instance/settings
caches and re-read from the DB, without a full process restart.
"""
from typing import List, Optional

from fastapi import APIRouter
from pydantic import BaseModel

from ..logger import logger

router = APIRouter(prefix="/api", tags=["reload"])


class ReloadRequest(BaseModel):
    """Scope of the reload. Both fields default to None, which reloads EVERYTHING (every
    cached expert instance + every cached account instance + a full schedule refresh) --
    the common case after an out-of-band settings edit whose scope isn't known precisely.
    Pass one or both ids to scope the reload to specific instances instead."""
    expert_instance_id: Optional[int] = None
    account_id: Optional[int] = None


@router.post("/reload")
def reload_from_db(body: ReloadRequest = ReloadRequest()):
    """Drop cached expert/account instances (which also drops their cached settings — see
    ExpertInstanceCache/AccountInstanceCache) so the next access re-reads from the DB, and
    queue an APScheduler schedule refresh so any changed run-schedule settings take effect.

    Cache invalidation is done directly and synchronously here (not solely via
    JobManager.refresh_expert_schedules): that method only QUEUES a control message for its
    background thread and is a no-op if the JobManager isn't running yet, so relying on it
    alone would make the "caches are now clear" guarantee this endpoint returns unreliable.
    The schedule refresh is still queued afterward as a best-effort addition on top.
    """
    from ..core.AccountInstanceCache import AccountInstanceCache
    from ..core.ExpertInstanceCache import ExpertInstanceCache
    from ..core.JobManager import get_job_manager

    if body.expert_instance_id is None and body.account_id is None:
        experts_reloaded = ExpertInstanceCache.get_cache_stats()["expert_instance_ids"]
        ExpertInstanceCache.clear_cache()
        accounts_reloaded = "all"
        AccountInstanceCache.clear_cache()
    else:
        experts_reloaded: List[int] = []
        accounts_reloaded: List[int] = []
        if body.expert_instance_id is not None:
            ExpertInstanceCache.invalidate_instance(body.expert_instance_id)
            experts_reloaded.append(body.expert_instance_id)
        if body.account_id is not None:
            AccountInstanceCache.invalidate_instance(body.account_id)
            accounts_reloaded.append(body.account_id)

    schedules_refresh_queued = False
    try:
        get_job_manager().refresh_expert_schedules(body.expert_instance_id)
        schedules_refresh_queued = True
    except Exception as e:  # noqa: BLE001 - best-effort side effect, must not fail the reload
        logger.warning(f"reload_from_db: schedule refresh queue failed: {e}")

    return {
        "status": "ok",
        "expertsReloaded": experts_reloaded,
        "accountsReloaded": accounts_reloaded,
        "schedulesRefreshQueued": schedules_refresh_queued,
    }
