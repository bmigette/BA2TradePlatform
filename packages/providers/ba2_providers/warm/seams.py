"""FMP-backed seams for the pure warm mechanism (spec section 6, section 9).

:class:`ba2_common.core.warm.WarmQueue` and
:class:`~ba2_common.core.warm.WarmBudget` know how to queue and how to budget;
they know nothing about FMP. These four callables are what a caller injects to
warm through the FMP stack, and they are the ONLY place the two layers meet.

The per-thread setup and the per-item context are separate on purpose:

* ``fmp_worker_init`` sets the TTL freeze on the worker thread. The flag is
  thread-local and is what makes ``fmp_history_disk_cached`` write to disk at all;
  the 2026-09-10 audit found a prewarm setting it on the SUBMITTING thread only, so
  every pool worker fetched over the network and wrote nothing while the run
  reported success.
* ``fmp_fetch_context`` wraps each item in the THREAD-LOCAL empty-result sentinel
  (never the process-global ``persist_empty_sentinel``: a warm runs inside the live
  trading process and "FMP was asked and has nothing" is a claim only a deliberate
  warm may write -- spec section 9) plus the ``warm`` purpose tag, so the bytes are
  charged to the warm allowance and not to live.
"""
from __future__ import annotations

import contextlib
from typing import Any

from ba2_providers.fmp_common import (
    PURPOSE_WARM,
    fmp_purpose,
    frozen_ttl_cache,
    gate_remaining_seconds,
    get_purpose_stats,
    set_ttl_frozen,
    thread_persist_empty_sentinel,
)


def fmp_meter() -> int:
    """Warm bytes measured on the FMP/FRED wire today (the budget's charge)."""
    return int(get_purpose_stats().get(PURPOSE_WARM, {}).get("bytes", 0))


def fmp_gate() -> float:
    """Seconds the shared FMP rate-limit cooldown still has to run (0.0 when clear)."""
    return gate_remaining_seconds()


def fmp_worker_init() -> None:
    """Per-thread setup for a warm worker: the TTL freeze that makes the cache write."""
    set_ttl_frozen(True)


@contextlib.contextmanager
def fmp_fetch_context():
    """Per-item context: freeze, thread-local empty sentinel, ``warm`` purpose tag."""
    with frozen_ttl_cache(), thread_persist_empty_sentinel(True), fmp_purpose(PURPOSE_WARM):
        yield


def new_warm_budget(*, allowance_bytes: int, unknown_reserve_bytes: int):
    """A :class:`~ba2_common.core.warm.WarmBudget` wired to the FMP meter and gate.

    One constructor, so a host cannot half-wire a budget (an unmetered budget would
    report every allowance as untouched and never pause).
    """
    from ba2_common.core.warm import WarmBudget

    return WarmBudget(allowance_bytes=allowance_bytes,
                      unknown_reserve_bytes=unknown_reserve_bytes,
                      meter=fmp_meter, gate=fmp_gate)


def new_warm_queue(*, workers: int, budget: Any, fetcher: Any, **kwargs) -> Any:
    """A :class:`~ba2_common.core.warm.WarmQueue` wired to the FMP thread context."""
    from ba2_common.core.warm import WarmQueue

    return WarmQueue(workers=workers, budget=budget, fetcher=fetcher,
                     fetch_context=fmp_fetch_context, worker_init=fmp_worker_init,
                     **kwargs)


__all__ = [
    "fmp_fetch_context",
    "fmp_gate",
    "fmp_meter",
    "fmp_worker_init",
    "new_warm_budget",
    "new_warm_queue",
]
