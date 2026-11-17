"""Per-RUN scope for the market state a host publishes once per bar.

The state is the regime overlay's stress flag (``regime_overlay.set_stressed`` / ``get_stressed``)
and the SPY benchmark regime with its failure reason (``benchmark_regime.set_benchmark_regime`` /
``get_benchmark_regime`` / ``get_benchmark_regime_failure``; ``regime_overlay.is_downtrend_now``
reads it). The read functions stay the ONE API that sizing, account validation, rule conditions
and actions call, live and in a backtest; this module only decides WHICH storage they reach.

WHY. That state was process-global, which held only while each backtest had a process to
itself (a GA trial). The test platform's serve master runs backtests as THREADS of one process
(the re-run queue, and the main queue's daily backtests and serial in-process GA), so run X read
the flag run Y had published for another date, and sized, validated and gated on it.

HOW. A backtest opens :func:`run_market_state_scope` around its bar loop. Inside the scope the
publish/read/reset functions operate on the scope's own :class:`RunMarketState`; outside any scope
they operate on the process-global exactly as before. LIVE never opens a scope, so its publisher
(the account-refresh / entry-pass thread) and its readers (the recommendation worker threads)
keep sharing the global, unchanged.

A ``ContextVar`` holding a MUTABLE state object, rather than ``threading.local``:

* work submitted with the run's context copied (``contextvars.copy_context().run``, e.g.
  ``replay.context.capture_aware_submit``) reads and writes the run's own state object;
* a plain new thread or executor task starts with an EMPTY context, so it reads the
  process-global, which no backtest writes: it sees "nothing published", never another run's
  value.

A fresh scope holds nothing published (``None``), the same as after ``reset_stressed()`` /
``reset_benchmark_regime()``, so a single run behaves exactly as it did on the global. Nesting is
refused: a run inside a run is a bug, and sharing one state between them would bring the
cross-talk back.
"""
from __future__ import annotations

import contextvars
from contextlib import contextmanager
from typing import Any, Iterator, Optional


class RunMarketState:
    """One run's published market state. ``None`` everywhere = nothing published."""

    __slots__ = ("stressed", "benchmark_regime", "benchmark_failure")

    def __init__(self) -> None:
        self.stressed: Optional[bool] = None
        self.benchmark_regime: Any = None
        self.benchmark_failure: Optional[str] = None


_RUN_STATE: contextvars.ContextVar[Optional[RunMarketState]] = contextvars.ContextVar(
    "ba2_run_market_state", default=None)


def current_run_state() -> Optional[RunMarketState]:
    """The state of the run this code executes in, or ``None`` outside any run (live)."""
    return _RUN_STATE.get()


@contextmanager
def run_market_state_scope() -> Iterator[RunMarketState]:
    """Give the enclosed run its own market state; the process-global is neither read nor written
    inside. Raises RuntimeError when a scope is already open in this context."""
    if _RUN_STATE.get() is not None:
        raise RuntimeError(
            "run_market_state_scope: a run scope is already open in this context; a backtest "
            "cannot run inside another one (their per-bar market regimes would be shared)")
    state = RunMarketState()
    token = _RUN_STATE.set(state)
    try:
        yield state
    finally:
        _RUN_STATE.reset(token)
