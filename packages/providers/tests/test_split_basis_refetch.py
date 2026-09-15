"""The LIVE path's split-basis repair (plan Task 6): the FMP cache top-up APPENDS bars, so a
symbol that splits after its file was first fetched ends up on a mixed basis. The provider must
notice (split calendar + ratio rule at that date) and force a FULL re-fetch, once.

Run from ``packages/providers``:
    ...python.exe -m pytest tests/test_split_basis_refetch.py -q -p no:cacheprovider
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

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
    assert marker and marker["fetched_on_utc"] == datetime.utcnow().date().isoformat()
    assert marker["rows"] == len(truth)

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
    provider._refresh_parquet_if_stale(df.copy(), symbol, "1d", "FMPOHLCVProvider")
    assert provider.impl_calls == [] or all(c[3] == "1d" for c in provider.impl_calls)
    assert not [c for c in provider.impl_calls if (datetime.now().date() - c[1]).days > 365 * 14]
    assert read_full_fetch_marker(path) is None


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
