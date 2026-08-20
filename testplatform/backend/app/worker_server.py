"""Remote worker HTTP server — the MASTER pushes work to THIS (push model).

A DB-less FastAPI app the master dispatches to. It runs the SAME deterministic backtest code as
the master (``_trial_worker``) in its own process pool, mirrors the master's cache on demand
(tar push), and self-updates on request so distributed trials run identical code.

Run it with: ``ba2-test worker --port 8100 --password <secret> [--workers N]``.

Endpoints (every one bearer-checked against the worker password):
  GET  /health              -> {ok, capacity, cpu, gpu, version}
  GET  /version              -> {app_version, git_commit, ...}
  GET  /diag/memory          -> {ok, server_rss_mb, child_count, child_rss_mb, system_*}
  GET  /logs/list            -> {dir, files:[...]}                  (this worker's LOGS_DIR listing)
  GET  /logs                 -> {file, total_lines, lines:[...]}    (tail of one log file, no SSH needed)
  GET  /cache/manifest       -> {files:[{rel_path,size,...}], ...}   (what this worker already has)
  POST /cache/push           -> accept a tar STREAM, extract into CACHE_FOLDER
  POST /cache/prune          -> {rel_paths} -> delete leftovers from a master-side rebuild/compaction
  POST /submit-trial         -> {config, fitness_metric} -> {job_id}      (async; poll /job-status)
  POST /submit-trial-full    -> {config, fitness_metric} -> {job_id}      (async; poll /job-status)
  GET  /job-status/{job_id}  -> {status: running} | {status: done, result: {...}}  (404 if unknown)
  POST /sync/strategy     -> {...Strategy row + natural keys} -> upsert by (name, created_at)
  POST /sync/optimization -> {...StrategyOptimization row + strategy natural key} -> upsert
  POST /sync/backtest     -> {...Backtest row + strategy/optimization natural keys} -> upsert
  POST /update         -> git pull + reinstall + restart (self_update)

SUBMIT/POLL, NOT A BLOCKING CALL (2026-07-19): /submit-trial(-full) return a job_id
IMMEDIATELY; the actual trial runs in the background pool and the master polls
/job-status/{job_id} every couple seconds until it reports done. This replaces the old
synchronous /run-trial(-full) (removed) that held a client HTTP connection open for the
WHOLE trial (up to a 1800s client-side timeout) -- if THIS worker restarted mid-request
(e.g. an /update landing right as a trial was in flight, or any other connection drop),
the master had no way to notice until that full timeout elapsed. A restart wipes _JOBS
(in-memory only, by design), so a poll for a job_id from before the restart gets a clean
404 within one poll interval (a couple seconds) instead of a ~30-minute stall — see
worker_client.py's WorkerJobLost.
"""

from __future__ import annotations

import hmac
import logging
import os
import tempfile
import threading
import uuid
from concurrent.futures import Future, ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from typing import Any, Optional

from fastapi import FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from app.services import cache_sync, self_update, sync_receiver

logger = logging.getLogger(__name__)

# docs/openapi disabled: this is a headless worker, not a browsable API — don't expose its
# schema unauthenticated (the functional endpoints are all password-gated regardless).
worker_app = FastAPI(title="BA2 Remote Worker", docs_url=None, redoc_url=None, openapi_url=None)

# Set by run_worker_server() before uvicorn starts.
_PASSWORD: Optional[str] = None
_CAPACITY: int = 1
# The daemon's --workers value: the largest pool it may ever hold. _CAPACITY is the CURRENT
# size and moves with per-job resizing; this does not. Keeping them apart is what stops a
# narrow job's shrink from permanently capping every later job.
_CAPACITY_MAX: int = 1
_POOL: Optional[ProcessPoolExecutor] = None
# Rebuilds _POOL after a BrokenProcessPool (set alongside _POOL in run_worker_server).
_POOL_FACTORY = None
_POOL_LOCK = threading.Lock()

# In-memory submit/poll job registry (2026-07-19): job_id -> Future. Deliberately NOT
# persisted anywhere — a restart wiping this is the FEATURE (see module docstring's
# "SUBMIT/POLL" note): a poll for a pre-restart job_id must come back "unknown" fast, not
# hang. A finished job is popped on its first successful status fetch (one-shot; the
# master never polls the same job_id twice), so this stays small in the steady state.
# Entries only accumulate if a client submits and then never polls (crash) — bounded by
# _JOBS_MAX_ORPHAN_AGE's sweep below.
_JOBS: dict[str, Future] = {}
_JOBS_SUBMITTED_AT: dict[str, float] = {}
# Last time the MASTER polled each job. A live master polls every ~2s (worker_client's
# _submit_and_poll), so a long gap means the master is gone -- killed, crashed, or it gave up.
_JOBS_LAST_POLL_AT: dict[str, float] = {}
_JOBS_LOCK = threading.Lock()
_JOBS_MAX_ORPHAN_AGE = 6 * 3600.0  # 6h: generous vs. any real trial; just bounds a leak
# ABANDONED-MASTER SWEEP (2026-08-06). The 6h age sweep above is keyed on SUBMIT time, so a
# killed master left its trials running for up to 6h, each holding a pool slot. Measured: after
# two killed optimize runs, remote150 had 7 children on a 6-slot pool -- it answered /health with
# ok:true capacity:6 while having ZERO free slots, so every new trial was accepted and never
# scheduled. That is what stalled the grid for ~9h on 2026-08-05 and again on 08-06.
# 300s is ~150 missed polls: unambiguous, while tolerating a slow/paused master.
_JOBS_ABANDONED_AFTER = 300.0

# COOPERATIVE CANCEL (2026-07-28). Per-job control block, a Manager dict proxy passed into the
# pool child: {"cancel": bool, "bars": int}. The trial's progress_cb reads it every throttled
# bar and aborts when cancel flips.
#
# WHY COOPERATIVE AND NOT A KILL: the trial runs in a shared ProcessPoolExecutor. Terminating one
# child raises BrokenProcessPool for every OTHER in-flight trial on this worker — unacceptable
# when three healthy trials are sharing the pool.
#
# WHY IT MATTERS: before this, a trial the master gave up on kept computing and held its slot
# until the 6h orphan sweep, so a TIMEOUT REMOVED CAPACITY instead of freeing it. Retries then
# landed on an ever-more-loaded worker: the Senate S5 grid went 4 slots x 3 retries = 12
# timeouts straight into "worker dead" (opt 226, 2026-07-28).
#
# A Manager proxy (not multiprocessing.Value) because it must be PICKLABLE to cross into a
# spawn-based pool child as a task argument; a raw Value can only be inherited at fork.
_JOB_CTL: dict[str, Any] = {}
_MANAGER = None


def _install_orchestration_file_logging() -> None:
    """Persist THIS process's own orchestration-layer logs (submit-trial, job-status, pool
    crashes, memory dumps, uvicorn) to ``<LOGS_DIR>/worker_server.log`` so the ``/logs`` endpoint
    has something to show for them.

    Deliberately separate from ``ba2_common.logger``'s per-instance rotating handlers: those are
    attached ONLY to the ``ba2_common`` logger (``propagate = False``) and the per-expert loggers
    — trial business logic running in a SPAWNED pool child, not this single long-lived server
    process. Spawned children already run with ``BA2_FILE_LOGGING=0`` (see logger.py's module
    docstring) specifically to dodge a multi-process RotatingFileHandler rollover race, so this
    does NOT attach to the root logger from a child process — only from the one, single-process
    server the pool children are spawned FROM. A ROOT handler here (not a module-specific one)
    also captures uvicorn's own access/error logs, useful context for the same incidents."""
    if not os.environ.get("BA2_FILE_LOGGING", "1") == "0":
        from logging.handlers import RotatingFileHandler
        from ba2_common.logger import LOGS_DIR

        os.makedirs(LOGS_DIR, exist_ok=True)
        path = os.path.join(LOGS_DIR, "worker_server.log")
        root = logging.getLogger()
        if any(isinstance(h, RotatingFileHandler) and getattr(h, "baseFilename", None) ==
               os.path.abspath(path) for h in root.handlers):
            return  # already installed (e.g. a hot-reload re-entering this)
        handler = RotatingFileHandler(path, maxBytes=10 * 1024 * 1024, backupCount=7, encoding="utf-8")
        handler.setFormatter(logging.Formatter(
            "%(asctime)s - %(name)s - %(levelname)s - %(message)s"))
        handler.setLevel(logging.INFO)
        root.addHandler(handler)
        root.setLevel(logging.INFO)


def _memory_snapshot() -> dict:
    """Best-effort memory snapshot of the worker SERVER + its trial children. Shared by
    ``_dump_worker_memory`` (logs it on a pool crash) and the ``/diag/memory`` endpoint (returns
    it on demand — e.g. from the master while investigating a slow/hung worker, no SSH needed)."""
    import psutil
    me = psutil.Process(os.getpid())
    vm = psutil.virtual_memory()
    kids = me.children(recursive=True)
    kid_rss = sorted((round(k.memory_info().rss / 1048576) for k in kids), reverse=True)
    return {
        "server_rss_mb": me.memory_info().rss // 1048576,
        "child_count": len(kids),
        "child_rss_mb": kid_rss,
        "system_available_mb": vm.available // 1048576,
        "system_total_mb": vm.total // 1048576,
        "system_percent_used": vm.percent,
    }


def _dump_worker_memory(context: str) -> None:
    """Best-effort memory snapshot of the worker SERVER + its trial children, logged at the
    moment a pool breaks — so the next WinError-1450-style incident shows what was depleting
    memory instead of leaving only 'terminated abruptly'."""
    try:
        snap = _memory_snapshot()
        logger.warning(
            "%s: server RSS=%dMB, %d child proc(s) RSS(MB)=%s, system available=%dMB/%dMB (%.1f%% used)",
            context, snap["server_rss_mb"], snap["child_count"], snap["child_rss_mb"],
            snap["system_available_mb"], snap["system_total_mb"], snap["system_percent_used"],
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("%s: memory dump unavailable (%r)", context, e)


def _rebuild_pool(exc: Exception) -> None:
    """Swap the broken trial pool for a fresh one (same recovery pattern as the master's
    DistributedEvaluator._recover_pool). Under a lock so concurrent /submit-trial or
    /job-status callers rebuild once, not once each."""
    global _POOL
    with _POOL_LOCK:
        if _POOL_FACTORY is None:
            logger.error("trial pool broken (%r) and no factory to rebuild it", exc)
            return
        logger.warning("trial pool broken (%r); rebuilding %d-slot pool", exc, _CAPACITY)
        _dump_worker_memory("trial pool crash")
        old = _POOL
        try:
            if old is not None:
                old.shutdown(wait=False, cancel_futures=True)
        except Exception:  # noqa: BLE001 — best-effort cleanup of the already-dead pool
            pass
        _POOL = _POOL_FACTORY()


def _sweep_orphaned_jobs() -> None:
    """Drop job registry entries older than _JOBS_MAX_ORPHAN_AGE that were never polled to
    completion (client crashed / gave up before fetching the result) — called opportunistically
    from _submit_job so it costs nothing on the (normal) submit-then-poll-once path."""
    import time as _time
    now = _time.monotonic()
    cutoff = now - _JOBS_MAX_ORPHAN_AGE
    abandoned_cutoff = now - _JOBS_ABANDONED_AFTER
    with _JOBS_LOCK:
        stale = [jid for jid, ts in _JOBS_SUBMITTED_AT.items() if ts < cutoff]
        # A job whose master stopped polling is abandoned NOW -- do not wait out the 6h age.
        # Falls back to submit time for a job never polled at all (master died between the
        # submit response and its first poll).
        abandoned = [jid for jid in _JOBS_SUBMITTED_AT
                     if jid not in stale
                     and _JOBS_LAST_POLL_AT.get(jid, _JOBS_SUBMITTED_AT[jid]) < abandoned_cutoff]
        for jid in stale + abandoned:
            _JOBS.pop(jid, None)
            _JOBS_SUBMITTED_AT.pop(jid, None)
            _JOBS_LAST_POLL_AT.pop(jid, None)
            ctl = _JOB_CTL.pop(jid, None)
            if ctl is not None:
                # Sweeping an orphan now also STOPS it. Previously the registry entry was
                # dropped while the trial kept burning a slot to completion.
                try:
                    ctl["cancel"] = True
                except Exception:  # noqa: BLE001
                    pass
    if stale:
        logger.warning("swept %d orphaned job(s) never polled to completion: %s", len(stale), stale)
    if abandoned:
        logger.warning("swept %d job(s) whose master stopped polling (>%.0fs) — slots released: %s",
                       len(abandoned), _JOBS_ABANDONED_AFTER, abandoned)


# ---------------------------------------------------------------- self-throttling on memory
# WHY THE WORKER AND NOT THE MASTER (2026-08-18). The master CAN stop feeding a starved worker
# (it walks its dispatch slots down), but it can never RECLAIM what that worker already holds:
# an idle pool child keeps its last working set resident (measured 4-7 GB on the mid cap band).
# Proven live on remote150: the master shed 12 -> 11 -> ... -> 1 slots, one per minute, and the
# box was STILL at 0.5-2.6% free of 65 GB, because 12 pool children existed and stayed resident
# regardless of whether anything was dispatched to them. Only this process owns those children,
# so only this process can free them.
_MEM_FLOOR_PCT = float(os.getenv("BT_WORKER_MEM_FLOOR_PCT", "10"))
_MEM_POLL_S = float(os.getenv("BT_WORKER_MEM_POLL_S", "60"))


class _MemoryPressure(Exception):
    """Reason handed to _rebuild_pool when the pool is recycled for memory, not for a crash."""


class _PoolResize(Exception):
    """Reason handed to _rebuild_pool when the master sized the pool for a new job."""


def _free_mem_pct() -> Optional[float]:
    """System free memory as a percentage, or None if psutil is unavailable."""
    try:
        import psutil
        return 100.0 - float(psutil.virtual_memory().percent)
    except Exception:  # noqa: BLE001 -- never let a probe failure break a trial path
        return None


def _busy_job_count() -> int:
    with _JOBS_LOCK:
        return sum(1 for f in _JOBS.values() if not f.done())


def _memory_watchdog() -> None:
    """Reclaim this worker's OWN pool memory when the box drops under the floor.

    Rebuilding the pool is the only thing that actually returns an idle child's working set to
    the OS, and it is safe ONLY when nothing is running -- so a busy worker is left alone and
    reclaimed on a later poll once its trials drain. Combined with the admission check in
    _submit_job (which stops new work landing meanwhile), a starved box empties out and recovers
    instead of sitting pinned until someone restarts the daemon by hand.
    """
    import time as _time
    while True:
        _time.sleep(_MEM_POLL_S)
        try:
            free_pct = _free_mem_pct()
            if free_pct is None or free_pct >= _MEM_FLOOR_PCT:
                continue
            busy = _busy_job_count()
            if busy:
                logger.warning(
                    "memory %.1f%% free < %.0f%% floor, but %d trial(s) running -- NOT recycling "
                    "the pool (that would discard live work); new submits are being refused and "
                    "the pool is reclaimed once they drain", free_pct, _MEM_FLOOR_PCT, busy)
                continue
            logger.warning(
                "memory %.1f%% free < %.0f%% floor and pool is IDLE -- recycling %d-slot pool to "
                "return every child's working set to the OS", free_pct, _MEM_FLOOR_PCT, _CAPACITY)
            _rebuild_pool(_MemoryPressure(f"{free_pct:.1f}% free"))
        except Exception as e:  # noqa: BLE001 -- the watchdog must outlive any single failure
            logger.error("memory watchdog poll failed: %r", e)


def _submit_job(fn, *args) -> str:
    """Submit *fn(*args)* to the trial pool, register it under a fresh job_id, and return that
    id immediately (does NOT wait for the trial). A BrokenProcessPool AT SUBMIT time (the pool
    was already dead) is handled the same way a broken pool during execution is: rebuild it and
    hand back a job whose result is already the retryable-failure dict, so the client's poll
    loop sees a normal 'done' status instead of a submit-time 500."""
    import time as _time
    _sweep_orphaned_jobs()
    if _POOL is None:
        raise HTTPException(status_code=503, detail="Worker pool not initialized.")
    # ADMISSION CONTROL. Refuse work when this box is already under the floor rather than
    # accepting it and thrashing. `retryable` is the existing contract: the master's dispatcher
    # REQUEUES such a trial onto another slot (see distributed_eval's retryable branch) instead
    # of recording a failed genome, so nothing is lost and the GA is not biased.
    _free = _free_mem_pct()
    if _free is not None and _free < _MEM_FLOOR_PCT:
        logger.warning("refusing trial: %.1f%% memory free < %.0f%% floor (retryable)",
                       _free, _MEM_FLOOR_PCT)
        job_id = uuid.uuid4().hex
        future = Future()
        future.set_result({"ok": False, "fatal": False, "retryable": True,
                           "backpressure": True,
                           "error": f"worker under memory floor ({_free:.1f}% free)"})
        with _JOBS_LOCK:
            _JOBS[job_id] = future
            import time as _t
            _JOBS_SUBMITTED_AT[job_id] = _t.monotonic()
        return job_id
    # CAPACITY GATE. The pool queues anything submitted past its worker count, so without
    # this a master that over-dispatches builds an unbounded backlog: measured busy:61 on a
    # 12-slot pool on 2026-08-19. Every queued trial is work the master has already given up
    # on (its no-bars timeout is 420s) and recomputed elsewhere, so running it is pure waste
    # AND it keeps every pool child resident, which is what starved the box.
    _busy = _busy_job_count()
    if _busy >= _CAPACITY:
        logger.warning("refusing trial: %d/%d slots busy (retryable backpressure)",
                       _busy, _CAPACITY)
        job_id = uuid.uuid4().hex
        future = Future()
        future.set_result({"ok": False, "fatal": False, "retryable": True,
                           "backpressure": True,
                           "error": f"worker at capacity ({_busy}/{_CAPACITY} busy)"})
        with _JOBS_LOCK:
            _JOBS[job_id] = future
            import time as _t2
            _JOBS_SUBMITTED_AT[job_id] = _t2.monotonic()
        return job_id
    job_id = uuid.uuid4().hex
    ctl = _new_job_ctl()
    try:
        # ctl rides as a trailing arg; _trial_worker takes it as an optional keyword-style
        # positional so an older master (which never sends one) still works unchanged.
        future = _POOL.submit(fn, *args, ctl)
    except BrokenProcessPool as e:
        _rebuild_pool(e)
        future = Future()
        future.set_result({"ok": False, "error": repr(e), "fatal": False, "retryable": True})
    with _JOBS_LOCK:
        _JOBS[job_id] = future
        _JOBS_SUBMITTED_AT[job_id] = _time.monotonic()
        if ctl is not None:
            _JOB_CTL[job_id] = ctl
    return job_id


def _new_job_ctl():
    """Fresh per-job control block, or None if a Manager can't be started.

    Degrades to None rather than failing the submit: without a control block the job simply
    isn't cancellable, which is exactly today's behaviour — never a reason to refuse work.
    """
    global _MANAGER
    try:
        if _MANAGER is None:
            import multiprocessing
            _MANAGER = multiprocessing.Manager()
        return _MANAGER.dict({"cancel": False, "bars": 0})
    except Exception as e:  # noqa: BLE001
        logger.warning("job control block unavailable (%r); job will not be cancellable", e)
        return None


def _cancel_job(job_id: str) -> dict:
    """Flag a job for cooperative cancellation and drop it from the registry.

    Returns ``{"cancelled": bool, "was_running": bool}``. A job that already finished, or whose
    control block is missing, is reported honestly rather than pretended-cancelled — the caller
    (a master that just timed out) uses this only as best-effort cleanup.
    """
    with _JOBS_LOCK:
        future = _JOBS.pop(job_id, None)
        _JOBS_SUBMITTED_AT.pop(job_id, None)
        ctl = _JOB_CTL.pop(job_id, None)
    if future is None:
        raise HTTPException(status_code=404, detail=f"job {job_id!r} unknown")
    # Not yet started -> a plain Future.cancel() is enough and frees the slot immediately.
    if future.cancel():
        return {"cancelled": True, "was_running": False}
    if ctl is None:
        return {"cancelled": False, "was_running": True}
    try:
        ctl["cancel"] = True
    except Exception as e:  # noqa: BLE001 — a dead manager must not 500 the caller
        logger.warning("could not flag job %s for cancel (%r)", job_id, e)
        return {"cancelled": False, "was_running": True}
    logger.warning("job %s flagged for cooperative cancel", job_id)
    return {"cancelled": True, "was_running": True}


def _job_status(job_id: str) -> dict:
    """Poll one job: {"status": "running"} while in flight, or {"status": "done", "result":
    {...}} once finished (popping the registry entry — one-shot fetch). Raises 404 if job_id
    is unknown, which happens for a genuinely bad id AND, deliberately, for any id from
    before this process's last restart (the registry is in-memory only) — see the module
    docstring's SUBMIT/POLL note for why that's the point, not a bug."""
    import time as _time
    with _JOBS_LOCK:
        future = _JOBS.get(job_id)
        ctl = _JOB_CTL.get(job_id)
        if future is not None:
            # Liveness of the MASTER, not the trial: proves someone is still waiting for this
            # result. _sweep_orphaned_jobs cancels jobs nobody has polled recently.
            _JOBS_LAST_POLL_AT[job_id] = _time.monotonic()
    if future is None:
        raise HTTPException(status_code=404, detail=f"job {job_id!r} unknown (unrecognized id, "
                                                     f"or this worker restarted since it was submitted).")
    if not future.done():
        # LIVENESS. "running" alone is ambiguous: a Future queued behind a saturated pool is
        # indistinguishable from one actively computing, so the master could only wait out the
        # full trial_timeout (2026-08-06: 3h x 3 retries x 6 slots, grid idle ~9h, because
        # remote150's pool was jammed by orphaned children). Report the trial's own bar
        # heartbeat so the client can fail fast — see worker_client._submit_and_poll.
        #
        # bars == 0 means ACCEPTED BUT NEVER STARTED, which is the saturated-pool case and is
        # worth failing much faster than a trial that started and then stalled.
        bars = 0
        if ctl is not None:
            try:
                bars = int(ctl.get("bars") or 0)
            except Exception:  # noqa: BLE001 — a dead manager must not 500 the poller
                bars = -1  # unknown; client treats this as "no heartbeat available"
        return {"status": "running", "bars": bars, "started": bars > 0}
    with _JOBS_LOCK:
        _JOBS.pop(job_id, None)
        _JOBS_SUBMITTED_AT.pop(job_id, None)
        _JOB_CTL.pop(job_id, None)   # control block dies with the job it belonged to
    try:
        result = future.result()
    except BrokenProcessPool as e:
        _rebuild_pool(e)
        result = {"ok": False, "error": repr(e), "fatal": False, "retryable": True}
    except Exception as e:  # noqa: BLE001 — surface as a failed trial, never 500 the poller
        result = {"ok": False, "error": repr(e), "fatal": False}
    return {"status": "done", "result": result}

# Bound lazily on first sync request (see _sync_session()) so tests can monkeypatch it to an
# isolated DB, and so importing this module doesn't eagerly touch app.models.database.
SessionLocal = None


def _verify(authorization: Optional[str]) -> None:
    """Bearer-check against the worker password (constant-time)."""
    if not _PASSWORD:
        raise HTTPException(status_code=503, detail="Worker password not configured.")
    if not authorization:
        raise HTTPException(status_code=401, detail="Missing Authorization header.")
    parts = authorization.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(status_code=401, detail="Expected 'Bearer <password>'.")
    if not hmac.compare_digest(parts[1], _PASSWORD):
        raise HTTPException(status_code=403, detail="Invalid worker password.")


class RunTrialReq(BaseModel):
    config: dict
    fitness_metric: str
    cache_root: Optional[str] = None  # the MASTER's CACHE_FOLDER, for path localization
    inmem_trades: Optional[bool] = None  # master's sql-less "dict trades" flag (None = worker default)


def _apply_inmem_trades_flag(inmem_trades: Optional[bool]) -> None:
    """Honor the master's sql-less "dict trades" flag on this worker so distributed trials use the
    SAME order/transaction backend (and thus byte-identical economics) as the master. Sets the
    process env consulted by ``backtest_db._inmem_trades_enabled()``; None leaves the worker's own
    default (ON). Process-global, but the flag is uniform across a GA run so pooled trials agree."""
    if inmem_trades is not None:
        os.environ["BT_INMEM_TRADES"] = "1" if inmem_trades else "0"


class SecretsReq(BaseModel):
    settings: dict  # {app_setting_key: value_str}, e.g. {"FMP_API_KEY": "...", "finnhub_api_key": "..."}


class PruneReq(BaseModel):
    rel_paths: list[str]  # worker-relative cache paths the master's manifest no longer lists


class SyncRowReq(BaseModel):
    """Body for the 3 ``/sync/*`` endpoints: a replicated model row plus natural-key sidecar
    fields (e.g. ``strategy_name``/``strategy_created_at`` for FK resolution).

    Only ``name``/``created_at`` (the natural key every target table is matched by) are declared
    — everything else legitimately varies per target model (every other column on
    Strategy/StrategyOptimization/Backtest, plus the FK-parent natural-key sidecar fields), so
    this deliberately does NOT enumerate a rigid schema of every possible field. ``extra="allow"``
    lets those pass through to ``sync_receiver.upsert_by_natural_key`` unchanged; declaring the
    two required fields turns a malformed payload (missing ``name``/``created_at``) into a clean
    422 at the FastAPI boundary instead of a ``None`` silently propagating deep into the upsert.
    """
    model_config = ConfigDict(extra="allow")
    name: str
    created_at: str


def _localize_paths(obj, master_root: str, local_root: str):
    """Recursively rewrite any string under the MASTER's cache root to THIS worker's cache root.

    Trial configs embed absolute cache paths computed on the master (screener_store,
    screener_runtime.store, options_cache_db, ...). The master and worker don't share a filesystem
    layout, so a verbatim path would miss the locally-synced cache. This remaps the master prefix
    to the local one; non-cache strings pass through unchanged. OS-separator tolerant.
    """
    mr = master_root.replace("\\", "/").rstrip("/")
    if isinstance(obj, str):
        v = obj.replace("\\", "/")
        if v == mr or v.startswith(mr + "/"):
            rel = v[len(mr):].lstrip("/")
            if not rel:
                return local_root
            # Join using WHATEVER separator local_root itself already uses (NOT os.path.join,
            # which would splice in THIS process's os.sep regardless of local_root's own
            # style — worker and master aren't guaranteed to be the same OS, and even on the
            # same OS this must be a pure string transform to stay deterministic/testable).
            sep = "\\" if ("\\" in local_root and "/" not in local_root) else "/"
            return local_root.rstrip("/\\") + sep + rel.replace("/", sep)
        return obj
    if isinstance(obj, dict):
        return {k: _localize_paths(val, master_root, local_root) for k, val in obj.items()}
    if isinstance(obj, list):
        return [_localize_paths(x, master_root, local_root) for x in obj]
    return obj


def _memory() -> dict:
    """Host RAM, in MB, plus this worker process tree's own resident usage.

    Added 2026-08-09. /health reported cpu and gpu but NOT memory, which made a remote worker's
    single most useful diagnostic unobservable: memory is what actually degrades a trial host.
    Locally the failure mode is documented and measured (47c4b26: 96.8% used stalled the grid for
    ~40 minutes mid-generation, and it resumed within 60s of freeing memory), and the master's own
    grid_status.sh prints a LOW-memory warning for exactly that reason -- but for the remote the
    master was blind, so the same stall would look like an unexplained slowdown.

    ``used_pct`` is what to alarm on. ``rss_mb`` covers this process AND its spawn children, which
    is what a trial slot actually costs; a worker whose rss_mb approaches total_mb is one trial away
    from paging regardless of what its nominal slot count claims.

    Degrades to {"available": False} rather than raising -- /health must answer even where psutil
    is missing, and a missing metric must not take the worker offline.
    """
    try:
        import psutil
        vm = psutil.virtual_memory()
        me = psutil.Process()
        rss = me.memory_info().rss
        for child in me.children(recursive=True):
            try:
                rss += child.memory_info().rss
            except Exception:  # noqa: BLE001 — a child may exit mid-walk
                continue
        return {
            "available": True,
            "total_mb": round(vm.total / 1048576),
            "free_mb": round(vm.available / 1048576),
            "used_pct": round(vm.percent, 1),
            "rss_mb": round(rss / 1048576),
        }
    except Exception as e:  # noqa: BLE001
        return {"available": False, "error": str(e)[:120]}


def _hardware() -> dict:
    """cpu/gpu/memory info, reusing the master's helper so the master's worker UI shows the same shape."""
    try:
        from app.api.workers import get_local_hardware_info
        cpu, gpu = get_local_hardware_info()
        return {"cpu": cpu, "gpu": gpu, "memory": _memory()}
    except Exception:  # noqa: BLE001
        return {"cpu": {"cores": os.cpu_count(), "model": "unknown"}, "gpu": None,
                "memory": _memory()}


@worker_app.get("/health")
def health(authorization: str = Header(default=None)):
    """Worker liveness + how much of it is actually AVAILABLE.

    Sweeps abandoned jobs first: the sweep used to run only from ``_submit_job``, so a worker
    whose master had been killed never swept (no new submits arrive) and sat on occupied slots
    indefinitely. /health is what the next master's pre-flight calls, so sweeping here is what
    guarantees a fresh run finds released slots.

    ``capacity`` stays the NOMINAL slot count (unchanged for existing callers); ``busy``/``free``
    report reality. A worker answering ok:true capacity:6 while holding 6 live jobs is exactly
    how the 2026-08-05/06 stalls looked from the master's side.
    """
    _verify(authorization)
    _sweep_orphaned_jobs()
    with _JOBS_LOCK:
        busy = sum(1 for f in _JOBS.values() if not f.done())
    return {"ok": True, "capacity": _CAPACITY, "capacity_max": _CAPACITY_MAX,
            "busy": busy, "free": max(0, _CAPACITY - busy),
            "version": self_update.get_version_info(), **_hardware()}


@worker_app.post("/pool/resize")
def pool_resize(body: dict, authorization: str = Header(default=None)):
    """Resize the trial pool to ``workers`` slots, for the job the master is about to run.

    WHY THIS EXISTS. Pool children are spawned once at daemon start and stay resident no
    matter how few slots the master dispatches, so a master-side cap never reclaimed
    anything -- remote150 held 12 children (~41 GB) to serve 6 engaged slots. Rebuilding at
    the new size is what actually returns the surplus children's working sets to the OS.

    Refused while trials are in flight: two optimizations can share a worker (the grid runs
    --parallel 2) and a rebuild under a live trial would discard it. The running size wins
    and the caller is told why, rather than the resize silently half-applying.
    """
    _verify(authorization)
    global _CAPACITY
    try:
        want = int(body.get("workers"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="workers must be an integer >= 1")
    if want < 1:
        raise HTTPException(status_code=400, detail="workers must be an integer >= 1")
    want = min(want, _CAPACITY_MAX)  # never spawn more children than the daemon was sized for
    if want == _CAPACITY:
        return {"ok": True, "capacity": _CAPACITY, "changed": False,
                "reason": "already at that size"}
    busy = _busy_job_count()
    if busy and not bool(body.get("force")):
        logger.warning("refusing pool resize %d -> %d: %d trial(s) in flight",
                       _CAPACITY, want, busy)
        return {"ok": False, "capacity": _CAPACITY, "changed": False,
                "reason": f"{busy} trial(s) in flight"}
    if busy:
        # FORCED, and only the memory governor does this. Rebuilding cancels the in-flight
        # trials; the master requeues them (they say nothing about the genome) and that is
        # far cheaper than the measured alternative -- a box pinned at 5% free for the rest
        # of the job because concurrency shedding cannot evict a resident pool child.
        logger.warning("FORCED pool resize %d -> %d with %d trial(s) in flight "
                       "(memory pressure); those trials are cancelled and will be requeued",
                       _CAPACITY, want, busy)
    was = _CAPACITY
    _CAPACITY = want
    logger.warning("resizing trial pool %d -> %d slots (per-job sizing from the master)",
                   was, want)
    _rebuild_pool(_PoolResize(f"resize {was} -> {want}"))
    return {"ok": True, "capacity": _CAPACITY, "changed": True}


@worker_app.get("/version")
def version(authorization: str = Header(default=None)):
    _verify(authorization)
    return self_update.get_version_info()


@worker_app.get("/diag/memory")
def diag_memory(authorization: str = Header(default=None)):
    """On-demand memory snapshot (see ``_memory_snapshot``) — lets the master check a remote
    worker's RSS/child-process footprint without SSH/RDP access, e.g. while investigating a
    slow or hung worker."""
    _verify(authorization)
    try:
        return {"ok": True, **_memory_snapshot()}
    except Exception as e:  # noqa: BLE001 — diagnostics must never 500 the caller
        return {"ok": False, "error": repr(e)}


@worker_app.get("/logs/list")
def logs_list(authorization: str = Header(default=None)):
    """List the log filenames in this worker's LOGS_DIR (app.log, app.debug.log,
    all.debug.log, all.error.log, ...) — see ``ba2_common.logger`` for what writes there."""
    _verify(authorization)
    from ba2_common.logger import LOGS_DIR

    try:
        files = sorted(os.listdir(LOGS_DIR))
    except FileNotFoundError:
        files = []
    return {"dir": LOGS_DIR, "files": files}


@worker_app.get("/logs")
def logs_tail(file: str = "app.log", tail_lines: int = 500,
              authorization: str = Header(default=None)):
    """Return the last *tail_lines* lines of one log file under LOGS_DIR — the remote-debugging
    tool this worker didn't have before: without it, diagnosing a hang/crash/anomaly on a
    machine with no SSH/RDP access meant flying blind. ``file`` must be a bare filename (no `/`,
    `\\`, or `..`) so a caller can't read arbitrary paths off the worker's disk."""
    _verify(authorization)
    from ba2_common.logger import LOGS_DIR

    name = os.path.basename(file)
    if not name or name != file:
        raise HTTPException(status_code=400, detail="file must be a bare filename (no path)")
    path = os.path.join(LOGS_DIR, name)
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail=f"no such log file: {name!r}")
    tail_lines = max(1, min(tail_lines, 20000))  # bound the response size
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        lines = f.readlines()
    return {"file": name, "total_lines": len(lines), "lines": lines[-tail_lines:]}


@worker_app.get("/cache/manifest")
def cache_manifest(with_hash: bool = False, authorization: str = Header(default=None)):
    _verify(authorization)
    return cache_sync.build_manifest(with_hash=with_hash)


@worker_app.post("/cache/push")
async def cache_push(request: Request, authorization: str = Header(default=None)):
    """Accept a tar STREAM from the master and extract it into CACHE_FOLDER.

    Spools the upload to a temp file (disk, not memory) so an arbitrarily large tar streams safely,
    then extracts (traversal-guarded). Returns ``{extracted, bytes, skipped}``.
    """
    _verify(authorization)
    tmp = tempfile.NamedTemporaryFile(prefix="ba2-cache-push-", suffix=".tar", delete=False)
    try:
        async for chunk in request.stream():
            tmp.write(chunk)
        tmp.close()
        with open(tmp.name, "rb") as fh:
            result = cache_sync.extract_tar(fh)
        logger.info("cache push: %s", result)
        return result
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass


@worker_app.post("/submit-trial")
def submit_trial(req: RunTrialReq, authorization: str = Header(default=None)):
    """Submit ONE deterministic trial to the worker pool and return a job_id IMMEDIATELY —
    does not wait for the trial. Poll /job-status/{job_id} for the result (see module
    docstring's SUBMIT/POLL note for why this replaced the old blocking /run-trial)."""
    _verify(authorization)
    _apply_inmem_trades_flag(req.inmem_trades)
    from app.services.strategy_optimization_handler import _trial_worker
    config = req.config
    if req.cache_root:
        from ba2_common.config import CACHE_FOLDER
        config = _localize_paths(req.config, req.cache_root, CACHE_FOLDER)
    job_id = _submit_job(_trial_worker, config, req.fitness_metric)
    return {"job_id": job_id}


@worker_app.post("/submit-trial-full")
def submit_trial_full(req: RunTrialReq, authorization: str = Header(default=None)):
    """Like ``/submit-trial`` but for the FULL results dict (equity curve, metrics, ...)
    instead of the trimmed ``{ok,fitness,trades,error}`` summary — for diagnosing a fitness
    mismatch against a master-side result field-by-field, or persisting a top-N backtest.
    Not on the hot GA path (that stays on ``/submit-trial``'s small payload); this is an
    operator/debug + top-N-persist tool."""
    _verify(authorization)
    _apply_inmem_trades_flag(req.inmem_trades)
    from app.services.strategy_optimization_handler import _persist_trial_worker
    config = req.config
    if req.cache_root:
        from ba2_common.config import CACHE_FOLDER
        config = _localize_paths(req.config, req.cache_root, CACHE_FOLDER)
    job_id = _submit_job(_persist_trial_worker, config)
    return {"job_id": job_id}


@worker_app.get("/job-status/{job_id}")
def job_status(job_id: str, authorization: str = Header(default=None)):
    """Poll a job submitted via /submit-trial or /submit-trial-full. 404 means the id is
    unknown — either a bad id, or (deliberately, see module docstring) this worker process
    restarted since the job was submitted, so the caller should treat it as lost and retry
    rather than keep polling."""
    _verify(authorization)
    return _job_status(job_id)


@worker_app.post("/cancel-job/{job_id}")
def cancel_job(job_id: str, authorization: str = Header(default=None)):
    """Abandon a job: flag it for cooperative cancellation and forget it.

    Called by a master that gave up (timeout). Without it the trial runs to completion for a
    result nobody will collect, holding a worker slot for up to _JOBS_MAX_ORPHAN_AGE — so a
    timeout REMOVED capacity instead of freeing it (see _JOB_CTL)."""
    _verify(authorization)
    return _cancel_job(job_id)


@worker_app.post("/cache/prune")
def cache_prune(req: PruneReq, authorization: str = Header(default=None)):
    """Delete rel_paths the master's CURRENT manifest no longer lists (leftovers from a rebuild/
    compaction, e.g. old screener metric_store fragments) — the reverse of ``/cache/push``."""
    _verify(authorization)
    return cache_sync.prune_paths(req.rel_paths)


@worker_app.post("/secrets")
def set_secrets(req: SecretsReq, authorization: str = Header(default=None)):
    """Upsert credential app-settings (FMP_API_KEY, finnhub_api_key) into THIS worker's ba2_common
    DB so its hermetic trials resolve them via get_app_setting.

    The worker is otherwise DB-less for app data, but ``_enter_backend`` configured ba2_common at the
    worker's on-disk default DB at startup; trial pool workers (``_worker_init``) point at the SAME
    file. Writing the keys here persists them across restarts (unlike env, which a self-update drops
    — the recurring 'FMP API key not configured' on remote trials). Idempotent upsert; values are
    never logged.
    """
    _verify(authorization)
    from sqlmodel import Session, select
    from ba2_common.core.db import get_engine, init_db
    from ba2_common.core.models import AppSetting
    n = 0
    try:
        init_db()  # ensure the AppSetting table exists in the worker's (possibly fresh) DB
        with Session(get_engine()) as s:
            for k, v in (req.settings or {}).items():
                if not v:
                    continue
                row = s.exec(select(AppSetting).where(AppSetting.key == k)).first()
                if row:
                    row.value_str = v
                    s.add(row)
                else:
                    s.add(AppSetting(key=k, value_str=v))
                n += 1
            s.commit()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"set_secrets failed: {e!r}")
    return {"set": n, "keys": sorted((req.settings or {}).keys())}


# Engine ids (per Python object identity) whose tables we've already ensured exist, so
# _ensure_tables() only pays the create_all()/has_table() round-trips once per distinct engine
# rather than once per request.
_ensured_engine_ids: set = set()


def _ensure_tables(session) -> None:
    """Create the app's tables on ``session``'s engine if we haven't already done so for it.

    ``Strategy.metadata`` **is** ``app.models.database.Base.metadata`` — every model in this app
    (``Worker``, ``TaskQueue``, ``Dataset``, ``Strategy``, ``StrategyOptimization``, ``Backtest``,
    ...) is registered on that ONE shared Base, so this call creates the entire app schema
    (~14 tables), not just the 3 sync tables the callers care about. That's harmless — creating
    tables that already exist is a no-op — but it's a `has_table` round-trip per table, so it's
    cached per engine (by ``id()``) rather than repeated on every request.
    """
    engine = session.get_bind()
    if id(engine) in _ensured_engine_ids:
        return
    from app.models.strategy import Strategy
    import app.models  # noqa: F401 — registers StrategyOptimization/Backtest alongside Strategy
    Strategy.metadata.create_all(bind=engine)
    _ensured_engine_ids.add(id(engine))


def _sync_session():
    """Lazily bind SessionLocal to app.models.database's session factory, and ensure the tables
    exist on whatever engine SessionLocal is CURRENTLY bound to (see ``_ensure_tables``).

    Module-level (not per-call) SessionLocal binding so tests can monkeypatch ``ws.SessionLocal``
    to an isolated engine. ``_ensure_tables`` is called (and, per engine, only actually runs
    ``create_all`` once) rather than skipped after the first bind, because a test can point
    ``SessionLocal`` at a freshly-created, schema-less engine (see
    test_sync_strategy_inserts_row's ``importlib.reload`` + ``monkeypatch.setattr(ws,
    "SessionLocal", ...)`` dance), and that DIFFERENT engine needs its tables created too — the
    per-engine cache in ``_ensure_tables`` handles exactly this: a new engine (new ``id()``) still
    gets ensured, a repeat call on the same engine is a no-op.
    """
    global SessionLocal
    if SessionLocal is None:
        from app.models.database import SessionLocal as _SessionLocal
        SessionLocal = _SessionLocal
    session = SessionLocal()
    _ensure_tables(session)
    return session


def _do_sync(model_cls, payload: dict, parent_fk: Optional[dict] = None) -> dict:
    """Shared body for the three ``/sync/*`` endpoints: session lifecycle, upsert, error handling.

    Mirrors ``/secrets``'s try/except/finally shape (open session -> upsert -> HTTPException on
    any failure -> always close), so a malformed payload or a DB-level error surfaces as a clean
    500 with a logged, descriptive detail instead of propagating as an unhandled exception into
    FastAPI's default handler.
    """
    s = None
    try:
        s = _sync_session()
        row = sync_receiver.upsert_by_natural_key(s, model_cls, payload, parent_fk=parent_fk)
        return {"ok": True, "skipped": row is None}
    except Exception as e:  # noqa: BLE001
        logger.error("sync failed for %s: %s", model_cls.__name__, e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"sync failed: {e!r}")
    finally:
        if s is not None:
            s.close()


@worker_app.post("/sync/strategy")
def sync_strategy(req: SyncRowReq, authorization: str = Header(default=None)):
    """Upsert a replicated Strategy row, matched by (name, created_at)."""
    _verify(authorization)
    from app.models.strategy import Strategy
    return _do_sync(Strategy, req.model_dump())


@worker_app.post("/sync/optimization")
def sync_optimization(req: SyncRowReq, authorization: str = Header(default=None)):
    """Upsert a replicated StrategyOptimization row, matched by (name, created_at).

    ``req`` carries strategy_name/strategy_created_at alongside the master's strategy_id
    so the FK can be remapped to this worker's own local Strategy id (see sync_receiver).
    ``strategy_id`` is NOT NULL on this table, so a missing parent means
    ``upsert_by_natural_key`` skips the write (returns None) rather than raising — reported
    back as ``{"skipped": true}``, still HTTP 200 (expected/benign, self-heals on retry).
    """
    _verify(authorization)
    from app.models.strategy import Strategy
    from app.models.strategy_optimization import StrategyOptimization
    return _do_sync(
        StrategyOptimization, req.model_dump(),
        parent_fk={"strategy_id": (Strategy, "strategy_name", "strategy_created_at")},
    )


@worker_app.post("/sync/backtest")
def sync_backtest(req: SyncRowReq, authorization: str = Header(default=None)):
    """Upsert a replicated Backtest row, matched by (name, created_at).

    ``req`` carries both strategy_name/strategy_created_at and
    optimization_name/optimization_created_at so both FKs remap to local ids. Both FK columns
    on this table are nullable, so a missing parent resolves to None and the row is still
    written (unlike StrategyOptimization.strategy_id) — ``skipped`` should always be false here
    in practice, but the return is handled the same way for consistency with the other two
    endpoints.
    """
    _verify(authorization)
    from app.models.strategy import Strategy
    from app.models.strategy_optimization import StrategyOptimization
    from app.models.backtest import Backtest
    return _do_sync(
        Backtest, req.model_dump(),
        parent_fk={
            "strategy_id": (Strategy, "strategy_name", "strategy_created_at"),
            "optimization_id": (StrategyOptimization, "optimization_name", "optimization_created_at"),
        },
    )


def _shutdown_pool_for_restart() -> None:
    """Pre-restart cleanup: stop the trial pool FIRST so its management thread cannot respawn
    replacement children between restart_now's _kill_children() and execv — the race that
    orphaned ~10 spawn children (2-4.8GB each) on every self-update and eventually depleted
    the worker box (WinError 1450).

    Also clears _POOL_FACTORY so any /run-trial handler mid-flight when this fires — and
    whose BrokenProcessPool lands during the shutdown window — sees _rebuild_pool() no-op
    (fails fast with retryable=True) instead of spawning a fresh pool that's just going to be
    killed again by _kill_children() a moment later. Without this, concurrent in-flight
    handlers would race to rebuild, each spawning a full torch/numpy child process only to
    have it killed immediately — a rebuild-then-kill storm (observed 15-20 cycles deep) before
    the process finally execv's. This does NOT wait for the pool to drain — restart stays fast,
    it just stops the pool from being pointlessly resurrected while restarting."""
    global _POOL, _POOL_FACTORY
    with _POOL_LOCK:
        pool, _POOL = _POOL, None
        _POOL_FACTORY = None
    if pool is not None:
        try:
            pool.shutdown(wait=False, cancel_futures=True)
        except Exception as e:  # noqa: BLE001 — cleanup must never block the restart
            logger.warning("pre-restart pool shutdown failed (continuing): %r", e)


@worker_app.post("/update")
def update(authorization: str = Header(default=None)):
    """git pull + reinstall (if non-editable) + restart this worker process."""
    _verify(authorization)
    report = self_update.perform_update()
    if not report.get("ok"):
        raise HTTPException(status_code=500, detail=f"update failed: {report.get('git_pull')}")
    self_update.schedule_restart(delay=2.0, on_before_restart=_shutdown_pool_for_restart)
    return {"restart": "scheduled", "version": report.get("version"), "git_pull": report.get("git_pull")}


def _sweep_orphaned_spawn_children() -> None:
    """Kill ORPHANED multiprocessing-spawn python processes left by force-kills / crashed
    restarts (their cmdline is `... -c "from multiprocessing.spawn import spawn_main;
    spawn_main(parent_pid=N, ...)"` with a parent that no longer exists). Each orphan holds a
    full trial working set (2-4.8GB); ~20 of them depleted the 64GB worker box on 2026-07-09.
    Conservative: only touches processes matching that exact cmdline signature whose stated
    parent_pid is gone."""
    try:
        import re
        import psutil
    except ImportError:
        return
    killed = []
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            if "python" not in (proc.info["name"] or "").lower():
                continue
            cmd = " ".join(proc.info["cmdline"] or [])
            m = re.search(r"multiprocessing\.spawn import spawn_main.*?parent_pid=(\d+)", cmd)
            if not m:
                continue
            parent_pid = int(m.group(1))
            if psutil.pid_exists(parent_pid):
                continue  # parent alive -> legitimate worker of another process
            proc.kill()
            killed.append(proc.info["pid"])
        except (psutil.Error, ValueError):
            continue
    if killed:
        logger.warning("startup sweep: killed %d orphaned spawn child(ren): %s",
                       len(killed), killed)
        print(f">> swept {len(killed)} orphaned trial worker(s) from previous runs: {killed}")


def run_worker_server(host: str, port: int, password: str, n_workers: int) -> None:
    """Initialise the trial pool + start the uvicorn server. Called by ``ba2-test worker``."""
    import multiprocessing as _mp

    import uvicorn

    from app.services.strategy_optimization_handler import (
        _BACKEND_DIR, _WORKER_ENV_KEYS, _worker_init,
    )

    global _PASSWORD, _CAPACITY, _CAPACITY_MAX, _POOL, _POOL_FACTORY
    if not password:
        raise SystemExit("ba2-test worker: --password (or $BA2_WORKER_PASSWORD) is required.")
    _PASSWORD = password
    _CAPACITY = max(1, n_workers)
    _CAPACITY_MAX = _CAPACITY
    # Hermetic trials run cache-only, so provider keys aren't required here; mirror any that
    # happen to be set (harmless) so a non-hermetic edge still resolves them.
    env = {k: os.environ[k] for k in _WORKER_ENV_KEYS if os.environ.get(k)}

    def _make_pool() -> ProcessPoolExecutor:
        return ProcessPoolExecutor(
            max_workers=_CAPACITY, mp_context=_mp.get_context("spawn"),
            initializer=_worker_init, initargs=(_BACKEND_DIR, env),
        )

    _install_orchestration_file_logging()
    _sweep_orphaned_spawn_children()
    _POOL_FACTORY = _make_pool
    _POOL = _make_pool()
    # Self-throttling: reclaim this worker's own pool memory when the box is starved. Daemon so
    # it never holds up shutdown; see _memory_watchdog for why this cannot live on the master.
    threading.Thread(target=_memory_watchdog, name="memory-watchdog", daemon=True).start()
    logger.info("memory watchdog armed: poll %.0fs, floor %.0f%% free, %d-slot pool",
                _MEM_POLL_S, _MEM_FLOOR_PCT, _CAPACITY)
    logger.info("worker server: %d trial slots, listening on %s:%d", _CAPACITY, host, port)
    print(f">> BA2 worker server: {_CAPACITY} slots, http://{host}:{port}  "
          f"(version {self_update.get_version_info().get('git_commit')})")
    try:
        uvicorn.run(worker_app, host=host, port=port, log_level="info")
    finally:
        if _POOL is not None:
            _POOL.shutdown(wait=False, cancel_futures=True)
