"""Plain HTTP (non-UI) API routes, registered onto the NiceGUI app's underlying FastAPI
instance (see ``ui/main.py``: ``app`` there is the FastAPI app NiceGUI wraps -- the same
object ``app.on_shutdown`` already hooks into).

Two endpoints so far: the DB reload callback (an external caller -- a script, the DB-editing
UI in another process, an ops action -- can hit this after changing expert/account settings
directly in the database to force the running platform to drop its in-memory singleton
instance/settings caches and re-read from the DB, without a full process restart) and a
manual schedule trigger (the API equivalent of the Scheduled Jobs page's "Run Now" button,
for restarting a job that's already fired today -- e.g. a screener scan that returned nothing
because of a since-fixed data bug -- without waiting for its next scheduled occurrence).
"""
from typing import List, Optional

from fastapi import APIRouter, HTTPException
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


class RunScheduleRequest(BaseModel):
    """Which registered schedule to fire right now. ``subtype`` is the AnalysisUseCase
    VALUE ("enter_market" / "open_positions"), matching what's stored on the expert's
    execution_schedule_* settings -- not the Python repr."""
    expert_instance_id: int
    subtype: str


@router.post("/run-schedule")
def run_schedule_now(body: RunScheduleRequest):
    """Manually fire a currently-registered scheduled analysis job right now -- the API
    equivalent of the Scheduled Jobs page's "Run Now" button. Restarting a job normally
    means waiting for its next cron occurrence (e.g. next Monday 09:30 ET); this lets an
    operator re-run TODAY'S occurrence immediately (e.g. after fixing a bug that caused it
    to return nothing) without touching the schedule itself.

    Looks up the LIVE APScheduler job for (expert_instance_id, subtype) to recover the
    exact ``symbol`` argument the schedule resolved when it was registered (a special
    placeholder -- SCREENER/DYNAMIC/EXPERT/OPEN_POSITIONS -- or a static symbol), then
    submits it through the SAME job_manager.submit_market_analysis() path the UI button
    calls (bypass_balance_check/bypass_transaction_check=True, matching a manual trigger).
    404s if no matching job is currently scheduled (e.g. the expert is disabled, or this
    subtype isn't configured for it).
    """
    from ..core.JobManager import get_job_manager
    from ..core.types import AnalysisUseCase

    try:
        subtype = AnalysisUseCase(body.subtype)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid subtype {body.subtype!r}; expected one of "
                   f"{[m.value for m in AnalysisUseCase]}",
        )

    jm = get_job_manager()
    match = None
    for job in jm._scheduled_jobs.values():
        args = getattr(job, "args", None) or []
        if len(args) >= 3 and args[0] == body.expert_instance_id and args[2] == subtype:
            match = args
            break

    if match is None:
        raise HTTPException(
            status_code=404,
            detail=f"No currently-scheduled job found for expert_instance_id="
                   f"{body.expert_instance_id}, subtype={subtype.value!r} "
                   f"(is the expert enabled? does it have this schedule configured?)",
        )

    expert_instance_id, symbol, resolved_subtype = match[0], match[1], match[2]
    try:
        task_id = jm.submit_market_analysis(
            expert_instance_id, symbol, subtype=resolved_subtype,
            bypass_balance_check=True, bypass_transaction_check=True,
        )
    except ValueError as e:
        # e.g. expert disabled/not found since the schedule was registered, or ENTER_MARKET
        # skipped because a transaction already exists for this expert+symbol.
        raise HTTPException(status_code=409, detail=str(e))

    if task_id is None:
        raise HTTPException(
            status_code=409,
            detail="Analysis skipped (e.g. an existing transaction already covers this "
                   "expert+symbol) -- nothing was queued.",
        )

    logger.info(
        f"run_schedule_now: manually triggered expert={expert_instance_id} "
        f"symbol={symbol} subtype={resolved_subtype.value} -> task_id={task_id}"
    )
    return {
        "status": "ok",
        "task_id": task_id,
        "expert_instance_id": expert_instance_id,
        "symbol": symbol,
        "subtype": resolved_subtype.value,
    }
