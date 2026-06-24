"""Regression test for the cross-job OHLCV-memo eviction (worker memory-leak fix).

The process-global `_FULL_SERIES_MEMO` is intentionally kept across one optimization job's GA
population, but the pool workers + master process are long-lived across jobs. Without eviction the
memo accumulated every band's universe (504-symbol large-cap, then 814-symbol mid-cap, ...) and was
never freed. `evict_memo_if_working_set_changed` drops it when the working set (universe + window +
interval) changes, while preserving in-job reuse.
"""
from app.services.backtest import price_source as ps


def _reset():
    ps._FULL_SERIES_MEMO.clear()
    ps._MEMO_WORKING_SET_SIG = None


def test_same_working_set_keeps_memo():
    _reset()
    sig = (("AAPL", "MSFT"), "5min", "2023-01-01", "2026-01-01", 5)
    assert ps.evict_memo_if_working_set_changed(sig) is True  # first time: sets the sig
    ps._FULL_SERIES_MEMO[("AAPL", "5min", "a", "b")] = "df"   # job loads a series
    # Every later trial of the SAME job reuses the memo (no eviction).
    assert ps.evict_memo_if_working_set_changed(sig) is False
    assert len(ps._FULL_SERIES_MEMO) == 1


def test_different_universe_frees_memo():
    _reset()
    large = (("AAPL", "ABBV", "ABT"), "5min", "2023-01-01", "2026-01-01", 5)
    mid = (("AA", "AAL", "AAON"), "5min", "2023-01-01", "2026-01-01", 5)
    ps.evict_memo_if_working_set_changed(large)
    ps._FULL_SERIES_MEMO[("AAPL", "5min", "a", "b")] = "df"   # large band loaded
    # Next band has a DIFFERENT universe -> the prior band's series are freed.
    assert ps.evict_memo_if_working_set_changed(mid) is True
    assert len(ps._FULL_SERIES_MEMO) == 0


def test_window_or_interval_change_frees_memo():
    _reset()
    base = (("AAPL",), "5min", "2023-01-01", "2026-01-01", 5)
    ps.evict_memo_if_working_set_changed(base)
    ps._FULL_SERIES_MEMO[("AAPL", "5min", "a", "b")] = "df"
    # A different interval (or window) is a different working set -> evict.
    diff_interval = (("AAPL",), "1d", "2023-01-01", "2026-01-01", 5)
    assert ps.evict_memo_if_working_set_changed(diff_interval) is True
    assert len(ps._FULL_SERIES_MEMO) == 0


def test_clear_ohlcv_memo_still_empties():
    _reset()
    ps._FULL_SERIES_MEMO[("X", "5min", "a", "b")] = "df"
    ps.clear_ohlcv_memo()
    assert len(ps._FULL_SERIES_MEMO) == 0
