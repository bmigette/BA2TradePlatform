"""The background warm queue (spec section 6, lifecycle steps 3 and 4).

"JobManager supplies scheduled work and analysis-completion hooks. Jobs use an
explicit background worker/process, not the trading worker queue at equal
priority."

So this is its own queue with its own threads, built on the
``SmartRiskManagerQueue`` template (own ``queue.Queue``, N daemon threads, a
sentinel per worker at shutdown) -- not another priority band in ``WorkerQueue``,
where a warm would compete with analyses for the same pool.

**Pure mechanism.** The queue knows nothing about FMP, caches or experts. What a
fetch needs around it -- a per-thread freeze flag, a sentinel scope, a request
purpose tag -- arrives as ``worker_init`` (run once per thread) and
``fetch_context`` (entered around each item). ``ba2_providers.warm.seams`` supplies
FMP's; a test supplies nothing and the queue is a plain executor.

**Two item types.** A REQUIREMENT is budgeted work: reserve, fetch, settle. A JOB is
local work with no download -- resolving and planning a batch. The distinction
exists because the batch-end hook must not plan on the trading thread (walking the
capture index, reading parquet footers and scanning cache directories): it submits
a job, and the planning happens here.

**What a warm worker must never do**: acquire an account submission lock, open a
broker, or touch a trading row. It fetches data into a cache; that is all.

**Pausing is recoverable and visible.** An exhausted allowance pauses the queue and
drops the rest of the run with ONE warning naming the gap (not one line per item,
which is how a real pause gets lost in a log). The pause lifts by itself when the
allowance recovers -- the counters reset at the UTC day change -- so tomorrow's
batch is not silently dead, and the dropped items are counted so a caller can say
how much of its plan actually ran.
"""
from __future__ import annotations

import contextlib
import queue
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, ContextManager, Dict, List, Optional, Sequence

from ba2_common.core.warm.budget import BudgetExhausted, WarmBudget
from ba2_common.logger import logger

#: Plan entry actions this queue treats as work. Kept as strings rather than
#: importing the planner: the mechanism must not depend on the cache-layout layer.
ACTION_FETCH = "fetch"
ACTION_REFRESH = "refresh"


@dataclass
class _Item:
    """Budgeted work: one requirement to fetch."""

    key: str
    requirement: Any
    estimated_bytes: Optional[int]


@dataclass
class _Job:
    """Local work with no download (resolve + plan a batch)."""

    key: str
    run: Callable[[], Any]


class WarmQueue:
    """A low-priority queue of warm work with its own worker threads."""

    def __init__(self, *, workers: int, budget: WarmBudget,
                 fetcher: Callable[[Any], Any],
                 fetch_context: Optional[Callable[[], ContextManager]] = None,
                 worker_init: Optional[Callable[[], None]] = None,
                 sleep: Callable[[float], None] = time.sleep,
                 name: str = "WarmWorker") -> None:
        if workers < 1:
            raise ValueError(f"a warm queue needs at least one worker, got {workers}")
        self._queue: "queue.Queue" = queue.Queue()
        self._num_workers = int(workers)
        self._budget = budget
        self._fetcher = fetcher
        self._fetch_context = fetch_context
        self._worker_init = worker_init
        self._sleep = sleep
        self._name = name
        self._threads: List[threading.Thread] = []
        self._running = False
        self._lock = threading.Lock()
        self._done = threading.Condition(self._lock)
        #: Keys enqueued, in flight or finished -- the dedupe set.
        self._seen: set = set()
        self._completed: set = set()
        self._failed_keys: set = set()
        self._in_flight = 0
        self._fetched = 0
        self._failed = 0
        self._skipped = 0
        self._paused = False
        self._paused_dropped = 0
        self._gap: Optional[Dict[str, Any]] = None
        #: What the item that caused the pause needed. The pause lifts when the
        #: allowance can cover THAT, not merely when a byte is free -- resuming on
        #: "remaining > 0" re-blocks on the very next item and logs a fresh pause for
        #: each one, which is the noise the single-warning rule exists to prevent.
        self._gap_required_bytes = 0
        logger.info(f"WarmQueue initialized with {self._num_workers} workers")

    # -- lifecycle --------------------------------------------------------- #
    def start(self) -> None:
        if self._running:
            logger.warning("WarmQueue is already running")
            return
        self._running = True
        for i in range(self._num_workers):
            worker_name = f"{self._name}-{i + 1}"
            # NAMED: every log line this queue writes carries the thread name, and an
            # operator reading "Thread-7" cannot tell a warm worker from anything else.
            thread = threading.Thread(target=self._worker_loop, args=(worker_name,),
                                      name=worker_name, daemon=True)
            thread.start()
            self._threads.append(thread)
        logger.info(f"WarmQueue started with {self._num_workers} worker threads")

    def stop(self, timeout: float = 5.0) -> None:
        """Stop the workers. Never waits for queued work: a warm may always be cut short."""
        if not self._running:
            return
        self._running = False
        for _ in range(self._num_workers):
            self._queue.put(None)
        for thread in self._threads:
            thread.join(timeout=timeout)
            if thread.is_alive():
                logger.warning(f"{thread.name} did not stop within {timeout}s")
        self._threads.clear()
        logger.info("WarmQueue stopped")

    def is_running(self) -> bool:
        return self._running

    # -- submission -------------------------------------------------------- #
    def submit(self, requirement: Any, estimated_bytes: Optional[int] = None) -> bool:
        """Enqueue one requirement. ``False`` when it is already known (a duplicate).

        Deduping at ENQUEUE time, not at fetch time: two experts on the same symbol
        declare the same payload, and two workers each discovering the file half way
        through the other's write is not a shared fetch, it is a race.
        """
        return self._enqueue(_Item(key=requirement.key, requirement=requirement,
                                   estimated_bytes=estimated_bytes))

    def submit_job(self, key: str, run: Callable[[], Any]) -> bool:
        """Enqueue LOCAL work (resolve + plan). No budget is reserved: it downloads nothing.

        This is what keeps planning off the trading thread. ``key`` dedupes it exactly
        like a requirement, so a batch cannot be planned twice concurrently.
        """
        return self._enqueue(_Job(key=key, run=run))

    def _enqueue(self, item) -> bool:
        with self._lock:
            if item.key in self._seen:
                self._skipped += 1
                return False
            self._seen.add(item.key)
            self._in_flight += 1
        self._queue.put(item)
        return True

    def submit_plan(self, plan) -> List[str]:
        """Enqueue everything the plan marked as work. Returns the keys enqueued.

        Entries the planner resolved as present, checked-empty, local state or
        unsupported are NOT work -- which is what makes a second run of an unchanged
        plan download nothing.

        Submitting a plan also LIFTS a pause whose cause has passed: a queue that
        stopped on yesterday's exhausted allowance must serve today's batch, and the
        allowance is the thing that says whether it can.
        """
        self._maybe_resume()
        enqueued: List[str] = []
        for entry in plan.pending():
            if entry.action not in (ACTION_FETCH, ACTION_REFRESH):
                continue
            if self.submit(entry.requirement, entry.estimated_bytes):
                enqueued.append(entry.requirement.key)
        return enqueued

    def join(self, timeout: float = 30.0) -> bool:
        """Wait until nothing is queued or running. ``False`` on timeout."""
        deadline = time.monotonic() + timeout
        with self._done:
            while self._in_flight > 0:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._done.wait(remaining)
        return True

    # -- reporting --------------------------------------------------------- #
    def stats(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "fetched": self._fetched,
                "failed": self._failed,
                "failed_keys": sorted(self._failed_keys),
                "skipped": self._skipped,
                "in_flight": self._in_flight,
                "completed": len(self._completed),
                "paused": self._paused,
                "paused_dropped": self._paused_dropped,
                "gap": dict(self._gap) if self._gap else None,
                "spent_bytes": self._budget.spent_bytes(),
                "remaining_bytes": self._budget.remaining_bytes(),
            }

    def warmed_keys(self) -> frozenset:
        """The requirement keys this queue actually fetched (for a pinned manifest)."""
        with self._lock:
            return frozenset(self._completed)

    def is_paused(self) -> bool:
        with self._lock:
            return self._paused

    def _maybe_resume(self) -> bool:
        """Lift a pause once the allowance can cover what blocked it. True if it lifted.

        The bar is the BLOCKED ITEM's size, not "any allowance at all": a warm that
        stopped needing 400 bytes with 100 left is not unblocked by having 100 left, and
        resuming there would re-pause (and re-warn) on every remaining item. In practice
        the thing that clears it is the UTC day rollover, which resets the provider's
        counters and hands the budget a full allowance again.
        """
        with self._lock:
            if not self._paused:
                return False
            required = self._gap_required_bytes
        if self._budget.remaining_bytes() < max(1, required):
            return False
        with self._lock:
            if not self._paused:
                return False
            dropped = self._paused_dropped
            self._paused = False
            self._gap = None
            self._gap_required_bytes = 0
        logger.info(
            f"Warm resumed: the daily allowance has recovered "
            f"({self._budget.remaining_bytes() / 1048576.0:.1f} MiB available). "
            f"{dropped} item(s) were dropped while paused and will be re-planned.")
        return True

    # -- the loop ---------------------------------------------------------- #
    def _worker_loop(self, worker_name: str) -> None:
        logger.info(f"{worker_name} started")
        if self._worker_init is not None:
            # ON THIS THREAD, once. The flags a warm fetch needs are thread-local by
            # design (the 2026-09-10 audit found a prewarm setting them on the
            # SUBMITTING thread only, so every pool worker fetched over the network and
            # wrote nothing while the run reported success).
            self._worker_init()
        while self._running:
            try:
                item = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue
            if item is None:
                logger.info(f"{worker_name} received shutdown signal")
                break
            try:
                self._execute(item, worker_name)
            except Exception as e:  # noqa: BLE001 - one item must never kill a worker
                logger.error(f"{worker_name} error handling {item.key}: {e}", exc_info=True)
            finally:
                self._queue.task_done()
                with self._done:
                    self._in_flight -= 1
                    self._done.notify_all()
        logger.info(f"{worker_name} stopped")

    def _execute(self, item, worker_name: str) -> None:
        if isinstance(item, _Job):
            self._execute_job(item, worker_name)
            return
        self._execute_item(item, worker_name)

    def _execute_job(self, job: _Job, worker_name: str) -> None:
        """Run local work. No budget, no fetch context: it downloads nothing."""
        try:
            job.run()
        except Exception as e:  # noqa: BLE001 - a bad plan must not kill the queue
            with self._lock:
                self._failed += 1
                self._failed_keys.add(job.key)
            logger.warning(f"{worker_name} warm job {job.key} failed: "
                           f"{type(e).__name__}: {e}")
        finally:
            # A job is repeatable by construction (it re-plans), so it never stays in
            # the dedupe set: the next batch with the same id must be able to run.
            with self._lock:
                self._seen.discard(job.key)

    def _execute_item(self, item: _Item, worker_name: str) -> None:
        key = item.key
        if self._paused and not self._maybe_resume():
            # The allowance is gone; draining the rest without fetching keeps the queue
            # honest (every item is accounted for) and cheap. COUNTED, not logged per
            # item: one warning was already emitted for this pause episode.
            with self._lock:
                self._paused_dropped += 1
                self._seen.discard(key)
            return

        waited = self._budget.wait_for_gate(self._sleep)
        if waited:
            logger.info(f"{worker_name} waited {waited:.1f}s on the provider rate-limit gate "
                        f"before {key}")

        try:
            self._budget.reserve(key, item.estimated_bytes, pending=self._pending_keys())
        except BudgetExhausted as e:
            already_paused = False
            with self._lock:
                already_paused = self._paused
                self._paused = True
                self._paused_dropped += 1
                self._gap = e.gap.to_mapping()
                self._gap_required_bytes = max(self._gap_required_bytes,
                                               e.gap.requested_bytes)
                self._seen.discard(key)
            if not already_paused:
                # ONE warning per pause episode. A line per dropped item buries the
                # report that says how much of the plan did not run.
                logger.warning(e.gap.to_markdown())
            return

        try:
            context = self._fetch_context() if self._fetch_context else contextlib.nullcontext()
            with context:
                self._fetcher(item.requirement)
        except Exception as e:  # noqa: BLE001 - one data gap must not abort the warm
            self._budget.release(key)
            with self._lock:
                self._failed += 1
                self._failed_keys.add(key)
                # Forget it, so a LATER plan can retry: a transient provider error must
                # not make this key permanently unwarmable for the life of the process.
                self._seen.discard(key)
            logger.warning(f"{worker_name} warm fetch failed for {key}: "
                           f"{type(e).__name__}: {e}")
            return

        self._budget.settle(key)
        with self._lock:
            self._fetched += 1
            self._completed.add(key)
            self._failed_keys.discard(key)

    def _pending_keys(self) -> Sequence[str]:
        """Keys still queued, for a remaining-gap report. Best effort, never blocking."""
        with self._queue.mutex:  # noqa: SLF001 - reading the deque without consuming it
            return tuple(i.key for i in list(self._queue.queue) if i is not None)


__all__ = ["ACTION_FETCH", "ACTION_REFRESH", "WarmQueue"]
