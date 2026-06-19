"""Hermetic OHLCV cache reads: native-cache discovery + hard-error on a genuine miss.

A backtest is hermetic — it never network-fetches mid-run. ``MemoizedOHLCVProvider(cached_only=
True)`` serves a symbol's bars from the single native cache
``CACHE_FOLDER/<ProviderClassName>/<SYM>_<interval>.parquet`` (the unified cache that both
``get_ohlcv_data`` and ``ba2-test fetch-cache`` write). When a symbol is absent, it raises
``BacktestCacheMiss`` (a hard error naming what to cache) instead of silently skipping it or
fetching live. ``AsOfPriceSource.preload`` aggregates those misses into one actionable error.
"""
from __future__ import annotations

from datetime import datetime

import pandas as pd
import pytest

from app.services.backtest import price_source as ps_mod
from app.services.backtest.price_source import (
    AsOfPriceSource,
    BacktestCacheMiss,
    MemoizedOHLCVProvider,
)


# A real-provider stand-in: class name drives the native-cache dir; having get_provider_name()
# marks it "network-backed" -> hermetic error on miss (vs an in-memory test provider).
class FMPOHLCVProvider:  # noqa: D401 - name is load-bearing (native cache dir == class name)
    def get_provider_name(self) -> str:
        return "fmp"

    def get_ohlcv_data(self, *a, **k):  # pragma: no cover - must NOT be called in hermetic mode
        raise AssertionError("hermetic backtest must not call the live provider")


def _bars(sym: str, n: int = 5) -> pd.DataFrame:
    dates = pd.date_range("2024-01-02", periods=n, freq="D")
    return pd.DataFrame(
        {
            "Date": dates,
            "Open": 100.0, "High": 101.0, "Low": 99.0, "Close": 100.5,
            "Volume": 1_000_000,
        }
    )


@pytest.fixture(autouse=True)
def _clear_memos():
    """The full-series memo + worker bar cache are process-global; clear them per test so a
    symbol cached under one tmp layout can't leak into another test's run."""
    ps_mod.clear_ohlcv_memo()
    ps_mod.clear_worker_bar_cache()
    yield
    ps_mod.clear_ohlcv_memo()
    ps_mod.clear_worker_bar_cache()


@pytest.fixture
def native_dir(tmp_path, monkeypatch):
    """Point the native cache at a tmp dir and return the FMPOHLCVProvider/ provider dir."""
    native = tmp_path / "FMPOHLCVProvider"
    native.mkdir()
    from ba2_common.core import native_cache
    monkeypatch.setattr(native_cache, "CACHE_FOLDER", str(tmp_path), raising=False)
    return native


def _mk(interval="1d"):
    return MemoizedOHLCVProvider(
        FMPOHLCVProvider(), datetime(2024, 1, 1), datetime(2024, 2, 1),
        interval=interval, cached_only=True,
    )


def test_reads_native_layout(native_dir):
    _bars("AAA").to_parquet(native_dir / "AAA_1d.parquet", index=False)
    df = _mk()._read_cached_df("AAA", "1d")
    assert df is not None and len(df) == 5


def test_missing_raises(native_dir):
    m = _mk()
    assert m._read_cached_df("CCC", "1d") is None
    with pytest.raises(BacktestCacheMiss):
        m._full("CCC", "1d")


def test_preload_aggregates_missing(native_dir):
    """preload loads what's cached and fails ONCE, naming every uncached symbol."""
    _bars("AAA").to_parquet(native_dir / "AAA_1d.parquet", index=False)
    _bars("BBB").to_parquet(native_dir / "BBB_1d.parquet", index=False)

    src = AsOfPriceSource(ohlcv_provider=_mk(), interval="1d")
    with pytest.raises(BacktestCacheMiss) as ei:
        src.preload(["AAA", "BBB", "CCC", "DDD"], datetime(2024, 1, 3), datetime(2024, 1, 10),
                    warmup_days=1)
    msg = str(ei.value)
    assert "2 of 4" in msg  # CCC + DDD missing
    assert "CCC" in msg and "DDD" in msg
    assert "AAA" not in msg and "BBB" not in msg
