"""A hermetic backtest reads the Alpaca history cache and never goes to a network
(plan 2026-10-20-alpaca-history-source, Task 5)."""
from __future__ import annotations

from datetime import datetime

import pandas as pd
import pytest

from app.services.backtest import price_source as ps_mod
from app.services.backtest.price_source import BacktestCacheMiss, MemoizedOHLCVProvider


@pytest.fixture(autouse=True)
def _clear_memos():
    ps_mod.clear_ohlcv_memo()
    ps_mod.clear_worker_bar_cache()
    yield
    ps_mod.clear_ohlcv_memo()
    ps_mod.clear_worker_bar_cache()


@pytest.fixture
def cache_dir(tmp_path, monkeypatch):
    from ba2_common.core import native_cache
    monkeypatch.setattr(native_cache, "CACHE_FOLDER", str(tmp_path), raising=False)
    d = tmp_path / "AlpacaHistoryOHLCVProvider"
    d.mkdir()
    return d


def _bars(n=5):
    dates = pd.date_range("2024-01-02", periods=n, freq="D")
    return pd.DataFrame({"Date": dates, "Open": 100.0, "High": 101.0, "Low": 99.0,
                         "Close": 100.5, "Volume": 1_000_000, "effective_date": dates})


def _mk(interval="1d"):
    from ba2_providers import get_provider
    return MemoizedOHLCVProvider(get_provider("ohlcv", "alpaca_hist"), datetime(2024, 1, 1),
                                 datetime(2024, 2, 1), interval=interval, cached_only=True)


def test_cache_only_read_returns_the_fixture_bars(cache_dir):
    _bars().to_parquet(cache_dir / "AAA_1d.parquet", index=False)
    df = _mk()._read_cached_df("AAA", "1d")
    assert df is not None and len(df) == 5


def test_a_read_beyond_the_cache_is_a_cache_miss_never_a_fetch(cache_dir):
    with pytest.raises(BacktestCacheMiss):
        _mk()._full("NOPE", "1d")
