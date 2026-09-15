"""The LIVE path's split-basis repair (plan Task 6): the FMP cache top-up APPENDS bars, so a
symbol that splits after its file was first fetched ends up on a mixed basis. The provider must
notice (split calendar + ratio rule at that date) and force a FULL re-fetch, once.

Run from ``packages/providers``:
    ...python.exe -m pytest tests/test_split_basis_refetch.py -q -p no:cacheprovider
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from ba2_common.core import native_cache
from ba2_common.core.market_calendar import NY_TZ, nyse_regular_sessions
from ba2_common.core.split_basis import (
    CalendarSplit,
    check_split_basis,
    full_fetch_marker_path,
    needs_full_refetch,
    read_full_fetch_marker,
)
from ba2_providers.ohlcv.FMPOHLCVProvider import FMPOHLCVProvider

SPLIT_DAY = date(2025, 2, 3)


def _sessions(a, b):
    return [o.astimezone(NY_TZ).date() for o, _c in nyse_regular_sessions(a, b)]


def _truth(last: date) -> pd.DataFrame:
    sessions = _sessions(date(2023, 1, 3), last)
    rng = np.random.default_rng(5)
    n = len(sessions)
    c = 50 * np.exp(np.cumsum(rng.normal(0.0004, 0.011, n)))
    o = c * (1 + rng.normal(0, 0.002, n))
    return pd.DataFrame({"Date": pd.to_datetime(sessions), "Open": o, "High": np.maximum(o, c) * 1.004,
                         "Low": np.minimum(o, c) * 0.996, "Close": c,
                         "Volume": rng.integers(1e6, 5e6, n).astype(float)})


def _unadjusted_before(df, day, factor):
    out = df.copy()
    pre = out["Date"] < pd.Timestamp(day)
    for col in ("Open", "High", "Low", "Close"):
        out.loc[pre, col] = out.loc[pre, col] * factor
    return out


class _Provider(FMPOHLCVProvider):
    """The real provider with the network replaced by a truth frame."""

    def __init__(self, truth, splits):
        super().__init__(api_key="test-key")
        self.truth = truth
        self.splits = splits
        self.impl_calls = []

    def _get_ohlcv_data_impl(self, symbol, start_date, end_date, interval="1d"):
        self.impl_calls.append((symbol, start_date.date(), end_date.date(), interval))
        df = self.truth
        return df[(df["Date"] >= pd.Timestamp(start_date.date())) & (df["Date"] <= pd.Timestamp(end_date.date()))] \
            .reset_index(drop=True).copy()

    def _split_calendar(self, symbol, interval):
        return list(self.splits)


def _write_cache(symbol, df):
    out = df.copy()
    out["effective_date"] = out["Date"]
    native_cache.write_timeseries("FMPOHLCVProvider", symbol, "1d", out)
    return native_cache.find_timeseries_path("FMPOHLCVProvider", symbol, "1d")


def test_pre_split_file_is_fully_refetched_once(tmp_path, monkeypatch):
    symbol = "XSPLIT"
    yesterday = date.today() - timedelta(days=1)
    truth = _truth(yesterday)
    cached = _unadjusted_before(truth[truth["Date"] <= pd.Timestamp("2025-02-28")], SPLIT_DAY, 2.0)
    path = _write_cache(symbol, cached)
    provider = _Provider(truth, [CalendarSplit(SPLIT_DAY, 2.0)])

    # The mixed basis is exactly what the checker sees before the repair.
    pre = pd.read_parquet(path)
    checks = check_split_basis(pre["Date"].to_numpy(), pre["Open"], pre["High"], pre["Low"], pre["Close"],
                               provider.splits, symbol=symbol, marker=read_full_fetch_marker(path))
    assert [c.verdict for c in checks] == ["drift"] and needs_full_refetch(checks)

    df = provider._refresh_parquet_if_stale(pre.copy(), symbol, "1d", "FMPOHLCVProvider")

    # A full-history fetch (15 years back), a REPLACED file and a marker.
    assert len(provider.impl_calls) == 2                      # the tail top-up, then the full re-fetch
    full = provider.impl_calls[-1]
    assert (datetime.now().date() - full[1]).days > 365 * 14
    after = pd.read_parquet(path)
    assert len(after) == len(truth)
    merged = after.merge(truth, on="Date", suffixes=("_c", "_t"))
    assert len(merged) == len(truth)
    assert np.allclose(merged["Close_c"], merged["Close_t"])
    assert len(df) == len(truth)
    marker = read_full_fetch_marker(path)
    assert marker and marker["fetched_on_utc"] == marker["fetched_at_utc"][:10]
    assert marker["fetched_at_utc"].endswith("+00:00")
    written_at = datetime.fromisoformat(marker["fetched_at_utc"])
    assert 0 <= (datetime.now(timezone.utc) - written_at).total_seconds() < 300
    assert marker["rows"] == len(truth)
    assert marker["first_bar"] == truth["Date"].iloc[0].date().isoformat()
    assert marker["last_bar"] == truth["Date"].iloc[-1].date().isoformat()

    # Second refresh: the marker proves the basis, so no SECOND full re-fetch happens (an
    # ordinary tail top-up still may, and returns nothing new).
    provider._refresh_parquet_if_stale(after.copy(), symbol, "1d", "FMPOHLCVProvider")
    full_fetches = [c for c in provider.impl_calls if (datetime.now().date() - c[1]).days > 365 * 14]
    assert len(full_fetches) == 1
    checks = check_split_basis(after["Date"].to_numpy(), after["Open"], after["High"], after["Low"], after["Close"],
                               provider.splits, symbol=symbol, marker=read_full_fetch_marker(path))
    assert [c.verdict for c in checks] == ["refetched"] and not needs_full_refetch(checks)


def test_consistent_file_is_not_refetched(tmp_path):
    symbol = "XCLEAN"
    truth = _truth(date.today() - timedelta(days=1))
    path = _write_cache(symbol, truth)
    provider = _Provider(truth, [CalendarSplit(SPLIT_DAY, 2.0)])
    df = pd.read_parquet(path)
    before = pd.read_parquet(path)
    provider._refresh_parquet_if_stale(df.copy(), symbol, "1d", "FMPOHLCVProvider")
    assert all(c[3] == "1d" and (datetime.now().date() - c[1]).days < 30 for c in provider.impl_calls), \
        provider.impl_calls
    assert read_full_fetch_marker(path) is None
    after = pd.read_parquet(path)
    assert len(after) == len(before) and np.allclose(after["Close"], before["Close"])


def test_split_calendar_failure_does_not_break_the_refresh(tmp_path):
    symbol = "XERR"
    truth = _truth(date.today() - timedelta(days=1))
    path = _write_cache(symbol, _unadjusted_before(truth, SPLIT_DAY, 2.0))

    class Broken(_Provider):
        def _split_calendar(self, symbol, interval):
            raise RuntimeError("FMP down")

    provider = Broken(truth, [])
    df = pd.read_parquet(path)
    out = provider._refresh_parquet_if_stale(df.copy(), symbol, "1d", "FMPOHLCVProvider")
    assert len(out) == len(df)
    assert not [c for c in provider.impl_calls if (datetime.now().date() - c[1]).days > 365 * 14]


def test_marker_path_is_out_of_the_parquet_glob(tmp_path):
    p = str(tmp_path / "FMPOHLCVProvider" / "AAPL_1d.parquet")
    marker = full_fetch_marker_path(p)
    assert marker.endswith("_split_basis" + "\\" + "AAPL_1d.json") or marker.endswith("_split_basis/AAPL_1d.json")


def test_fmp_split_calendar_parses_the_real_payload(monkeypatch):
    """``FMPOHLCVProvider._split_calendar`` over FMP's ``historical-price-full/stock_split``
    payload: dates and numerator/denominator ratios, an unknown ratio dropped, and no call at all
    for an interval this provider does not serve daily bars for."""
    from ba2_providers import symbol_info
    from ba2_providers.ohlcv.FMPOHLCVProvider import FMPOHLCVProvider

    payload = {"symbol": "XYZ", "historical": [
        {"date": "2024-06-10", "label": "June 10, 24", "numerator": 10.0, "denominator": 1.0},
        {"date": "2020-08-31", "numerator": 4, "denominator": 1},
        {"date": "2022-07-18", "numerator": 1, "denominator": 20},      # reverse split
        {"date": "2021-01-04", "numerator": None, "denominator": 1},    # unknown: dropped
    ]}
    seen = []

    def fake_fetch_splits(api_key, symbol):
        seen.append((api_key, symbol))
        return payload

    monkeypatch.setattr(symbol_info, "fetch_splits", fake_fetch_splits)
    provider = FMPOHLCVProvider(api_key="test-key")

    splits = provider._split_calendar("XYZ", "1d")
    assert seen == [("test-key", "XYZ")]
    assert [(c.date, c.ratio) for c in splits] == [
        (date(2020, 8, 31), 4.0), (date(2022, 7, 18), 0.05), (date(2024, 6, 10), 10.0)]

    assert provider._split_calendar("XYZ", "5min") is None
    assert provider._split_calendar("XYZ", "1wk") is None    # not in TIMEFRAME_MAP at all
    assert len(seen) == 1                                    # neither asked FMP
