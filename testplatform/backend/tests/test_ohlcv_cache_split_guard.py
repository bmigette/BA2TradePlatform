"""The two writers of the daily OHLCV cache the backtests read share ONE guarded top-up (APH).

* ``ba2-test fetch-cache`` (``extend_ohlcv_cache`` on a wrapped provider) extends a cached history
  through ``MarketDataProviderInterface._verified_tail_topup`` -- the same path as the live
  refresh: across a split it REPLACES the history with a full re-fetch, never appends; a fetched
  gap/head piece that disagrees with the cache is refused.
* The backtest's reader (``MemoizedOHLCVProvider(cached_only=True)``) is hermetic: it never fetches,
  however stale the file is.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from ba2_common.core import native_cache
from ba2_common.core.interfaces.MarketDataProviderInterface import MarketDataProviderInterface
from ba2_common.core.ohlcv_topup_guard import OHLCVTopUpRefused
from ba2_common.core.split_basis import CalendarSplit, read_full_fetch_marker
from ba2_providers.ohlcv.FMPOHLCVProvider import FMPOHLCVProvider


def _truth() -> pd.DataFrame:
    days = pd.bdate_range("2023-01-02", date.today() - timedelta(days=1))
    rng = np.random.default_rng(7)
    c = 60 * np.exp(np.cumsum(rng.normal(0.0003, 0.012, len(days))))
    o = c * (1 + rng.normal(0, 0.003, len(days)))
    return pd.DataFrame({"Date": days, "Open": o.round(2), "High": (np.maximum(o, c) * 1.006).round(2),
                         "Low": (np.minimum(o, c) * 0.994).round(2), "Close": c.round(2),
                         "Volume": rng.integers(1_000_000, 3_000_000, len(days))})


TRUTH = _truth()
SPLIT_DAY = TRUTH["Date"].iloc[-18].date()
CACHE_END = TRUTH["Date"].iloc[-80]


def _as_traded(df):
    out = df.copy()
    pre = out["Date"] < pd.Timestamp(SPLIT_DAY)
    for col in ("Open", "High", "Low", "Close"):
        out.loc[pre, col] = (out.loc[pre, col] * 2).round(2)
    return out


class FakeFMP(FMPOHLCVProvider):
    def __init__(self, vendor, splits):
        super().__init__(api_key="test-key")
        self.vendor, self.splits, self.calls = vendor, splits, []

    def _get_ohlcv_data_impl(self, symbol, start_date, end_date, interval="1d"):
        self.calls.append((start_date.date(), end_date.date()))
        v = self.vendor
        out = v[(v["Date"] >= pd.Timestamp(start_date.date()))
                & (v["Date"] <= pd.Timestamp(end_date.date()))].reset_index(drop=True).copy()
        out["Date"] = out["Date"].dt.tz_localize("UTC")
        return out

    def _split_calendar(self, symbol, interval):
        return list(self.splits)


@pytest.fixture(autouse=True)
def _isolated_cache(tmp_path, monkeypatch):
    import ba2_common.config as cfg
    import ba2_common.core.native_cache as nc_src
    # config.CACHE_FOLDER too: MarketDataProviderInterface.__init__ creates its folder there.
    monkeypatch.setattr(cfg, "CACHE_FOLDER", str(tmp_path), raising=False)
    monkeypatch.setattr(nc_src, "CACHE_FOLDER", str(tmp_path), raising=False)
    monkeypatch.setattr(native_cache, "CACHE_FOLDER", str(tmp_path), raising=False)
    MarketDataProviderInterface._TOPUP_REFUSED.clear()
    yield
    MarketDataProviderInterface._TOPUP_REFUSED.clear()


def _seed(symbol, df):
    out = df.copy()
    out["effective_date"] = out["Date"]
    native_cache.write_timeseries("FakeFMP", symbol, "1d", out)
    return native_cache.find_timeseries_path("FakeFMP", symbol, "1d")


def test_fetch_cache_across_a_split_replaces_the_history_never_appends():
    from app.services.ohlcv_cache_provider import wrap_with_cache

    cached = _as_traded(TRUTH[TRUTH["Date"] <= CACHE_END])
    path = _seed("XAPH", cached)
    prov = wrap_with_cache(FakeFMP(TRUTH, [CalendarSplit(SPLIT_DAY, 2.0)]))

    prov.extend_ohlcv_cache("XAPH", datetime(2023, 1, 2), datetime.now(), "1d", executor_workers=1)

    after = pd.read_parquet(path).sort_values("Date")
    c = after["Close"].to_numpy(dtype=float)
    assert (c[1:] / c[:-1]).min() > 0.8                     # no fake x0.5 day
    merged = after.merge(TRUTH, on="Date", suffixes=("_c", "_t"))
    assert len(merged) == len(TRUTH) and np.allclose(merged["Close_c"], merged["Close_t"])
    assert any((date.today() - s).days > 365 * 3 for s, _e in prov.calls)   # a FULL re-fetch
    assert read_full_fetch_marker(path) is not None


def test_fetch_cache_refuses_a_gap_fill_on_another_basis():
    """A gap inside the cache filled from a vendor that re-based since: refused, file untouched."""
    from app.services.ohlcv_cache_provider import wrap_with_cache

    cached = _as_traded(TRUTH)
    cached = cached.drop(index=range(100, 110)).reset_index(drop=True)      # an internal gap
    path = _seed("XGAP", cached)
    before = open(path, "rb").read()
    prov = wrap_with_cache(FakeFMP(TRUTH, [CalendarSplit(SPLIT_DAY, 2.0)]))

    with pytest.raises(OHLCVTopUpRefused, match="gap fill REFUSED"):
        prov.extend_ohlcv_cache("XGAP", datetime(2023, 1, 2), cached["Date"].max().to_pydatetime(),
                                "1d", executor_workers=1)
    assert open(path, "rb").read() == before


def test_the_backtest_reader_never_fetches():
    from app.services.backtest.price_source import MemoizedOHLCVProvider

    cached = _as_traded(TRUTH[TRUTH["Date"] <= CACHE_END])
    _seed("XBT", cached)
    inner = FakeFMP(TRUTH, [CalendarSplit(SPLIT_DAY, 2.0)])
    reader = MemoizedOHLCVProvider(inner, datetime(2023, 1, 2), datetime.now(), interval="1d",
                                   cached_only=True)
    df = reader.read_window("XBT", datetime(2023, 1, 2), datetime.now(), "1d")
    assert len(df) == len(cached) and inner.calls == []
