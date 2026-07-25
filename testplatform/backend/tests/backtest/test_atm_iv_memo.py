"""get_atm_iv result memo (2026-07-25).

The chain/bar caches stop the SQL re-reads, but get_atm_iv still re-ran its full scan on
every call: for one (underlying, as_of) it walks every CALL in the 20-45 DTE band doing a
per-contract bar lookup. Measured 56 ms/call — fine for a signal-driven expert, fatal for a
portfolio SCANNER. PremiumSeller evaluates ~98 universe symbols on every entry bar, so one
trial cost ~98 x 440 bars x 56 ms ~= 40 min of pure IV lookups; an 80x8 GA produced ZERO
completed individuals in 34 minutes and every remote trial hit the 1800s timeout.

Every GA trial re-evaluates the identical (symbol, date) pairs, so a worker-process memo is
near-100% hit rate after the first trial.
"""
from datetime import date

import pytest

from app.services.backtest import options_provider as op
from app.services.backtest.options_provider import (
    HistoricalOptionsProvider, clear_worker_options_cache,
)


class _CountingProvider(HistoricalOptionsProvider):
    """Counts how often the UNCACHED scan body actually runs."""

    def __init__(self, db_path, results):
        super().__init__(db_path)
        self._results = results
        self.compute_calls = 0

    def _compute_atm_iv(self, underlying, as_of):
        self.compute_calls += 1
        return self._results.get((underlying, as_of))


@pytest.fixture(autouse=True)
def _clean_cache():
    clear_worker_options_cache()
    yield
    clear_worker_options_cache()


D1, D2 = date(2024, 10, 1), date(2024, 10, 2)


def test_repeated_lookups_compute_once_per_symbol_date():
    p = _CountingProvider("db", {("AAPL", D1): 0.25, ("AAPL", D2): 0.30})
    assert [p.get_atm_iv("AAPL", D1) for _ in range(50)] == [0.25] * 50
    assert p.compute_calls == 1, "second+ lookups must hit the memo"

    assert p.get_atm_iv("AAPL", D2) == 0.30
    assert p.compute_calls == 2, "a DIFFERENT date is a distinct key"


def test_none_results_are_cached_too():
    """A symbol with no usable chain returns None — and re-deriving that costs a FULL scan
    (it only returns None after walking everything), so it is the most important case to
    memo. A naive `.get(key) is None` miss-check would re-scan every time."""
    p = _CountingProvider("db", {})
    assert [p.get_atm_iv("NOPE", D1) for _ in range(50)] == [None] * 50
    assert p.compute_calls == 1


def test_distinct_symbols_and_db_paths_do_not_collide():
    p = _CountingProvider("db", {("AAPL", D1): 0.25, ("MSFT", D1): 0.40})
    assert p.get_atm_iv("AAPL", D1) == 0.25
    assert p.get_atm_iv("MSFT", D1) == 0.40
    assert p.compute_calls == 2

    other = _CountingProvider("OTHER_DB", {("AAPL", D1): 0.99})
    assert other.get_atm_iv("AAPL", D1) == 0.99, "db_path is part of the key"
    assert other.compute_calls == 1


def test_clear_worker_options_cache_drops_the_memo():
    p = _CountingProvider("db", {("AAPL", D1): 0.25})
    p.get_atm_iv("AAPL", D1)
    clear_worker_options_cache()
    p.get_atm_iv("AAPL", D1)
    assert p.compute_calls == 2, "cache clear must force a recompute (test isolation)"


def test_memo_is_lru_bounded(monkeypatch):
    """Unbounded growth would be a slow leak across a long GA run."""
    monkeypatch.setattr(op, "_ATM_IV_CACHE_MAX", 10)
    p = _CountingProvider("db", {})
    for i in range(1, 41):
        p.get_atm_iv("AAPL", date(2024, 10, 1).replace(day=min(i, 28)))
    assert len(op._WORKER_ATM_IV_CACHE) <= 10
