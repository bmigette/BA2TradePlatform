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

Determinism: a trial config is hermetic + seeded, so its fitness is independent of WHERE it ran;
``execute_jobs`` reassembles results by the GA's input index. With no workers selected the caller
keeps the plain local-pool path (byte-identical to before).
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Iterator, List, Optional, Tuple

from app.services import worker_client
from app.services.trial_broker import TrialBroker

logger = logging.getLogger(__name__)

# A trial job as built by the handler: (index, decoded_flat, trial_key, config).
Job = Tuple[int, dict, str, dict]

# A dispatcher gives up on a worker after this many consecutive run_trial failures (dead box).
_MAX_WORKER_FAILURES = 3


class DistributedEvaluator:
    """Bridges the GA batch loop to local + remote workers via a per-optimization TrialBroker.

    *submit_pool* is the master's ``ProcessPoolExecutor`` (local consumers run trials through it).
    *workers* is a list of resolved worker dicts ``{id,name,url,password,capacity}``. *master_version*
    is the master's app version (workers are version-matched to it before use).
    """

    def __init__(self, submit_pool, fitness_metric: str, n_consumers: int,
                 optimization_id: Any, workers: Optional[List[dict]] = None,
                 master_version: Optional[str] = None, log=logger.warning,
                 requeue_timeout: float = 1800.0):
        self.pool = submit_pool
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

    # -- lifecycle ---------------------------------------------------------------------------
    def start(self) -> None:
        # Pre-flight: version-match + cache-push each selected worker; drop the unusable ones.
        for w in self.workers:
            try:
                if not w.get("password"):
                    self.log(f"worker {w.get('name')} has no password configured; excluding")
                    continue
                if not worker_client.ensure_synced(w, self.master_version, log=self.log):
                    continue
                worker_client.push_cache(w, log=self.log)
                try:
                    w["capacity"] = max(1, int(worker_client.health(w).get("capacity") or 1))
                except Exception:  # noqa: BLE001 — fall back to 1 slot if /health didn't report
                    w["capacity"] = max(1, int(w.get("capacity") or 1))
                self._active_workers.append(w)
            except Exception as e:  # noqa: BLE001 — a bad worker must never abort the run
                self.log(f"worker {w.get('name')} pre-flight failed: {e}; excluding")

        # Local consumers (master-as-worker).
        for i in range(self.n_consumers):
            self._spawn(self._consume_local, f"local-trial-consumer-{i}")
        # Remote dispatchers: one thread per worker slot.
        remote_slots = 0
        for w in self._active_workers:
            cap = max(1, int(w.get("capacity") or 1))
            remote_slots += cap
            for i in range(cap):
                self._spawn(lambda w=w: self._dispatch_remote(w), f"remote-{w['name']}-{i}")
        self.log(f"distributed evaluator (opt {self.optimization_id}): {self.n_consumers} local + "
                 f"{remote_slots} remote slot(s) across {len(self._active_workers)} worker(s)")
        # Surface the engaged fleet in the UI: each remote worker's active_jobs_count = its slot
        # count, the local worker's = the local consumer count. (The CLI path never updates these,
        # so without this the dashboard shows "0 jobs" + "offline" while a run is in flight.)
        self._report_fleet_state(active=True)

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

    # -- workers -----------------------------------------------------------------------------
    def _consume_local(self) -> None:
        from app.services.strategy_optimization_handler import _trial_worker
        while not self._stop.is_set():
            job = self.broker.claim(worker_id="local")
            if job is None:
                self._stop.wait(0.05)
                continue
            try:
                out = self.pool.submit(_trial_worker, job["config"], job["fitness_metric"]).result()
            except Exception as e:  # noqa: BLE001
                out = {"ok": False, "fitness": 0.0, "trades": 0, "error": repr(e), "fatal": False}
            self.broker.post_result(job["trial_id"], out)

    def _dispatch_remote(self, w: dict) -> None:
        failures = 0
        while not self._stop.is_set():
            job = self.broker.claim(worker_id=f"remote:{w['name']}")
            if job is None:
                self._stop.wait(0.1)
                continue
            try:
                out = worker_client.run_trial(w, job["config"], job["fitness_metric"])
                self.broker.post_result(job["trial_id"], out)
                failures = 0
            except Exception as e:  # noqa: BLE001 — push the trial back so local/another worker runs it
                self.broker.requeue_one(job["trial_id"])
                failures += 1
                self.log(f"worker {w['name']} run_trial failed ({failures}/{_MAX_WORKER_FAILURES}): {e}")
                if failures >= _MAX_WORKER_FAILURES:
                    self.log(f"worker {w['name']} giving up (dead); trials fall back to local/others")
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
                yield (i, flat, key, out)

    def stop(self) -> None:
        self._stop.set()
        for t in self._threads:
            t.join(timeout=2.0)
        self.broker.clear(self.optimization_id)
        # Release the engaged-slot counts so the panel doesn't show stale "busy" between jobs.
        self._report_fleet_state(active=False)
