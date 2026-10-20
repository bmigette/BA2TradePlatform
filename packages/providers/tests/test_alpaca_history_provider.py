"""AlpacaHistoryOHLCVProvider: the cache-only OHLCV history built by ``tools/prepare_history.py
--source alpaca`` (plan 2026-10-20-alpaca-history-source, Task 5).

Keyless, served from ``CACHE_FOLDER/AlpacaHistoryOHLCVProvider/`` by the base cache layer, and it
never goes to a vendor. The hermetic MemoizedOHLCVProvider read lives in the backend suite
(``testplatform/backend/tests/backtest/test_alpaca_history_hermetic.py``)."""
from datetime import datetime

import pandas as pd
import pytest

from ba2_common.core import native_cache

FOLDER = "AlpacaHistoryOHLCVProvider"


@pytest.fixture
def cache_root(tmp_path, monkeypatch):
    monkeypatch.setattr(native_cache, "CACHE_FOLDER", str(tmp_path))
    import ba2_common.config as cfg
    monkeypatch.setattr(cfg, "CACHE_FOLDER", str(tmp_path))
    d = tmp_path / FOLDER
    d.mkdir()
    return d


def _bars(start: str, n: int, freq: str) -> pd.DataFrame:
    dates = pd.date_range(start, periods=n, freq=freq)
    return pd.DataFrame({"Date": dates, "Open": 10.0, "High": 11.0, "Low": 9.0, "Close": 10.5,
                         "Volume": 1000, "effective_date": dates})


def test_constructs_without_any_key_and_names_itself():
    from ba2_providers.ohlcv.AlpacaHistoryOHLCVProvider import AlpacaHistoryOHLCVProvider
    p = AlpacaHistoryOHLCVProvider()
    assert p.get_provider_name() == "alpaca_hist"
    assert p.WRITES_FULL_FETCH_MARKER is False
    assert p._split_calendar("AAA", "1d") is None
    assert p.validate_config() is True


def test_registry_returns_it_with_no_key_configured():
    from ba2_providers import get_provider
    from ba2_providers.ohlcv.AlpacaHistoryOHLCVProvider import AlpacaHistoryOHLCVProvider
    assert isinstance(get_provider("ohlcv", "alpaca_hist"), AlpacaHistoryOHLCVProvider)


def test_cache_folder_is_the_class_name(cache_root):
    from ba2_providers.ohlcv.AlpacaHistoryOHLCVProvider import AlpacaHistoryOHLCVProvider
    from ba2_common.core.ohlcv_history import history_cache_dirname
    assert AlpacaHistoryOHLCVProvider().cache_folder.endswith(FOLDER)
    assert history_cache_dirname("alpaca") == FOLDER


@pytest.mark.parametrize("interval,freq,start", [("1d", "D", "2024-01-02"),
                                                  ("5min", "5min", "2024-01-02 09:30")])
def test_both_intervals_are_served_from_the_cache(cache_root, interval, freq, start):
    from ba2_providers.ohlcv.AlpacaHistoryOHLCVProvider import AlpacaHistoryOHLCVProvider
    _bars(start, 6, freq).to_parquet(cache_root / f"AAA_{interval}.parquet", index=False)
    df = AlpacaHistoryOHLCVProvider().get_ohlcv_data(
        "AAA", start_date=datetime(2024, 1, 1), end_date=datetime(2024, 1, 31), interval=interval)
    assert len(df) == 6
    assert list(df.columns) == ["Date", "Open", "High", "Low", "Close", "Volume"]


def test_meta_folder_is_never_read_as_bars(cache_root):
    from ba2_providers.ohlcv.AlpacaHistoryOHLCVProvider import (
        AlpacaHistoryOHLCVProvider, HistoryTopUpUnavailable)
    meta = cache_root / "_meta"
    meta.mkdir()
    _bars("2024-01-02", 6, "D").to_parquet(meta / "AAA_1d.parquet", index=False)
    with pytest.raises(HistoryTopUpUnavailable):   # no bars for AAA: a cold miss, never the _meta file
        AlpacaHistoryOHLCVProvider().get_ohlcv_data(
            "AAA", start_date=datetime(2024, 1, 1), end_date=datetime(2024, 1, 31), interval="1d")
    assert native_cache.find_timeseries_path(FOLDER, "AAA", "1d") is None


def test_a_read_beyond_the_cache_raises_and_writes_nothing(cache_root):
    from ba2_providers.ohlcv.AlpacaHistoryOHLCVProvider import (
        AlpacaHistoryOHLCVProvider, HistoryTopUpUnavailable)
    p = AlpacaHistoryOHLCVProvider()
    with pytest.raises(HistoryTopUpUnavailable):
        p._get_ohlcv_data_impl("ZZZ", datetime(2024, 1, 1), datetime(2024, 2, 1), "1d")
    with pytest.raises(HistoryTopUpUnavailable):
        p.get_ohlcv_data("ZZZ", start_date=datetime(2024, 1, 1), end_date=datetime(2024, 2, 1),
                         interval="1d")
    assert list(cache_root.iterdir()) == []


@pytest.mark.parametrize("interval,freq,start", [("1d", "D", "2024-01-02"),
                                                  ("5min", "5min", "2024-01-02 09:30")])
def test_latest_read_of_a_stale_cache_raises_but_a_pinned_read_does_not(cache_root, interval, freq, start):
    from ba2_providers.ohlcv.AlpacaHistoryOHLCVProvider import (
        AlpacaHistoryOHLCVProvider, HistoryTopUpUnavailable)
    _bars(start, 6, freq).to_parquet(cache_root / f"AAA_{interval}.parquet", index=False)
    p = AlpacaHistoryOHLCVProvider()
    with pytest.raises(HistoryTopUpUnavailable):
        p.get_ohlcv_data("AAA", start_date=datetime(2024, 1, 1), interval=interval)   # end_date=None
    pinned = p.get_ohlcv_data("AAA", start_date=datetime(2024, 1, 1),
                              end_date=datetime(2024, 1, 31), interval=interval)
    assert len(pinned) == 6
