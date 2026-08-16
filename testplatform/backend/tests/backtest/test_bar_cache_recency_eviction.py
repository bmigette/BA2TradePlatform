"""Recency bound on the parsed-bar cache: keep a series only while some individual in the last N
has used it.

WHY THIS EXISTS (measured 2026-08-15, goal2020 FMPEarningsDrift small band, 5min, 1368 symbols):
each individual's screener touches a SUBSET (~565 symbols) and different individuals touch
different subsets, so the cache accumulated their UNION and climbed 506 -> 1125 symbols /
3.9GB -> 8.5GB inside ONE generation, heading for ~10.5GB per process. The count cap could not
help: 1500 was set ABOVE the 1368-symbol universe, so the LRU never fired. A count cap has to be
re-guessed per universe. This bound is self-tuning.
"""
import numpy as np
import pytest

from app.services.backtest import price_source as ps


class _FakeOHLCV:
    """Minimal provider: every symbol has 3 bars. Counts reads so tests can prove a re-parse."""

    def __init__(self):
        self.reads = []

    def read_window(self, symbol, start, end, interval):
        import pandas as pd
        self.reads.append(symbol)
        return pd.DataFrame({
            "Date": pd.to_datetime(["2024-01-02", "2024-01-03", "2024-01-04"]),
            "Open": [1.0, 2.0, 3.0], "High": [1.0, 2.0, 3.0],
            "Low": [1.0, 2.0, 3.0], "Close": [1.0, 2.0, 3.0], "Volume": [10, 20, 30],
        })


@pytest.fixture(autouse=True)
def _clean():
    ps.clear_worker_bar_cache()
    ps._TRIAL_SEQ = 0
    yield
    ps.clear_worker_bar_cache()
    ps._TRIAL_SEQ = 0


def _preload(provider, symbols):
    from datetime import datetime
    src = ps.AsOfPriceSource(ohlcv_provider=provider, interval="1d")
    src.preload(symbols, datetime(2024, 1, 2), datetime(2024, 1, 4), warmup_days=0)
    return src


def _cached_symbols():
    return {k[0] for k in ps._WORKER_BAR_CACHE}


def test_series_untouched_for_n_individuals_is_dropped(monkeypatch):
    monkeypatch.setattr(ps, "_WORKER_BAR_CACHE_TRIALS", 2)
    prov = _FakeOHLCV()

    _preload(prov, ["AAA"])                  # individual 1 uses AAA
    assert _cached_symbols() == {"AAA"}
    _preload(prov, ["BBB"])                  # individual 2: AAA still within the 2-window
    assert _cached_symbols() == {"AAA", "BBB"}
    _preload(prov, ["CCC"])                  # individual 3: AAA now 2 individuals stale -> gone
    assert _cached_symbols() == {"BBB", "CCC"}


def test_a_symbol_that_keeps_being_used_is_never_dropped(monkeypatch):
    """The reuse this cache exists for must survive: a symbol touched every individual stays hot
    however many individuals run."""
    monkeypatch.setattr(ps, "_WORKER_BAR_CACHE_TRIALS", 2)
    prov = _FakeOHLCV()
    for i in range(6):
        _preload(prov, ["HOT", f"COLD{i}"])
    assert "HOT" in _cached_symbols()
    assert prov.reads.count("HOT") == 1, "HOT was re-parsed despite being used every individual"


def test_eviction_is_result_neutral_and_costs_only_a_reparse(monkeypatch):
    """Dropping a series must never change results -- a later miss re-reads the same source."""
    monkeypatch.setattr(ps, "_WORKER_BAR_CACHE_TRIALS", 1)
    prov = _FakeOHLCV()
    first = _preload(prov, ["AAA"])
    before = np.array(first._c["AAA"], copy=True)

    _preload(prov, ["ZZZ"])                  # pushes AAA out (window of 1)
    assert "AAA" not in _cached_symbols()

    again = _preload(prov, ["AAA"])
    assert prov.reads.count("AAA") == 2, "expected exactly one re-parse"
    np.testing.assert_array_equal(again._c["AAA"], before)


def test_window_of_one_bounds_a_process_to_a_single_individual(monkeypatch):
    """The tight setting for a memory-constrained box: nothing but the current individual's set."""
    monkeypatch.setattr(ps, "_WORKER_BAR_CACHE_TRIALS", 1)
    prov = _FakeOHLCV()
    _preload(prov, ["A", "B", "C"])
    _preload(prov, ["D", "E"])
    assert _cached_symbols() == {"D", "E"}


def test_flush_mode_bounds_the_PEAK_not_just_the_resting_size(monkeypatch):
    """THE test the first two attempts failed.

    N>=1 evicts at the END of preload, so while individual B is loading, the cache still holds all
    of A plus B-so-far -- peak |A u B|. Both N=2 and N=1 shipped and still exhausted a 64GB box,
    because the peak is what OOMs, not the resting size. Flush mode (N=0, default) drops everything
    at the START, so the peak is |B|.

    Asserted by observing the cache DURING B's load, not after it.
    """
    monkeypatch.setattr(ps, "_WORKER_BAR_CACHE_TRIALS", 0)
    prov = _FakeOHLCV()
    _preload(prov, [f"A{i}" for i in range(5)])          # individual A: 5 symbols
    assert len(_cached_symbols()) == 5

    seen_during_load = []

    class _Watching(_FakeOHLCV):
        def read_window(self, symbol, start, end, interval):
            seen_during_load.append(len(ps._WORKER_BAR_CACHE))
            return super().read_window(symbol, start, end, interval)

    _preload(_Watching(), [f"B{i}" for i in range(3)])   # individual B: 3 symbols
    # If A were still resident while B loaded, the first observation would be >= 5.
    assert seen_during_load[0] == 0, (
        f"A's series were still held while B was loading (saw {seen_during_load[0]} entries) -- "
        "the peak is |A u B|, which is exactly the OOM that N=2 and N=1 both hit")
    assert max(seen_during_load) < 5
    assert _cached_symbols() == {"B0", "B1", "B2"}


def test_flush_mode_is_result_neutral(monkeypatch):
    """Flushing every individual must change nothing but timing -- same bars, re-parsed."""
    monkeypatch.setattr(ps, "_WORKER_BAR_CACHE_TRIALS", 0)
    prov = _FakeOHLCV()
    first = _preload(prov, ["AAA"])
    before = np.array(first._c["AAA"], copy=True)
    _preload(prov, ["ZZZ"])
    again = _preload(prov, ["AAA"])
    assert prov.reads.count("AAA") == 2          # re-parsed, as designed
    np.testing.assert_array_equal(again._c["AAA"], before)


def test_count_cap_still_backstops_a_single_oversized_individual(monkeypatch):
    """One individual whose own set exceeds the cap must still be bounded -- the recency sweep
    alone would keep every symbol it just touched."""
    monkeypatch.setattr(ps, "_WORKER_BAR_CACHE_TRIALS", 2)
    monkeypatch.setattr(ps, "_WORKER_BAR_CACHE_MAX", 3)
    prov = _FakeOHLCV()
    _preload(prov, [f"S{i}" for i in range(10)])
    assert len(ps._WORKER_BAR_CACHE) == 3
    # the tracking dict must not retain keys the cap dropped, or it would leak across a long run
    assert len(ps._BAR_CACHE_LAST_USED) == 3


def test_tracking_dict_does_not_leak_on_hermetic_misses(monkeypatch):
    """A symbol with no cached bars is never stored, so it must never enter the tracking dict."""
    monkeypatch.setattr(ps, "_WORKER_BAR_CACHE_TRIALS", 2)

    class _MissingAAA(_FakeOHLCV):
        def read_window(self, symbol, start, end, interval):
            if symbol == "GONE":
                raise ps.BacktestCacheMiss("no bars")
            return super().read_window(symbol, start, end, interval)

    with pytest.raises(ps.BacktestCacheMiss):
        _preload(_MissingAAA(), ["AAA", "GONE"])
    assert all(k[0] != "GONE" for k in ps._BAR_CACHE_LAST_USED)
