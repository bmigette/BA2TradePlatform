"""The background warm queue (spec section 6, lifecycle steps 3 and 4).

"JobManager supplies scheduled work and analysis-completion hooks. Jobs use an
explicit background worker/process, not the trading worker queue at equal
priority."

So this is its own queue with its own threads, built on the
``SmartRiskManagerQueue`` template (own ``queue.Queue``, N daemon threads, a
sentinel per worker at shutdown) -- not another priority band in ``WorkerQueue``,
where a warm would compete with analyses for the same pool.

**What a warm worker must and must not do.**

* It sets ``set_ttl_frozen(True)`` ON ITS OWN THREAD. The freeze flag is what makes
  ``fmp_history_disk_cached`` write to disk at all; the 2026-09-10 audit found the
  API prewarm entering the freeze on the submitting thread only, so every pool
  worker fetched over the network and wrote nothing while the task reported
  success.
* It enables the empty-result sentinel THREAD-LOCALLY
  (``thread_persist_empty_sentinel``), never the process-global
  ``persist_empty_sentinel``. It runs inside the live trading process, and "FMP was
  asked and has nothing" is a claim only a deliberate warm may write -- spec
  section 9: "process-global empty-sentinel flags must not leak into concurrent
  live work."
* It tags its requests ``warm`` so the bytes are charged to the warm allowance and
  not to live.
* It waits out the shared FMP gate before each item, so a provider already pushing
  back on live traffic does not get background load on top.
* It never acquires an account submission lock, opens a broker or touches a trading
  row. It fetches data into a cache; that is all.

Repeating an unchanged warm performs zero downloads: ``submit_plan`` enqueues only
the entries the planner marked ``fetch``/``refresh``, and a key already completed or
in flight is not enqueued again.
"""
from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Sequence

from ba2_common.core.replay.dependencies import (
    KIND_HISTORY,
    KIND_INDICATOR,
    KIND_SERIES,
    KIND_TIMESERIES,
    Requirement,
)
from ba2_common.logger import logger
from ba2_providers.fmp_common import (
    PURPOSE_WARM,
    fmp_purpose,
    frozen_ttl_cache,
    set_ttl_frozen,
    thread_persist_empty_sentinel,
)

from app.services.warm.budget import BudgetExhausted, WarmBudget
from app.services.warm.planner import ACTION_FETCH, ACTION_REFRESH


class WarmFetchError(RuntimeError):
    """This requirement cannot be fetched by this fetcher.

    A configuration gap, not a data gap: an unknown namespace or a kind nothing can
    download (platform state). Raised rather than skipped so a plan that declares
    something unwarmable is visible instead of quietly never completing.
    """


@dataclass
class _Item:
    requirement: Requirement
    estimated_bytes: Optional[int]


class WarmQueue:
    """A low-priority queue of :class:`Requirement` items with its own worker threads."""

    def __init__(self, *, workers: int, budget: WarmBudget,
                 fetcher: Callable[[Requirement], Any],
                 sleep: Callable[[float], None] = time.sleep,
                 name: str = "WarmWorker") -> None:
        if workers < 1:
            raise ValueError(f"a warm queue needs at least one worker, got {workers}")
        self._queue: "queue.Queue" = queue.Queue()
        self._num_workers = int(workers)
        self._budget = budget
        self._fetcher = fetcher
        self._sleep = sleep
        self._name = name
        self._threads: List[threading.Thread] = []
        self._running = False
        self._lock = threading.Lock()
        self._done = threading.Condition(self._lock)
        #: Keys enqueued, in flight or finished -- the dedupe set.
        self._seen: set = set()
        self._completed: set = set()
        self._in_flight = 0
        self._fetched = 0
        self._failed = 0
        self._skipped = 0
        self._paused = False
        self._gap: Optional[Dict[str, Any]] = None
        logger.info(f"WarmQueue initialized with {self._num_workers} workers")

    # -- lifecycle --------------------------------------------------------- #
    def start(self) -> None:
        if self._running:
            logger.warning("WarmQueue is already running")
            return
        self._running = True
        for i in range(self._num_workers):
            thread = threading.Thread(target=self._worker_loop, args=(f"{self._name}-{i + 1}",),
                                      daemon=True)
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
    def submit(self, requirement: Requirement,
               estimated_bytes: Optional[int] = None) -> bool:
        """Enqueue one requirement. ``False`` when it is already known (a duplicate).

        Deduping at ENQUEUE time, not at fetch time: two experts on the same symbol
        declare the same payload, and two workers each discovering the file half
        way through the other's write is not a shared fetch, it is a race.
        """
        key = requirement.key
        with self._lock:
            if key in self._seen:
                self._skipped += 1
                return False
            self._seen.add(key)
            self._in_flight += 1
        self._queue.put(_Item(requirement=requirement, estimated_bytes=estimated_bytes))
        return True

    def submit_plan(self, plan) -> List[str]:
        """Enqueue everything the plan marked as work. Returns the keys enqueued.

        Entries the planner resolved as present, checked-empty, local state or
        unsupported are NOT work -- which is what makes a second run of an unchanged
        plan download nothing.
        """
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
                "skipped": self._skipped,
                "in_flight": self._in_flight,
                "completed": len(self._completed),
                "paused": self._paused,
                "gap": dict(self._gap) if self._gap else None,
                "spent_bytes": self._budget.spent_bytes(),
                "remaining_bytes": self._budget.remaining_bytes(),
            }

    def warmed_keys(self) -> frozenset:
        """The requirement keys this queue actually fetched (for a pinned manifest)."""
        with self._lock:
            return frozenset(self._completed)

    # -- the loop ---------------------------------------------------------- #
    def _worker_loop(self, worker_name: str) -> None:
        logger.info(f"{worker_name} started")
        # ON THIS THREAD, once: the freeze flag is thread-local and is what makes the
        # disk cache write at all. ``frozen_ttl_cache()`` as a context manager around
        # the whole loop would do the same; the explicit setter matches the
        # ``initializer=set_ttl_frozen`` the shared prewarm already relies on.
        set_ttl_frozen(True)
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
                logger.error(f"{worker_name} error handling {item.requirement.key}: {e}",
                             exc_info=True)
            finally:
                self._queue.task_done()
                with self._done:
                    self._in_flight -= 1
                    self._done.notify_all()
        logger.info(f"{worker_name} stopped")

    def _execute(self, item: _Item, worker_name: str) -> None:
        key = item.requirement.key
        with self._lock:
            paused = self._paused
        if paused:
            # The allowance is already gone; draining the rest without fetching keeps
            # the queue honest (every item is accounted for) and cheap.
            logger.debug(f"{worker_name} skipping {key}: the warm is paused")
            with self._lock:
                self._seen.discard(key)
            return

        waited = self._budget.wait_for_gate(self._sleep)
        if waited:
            logger.info(f"{worker_name} waited {waited:.1f}s on the shared FMP gate before {key}")

        try:
            self._budget.reserve(key, item.estimated_bytes, pending=self._pending_keys())
        except BudgetExhausted as e:
            with self._lock:
                self._paused = True
                self._gap = e.gap.to_mapping()
                self._seen.discard(key)
            logger.warning(e.gap.to_markdown())
            return

        try:
            # The three flags a warm fetch needs, all scoped to THIS item:
            # thread-local sentinel semantics (never the process global), the warm
            # purpose tag for the byte counters, and the freeze (already set on the
            # thread; re-entered so an injected fetcher that clears it cannot leak).
            with frozen_ttl_cache(), thread_persist_empty_sentinel(True), \
                    fmp_purpose(PURPOSE_WARM):
                self._fetcher(item.requirement)
        except Exception as e:  # noqa: BLE001 - one data gap must not abort the warm
            self._budget.release(key)
            with self._lock:
                self._failed += 1
            logger.warning(f"{worker_name} warm fetch failed for {key}: "
                           f"{type(e).__name__}: {e}")
            return

        self._budget.settle(key)
        with self._lock:
            self._fetched += 1
            self._completed.add(key)

    def _pending_keys(self) -> Sequence[str]:
        """Keys still queued, for a remaining-gap report. Best effort, never blocking."""
        with self._queue.mutex:  # noqa: SLF001 - reading the deque without consuming it
            return tuple(i.requirement.key for i in list(self._queue.queue) if i is not None)


# --------------------------------------------------------------------------- #
# The default fetcher
# --------------------------------------------------------------------------- #
class DefaultWarmFetcher:
    """Fetch one requirement through the SAME data-layer call the expert makes.

    Never a hand-rolled re-implementation of a fetch: the warmed surface would drift
    away from the read surface, which is the failure ``prewarm_fetchers`` documents
    at length. Each entry in the namespace table calls the function the expert's
    ``_gather`` calls, so the cache key, the namespace spelling and the payload shape
    are the same by construction.

    Every input is stated by the caller -- keys, the reference date, the OHLCV
    provider name (host wiring decides which one backs the indicator provider, so it
    cannot be inferred here). A key that is needed but absent is refused per
    requirement, not silently skipped.
    """

    def __init__(self, *, ohlcv_provider: str, end_date: datetime,
                 fmp_key: Optional[str], fred_key: Optional[str],
                 history_fetchers: Optional[Dict[str, Callable]] = None) -> None:
        self.ohlcv_provider = ohlcv_provider
        self.end_date = end_date
        self.fmp_key = fmp_key
        self.fred_key = fred_key
        self._history_fetchers = (self._default_history_fetchers()
                                  if history_fetchers is None else dict(history_fetchers))

    # -- dispatch ---------------------------------------------------------- #
    def __call__(self, requirement: Requirement) -> None:
        if requirement.kind == KIND_HISTORY:
            return self._fetch_history(requirement)
        if requirement.kind == KIND_SERIES:
            return self._fetch_series(requirement)
        if requirement.kind in (KIND_TIMESERIES, KIND_INDICATOR):
            return self._fetch_timeseries(requirement)
        raise WarmFetchError(
            f"nothing can download a {requirement.kind!r} requirement "
            f"({requirement.namespace}); the plan reports it instead")

    # -- fmp_history ------------------------------------------------------- #
    def _fetch_history(self, requirement: Requirement) -> None:
        fetch = self._history_fetchers.get(requirement.namespace)
        if fetch is None:
            raise WarmFetchError(
                f"no warm fetcher for the fmp_history namespace {requirement.namespace!r}; "
                f"known: {', '.join(sorted(self._history_fetchers))}")
        fetch(requirement.symbol, requirement)

    def _require_fmp_key(self) -> str:
        if not self.fmp_key:
            raise WarmFetchError(
                "FMP_API_KEY is not configured; this requirement cannot be warmed")
        return self.fmp_key

    def _details(self):
        from ba2_providers.fundamentals.details.FMPCompanyDetailsProvider import (
            FMPCompanyDetailsProvider,
        )
        return FMPCompanyDetailsProvider()

    def _default_history_fetchers(self) -> Dict[str, Callable]:
        """namespace -> ``(symbol, requirement) -> None``, each the expert's own call."""
        def _price_target(symbol, requirement):
            from ba2_experts.FMPRating import fetch_price_target_history_cached
            fetch_price_target_history_cached(self._require_fmp_key(), symbol)

        def _grades(symbol, requirement):
            from ba2_experts.FMPRating import fetch_grades_historical_cached
            fetch_grades_historical_cached(self._require_fmp_key(), symbol)

        def _analyst_grades(symbol, requirement):
            from ba2_experts.FMPRating import fetch_analyst_grades_cached
            fetch_analyst_grades_cached(self._require_fmp_key(), symbol)

        def _statement(getter_name):
            def _fetch(symbol, requirement):
                getattr(self._details(), getter_name)(
                    symbol=symbol, frequency="annual", end_date=self.end_date,
                    lookback_periods=6, as_of=self.end_date, format_type="dict")
            return _fetch

        def _past_earnings(symbol, requirement):
            # 16 quarters: the deepest any reader asks for (DeterministicScorer's SUE
            # standardization). The disk cache is keyed WITHOUT depth, so warming the
            # shallowest would pin a truncated payload for every other reader.
            self._details().get_past_earnings(
                symbol=symbol, frequency="quarterly", end_date=self.end_date,
                lookback_periods=16, format_type="dict")

        def _estimates(symbol, requirement):
            self._details().get_earnings_estimates(
                symbol=symbol, frequency="quarterly", as_of_date=self.end_date,
                lookback_periods=2, format_type="dict")

        def _insider(symbol, requirement):
            from ba2_providers.insider.FMPInsiderProvider import FMPInsiderProvider

            window = requirement.window
            if window is None or window.start is None:
                raise WarmFetchError(
                    f"the insider history for {symbol} needs a bounded window; the "
                    f"requirement carries none")
            lookback_days = max(1, (window.end - window.start).days)
            FMPInsiderProvider().get_insider_transactions(
                symbol, end_date=self.end_date, lookback_days=lookback_days,
                as_of=self.end_date, format_type="dict")

        return {
            "price_target": _price_target,
            "grades_historical": _grades,
            "analyst_grades": _analyst_grades,
            "income_statement_annual": _statement("get_income_statement"),
            "balance_sheet_annual": _statement("get_balance_sheet"),
            "cashflow_statement_annual": _statement("get_cashflow_statement"),
            "past_earnings_quarterly": _past_earnings,
            "earnings_estimates_quarterly": _estimates,
            "insider_v2": _insider,
        }

    # -- FRED -------------------------------------------------------------- #
    def _fetch_series(self, requirement: Requirement) -> None:
        from ba2_providers.macro import fred_series

        if not self.fred_key:
            raise WarmFetchError(
                "fred_api_key is not configured; the macro series cannot be warmed")
        fred_series.refresh_series(requirement.namespace, self.fred_key)

    # -- parquet price series ---------------------------------------------- #
    def _fetch_timeseries(self, requirement: Requirement) -> None:
        """Read the series through the OHLCV provider, which fills its own parquet.

        An INDICATOR requirement lands here too: an ATR has no artifact of its own,
        it is computed from these bars, so warming the underlying series is the whole
        of the work.
        """
        from ba2_providers import get_provider

        window = requirement.window
        if window is None or window.start is None:
            raise WarmFetchError(
                f"the price series for {requirement.symbol} needs a bounded window; the "
                f"requirement carries none")
        provider = get_provider("ohlcv", self.ohlcv_provider)
        provider.get_ohlcv_data(symbol=requirement.symbol, start_date=window.start,
                                end_date=window.end, interval=requirement.interval)


__all__ = [
    "DefaultWarmFetcher",
    "WarmFetchError",
    "WarmQueue",
]
