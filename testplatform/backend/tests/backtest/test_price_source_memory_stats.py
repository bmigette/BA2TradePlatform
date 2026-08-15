"""price_source.memory_stats — the per-trial memory telemetry backing the worker-crash
diagnostics (2026-07-09 WinError-1450 incident): reports entries + estimated MB for the
LRU bar cache and the full-series memo, cheap enough to run once per trial."""
import numpy as np

from app.services.backtest import price_source as ps


def _fake_series(n_bars: int):
    return ([object()] * n_bars, np.zeros(n_bars), np.zeros(n_bars),
            np.zeros(n_bars), np.zeros(n_bars), np.zeros(n_bars))


def test_memory_stats_counts_bar_cache_entries_and_bytes():
    ps.clear_worker_bar_cache()
    try:
        # ~78k bars (3yr x 5min) x 5 float64 arrays ≈ 3.1MB + key overhead — a realistic
        # single-symbol footprint, so the MB estimate must be visibly non-zero.
        ps._WORKER_BAR_CACHE[("AAPL", "5min", "s", "e")] = _fake_series(78_000)
        ps._WORKER_BAR_CACHE[("MSFT", "5min", "s", "e")] = _fake_series(78_000)
        stats = ps.memory_stats()
        assert stats["bar_cache"]["entries"] == 2
        assert stats["bar_cache"]["symbols"] == 2
        assert stats["bar_cache"]["bars"] == 156_000
        assert 5.0 < stats["bar_cache"]["mb"] < 20.0  # ~2 x (3.1MB arrays + ~3.9MB keys)
        assert stats["series_memo"]["entries"] == 0
    finally:
        ps.clear_worker_bar_cache()


def test_bar_cache_symbols_distinct_from_entries_across_windows():
    """A long-lived worker holding two WINDOWS of one symbol has 2 entries but 1 symbol.
    That divergence is the accumulation signal the per-trial log exists to surface."""
    ps.clear_worker_bar_cache()
    try:
        ps._WORKER_BAR_CACHE[("AAPL", "1d", "s1", "e1")] = _fake_series(10)
        ps._WORKER_BAR_CACHE[("AAPL", "1d", "s2", "e2")] = _fake_series(10)
        stats = ps.memory_stats()
        assert stats["bar_cache"]["entries"] == 2
        assert stats["bar_cache"]["symbols"] == 1
    finally:
        ps.clear_worker_bar_cache()


def test_series_memo_counts_dataframe_tuples():
    """REGRESSION: _FULL_SERIES_MEMO stores (DataFrame, dates) TUPLES, but memory_stats called
    .memory_usage() on the tuple -- AttributeError, swallowed by the bare except, so this layer
    reported 0.0 MB always. It is the layer holding the full pandas frames, i.e. the one most
    worth watching, and the bug made it invisible."""
    import pandas as pd

    ps.clear_ohlcv_memo()
    try:
        df = pd.DataFrame({"Open": np.zeros(50_000), "Close": np.zeros(50_000)})
        dates = np.zeros(50_000, dtype="datetime64[ns]")
        ps._FULL_SERIES_MEMO[("AAPL", "1d", "s", "e")] = (df, dates)
        stats = ps.memory_stats()
        assert stats["series_memo"]["entries"] == 1
        assert stats["series_memo"]["symbols"] == 1
        assert stats["series_memo"]["rows"] == 50_000
        # 2 float64 cols + the dates array = ~1.2MB. The pre-fix code returned exactly 0.0.
        assert stats["series_memo"]["mb"] > 0.5
    finally:
        ps.clear_ohlcv_memo()


def test_memory_stats_empty_caches():
    ps.clear_worker_bar_cache()
    ps.clear_ohlcv_memo()
    stats = ps.memory_stats()
    assert stats["bar_cache"] == {"entries": 0, "symbols": 0, "bars": 0, "mb": 0.0}
    assert stats["series_memo"] == {"entries": 0, "symbols": 0, "rows": 0, "mb": 0.0}


def test_bar_cache_max_env_override(monkeypatch):
    """BT_BAR_CACHE_MAX tunes the per-process LRU bound (memory-constrained worker hosts)."""
    import importlib
    monkeypatch.setenv("BT_BAR_CACHE_MAX", "7")
    mod = importlib.reload(ps)
    try:
        assert mod._WORKER_BAR_CACHE_MAX == 7
    finally:
        monkeypatch.delenv("BT_BAR_CACHE_MAX")
        importlib.reload(mod)
