"""In-memory trial broker — fans a generation's GA trials out to local + remote consumers.

Each ``DistributedEvaluator`` owns ONE broker instance (per-optimization isolation — the
strategy_optimization task queue runs up to 4 concurrently). The GA coordinator ``submit``s a
generation's trials, then the master's local consumer threads AND remote dispatcher threads
atomically ``claim`` from the same queue — so no trial is ever evaluated twice. Results
(``post_result``) are keyed by trial id; the coordinator reassembles them in GA input order.

Determinism is preserved because a trial config is hermetic + seeded: its fitness is identical
no matter WHICH host runs it. ``requeue_one`` (a failed remote worker) and ``requeue_stale`` (a
vanished worker) return a claimed trial to the queue for fault tolerance.
"""

from __future__ import annotations

import threading
import time
import uuid
from collections import deque
from typing import Any, Dict, List, Optional, Set


class TrialBroker:
    def __init__(self) -> None:
        self._cv = threading.Condition()
        self._pending: deque = deque()                 # trial dicts awaiting a consumer
        self._claimed: Dict[str, dict] = {}            # trial_id -> {trial, worker, claim_time}
        self._results: Dict[str, dict] = {}            # trial_id -> result (drained by waiters)
        self._seen: Set[str] = set()                   # trial_ids that already got a result
        self._opt_of: Dict[str, Any] = {}              # trial_id -> optimization_id (for clear)

    # -- producer (GA coordinator) -----------------------------------------------------------
    def submit_one(self, optimization_id: Any, config: dict, fitness_metric: str) -> str:
        """Queue one trial; returns its unique id."""
        tid = uuid.uuid4().hex
        trial = {"trial_id": tid, "optimization_id": optimization_id,
                 "config": config, "fitness_metric": fitness_metric}
        with self._cv:
            self._pending.append(trial)
            self._opt_of[tid] = optimization_id
            self._cv.notify_all()
        return tid

    # -- consumers (local threads + remote HTTP) ---------------------------------------------
    def claim(self, worker_id: str = "?") -> Optional[dict]:
        """Atomically pop the next pending trial (or None). Returns a copy safe to send over HTTP."""
        with self._cv:
            if not self._pending:
                return None
            trial = self._pending.popleft()
            self._claimed[trial["trial_id"]] = {
                "trial": trial, "worker": worker_id, "claim_time": time.time(),
            }
            return dict(trial)

    def post_result(self, trial_id: str, result: dict) -> bool:
        """Record a trial's result. First result wins; duplicates (requeue race) are ignored."""
        with self._cv:
            self._claimed.pop(trial_id, None)
            if trial_id in self._seen:
                return False
            self._seen.add(trial_id)
            self._results[trial_id] = result
            self._cv.notify_all()
            return True

    # -- barrier (GA coordinator) ------------------------------------------------------------
    def wait_ready(self, trial_ids: Set[str], timeout: float = 1.0) -> Dict[str, dict]:
        """Block up to *timeout* until ≥1 of *trial_ids* has a result; return+drain those."""
        deadline = time.time() + timeout
        with self._cv:
            while True:
                ready = {tid: self._results.pop(tid)
                         for tid in list(trial_ids) if tid in self._results}
                if ready:
                    return ready
                remaining = deadline - time.time()
                if remaining <= 0:
                    return {}
                self._cv.wait(remaining)

    def requeue_one(self, trial_id: str) -> bool:
        """Move a specific claimed trial back to the FRONT of pending (e.g. a remote worker that
        failed mid-trial). Returns False if it wasn't claimed or already has a result."""
        with self._cv:
            claim = self._claimed.pop(trial_id, None)
            if claim is None or trial_id in self._seen:
                return False
            self._pending.appendleft(claim["trial"])
            self._cv.notify_all()
            return True

    def requeue_stale(self, claim_timeout: float = 300.0) -> int:
        """Re-queue trials claimed longer than *claim_timeout* ago (dead worker). Returns count."""
        now = time.time()
        with self._cv:
            stale = [tid for tid, c in self._claimed.items()
                     if now - c["claim_time"] > claim_timeout]
            for tid in stale:
                c = self._claimed.pop(tid)
                self._pending.appendleft(c["trial"])
            if stale:
                self._cv.notify_all()
            return len(stale)

    # -- housekeeping ------------------------------------------------------------------------
    def stats(self) -> dict:
        with self._cv:
            return {"pending": len(self._pending), "claimed": len(self._claimed),
                    "results": len(self._results), "seen": len(self._seen)}

    def clear(self, optimization_id: Any = None) -> None:
        """Drop all state, or only the given optimization's trials."""
        with self._cv:
            if optimization_id is None:
                self._pending.clear(); self._claimed.clear()
                self._results.clear(); self._seen.clear(); self._opt_of.clear()
                return
            ours = {tid for tid, oid in self._opt_of.items() if oid == optimization_id}
            self._pending = deque(t for t in self._pending if t["trial_id"] not in ours)
            for tid in ours:
                self._claimed.pop(tid, None)
                self._results.pop(tid, None)
                self._seen.discard(tid)
                self._opt_of.pop(tid, None)
