"""Master-side client for talking to a remote worker's HTTP server (push model).

Every call authenticates with that worker's own password. A *worker* here is a plain dict
``{id, name, url, password, capacity}`` resolved once from the Worker row (so it can be handed to
dispatcher threads without dragging a SQLAlchemy session across threads).
"""

from __future__ import annotations

import logging
import time
from typing import Callable, Optional

import httpx

from app.services import cache_sync

logger = logging.getLogger(__name__)


def _base(worker: dict) -> str:
    return str(worker["url"]).rstrip("/")


def _headers(worker: dict) -> dict:
    return {"Authorization": f"Bearer {worker.get('password') or ''}"}


def health(worker: dict, timeout: float = 10.0) -> dict:
    with httpx.Client(timeout=timeout) as c:
        r = c.get(f"{_base(worker)}/health", headers=_headers(worker))
        r.raise_for_status()
        return r.json()


def version(worker: dict, timeout: float = 10.0) -> dict:
    with httpx.Client(timeout=timeout) as c:
        r = c.get(f"{_base(worker)}/version", headers=_headers(worker))
        r.raise_for_status()
        return r.json()


def memory(worker: dict, timeout: float = 15.0) -> dict:
    """On-demand RSS snapshot of *worker* (server + trial-pool children) — for diagnosing a
    slow/hung remote worker without SSH/RDP access."""
    with httpx.Client(timeout=timeout) as c:
        r = c.get(f"{_base(worker)}/diag/memory", headers=_headers(worker))
        r.raise_for_status()
        return r.json()


def logs_list(worker: dict, timeout: float = 15.0) -> dict:
    """List *worker*'s log filenames (``{dir, files}``)."""
    with httpx.Client(timeout=timeout) as c:
        r = c.get(f"{_base(worker)}/logs/list", headers=_headers(worker))
        r.raise_for_status()
        return r.json()


def logs(worker: dict, file: str = "app.log", tail_lines: int = 500,
         timeout: float = 30.0) -> dict:
    """Tail *file* from *worker*'s LOGS_DIR (``{file, total_lines, lines}``)."""
    with httpx.Client(timeout=timeout) as c:
        r = c.get(f"{_base(worker)}/logs", headers=_headers(worker),
                  params={"file": file, "tail_lines": tail_lines})
        r.raise_for_status()
        return r.json()


def quick_status(worker: dict, timeout: float = 1.5) -> tuple:
    """Live one-shot reachability probe. Returns ``(status, capacity)`` where status is
    ``"online"``/``"offline"`` and capacity is the worker's reported slot count (or None).

    Never raises — a timeout/unreachable host maps to ``("offline", None)``. Used by the
    dashboard/workers API to show the TRUE badge instead of the last value the DB happened to
    store (the CLI/distributed path talks to workers directly and never writes status back)."""
    try:
        h = health(worker, timeout=timeout)
        cap = h.get("capacity")
        return ("online", int(cap) if cap else None)
    except Exception:  # noqa: BLE001 — unreachable / timeout / auth -> offline
        return ("offline", None)


class WorkerJobLost(Exception):
    """A submitted job's id came back 404 on poll — the worker doesn't know it anymore, almost
    always because the worker process restarted (self-update, crash, manual kill) after the
    submit and before the trial finished. The trial's outcome is simply unknowable now; the
    caller should treat this like any other worker failure (existing call sites already catch
    broad ``Exception`` and requeue/retry — see distributed_eval.py's ``_dispatch_remote``)."""


class WorkerJobStalled(Exception):
    """A submitted job stopped making progress (or never started) well before its timeout.

    Distinct from ``WorkerJobLost``: the worker still HAS the job and keeps answering "running".
    Detected from the trial's own per-bar heartbeat rather than from wall clock alone, so it
    fires in minutes instead of burning the full ``trial_timeout``.

    Why this exists (2026-08-06, opt 251): remote150 accepted 6 trials onto a pool already
    saturated by orphaned children. They queued and never ran. ``/job-status`` reported a bare
    "running" for each, which is indistinguishable from healthy work, so the master waited the
    full 10800s x 3 retries per slot — the grid sat idle ~9h for zero trials. Callers treat this
    like any other worker failure and requeue elsewhere."""


# Fail a job that was ACCEPTED but never reported a single bar. This is the saturated-pool /
# never-scheduled case, where waiting longer cannot help — the trial is not running at all.
# Generous enough to cover a slow cold start (spawn + imports + first cache reads).
_NEVER_STARTED_GRACE = 420.0
# Fail a job that started and then went quiet. Must stay comfortably above the trial's own
# heartbeat cadence (_CANCEL_CHECK_INTERVAL_S, seconds) and above any legitimate long gap
# between throttled progress callbacks.
_NO_PROGRESS_TIMEOUT = 900.0


def resize_pool(worker: dict, workers: int, timeout: float = 120.0) -> dict:
    """Ask *worker* to size its trial pool to *workers* slots for the coming job.

    Generous timeout: the worker tears its pool down and respawns, which on a box that has
    been paging can take a while. Returns the worker's verdict -- a refusal (trials in
    flight) is a normal answer, not an error.
    """
    with httpx.Client(timeout=timeout) as c:
        r = c.post(f"{_base(worker)}/pool/resize", headers=_headers(worker),
                   json={"workers": int(workers)})
        r.raise_for_status()
        return r.json()


def cancel_job(worker: dict, job_id: str, timeout: float = 10.0) -> dict:
    """Best-effort: tell *worker* to abandon *job_id*.

    Called when the master gives up on a trial. Without it the worker keeps computing a result
    nobody will collect, holding a slot until its 6h orphan sweep -- so a timeout REMOVED
    capacity instead of freeing it, which is how the Senate S5 grid went 12 timeouts straight
    into "worker dead" (opt 226, 2026-07-28).

    NEVER raises: this runs on the failure path, and a cancel that fails must not mask the
    original error the caller is already handling.
    """
    try:
        with httpx.Client(timeout=timeout) as c:
            r = c.post(f"{_base(worker)}/cancel-job/{job_id}", headers=_headers(worker))
            if r.status_code == 404:
                return {"cancelled": False, "reason": "unknown job"}
            r.raise_for_status()
            return r.json()
    except Exception as e:  # noqa: BLE001 — see docstring
        return {"cancelled": False, "reason": repr(e)}


def _submit_and_poll(worker: dict, submit_path: str, payload: dict, timeout: float,
                     poll_interval: float = 2.0) -> dict:
    """POST *payload* to *submit_path* for a job_id, then GET /job-status/{job_id} every
    ``poll_interval`` seconds until it reports done. Raises WorkerJobLost immediately (no
    waiting) if the worker no longer recognizes the job_id — the fast-failure case a blocking
    request could never distinguish from "still working" (2026-07-19: replaced the old
    single-long-blocking-POST design specifically to fix this — see worker_server.py's
    module docstring for the full incident this was written to prevent).

    Each individual HTTP call (submit, each poll) uses its own SHORT timeout — only the overall
    *timeout* budget is long, so a single dropped connection fails fast and gets retried on the
    next poll tick instead of hanging for the full budget.
    """
    call_timeout = min(30.0, timeout)
    with httpx.Client(timeout=call_timeout) as c:
        r = c.post(f"{_base(worker)}{submit_path}", headers=_headers(worker), json=payload)
        r.raise_for_status()
        job_id = r.json()["job_id"]

    deadline = time.monotonic() + timeout
    status_url = f"{_base(worker)}/job-status/{job_id}"
    started_at = time.monotonic()
    last_bars = 0
    last_bars_at = started_at
    with httpx.Client(timeout=call_timeout) as c:
        while True:
            now = time.monotonic()
            if now >= deadline:
                # Abandon it on the worker too, or it runs to completion for nobody and keeps
                # its slot for up to 6h -- see cancel_job.
                cancel_job(worker, job_id)
                raise TimeoutError(f"worker {worker.get('name')} job {job_id} did not "
                                   f"complete within {timeout:.0f}s")
            r = c.get(status_url, headers=_headers(worker))
            if r.status_code == 404:
                raise WorkerJobLost(f"worker {worker.get('name')} job {job_id} unknown "
                                    f"(worker likely restarted mid-job)")
            r.raise_for_status()
            body = r.json()
            if body["status"] == "done":
                return body["result"]

            # HEARTBEAT CHECK. `bars` is the trial's own per-bar progress counter (see
            # strategy_optimization_handler._cancel_progress_cb). A worker too old to report it
            # omits the key entirely -> bars is None -> both checks are skipped and behaviour is
            # exactly as before, so a version-skewed worker is never failed for our new field.
            bars = body.get("bars")
            if isinstance(bars, int) and bars >= 0:
                if bars > last_bars:
                    last_bars, last_bars_at = bars, now
                elif bars == 0 and (now - started_at) >= _NEVER_STARTED_GRACE:
                    cancel_job(worker, job_id)
                    raise WorkerJobStalled(
                        f"worker {worker.get('name')} job {job_id} accepted but never started "
                        f"({now - started_at:.0f}s, no bars) — pool likely saturated")
                elif bars > 0 and (now - last_bars_at) >= _NO_PROGRESS_TIMEOUT:
                    cancel_job(worker, job_id)
                    raise WorkerJobStalled(
                        f"worker {worker.get('name')} job {job_id} stalled at bar {bars} "
                        f"for {now - last_bars_at:.0f}s")
            time.sleep(poll_interval)


def run_trial(worker: dict, config: dict, fitness_metric: str, timeout: float = 1800.0) -> dict:
    """Submit ONE trial to the worker and poll until it returns its ``{ok,fitness,trades,error,
    fatal}`` summary (same external contract as before — callers don't need to change).

    Sends the master's ``cache_root`` so the worker can remap absolute cache paths embedded in the
    config (screener_store, options_cache_db, ...) to ITS OWN local cache — the master and worker
    don't share a filesystem path.
    """
    from ba2_common.config import CACHE_FOLDER
    from app.services.backtest.backtest_db import _inmem_trades_enabled
    payload = {"config": config, "fitness_metric": fitness_metric, "cache_root": CACHE_FOLDER,
              # Propagate the sql-less "dict trades" flag so distributed trials use the
              # SAME backend (and thus byte-identical economics) as the master.
              "inmem_trades": _inmem_trades_enabled()}
    return _submit_and_poll(worker, "/submit-trial", payload, timeout)


def run_trial_full(worker: dict, config: dict, fitness_metric: str, timeout: float = 1800.0) -> dict:
    """Like ``run_trial`` but returns ``{ok, results:{...}}`` (the full backtest results dict)
    instead of the trimmed summary — an operator/debug tool for diagnosing a fitness mismatch
    against a master-side result field-by-field, and for remote top-N persist re-runs, not the
    hot GA path."""
    from ba2_common.config import CACHE_FOLDER
    from app.services.backtest.backtest_db import _inmem_trades_enabled
    payload = {"config": config, "fitness_metric": fitness_metric, "cache_root": CACHE_FOLDER,
              "inmem_trades": _inmem_trades_enabled()}
    return _submit_and_poll(worker, "/submit-trial-full", payload, timeout)


def push_cache(worker: dict, log: Callable[[str], None] = logger.info) -> dict:
    """Diff the master's cache against the worker's manifest and stream the missing files as ONE
    tar, THEN prune anything the worker has that the master's CURRENT manifest no longer lists
    (leftovers from a local rebuild/compaction, e.g. old screener metric_store fragments — see
    ``cache_sync.diff_stale``). Returns ``{pushed, pruned, ...}``.
    """
    base, headers = _base(worker), _headers(worker)
    with httpx.Client(timeout=60.0) as c:
        r = c.get(f"{base}/cache/manifest", headers=headers)
        r.raise_for_status()
        remote = r.json()
    local = cache_sync.build_manifest()
    missing = cache_sync.diff_missing(local["files"], remote)
    res = {"pushed": 0, "extracted": 0}
    if not missing:
        log(f"cache push -> {worker['name']}: already in sync ({local['count']} files)")
    else:
        log(f"cache push -> {worker['name']}: streaming {len(missing)} file(s)...")
        stream = cache_sync.iter_tar(missing, local["root"])
        with httpx.Client(timeout=None) as c:  # large upload: no read timeout
            r = c.post(f"{base}/cache/push", headers=headers, content=stream)
            r.raise_for_status()
            res = {"pushed": len(missing), **r.json()}
        log(f"cache push -> {worker['name']}: {res}")

    stale = cache_sync.diff_stale(local["files"], remote)
    if stale:
        log(f"cache prune -> {worker['name']}: removing {len(stale)} stale leftover file(s)...")
        with httpx.Client(timeout=120.0) as c:
            r = c.post(f"{base}/cache/prune", headers=headers, json={"rel_paths": stale})
            r.raise_for_status()
            prune_res = r.json()
        log(f"cache prune -> {worker['name']}: {prune_res}")
        res["pruned"] = prune_res.get("pruned", 0)
    else:
        res["pruned"] = 0
    return res


def check_cache_integrity(worker: dict, timeout: float = 600.0) -> dict:
    """Deep, content-hash-based comparison of the master's cache against *worker*'s.

    Unlike ``push_cache`` (fast, size-only — the normal per-job pre-flight path), this reads +
    CRC32-checksums every file on BOTH ends to catch the drift ``(rel_path, size)`` structurally
    can't see: a rebuild that rewrites a file's content at its OLD byte size. Slow (full cache
    read on both machines) — meant for a periodic/manual health check, not the hot GA path.
    CRC32, not sha256: this is corruption/staleness detection, not a security boundary, so the
    much faster non-cryptographic checksum is the right tradeoff at cache-wide scale.

    Returns ``{ok, missing, stale, content_mismatch, local_count, remote_count}`` — ``ok`` is
    True only when all three lists are empty.
    """
    base, headers = _base(worker), _headers(worker)
    with httpx.Client(timeout=timeout) as c:
        r = c.get(f"{base}/cache/manifest", headers=headers, params={"with_hash": "true"})
        r.raise_for_status()
        remote = r.json()
    local = cache_sync.build_manifest(with_hash=True)
    missing = cache_sync.diff_missing(local["files"], remote)
    stale = cache_sync.diff_stale(local["files"], remote)
    mismatch = cache_sync.diff_content_mismatch(local["files"], remote)
    return {
        "ok": not (missing or stale or mismatch),
        "missing": missing, "stale": stale, "content_mismatch": mismatch,
        "local_count": local["count"], "remote_count": remote["count"],
    }


def push_secrets(worker: dict, settings: dict, log: Callable[[str], None] = logger.info) -> dict:
    """Push credential app-settings (FMP_API_KEY, finnhub_api_key) into the DB-less worker so its
    hermetic trials resolve them via get_app_setting.

    The worker keeps no app-settings DB of its own, and a self-update restart drops env-only keys —
    the recurring 'FMP API key not configured' on remote trials. Writing them to the worker's DB
    (persisted on disk) survives restarts. Best-effort: never blocks a run; values are never logged.
    """
    settings = {k: v for k, v in (settings or {}).items() if v}
    if not settings:
        return {"set": 0}
    try:
        with httpx.Client(timeout=15.0) as c:
            r = c.post(f"{_base(worker)}/secrets", headers=_headers(worker),
                       json={"settings": settings})
            r.raise_for_status()
            res = r.json()
        log(f"secrets push -> {worker['name']}: set {res.get('set')} key(s) {res.get('keys')}")
        return res
    except Exception as e:  # noqa: BLE001 — a missing key surfaces as the provider's own clear error
        log(f"secrets push -> {worker['name']} failed (non-fatal): {e}")
        return {"set": 0, "error": repr(e)}


def ensure_synced(worker: dict, master_version: Optional[str],
                  log: Callable[[str], None] = logger.info, max_wait: float = 300.0) -> bool:
    """Make the worker run a compatible build: if its app version differs from the master's,
    trigger its /update and wait (polling /version) until it matches. Returns True if usable,
    False to exclude.

    Compatibility is keyed on ``app_version`` (not the git commit) so that ordinary pushes —
    docs, scratch scripts, unrelated fixes — don't force every connected worker to self-update
    mid-run. A worker only needs to re-sync when the app version is intentionally bumped.
    """
    try:
        wv = version(worker).get("app_version")
    except Exception as e:  # noqa: BLE001
        log(f"worker {worker['name']} unreachable ({e}); excluding")
        return False
    if not master_version or not wv or wv == master_version:
        return True
    log(f"worker {worker['name']} version {wv} != master {master_version}; updating + waiting...")
    try:
        with httpx.Client(timeout=120.0) as c:
            c.post(f"{_base(worker)}/update", headers=_headers(worker))
    except Exception as e:  # noqa: BLE001 — the restart may drop the connection; that's expected
        logger.debug(f"update call returned/dropped (expected on restart): {e}")
    deadline = time.time() + max_wait
    while time.time() < deadline:
        time.sleep(3.0)
        try:
            if version(worker).get("app_version") == master_version:
                log(f"worker {worker['name']} updated to {master_version}")
                return True
        except Exception:  # noqa: BLE001 — still restarting
            continue
    log(f"worker {worker['name']} did not converge to {master_version} in {max_wait:.0f}s; excluding")
    return False
