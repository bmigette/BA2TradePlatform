"""Distributed batch evaluator (PUSH model) — master dispatches trials to local + remote workers.

Drops in for the strategy optimizer's local ``ProcessPoolExecutor`` path. Trials for a generation
are queued on a per-optimization ``TrialBroker``; two kinds of threads drain it:
  * LOCAL consumer threads run each trial through the master's process pool (the master is also a
    worker);
  * REMOTE dispatcher threads (one per worker slot = the worker's reported capacity) PUSH each
    trial to a worker over HTTP (``worker_client.run_trial``) and post the result back.
On a worker error the dispatcher REQUEUES the trial (a local consumer or another worker picks it
up) and backs off; after repeated failures it gives up on that worker — graceful degradation to
local-only.

Pre-flight per selected worker: ``ensure_synced`` (auto-update+wait so it runs a compatible build,
matched on app version) then ``push_cache`` (stream the missing cache as one tar). Workers
that can't be reached/synced are dropped with a warning.

Re-admission: a worker that failed pre-flight (or gave up mid-run) is not excluded for the rest
of the job. Every ``n_consumers`` individuals completed, one background re-check re-runs the same
pre-flight against the down worker list; a worker that now syncs is re-admitted with fresh
dispatcher threads. The check runs off the result-consuming thread (non-blocking, one in flight
at a time) so a still-dead worker's timeout never stalls trial throughput.

Determinism: a trial config is hermetic + seeded, so its fitness is independent of WHERE it ran;
``execute_jobs`` reassembles results by the GA's input index. With no workers selected the caller
keeps the plain local-pool path (byte-identical to before).
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable, Iterator, List, Optional, Tuple

from app.services import worker_client
from app.services.trial_broker import TrialBroker

logger = logging.getLogger(__name__)

# A trial job as built by the handler: (index, decoded_flat, trial_key, config).
Job = Tuple[int, dict, str, dict]

# A dispatcher gives up on a worker after this many consecutive run_trial failures (dead box).
_MAX_WORKER_FAILURES = 3


def _log_memory_diagnostics(log, context: str) -> None:
    """Best-effort process/system memory snapshot, logged when a local trial dies unexpectedly
    (e.g. BrokenProcessPool) so a recurrence leaves a real data point instead of just the bare
    exception repr. Never raises — diagnostics must not mask the original failure."""
    try:
        import os
        import psutil
        p = psutil.Process(os.getpid())
        rss_mb = p.memory_info().rss / (1024 * 1024)
        vm = psutil.virtual_memory()
        log(f"{context}: master RSS={rss_mb:.0f}MB, system available="
            f"{vm.available / (1024 * 1024):.0f}MB / {vm.total / (1024 * 1024):.0f}MB "
            f"({vm.percent:.1f}% used)")
    except Exception as e:  # noqa: BLE001 — diagnostics are best-effort, never fatal
        log(f"{context}: memory diagnostics unavailable ({e!r})")


class DistributedEvaluator:
    """Bridges the GA batch loop to local + remote workers via a per-optimization TrialBroker.

    *submit_pool* is the master's ``ProcessPoolExecutor`` (local consumers run trials through it).
    *pool_factory*, if given, rebuilds an equivalent fresh pool (same max_workers/mp_context/
    initializer/initargs) when the local pool dies mid-run (``BrokenProcessPool`` — one worker
    crashed, e.g. from a transient MemoryError, which permanently breaks a
    ``concurrent.futures.ProcessPoolExecutor``: EVERY subsequent ``.submit()`` on it raises
    immediately). Without a factory, local consumers degrade to remote-only for the rest of the
    run (the previous behaviour) instead of recovering.
    *workers* is a list of resolved worker dicts ``{id,name,url,password,capacity}``. *master_version*
    is the master's app version (workers are version-matched to it before use).
    """

    def __init__(self, submit_pool, fitness_metric: str, n_consumers: int,
                 optimization_id: Any, workers: Optional[List[dict]] = None,
                 master_version: Optional[str] = None, log=logger.warning,
                 requeue_timeout: float = 1800.0,
                 pool_factory: Optional[Callable[[], Any]] = None):
        self.pool = submit_pool
        self._pool_factory = pool_factory
        self._pool_lock = threading.Lock()  # guards self.pool reads/recreation across consumers
        self.fitness_metric = fitness_metric
        self.n_consumers = max(1, n_consumers)
        self.optimization_id = optimization_id
        self.workers = workers or []
        self.master_version = master_version
        self.log = log
        self.requeue_timeout = requeue_timeout  # safety net: re-queue a trial whose worker vanished
        self.broker = TrialBroker()  # OWN broker (per-optimization isolation; queue is max_workers=4)
        self._stop = threading.Event()
        self._threads: List[threading.Thread] = []
        self._active_workers: List[dict] = []
        self._down_workers: List[dict] = []  # excluded at pre-flight or gave up mid-run; retried
        self._worker_lock = threading.Lock()  # guards _active_workers/_down_workers mutations
        self._recheck_lock = threading.Lock()  # at most one re-admission pre-flight in flight
        self._secrets: dict = {}

    # -- lifecycle ---------------------------------------------------------------------------
    def start(self) -> None:
        # Credential app-settings to mirror onto each (DB-less) worker so its hermetic trials
        # resolve them via get_app_setting — survives the worker's self-update restart, which drops
        # env-only keys (the recurring 'FMP API key not configured' on remote trials).
        secrets = self._resolve_master_secrets()
        self._secrets = secrets  # kept for later re-admission pre-flight retries
        # Pre-flight: version-match + cache-push each selected worker; drop the unusable ones.
        # Workers with no password are a static misconfiguration (never resolves mid-run) and are
        # excluded outright; anything else is a transient failure candidate, tracked in
        # _down_workers so _recheck_down_workers can retry it periodically.
        for w in self.workers:
            if not w.get("password"):
                self.log(f"worker {w.get('name')} has no password configured; excluding")
                continue
            if self._preflight_worker(w, secrets):
                self._active_workers.append(w)
            else:
                self._down_workers.append(w)

        # Local consumers (master-as-worker).
        for i in range(self.n_consumers):
            self._spawn(self._consume_local, f"local-trial-consumer-{i}")
        # Remote dispatchers: one thread per worker slot.
        remote_slots = 0
        for w in self._active_workers:
            remote_slots += self._spawn_remote_dispatchers(w)
        self.log(f"distributed evaluator (opt {self.optimization_id}): {self.n_consumers} local + "
                 f"{remote_slots} remote slot(s) across {len(self._active_workers)} worker(s)")
        # Surface the engaged fleet in the UI: each remote worker's active_jobs_count = its slot
        # count, the local worker's = the local consumer count. (The CLI path never updates these,
        # so without this the dashboard shows "0 jobs" + "offline" while a run is in flight.)
        self._report_fleet_state(active=True)

    def _resolve_master_secrets(self) -> dict:
        """Read the credential app-settings to mirror onto workers from the MASTER's DB. Keys absent
        on the master are simply omitted (the worker keeps whatever it had). Never raises."""
        out: dict = {}
        try:
            from ba2_common.config import get_app_setting
            for k in ("FMP_API_KEY", "finnhub_api_key"):
                v = get_app_setting(k)
                if v:
                    out[k] = v
        except Exception as e:  # noqa: BLE001 — best-effort; a missing key fails loudly at the worker
            self.log(f"master secret resolution failed (non-fatal): {e}")
        return out

    def _report_fleet_state(self, active: bool) -> None:
        """Write engaged-slot counts (and online status) for the participating workers to the DB so
        the dashboard/workers panels reflect reality. Best-effort: never raises."""
        try:
            from datetime import datetime
            from app.models.database import SessionLocal
            from app.models import Worker
            db = SessionLocal()
            try:
                for w in self._active_workers:
                    row = db.query(Worker).filter(Worker.id == w.get("id")).first()
                    if not row:
                        continue
                    row.active_jobs_count = int(w.get("capacity") or 1) if active else 0
                    if active:
                        row.status = "online"
                        row.last_heartbeat = datetime.utcnow()
                local = db.query(Worker).filter(Worker.is_local == True).first()  # noqa: E712
                if local:
                    local.active_jobs_count = self.n_consumers if active else 0
                db.commit()
            finally:
                db.close()
        except Exception as e:  # noqa: BLE001 — fleet reporting must never affect the run
            self.log(f"fleet-state report (active={active}) skipped: {e}")

    def _spawn(self, target, name: str) -> None:
        t = threading.Thread(target=target, name=name, daemon=True)
        t.start()
        self._threads.append(t)

    def _preflight_worker(self, w: dict, secrets: dict) -> bool:
        """Version-match + cache/secrets-push one worker. Returns True if it's usable now."""
        try:
            if not worker_client.ensure_synced(w, self.master_version, log=self.log):
                return False
            worker_client.push_cache(w, log=self.log)
            worker_client.push_secrets(w, secrets, log=self.log)
            try:
                w["capacity"] = max(1, int(worker_client.health(w).get("capacity") or 1))
            except Exception:  # noqa: BLE001 — fall back to 1 slot if /health didn't report
                w["capacity"] = max(1, int(w.get("capacity") or 1))
            return True
        except Exception as e:  # noqa: BLE001 — a bad worker must never abort the run
            self.log(f"worker {w.get('name')} pre-flight failed: {e}; excluding")
            return False

    def _spawn_remote_dispatchers(self, w: dict) -> int:
        cap = max(1, int(w.get("capacity") or 1))
        for i in range(cap):
            self._spawn(lambda w=w, i=i: self._dispatch_remote(w), f"remote-{w['name']}-{i}")
        return cap

    def _recheck_down_workers(self) -> None:
        """Re-run pre-flight against previously-excluded/failed workers; re-admit any that
        recover. Best-effort and never raises — called off a background thread so a still-dead
        worker's connect timeout can't stall trial throughput."""
        with self._worker_lock:
            candidates = list(self._down_workers)
        for w in candidates:
            if self._stop.is_set():
                return
            if not self._preflight_worker(w, self._secrets):
                continue
            with self._worker_lock:
                if w in self._down_workers:
                    self._down_workers.remove(w)
                self._active_workers.append(w)
            cap = self._spawn_remote_dispatchers(w)
            self.log(f"worker {w['name']} recovered; re-admitted with {cap} slot(s)")
            self._report_fleet_state(active=True)

    def _maybe_recheck_async(self) -> None:
        """Kick off a re-admission pre-flight in the background if one isn't already running."""
        with self._worker_lock:
            if not self._down_workers:
                return
        if not self._recheck_lock.acquire(blocking=False):
            return  # a recheck is already in flight; don't pile up
        def _run():
            try:
                self._recheck_down_workers()
            finally:
                self._recheck_lock.release()
        self._spawn(_run, "worker-recheck")

    # -- workers -----------------------------------------------------------------------------
    def _consume_local(self) -> None:
        from concurrent.futures.process import BrokenProcessPool
        from app.services.strategy_optimization_handler import _trial_worker
        while not self._stop.is_set():
            job = self.broker.claim(worker_id="local")
            if job is None:
                self._stop.wait(0.05)
                continue
            pool = self.pool  # snapshot: may be swapped by another consumer's _recover_pool below
            try:
                out = pool.submit(_trial_worker, job["config"], job["fitness_metric"]).result()
            except BrokenProcessPool as e:
                # A crashed worker (e.g. a transient MemoryError) leaves the WHOLE pool unusable —
                # every future .submit() raises the SAME error immediately (no computation, near-
                # zero latency). Without recovery every local consumer thread spins in a tight
                # claim/fail/requeue loop for the rest of the run: local capacity is silently lost
                # AND the log floods with one line per loop iteration. Recreate once (thread-safe)
                # and requeue this trial so it isn't lost.
                self._recover_pool(pool, e)
                self.broker.requeue_one(job["trial_id"])
                continue
            except Exception as e:  # noqa: BLE001
                out = {"ok": False, "fitness": 0.0, "trades": 0, "error": repr(e), "fatal": False}
                self.broker.post_result(job["trial_id"], out)
                continue
            self.broker.post_result(job["trial_id"], out)

    def _recover_pool(self, bad_pool, exc: Exception) -> None:
        """Replace a dead local pool with a fresh one. Thread-safe: if several consumers hit the
        SAME break around the same time, only the first to acquire the lock rebuilds it — the
        rest see ``self.pool is not bad_pool`` and no-op. No-op entirely (degrade to remote-only,
        the pre-existing behaviour) if no ``pool_factory`` was supplied at construction."""
        if self._pool_factory is None:
            self.log(f"local pool broken ({exc!r}); no pool_factory to recover, degrading to remote-only")
            return
        with self._pool_lock:
            if self.pool is not bad_pool:
                return  # another consumer already recreated it
            self.log(f"local pool broken ({exc!r}); recreating")
            _log_memory_diagnostics(self.log, "local pool crash")
            try:
                bad_pool.shutdown(wait=False, cancel_futures=True)
            except Exception:  # noqa: BLE001 — best-effort cleanup of the already-dead pool
                pass
            self.pool = self._pool_factory()

    def _dispatch_remote(self, w: dict) -> None:
        failures = 0
        while not self._stop.is_set():
            job = self.broker.claim(worker_id=f"remote:{w['name']}")
            if job is None:
                self._stop.wait(0.1)
                continue
            try:
                out = worker_client.run_trial(w, job["config"], job["fitness_metric"])
                # RETRYABLE worker-side failure (e.g. the remote's own trial pool broke —
                # WinError 1450 killing a child mid-IPC): the worker returns HTTP 200 with
                # ``retryable: True`` after rebuilding its pool. The failure says nothing
                # about the GENOME, so requeue the trial (local/another slot runs it) instead
                # of accepting a poisoned failed-trial result.
                if isinstance(out, dict) and out.get("retryable"):
                    self.broker.requeue_one(job["trial_id"])
                    failures += 1
                    self.log(f"worker {w['name']} returned retryable failure "
                             f"({failures}/{_MAX_WORKER_FAILURES}): {out.get('error')}")
                    if failures >= _MAX_WORKER_FAILURES:
                        self.log(f"worker {w['name']} giving up (repeated retryable failures); "
                                 f"trials fall back to local/others")
                        with self._worker_lock:
                            if w in self._active_workers:
                                self._active_workers.remove(w)
                            self._down_workers.append(w)
                        return
                    self._stop.wait(2.0)
                    continue
                self.broker.post_result(job["trial_id"], out)
                failures = 0
            except Exception as e:  # noqa: BLE001 — push the trial back so local/another worker runs it
                self.broker.requeue_one(job["trial_id"])
                failures += 1
                self.log(f"worker {w['name']} run_trial failed ({failures}/{_MAX_WORKER_FAILURES}): {e}")
                if failures >= _MAX_WORKER_FAILURES:
                    self.log(f"worker {w['name']} giving up (dead); trials fall back to local/others")
                    with self._worker_lock:
                        if w in self._active_workers:
                            self._active_workers.remove(w)
                        self._down_workers.append(w)
                    return
                self._stop.wait(2.0)

    # -- coordinator -------------------------------------------------------------------------
    def execute_jobs(self, jobs: List[Job]) -> Iterator[Tuple[int, dict, str, dict]]:
        """Submit *jobs* to the broker; yield ``(index, flat, key, result)`` as each completes."""
        trial_map = {}
        for (i, flat, key, cfg) in jobs:
            tid = self.broker.submit_one(self.optimization_id, cfg, self.fitness_metric)
            trial_map[tid] = (i, flat, key)
        remaining = set(trial_map)
        completed = 0
        while remaining:
            ready = self.broker.wait_ready(remaining, timeout=2.0)
            if not ready:
                # Safety net: a trial claimed by a worker/thread that vanished without the caught
                # error path (e.g. a hard-killed dispatcher) is re-queued so it can't hang the gen.
                self.broker.requeue_stale(self.requeue_timeout)
                continue
            for tid, out in ready.items():
                i, flat, key = trial_map[tid]
                remaining.discard(tid)
                completed += 1
                # Re-admission cadence: every n_consumers individuals, give previously-excluded/
                # failed workers a chance to rejoin (see _maybe_recheck_async — no-op if none down).
                if completed % self.n_consumers == 0:
                    self._maybe_recheck_async()
                yield (i, flat, key, out)

    def stop(self) -> None:
        self._stop.set()
        for t in self._threads:
            t.join(timeout=2.0)
        self.broker.clear(self.optimization_id)
        # Release the engaged-slot counts so the panel doesn't show stale "busy" between jobs.
        self._report_fleet_state(active=False)
