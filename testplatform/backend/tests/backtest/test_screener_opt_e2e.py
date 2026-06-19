"""Screener-settings optimization: engine gating unit slice (Task 11).

The pure-logic slices (metric-store filter, gene decode, trial-config wiring) are covered in
``BA2TradeProviders/tests/test_screener_metric_store.py`` and ``test_screener_genes.py``; this
file unit-tests the engine's per-bar gating HELPER — the single function that turns a run's
``screener_runtime`` block into the day's allowed-to-ENTER symbol list. The full per-bar
integration (entries gated, exits/management untouched) is the engine's existing golden
regression staying green with ``screener_runtime`` absent (a cheap no-op).
"""
from __future__ import annotations

import datetime as dt

from app.services.backtest.daily_engine import _screened_symbols_for_bar


def test_screened_symbols_for_bar_filters_by_day(tmp_path):
    from ba2_providers.screener import metric_store as ms
    import pandas as pd

    store = str(tmp_path / "s")
    ms.write_partitions(store, pd.DataFrame({
        "symbol": ["AAA", "BBB"], "date": ["2023-03-01", "2023-03-01"], "close": [10, 20.0],
        "market_cap": [5e9, 8e8], "relative_volume": [2.0, 2.0], "price_drop_pct": [20.0, 20.0],
        "sector": ["T", "T"], "volume": [2e6, 2e6], "price": [10, 20.0]}))
    rt = {"store": store, "settings": {"market_cap_min": 1e9, "max_stocks": 5}}
    got = _screened_symbols_for_bar(rt, dt.datetime(2023, 3, 1))
    assert got == ["AAA"]               # BBB filtered (cap 8e8 < 1e9)


def test_screened_symbols_for_bar_is_none_without_screener():
    # No screener_runtime -> None (the gate is a no-op; existing runs unchanged).
    assert _screened_symbols_for_bar(None, dt.datetime(2023, 3, 1)) is None
    assert _screened_symbols_for_bar({}, dt.datetime(2023, 3, 1)) is None
