"""Incremental per-expiry flush in warm_options_history's --wide path.

The property that matters: a failure mid-fetch must KEEP every partition that had already
closed. Observed live 2026-09-03 before this existed -- ABEV died 941 s in, on the last
window, and wrote nothing at all.
"""
import argparse
import os
import sys
from datetime import date

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ba2_common.core.interfaces.OptionsDataProviderInterface import (
    OptionContractMeta, OptionEodBar,
)
from ba2_providers.options.parquet_store import OptionHistoryParquetStore
import tools.warm_options_history as w


_START, _END = date(2024, 1, 1), date(2024, 12, 31)
_E1, _E2, _E3 = date(2024, 3, 15), date(2024, 6, 21), date(2024, 12, 20)


def _occ(expiry, strike=100.0):
    return f"AAPL{expiry:%y%m%d}C{int(strike * 1000):08d}"


def _unit():
    return w.SymbolUnit(
        underlying="AAPL",
        pending_expiries=[_E1, _E2, _E3],
        contracts=[OptionContractMeta(occ_symbol=_occ(e), underlying="AAPL",
                                      option_type="call", strike=100.0, expiry=e)
                   for e in (_E1, _E2, _E3)])


def _bar(expiry, on):
    return OptionEodBar(occ_symbol=_occ(expiry), bar_date=on, open=1.0, high=1.0, low=1.0,
                        close=1.0, volume=1, bid=0.9, ask=1.1)


class _Provider:
    """Yields chronologically, then optionally explodes -- the ABEV shape."""

    def __init__(self, bars, fail_after=None, exc=None):
        self._bars, self._fail_after, self._exc = bars, fail_after, exc
        self.attempts = 0

    def fetch_underlying_eod_bars(self, underlying, *, start, end):
        self.attempts += 1
        for n, b in enumerate(self._bars, 1):
            yield b
            if self._fail_after is not None and n == self._fail_after:
                raise self._exc


def _ns(**kw):
    base = dict(max_retries=1, backoff=0.0, rate_limit=0, progress_every=0)
    base.update(kw)
    return argparse.Namespace(**base)


def _run(tmp_path, provider, ns=None):
    store = OptionHistoryParquetStore(root=str(tmp_path / "store"))
    stats = w.run_symbol_units([_unit()], provider, store, _START, _END, ns or _ns(),
                               clock=lambda: __import__("datetime").datetime(2024, 1, 1),
                               sleep=lambda s: None, log=lambda m: None)
    return store, stats


# chronological: E1's bars, then a later date (closing E1), then E2's, etc.
_BARS = [
    _bar(_E1, date(2024, 2, 1)),
    _bar(_E1, date(2024, 3, 14)),
    _bar(_E2, date(2024, 4, 1)),     # > E1 -> E1 closes here
    _bar(_E2, date(2024, 6, 20)),
    _bar(_E3, date(2024, 7, 1)),     # > E2 -> E2 closes here
    _bar(_E3, date(2024, 12, 19)),
]


def test_a_failure_mid_fetch_keeps_the_partitions_that_already_closed(tmp_path):
    """The ABEV case: without the flush this wrote NOTHING after 941 s."""
    p = _Provider(_BARS, fail_after=5, exc=RuntimeError("boom"))
    store, stats = _run(tmp_path, p)

    from ba2_providers.options.parquet_store import PartitionState
    assert store.partition_state("AAPL", _E1, _START, _END) is PartitionState.COMPLETE
    assert store.partition_state("AAPL", _E2, _START, _END) is PartitionState.COMPLETE
    assert store.partition_state("AAPL", _E3, _START, _END) is not PartitionState.COMPLETE
    assert stats.units_written == 2, "the two closed expiries must be durable"
    assert stats.units_failed == 1, "only the still-open tail is owed"


def test_a_clean_run_writes_every_expiry_exactly_once(tmp_path):
    """Three expiries in, three partitions out -- the flush must not double-write the ones it
    closes early, nor drop the one it closes at the end."""
    from ba2_providers.options.parquet_store import PartitionState

    store, stats = _run(tmp_path, _Provider(_BARS))
    assert stats.units_written == 3
    assert stats.units_empty == 0 and stats.units_failed == 0
    assert stats.rows == len(_BARS), "every yielded bar must land in exactly one partition"
    for e in (_E1, _E2, _E3):
        assert store.partition_state("AAPL", e, _START, _END) is PartitionState.COMPLETE


def test_a_retry_does_not_rewrite_an_already_flushed_partition(tmp_path):
    """A retry re-fetches the symbol (the wide call cannot ask for a subset), so the flushed
    expiries must be filtered out rather than written twice."""
    p = _Provider(_BARS, fail_after=5, exc=ConnectionError("transient"))
    store, stats = _run(tmp_path, p, _ns(max_retries=2))

    assert p.attempts == 2, "a transient error must be retried"
    # E1/E2 flushed on attempt 1; attempt 2 must not double-count them.
    assert stats.units_written == 2, f"expected 2 writes, got {stats.units_written}"


def test_bars_for_an_expiry_that_never_closes_are_still_written_at_the_end(tmp_path):
    """E3 never sees a later-dated bar, so only the end-of-symbol tail writes it."""
    store, stats = _run(tmp_path, _Provider(_BARS))
    from ba2_providers.options.parquet_store import PartitionState
    assert store.partition_state("AAPL", _E3, _START, _END) is PartitionState.COMPLETE


# --------------------------------------------------------------------------- #
# Shared work queue (2026-09-04).
#
# Symbol cost spans two orders of magnitude -- AEFC 18s (no listed options) vs AAPL ~3.7h --
# so the previous fixed contiguous slices left threads idle on a straggler. Observed live: with
# 7 symbols over 4 threads, t3 finished its entire slice and idled ~2h while t1 ground through
# AAPL. A shared queue lets a thread that draws cheap symbols simply take more of them.
# --------------------------------------------------------------------------- #
def _cheap_unit(sym, expiry):
    return w.SymbolUnit(underlying=sym, pending_expiries=[expiry],
                        contracts=[OptionContractMeta(occ_symbol=_occ(expiry), underlying=sym,
                                                      option_type="call", strike=100.0,
                                                      expiry=expiry)])


class _CountingProvider:
    """Records which thread fetched which symbol, so the distribution is observable."""

    def __init__(self, slow=()):
        self.by_thread = {}
        self.slow = set(slow)
        self._lock = __import__("threading").Lock()

    def fetch_underlying_eod_bars(self, underlying, *, start, end):
        import threading as _t
        import time as _time
        name = _t.current_thread().name
        with self._lock:
            self.by_thread.setdefault(name, []).append(underlying)
        if underlying in self.slow:
            _time.sleep(0.4)          # the straggler
        yield _bar(_E1, date(2024, 2, 1))


def test_a_straggler_does_not_leave_the_other_threads_idle(tmp_path):
    """The whole point: one slow symbol must not stop the others from draining the queue."""
    store = OptionHistoryParquetStore(root=str(tmp_path / "s"))
    units = [_cheap_unit("SLOW", _E1)] + [_cheap_unit(f"C{i}", _E1) for i in range(11)]
    p = _CountingProvider(slow={"SLOW"})

    stats = w.run_symbol_units_concurrent(
        units, p, store, _START, _END, _ns(),
        clock=lambda: __import__("datetime").datetime(2024, 1, 1),
        sleep=lambda s: None, log=lambda m: None, concurrency=4)

    fetched = sum(len(v) for v in p.by_thread.values())
    assert fetched == 12, f"every symbol must be fetched exactly once, got {fetched}"
    slow_thread = next(t for t, syms in p.by_thread.items() if "SLOW" in syms)
    n_slow = len(p.by_thread[slow_thread])

    # THE DISCRIMINATING ASSERTION. 12 units over 4 threads is exactly 3 each under the old
    # fixed contiguous slices, whatever each symbol costs -- so "spread across threads" alone
    # would pass either way and prove nothing. Under a queue the thread stuck on SLOW draws
    # FEWER than its even share, because the others keep pulling while it waits.
    assert n_slow < 3, (
        f"the slow thread took {n_slow} symbols; a static 12/4 split would give exactly 3, so "
        f"work is not being pulled on demand")
    assert max(len(v) for v in p.by_thread.values()) > 3, (
        "some thread must have taken MORE than its even share -- that is the win")
    assert stats.units_failed == 0


def test_every_symbol_is_processed_exactly_once_under_the_queue(tmp_path):
    """A queue must not drop or duplicate work when threads outnumber... or under-number it."""
    for conc in (2, 4, 8):
        store = OptionHistoryParquetStore(root=str(tmp_path / f"s{conc}"))
        units = [_cheap_unit(f"S{i}", _E1) for i in range(7)]
        p = _CountingProvider()
        w.run_symbol_units_concurrent(
            units, p, store, _START, _END, _ns(),
            clock=lambda: __import__("datetime").datetime(2024, 1, 1),
            sleep=lambda s: None, log=lambda m: None, concurrency=conc)
        got = sorted(s for v in p.by_thread.values() for s in v)
        assert got == sorted(f"S{i}" for i in range(7)), f"conc={conc}: {got}"


def test_a_thread_that_raises_still_reports_the_work_it_finished(tmp_path):
    """Partitions already written are durable, so their counters must survive the exception --
    dropping them would under-report a resumable run's real progress."""
    class _Boom(_CountingProvider):
        def fetch_underlying_eod_bars(self, underlying, *, start, end):
            if underlying == "BOOM":
                raise KeyboardInterrupt("stop")
            yield _bar(_E1, date(2024, 2, 1))

    store = OptionHistoryParquetStore(root=str(tmp_path / "boom"))
    units = [_cheap_unit("BOOM", _E1)]
    try:
        w.run_symbol_units_concurrent(
            units, _Boom(), store, _START, _END, _ns(),
            clock=lambda: __import__("datetime").datetime(2024, 1, 1),
            sleep=lambda s: None, log=lambda m: None, concurrency=2)
    except KeyboardInterrupt:
        pass  # re-raised on the main thread, which is the contract
