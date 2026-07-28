"""Live OHLCV reads must not be served from an indefinitely stale parquet cache.

Audit 2026-07-28. ``get_ohlcv_data`` accepts and documents ``max_cache_age_hours``
(default 24) but never used it: a daily parquet hit was returned verbatim forever
("cache-once"), and the only top-up path -- ``_refresh_intraday_parquet_if_stale`` --
was (a) called for intraday intervals only and (b) guarded by
``if last_bar.date() != now.date(): return df``, so it could only extend a cache that
was ALREADY current and could never catch up after a weekend/outage.

Measured on the dev box: all 60 currently-held symbols had a last daily bar 13-139
days old (median 28), in two cohorts matching the dates their caches were first
written. Live experts were computing ATR / factors / ema+vwap trade conditions on
month-old bars.

The fix must keep BACKTESTS byte-identical: a pinned past ``end_date`` is immutable
history and must never trigger a re-fetch. Only latest/live requests refresh.
"""

import os
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from ba2_common.core import native_cache
from ba2_common.core.interfaces.MarketDataProviderInterface import (
    MarketDataProviderInterface,
)

PROVIDER = "_FreshnessStubProvider"


class _FreshnessStubProvider(MarketDataProviderInterface):
    """Records every source fetch so tests can assert cache-hit vs re-fetch."""

    def __init__(self):
        super().__init__()
        self.impl_calls = []

    # --- DataProviderInterface boilerplate (not exercised here) ---
    def get_provider_name(self):
        return PROVIDER

    def get_supported_features(self):
        return ["ohlcv"]

    def validate_config(self):
        return True

    def _get_ohlcv_data_impl(self, symbol, start_date, end_date, interval):
        self.impl_calls.append({
            "symbol": symbol, "start": start_date, "end": end_date, "interval": interval,
        })
        return _bars(start_date, end_date, interval)


def _bars(start, end, interval):
    """Synthetic OHLCV frame covering [start, end] at the given interval."""
    if start is None or end is None or end < start:
        return pd.DataFrame(columns=["Date", "Open", "High", "Low", "Close", "Volume"])
    freq = "D" if interval in ("1d", "1day", "daily") else "h"
    dates = pd.date_range(start=start, end=end, freq=freq)
    if len(dates) == 0:
        return pd.DataFrame(columns=["Date", "Open", "High", "Low", "Close", "Volume"])
    return pd.DataFrame({
        "Date": dates,
        "Open": 10.0, "High": 11.0, "Low": 9.0, "Close": 10.5, "Volume": 1000,
    })


@pytest.fixture
def cache_dir(tmp_path, monkeypatch):
    """Redirect the parquet cache root so tests never touch the real cache tree."""
    d = tmp_path / "cache"
    d.mkdir()
    # native_cache binds CACHE_FOLDER at import; the provider __init__ reads
    # config.CACHE_FOLDER at call time. Patch both or the real cache tree gets touched.
    import ba2_common.config as cfg
    monkeypatch.setattr(cfg, "CACHE_FOLDER", str(d))
    monkeypatch.setattr(native_cache, "CACHE_FOLDER", str(d))
    monkeypatch.setattr(native_cache, "_CACHE_ROOT", str(d / "datasets" / "cache"))
    return str(d)


def _seed_cache(symbol, interval, last_bar: datetime, n_bars=40, age_hours=None):
    """Write a parquet ending at ``last_bar``; optionally backdate its file mtime."""
    freq = "D" if interval in ("1d", "1day", "daily") else "h"
    dates = pd.date_range(end=last_bar, periods=n_bars, freq=freq)
    df = pd.DataFrame({
        "Date": dates,
        "Open": 10.0, "High": 11.0, "Low": 9.0, "Close": 10.5, "Volume": 1000,
    })
    df["effective_date"] = df["Date"]
    native_cache.write_timeseries(PROVIDER, symbol, interval, df)
    path = native_cache.find_timeseries_path(PROVIDER, symbol, interval)
    assert path is not None
    if age_hours is not None:
        old = (datetime.now() - timedelta(hours=age_hours)).timestamp()
        os.utime(path, (old, old))
    return path


# ---------------------------------------------------------------------------
# Live daily
# ---------------------------------------------------------------------------

def test_live_daily_refetches_when_cache_is_older_than_max_age(cache_dir):
    """The bug: a month-old daily cache was returned verbatim, forever."""
    _seed_cache("AAA", "1d", datetime.now() - timedelta(days=28), age_hours=28 * 24)
    p = _FreshnessStubProvider()

    p.get_ohlcv_data("AAA", interval="1d")  # end_date=None -> latest/live

    assert p.impl_calls, (
        "live daily read served a 28-day-old cache without re-fetching -- "
        "max_cache_age_hours was ignored"
    )


def test_live_daily_does_not_refetch_when_cache_is_fresh(cache_dir):
    """A cache written minutes ago must still be a pure hit (no API hammering)."""
    _seed_cache("BBB", "1d", datetime.now(), age_hours=0)
    p = _FreshnessStubProvider()

    p.get_ohlcv_data("BBB", interval="1d")

    assert p.impl_calls == [], "fresh daily cache should not trigger a re-fetch"


def test_max_cache_age_hours_is_actually_honoured(cache_dir):
    """The mtime budget alone decides whether a top-up is ATTEMPTED.

    Isolates the age gate from bar-recency: the cache is genuinely 3 days behind (so a
    fetch WOULD happen if attempted) and only ``max_cache_age_hours`` varies. Seeding a
    12h-old bar instead would prove nothing — that bar is still today, so
    ``expected_latest`` correctly finds nothing newer to fetch either way.
    """
    _seed_cache("CCC", "1d", datetime.now() - timedelta(days=3), age_hours=12)

    p1 = _FreshnessStubProvider()
    p1.get_ohlcv_data("CCC", interval="1d", max_cache_age_hours=24)
    assert p1.impl_calls == [], "12h-old file is valid under a 24h budget -> no attempt"

    p2 = _FreshnessStubProvider()
    p2.get_ohlcv_data("CCC", interval="1d", max_cache_age_hours=6)
    assert p2.impl_calls, "12h-old file exceeds a 6h budget -> must top up"


# ---------------------------------------------------------------------------
# Backtests must stay byte-identical
# ---------------------------------------------------------------------------

def test_pinned_past_end_date_never_refetches(cache_dir):
    """Backtest as_of is immutable history: a stale cache is CORRECT, don't re-fetch."""
    _seed_cache("DDD", "1d", datetime(2025, 6, 30), age_hours=400 * 24)
    p = _FreshnessStubProvider()

    p.get_ohlcv_data(
        "DDD",
        start_date=datetime(2025, 6, 1),
        end_date=datetime(2025, 6, 30),
        interval="1d",
    )

    assert p.impl_calls == [], (
        "a pinned historical end_date must never trigger a live re-fetch -- "
        "backtest reads have to stay byte-identical"
    )


def test_pinned_past_end_date_returns_same_rows_as_before_the_fix(cache_dir):
    """Byte-identity check: the returned frame equals a direct sliced cache read."""
    _seed_cache("EEE", "1d", datetime(2025, 6, 30), age_hours=400 * 24)
    end = datetime(2025, 6, 30)
    expected = native_cache.read_timeseries(PROVIDER, "EEE", "1d", as_of=end)
    expected = expected[pd.to_datetime(expected["Date"]) >= pd.Timestamp(2025, 6, 1)]

    got = _FreshnessStubProvider().get_ohlcv_data(
        "EEE", start_date=datetime(2025, 6, 1), end_date=end, interval="1d")

    # Compare tz-naive: the returned frame is localized downstream of the cache read,
    # which is orthogonal to the row set this test is pinning.
    def _naive(s):
        s = pd.to_datetime(s)
        return list(s.dt.tz_localize(None) if s.dt.tz is not None else s)

    assert _naive(got["Date"]) == _naive(expected["Date"])


# ---------------------------------------------------------------------------
# Intraday catch-up
# ---------------------------------------------------------------------------

def test_tz_aware_fetch_merges_into_a_tz_naive_cache(cache_dir):
    """Regression: the provider may return tz-AWARE bars while the parquet holds NAIVE ones.

    Caught end-to-end against real FMP data after the first version of this fix shipped
    green unit tests: concatenating the two produced an object Date column of mixed
    aware/naive values, and get_ohlcv_data's pd.to_datetime then raised
    "Tz-aware datetime.datetime cannot be converted to datetime64 unless utc=True".
    Daily only started hitting this path once it began refreshing at all.
    """
    _seed_cache("TZA", "1d", datetime.now() - timedelta(days=20), age_hours=20 * 24)

    class _AwareProvider(_FreshnessStubProvider):
        def _get_ohlcv_data_impl(self, symbol, start_date, end_date, interval):
            df = super()._get_ohlcv_data_impl(symbol, start_date, end_date, interval)
            if not df.empty:  # provider hands back tz-aware UTC timestamps
                df["Date"] = pd.to_datetime(df["Date"]).dt.tz_localize(timezone.utc)
            return df

    p = _AwareProvider()
    got = p.get_ohlcv_data("TZA", interval="1d")  # must not raise

    assert p.impl_calls, "stale cache should still have been topped up"
    assert len(got) > 0
    # A single consistent dtype, not an object column of mixed tz values.
    assert pd.api.types.is_datetime64_any_dtype(got["Date"]), (
        f"Date column ended up mixed-tz (dtype={got['Date'].dtype})"
    )


def test_intraday_catches_up_when_last_bar_is_not_today(cache_dir):
    """Old guard returned early unless the last bar was already from today, so an
    intraday cache that fell a day behind could never recover."""
    _seed_cache("FFF", "1h", datetime.now() - timedelta(days=3), age_hours=72)
    p = _FreshnessStubProvider()

    p.get_ohlcv_data("FFF", interval="1h")

    assert p.impl_calls, (
        "intraday cache 3 days behind was returned as-is -- the refresh only fired "
        "when the last bar was already current"
    )
