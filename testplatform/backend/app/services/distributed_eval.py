"""Distributed batch evaluator — runs a GA generation's trials across local + remote workers.

Drops in for the strategy optimizer's local ``ProcessPoolExecutor`` path: instead of submitting
trials straight to the pool, it ``submit``s them to the shared ``TrialBroker`` and consumes
results as they land. The MASTER is also a worker — ``DistributedEvaluator`` starts N local
consumer threads that ``claim`` from the broker and run each trial through the master's pool —
while remote workers claim the rest over HTTP (``/api/worker/claim-trial``). A generation
"barrier" is simply the coordinator awaiting all of that generation's trial ids.

Determinism: a trial config is hermetic + seeded, so its fitness is independent of WHERE it ran;
``execute_jobs`` reassembles results by the GA's input index. With zero remote workers online the
caller keeps the plain local-pool path (byte-identical to before), so distribution is opt-in and
zero-overhead by default.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta
from typing import Any, Iterator, List, Optional, Tuple

from app.services.trial_broker import TrialBroker, get_broker

logger = logging.getLogger(__name__)

# A trial job as built by the handler: (index, decoded_flat, trial_key, config).
Job = Tuple[int, dict, str, dict]


def has_online_remote_workers(db, max_age_seconds: int = 90) -> bool:
    """True iff at least one enabled, non-local worker has heartbeated within *max_age_seconds*."""
    from app.models import Worker
    cutoff = datetime.utcnow() - timedelta(seconds=max_age_seconds)
    return db.query(
        db.query(Worker)
        .filter(
            Worker.is_local == False,                  # noqa: E712
            Worker.is_enabled == True,                 # noqa: E712
            Worker.last_heartbeat.isnot(None),
            Worker.last_heartbeat >= cutoff,
        )
        .exists()
    ).scalar()


class DistributedEvaluator:
    """Bridges the GA batch loop to the TrialBroker, with the master running as a local worker.

    *submit_pool* is the master's ``ProcessPoolExecutor`` (the local consumers run trials through
    it). *n_consumers* local consumer threads keep the pool saturated; remote workers add capacity.
    """

    def __init__(self, submit_pool, fitness_metric: str, n_consumers: int,
                 optimization_id: Any, broker: Optional[TrialBroker] = None,
                 requeue_timeout: float = 600.0):
        self.pool = submit_pool
        self.fitness_metric = fitness_metric
        self.n_consumers = max(1, n_consumers)
        self.optimization_id = optimization_id
        self.broker = broker or get_broker()
        self.requeue_timeout = requeue_timeout
        self._stop = threading.Event()
        self._threads: List[threading.Thread] = []

    # -- master-as-worker local consumers ----------------------------------------------------
    def start(self) -> None:
        for i in range(self.n_consumers):
            t = threading.Thread(target=self._consume, name=f"local-trial-consumer-{i}", daemon=True)
            t.start()
            self._threads.append(t)
        logger.info("distributed evaluator: %d local consumers started (opt %s)",
                    self.n_consumers, self.optimization_id)

    def _consume(self) -> None:
        # Import here to avoid a module-load cycle (handler imports this module lazily).
        from app.services.strategy_optimization_handler import _trial_worker
        while not self._stop.is_set():
            job = self.broker.claim(worker_id="local")
            if job is None:
                self._stop.wait(0.05)
                continue
            try:
                out = self.pool.submit(_trial_worker, job["config"], job["fitness_metric"]).result()
            except Exception as e:  # noqa: BLE001 — surface as a failed trial, never kill the thread
                out = {"ok": False, "fitness": 0.0, "trades": 0, "error": repr(e), "fatal": False}
            self.broker.post_result(job["trial_id"], out)

    # -- coordinator: submit a generation + yield results in completion order ----------------
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
                # No result in the window — re-queue any trial whose worker died, then keep waiting.
                requeued = self.broker.requeue_stale(self.requeue_timeout)
                if requeued:
                    logger.warning("re-queued %d stale trial(s) (dead worker)", requeued)
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
        logger.info("distributed evaluator stopped (opt %s)", self.optimization_id)
