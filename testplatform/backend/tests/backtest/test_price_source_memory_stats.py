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
        assert 5.0 < stats["bar_cache"]["mb"] < 20.0  # ~2 x (3.1MB arrays + ~3.9MB keys)
        assert stats["series_memo"]["entries"] == 0
    finally:
        ps.clear_worker_bar_cache()


def test_memory_stats_empty_caches():
    ps.clear_worker_bar_cache()
    stats = ps.memory_stats()
    assert stats["bar_cache"] == {"entries": 0, "mb": 0.0}
    assert stats["series_memo"]["entries"] == 0


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
