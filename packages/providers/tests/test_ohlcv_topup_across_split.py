"""The live daily top-up never writes a mixed-basis file (APH, dev app, 2026-09-28 15:34).

The APH cache (bars to 2026-06-30, as traded) was topped up with 2026-07-01..09-29 bars already
adjusted for the 2:1 split of 2026-09-03, by APPENDING -- a fake x0.488 day on 07-01. The top-up
now compares the vendor's answer for the last cached bars with the cache first
(``ba2_common.core.ohlcv_topup_guard``) and:

* re-bases (a split) -> REPLACES the history with a full re-fetch, logged at WARNING;
* disagrees otherwise -> refuses (``OHLCVTopUpRefused``), nothing written;
* agrees -> appends exactly as before (byte-identical file).

Run from ``packages/providers``:
    ...python.exe -m pytest tests/test_ohlcv_topup_across_split.py -q -p no:cacheprovider
"""
from __future__ import annotations

import os
import time
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from ba2_common.core import native_cache
import importlib
mdpi_mod = importlib.import_module("ba2_common.core.interfaces.MarketDataProviderInterface")
from ba2_common.core.interfaces.MarketDataProviderInterface import MarketDataProviderInterface
from ba2_common.core.market_calendar import NY_TZ, nyse_regular_sessions
from ba2_common.core.ohlcv_topup_guard import OHLCVTopUpRefused
from ba2_common.core.split_basis import (
    CalendarSplit, check_split_basis, needs_full_refetch, read_full_fetch_marker,
)
from ba2_providers.ohlcv.FMPOHLCVProvider import FMPOHLCVProvider

PROVIDER = "FMPOHLCVProvider"


def _sessions(a, b):
    return [o.astimezone(NY_TZ).date() for o, _c in nyse_regular_sessions(a, b)]


YESTERDAY = date.today() - timedelta(days=1)
SESSIONS = _sessions(date(2023, 1, 3), YESTERDAY)
SPLIT_DAY = SESSIONS[-18]            # APH: ex-date 2026-09-03, ~18 sessions before the refresh
CACHE_END = SESSIONS[-62]            # APH: cache to 2026-06-30, ~62 sessions before the ex-date


def _truth() -> pd.DataFrame:
    """The vendor's CURRENT view: split-adjusted for SPLIT_DAY throughout."""
    rng = np.random.default_rng(11)
    n = len(SESSIONS)
    c = 80 * np.exp(np.cumsum(rng.normal(0.0003, 0.012, n)))
    o = c * (1 + rng.normal(0, 0.003, n))
    return pd.DataFrame({"Date": pd.to_datetime(SESSIONS), "Open": o.round(2),
                         "High": (np.maximum(o, c) * 1.006).round(2),
                         "Low": (np.minimum(o, c) * 0.994).round(2), "Close": c.round(2),
                         "Volume": rng.integers(8_000_000, 20_000_000, n)})


def _as_traded_before(df: pd.DataFrame, day: date, ratio: float) -> pd.DataFrame:
    """Bars before ``day`` as they traded (= as a fetch before the split delivered them)."""
    out = df.copy()
    pre = out["Date"] < pd.Timestamp(day)
    for col in ("Open", "High", "Low", "Close"):
        out.loc[pre, col] = (out.loc[pre, col] * ratio).round(2)
    out.loc[pre, "Volume"] = (out.loc[pre, "Volume"] / ratio).astype(out["Volume"].dtype)
    return out


class _Provider(FMPOHLCVProvider):
    """The real FMP provider with the network replaced by a frame; dates come back tz-aware UTC
    midnights, like ``_fetch_daily_data``."""

    def __init__(self, vendor: pd.DataFrame, splits):
        super().__init__(api_key="test-key")
        self.vendor = vendor
        self.splits = splits
        self.impl_calls = []

    def _get_ohlcv_data_impl(self, symbol, start_date, end_date, interval="1d"):
        self.impl_calls.append((symbol, start_date.date(), end_date.date(), interval))
        df = self.vendor
        out = df[(df["Date"] >= pd.Timestamp(start_date.date()))
                 & (df["Date"] <= pd.Timestamp(end_date.date()))].reset_index(drop=True).copy()
        out["Date"] = pd.to_datetime(out["Date"]).dt.tz_localize("UTC")
        return out

    def _split_calendar(self, symbol, interval):
        return list(self.splits)


def _write_cache(symbol: str, df: pd.DataFrame, provider_name: str = PROVIDER) -> str:
    out = df.copy()
    out["effective_date"] = out["Date"]
    native_cache.write_timeseries(provider_name, symbol, "1d", out)
    return native_cache.find_timeseries_path(provider_name, symbol, "1d")


def _full_fetches(provider):
    return [c for c in provider.impl_calls if (date.today() - c[1]).days > 365 * 3]


@pytest.fixture(autouse=True)
def _clean_memos():
    MarketDataProviderInterface._SPLIT_BASIS_REPORTED.clear()
    MarketDataProviderInterface._TOPUP_REFUSED.clear()
    yield
    MarketDataProviderInterface._SPLIT_BASIS_REPORTED.clear()
    MarketDataProviderInterface._TOPUP_REFUSED.clear()


@pytest.fixture
def logged(monkeypatch):
    """``ba2_common.logger`` does not propagate (caplog sees nothing): record its calls."""
    seen = {"warning": [], "error": [], "info": []}
    for level in seen:
        real = getattr(mdpi_mod.logger, level)

        def rec(msg, *a, _level=level, _real=real, **k):
            seen[_level].append(str(msg))
            return _real(msg, *a, **k)
        monkeypatch.setattr(mdpi_mod.logger, level, rec)
    return seen


def _one_day_moves(df: pd.DataFrame) -> np.ndarray:
    c = df.sort_values("Date")["Close"].to_numpy(dtype=float)
    return c[1:] / c[:-1]


# --------------------------------------------------------------------------- the APH case
def test_aph_topup_across_a_split_is_a_full_refetch_never_an_append(logged):
    symbol = "XAPH"
    truth = _truth()
    cached = _as_traded_before(truth[truth["Date"] <= pd.Timestamp(CACHE_END)], SPLIT_DAY, 2.0)
    path = _write_cache(symbol, cached)
    assert read_full_fetch_marker(path) is None
    provider = _Provider(truth, [CalendarSplit(SPLIT_DAY, 2.0)])

    # What the old top-up did: append truth after CACHE_END -> a fake x0.5 day. Proven here so the
    # test cannot pass on data that never had the defect.
    appended = pd.concat([cached, truth[truth["Date"] > pd.Timestamp(CACHE_END)]])
    assert _one_day_moves(appended).min() < 0.6

    out = provider._refresh_parquet_if_stale(pd.read_parquet(path), symbol, "1d", PROVIDER)

    assert len(_full_fetches(provider)) == 1, provider.impl_calls   # the FULL history was fetched
    after = pd.read_parquet(path)
    assert len(after) == len(truth) and len(out) == len(truth)
    merged = after.merge(truth, on="Date", suffixes=("_c", "_t"))
    assert len(merged) == len(truth) and np.allclose(merged["Close_c"], merged["Close_t"])
    moves = _one_day_moves(after)
    assert 0.8 < moves.min() and moves.max() < 1.25                 # no fake split day anywhere
    assert read_full_fetch_marker(path) is not None
    checks = check_split_basis(pd.to_datetime(after["Date"]).to_numpy(dtype="datetime64[D]"),
                               after["Open"], after["High"], after["Low"], after["Close"],
                               provider.splits, symbol=symbol, marker=read_full_fetch_marker(path))
    assert not needs_full_refetch(checks)
    # ... and the refresh SAID so, at WARNING.
    said = [m for m in logged["warning"] if "FULL RE-FETCH" in m and symbol in m]
    assert len(said) == 1 and "x0.5000" in said[0] and SPLIT_DAY.isoformat() in said[0]
    assert logged["error"] == []


def test_the_live_read_reaches_the_guard_and_a_pinned_read_never_fetches(logged):
    """``get_ohlcv_data`` (LATEST, stale file) goes through the same guarded top-up; a pinned
    historical ``end_date`` -- what every backtest-style read passes -- never fetches at all."""
    symbol = "XAPHLIVE"
    truth = _truth()
    cached = _as_traded_before(truth[truth["Date"] <= pd.Timestamp(CACHE_END)], SPLIT_DAY, 2.0)
    # get_ohlcv_data keys the cache on the provider's CLASS name.
    path = _write_cache(symbol, cached, provider_name=_Provider.__name__)
    old = time.time() - 1391 * 3600                                  # APH: 1,391 hours old
    os.utime(path, (old, old))
    provider = _Provider(truth, [CalendarSplit(SPLIT_DAY, 2.0)])

    provider.get_ohlcv_data(symbol, start_date=datetime(2024, 1, 1), end_date=datetime(2024, 6, 1))
    assert provider.impl_calls == []                                 # pinned: cache only

    df = provider.get_ohlcv_data(symbol, start_date=datetime.combine(CACHE_END, datetime.min.time())
                                 - timedelta(days=10))
    assert len(_full_fetches(provider)) == 1
    assert _one_day_moves(df).min() > 0.8
    assert any("FULL RE-FETCH" in m for m in logged["warning"])


def test_a_vendor_that_has_not_adjusted_yet_is_refused_and_nothing_is_written(logged):
    """The calendar lists the split inside the top-up, but the vendor still serves the pre-split
    bars as traded (= the cache) and the x0.5 step on the ex-date: appending and a full re-fetch
    would both write a mixed file."""
    symbol = "XLAG"
    truth = _truth()
    vendor_view = _as_traded_before(truth, SPLIT_DAY, 2.0)
    path = _write_cache(symbol, vendor_view[vendor_view["Date"] <= pd.Timestamp(CACHE_END)])
    before = open(path, "rb").read()
    provider = _Provider(vendor_view, [CalendarSplit(SPLIT_DAY, 2.0)])

    with pytest.raises(OHLCVTopUpRefused, match="has not adjusted"):
        provider._refresh_parquet_if_stale(pd.read_parquet(path), symbol, "1d", PROVIDER)
    assert open(path, "rb").read() == before and _full_fetches(provider) == []
    assert read_full_fetch_marker(path) is None
    assert len(logged["error"]) == 1 and "REFUSED" in logged["error"][0]

    # Refused again on the next read WITHOUT asking the vendor (memo), still loud.
    n = len(provider.impl_calls)
    with pytest.raises(OHLCVTopUpRefused, match="refused .*s ago"):
        provider._refresh_parquet_if_stale(pd.read_parquet(path), symbol, "1d", PROVIDER)
    assert len(provider.impl_calls) == n


# --------------------------------------------------------------------------- not split-like
def test_an_overlap_mismatch_that_is_not_split_like_is_refused(logged):
    symbol = "XODD"
    truth = _truth()
    cached = truth[truth["Date"] <= pd.Timestamp(CACHE_END)].copy()
    i = cached.index[-3]
    for col in ("Open", "High", "Low", "Close"):
        cached.loc[i, col] = round(float(cached.loc[i, col]) * 1.13, 2)
    path = _write_cache(symbol, cached)
    before = open(path, "rb").read()
    provider = _Provider(truth, [])

    with pytest.raises(OHLCVTopUpRefused) as e:
        provider._refresh_parquet_if_stale(pd.read_parquet(path), symbol, "1d", PROVIDER)
    assert "disagrees with the cache" in str(e.value)
    assert pd.Timestamp(cached.loc[i, "Date"]).date().isoformat() in str(e.value)
    assert open(path, "rb").read() == before and _full_fetches(provider) == []
    assert logged["error"] and not any("FULL RE-FETCH" in m for m in logged["warning"])


def test_a_uniform_rescale_that_is_no_split_is_refused():
    symbol = "XUNI"
    truth = _truth()
    cached = truth[truth["Date"] <= pd.Timestamp(CACHE_END)].copy()
    for col in ("Open", "High", "Low", "Close"):
        cached[col] = (cached[col] * 1.07).round(2)
    path = _write_cache(symbol, cached)
    before = open(path, "rb").read()
    provider = _Provider(truth, [])
    with pytest.raises(OHLCVTopUpRefused, match="matches no calendar split"):
        provider._refresh_parquet_if_stale(pd.read_parquet(path), symbol, "1d", PROVIDER)
    assert open(path, "rb").read() == before


def test_a_failed_full_refetch_is_refused_and_leaves_the_cache_untouched():
    """The replacement's own safety check (refuse-shorter) still applies inside the top-up."""
    symbol = "XSHORT"
    truth = _truth()
    cached = _as_traded_before(truth[truth["Date"] <= pd.Timestamp(CACHE_END)], SPLIT_DAY, 2.0)
    path = _write_cache(symbol, cached)
    before = open(path, "rb").read()

    class Capped(_Provider):
        def _get_ohlcv_data_impl(self, symbol, start_date, end_date, interval="1d"):
            return super()._get_ohlcv_data_impl(symbol, start_date, end_date, interval).tail(300)

    provider = Capped(truth, [CalendarSplit(SPLIT_DAY, 2.0)])
    with pytest.raises(OHLCVTopUpRefused, match="LESS history"):
        provider._refresh_parquet_if_stale(pd.read_parquet(path), symbol, "1d", PROVIDER)
    assert open(path, "rb").read() == before and read_full_fetch_marker(path) is None


# --------------------------------------------------------------------------- unchanged top-up
def _old_append(provider, df, symbol):
    """The pre-2026-09-28 top-up, verbatim in effect: fetch after the last bar, clean, match the
    cache's tz, stamp effective_date, concat, dedupe, sort."""
    df = df.copy()
    df["Date"] = pd.to_datetime(df["Date"])
    last = df["Date"].iloc[-1].to_pydatetime()
    new_df = provider._get_ohlcv_data_impl(symbol, last + timedelta(days=1),
                                           datetime.now() + timedelta(days=1), "1d")
    new_df = provider._clean_dataframe(new_df)
    new_df["Date"] = pd.to_datetime(new_df["Date"])
    new_df["Date"] = provider._match_tz(new_df["Date"], df["Date"])
    new_df["effective_date"] = new_df["Date"]
    df = pd.concat([df, new_df], ignore_index=True)
    return df.drop_duplicates(subset=["Date"]).sort_values("Date").reset_index(drop=True)


def test_a_normal_topup_without_a_split_is_byte_identical(logged):
    truth = _truth()
    cached = truth[truth["Date"] <= pd.Timestamp(CACHE_END)]
    path_new = _write_cache("XNORMA", cached)
    path_old = _write_cache("XNORMB", cached)
    provider = _Provider(truth, [])

    out = provider._refresh_parquet_if_stale(pd.read_parquet(path_new), "XNORMA", "1d", PROVIDER)
    expected = _old_append(provider, pd.read_parquet(path_old), "XNORMB")
    native_cache.write_timeseries(PROVIDER, "XNORMB", "1d", expected)

    assert open(path_new, "rb").read() == open(path_old, "rb").read()
    pd.testing.assert_frame_equal(out.reset_index(drop=True), expected)
    assert _full_fetches(provider) == [] and read_full_fetch_marker(path_new) is None
    assert not any("FULL RE-FETCH" in m for m in logged["warning"]) and logged["error"] == []


def test_a_provisional_last_bar_is_replaced_by_the_vendors_final_bar():
    """A refresh during the session cached today's bar mid-session (AAOI 2026-09-21: O=H=L=C,
    360k shares). The next top-up takes the vendor's final bar for it."""
    symbol = "XPROV"
    truth = _truth()
    cached = truth[truth["Date"] <= pd.Timestamp(CACHE_END)].copy()
    j = cached.index[-1]
    o = cached.loc[j, "Open"]
    cached.loc[j, ["High", "Low", "Close"]] = [o, o, o]
    cached.loc[j, "Volume"] = 360_654
    path = _write_cache(symbol, cached)
    provider = _Provider(truth, [])
    provider._refresh_parquet_if_stale(pd.read_parquet(path), symbol, "1d", PROVIDER)
    after = pd.read_parquet(path)
    assert len(after) == len(truth)
    row = after[after["Date"] == pd.Timestamp(CACHE_END)].iloc[0]
    want = truth[truth["Date"] == pd.Timestamp(CACHE_END)].iloc[0]
    assert row["Close"] == want["Close"] and row["Volume"] == want["Volume"]
