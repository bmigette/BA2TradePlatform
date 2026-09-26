"""``force_full_refetch(keep_newer_cached_tail=True)``: the SATS repair, as an explicit option.

FOUND IN THE STAGE-2 REPAIR (2026-09-25). FMP's full SATS history was correctly adjusted for the
2019-09-11 event the cache was not, but it ENDED 2026-06-24 while the cache held bars to
2026-06-30. The shorter-replacement guard rightly refused (a replacement must never lose
history), so the drifted file could not be repaired at all. The fix is NOT to relax that guard:
it is an opt-in that keeps the cache's own newer bars after the vendor's last one, under checks
strict enough that the kept bars are provably on the fresh file's basis:

* at most ``KEEP_TAIL_MAX_BARS`` bars (a few sessions of vendor lag, not a different history);
* no split-calendar event between the vendor's last bar and the cache's last bar (the tail would
  then be on another basis), and a calendar that cannot be read refuses;
* the last ``KEEP_TAIL_OVERLAP_SESSIONS`` sessions the two files share agree within
  ``ANCHOR_TOLERANCE`` (the same basis where they meet);
* every column of the fresh frame is present in the tail;
* the result still passes the shorter-replacement guard (a later FIRST bar is still refused).

Anything else refuses and leaves the file and its marker byte-identical. No network.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from ba2_common.core import native_cache
from ba2_common.core.interfaces.MarketDataProviderInterface import (
    KEEP_TAIL_MAX_BARS, KEEP_TAIL_OVERLAP_SESSIONS, MarketDataProviderInterface,
)
from ba2_common.core.market_calendar import NY_TZ, nyse_regular_sessions
from ba2_common.core.split_basis import (
    CalendarSplit, check_split_basis, needs_full_refetch, read_full_fetch_marker,
)
from ba2_providers.ohlcv.FMPOHLCVProvider import FMPOHLCVProvider

SPLIT_DAY = date(2025, 2, 3)


def _sessions(a, b):
    return [o.astimezone(NY_TZ).date() for o, _c in nyse_regular_sessions(a, b)]


def _truth(last: date) -> pd.DataFrame:
    sessions = _sessions(date(2023, 1, 3), last)
    rng = np.random.default_rng(11)
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


class _LaggingVendor(FMPOHLCVProvider):
    """A vendor whose full history is correct but ends ``lag`` sessions before the truth."""

    def __init__(self, truth, splits, lag, start_from=None, calendar_error=None):
        super().__init__(api_key="test-key")
        self.truth, self.splits, self.lag = truth, splits, lag
        self.start_from, self.calendar_error = start_from, calendar_error

    def _get_ohlcv_data_impl(self, symbol, start_date, end_date, interval="1d"):
        df = self.truth.iloc[:len(self.truth) - self.lag]
        if self.start_from is not None:
            df = df[df["Date"] >= pd.Timestamp(self.start_from)]
        return df[(df["Date"] >= pd.Timestamp(start_date.date()))].reset_index(drop=True).copy()

    def _split_calendar(self, symbol, interval):
        if self.calendar_error:
            raise RuntimeError(self.calendar_error)
        return list(self.splits)


def _write_cache(symbol, df):
    out = df.copy()
    out["effective_date"] = out["Date"]
    native_cache.write_timeseries("FMPOHLCVProvider", symbol, "1d", out)
    return native_cache.find_timeseries_path("FMPOHLCVProvider", symbol, "1d")


@pytest.fixture(autouse=True)
def _forget_reported_symbols():
    MarketDataProviderInterface._SPLIT_BASIS_REPORTED.clear()
    yield
    MarketDataProviderInterface._SPLIT_BASIS_REPORTED.clear()


def _sats_shape(symbol, lag=4):
    """A cache that is NOT adjusted for SPLIT_DAY and reaches ``lag`` bars past the vendor."""
    truth = _truth(date.today() - timedelta(days=3))
    cached = _unadjusted_before(truth, SPLIT_DAY, 1.234)
    path = _write_cache(symbol, cached)
    return truth, cached, path


def test_the_default_still_refuses_a_vendor_file_that_ends_earlier(tmp_path):
    truth, _cached, path = _sats_shape("XSATS0")
    before = open(path, "rb").read()
    p = _LaggingVendor(truth, [CalendarSplit(SPLIT_DAY, 1.234)], lag=4)
    with pytest.raises(RuntimeError, match="LESS history"):
        p.force_full_refetch("XSATS0", "1d", provider_name="FMPOHLCVProvider")
    assert open(path, "rb").read() == before and read_full_fetch_marker(path) is None


def test_the_opt_in_keeps_the_newer_cached_bars_and_repairs_the_basis(tmp_path, monkeypatch):
    import sys
    mdpi = sys.modules[MarketDataProviderInterface.__module__]
    warned = []
    monkeypatch.setattr(mdpi.logger, "warning", lambda msg, *a, **k: warned.append(str(msg)))
    truth, cached, path = _sats_shape("XSATS1")
    p = _LaggingVendor(truth, [CalendarSplit(SPLIT_DAY, 1.234)], lag=4)

    out = p.force_full_refetch("XSATS1", "1d", provider_name="FMPOHLCVProvider",
                               keep_newer_cached_tail=True)

    after = pd.read_parquet(path)
    assert len(after) == len(truth) == len(out)            # vendor rows + the 4 kept ones
    assert after["Date"].iloc[-1] == truth["Date"].iloc[-1]
    # The kept tail is the cache's own bars, unchanged ...
    tail_c = cached.tail(4)["Close"].to_numpy()
    assert np.allclose(after.tail(4)["Close"].to_numpy(), tail_c)
    # ... and the pre-split history is now the vendor's ADJUSTED one (the repair happened).
    pre = after[after["Date"] < pd.Timestamp(SPLIT_DAY)]
    assert np.allclose(pre["Close"].to_numpy(), truth[truth["Date"] < pd.Timestamp(SPLIT_DAY)]["Close"])
    marker = read_full_fetch_marker(path)
    assert marker["rows"] == len(after) and marker["last_bar"] == truth["Date"].iloc[-1].date().isoformat()
    checks = check_split_basis(after["Date"].to_numpy(), after["Open"], after["High"], after["Low"],
                               after["Close"], p.splits, symbol="XSATS1", marker=marker)
    assert not needs_full_refetch(checks)
    assert any("kept 4 cached bar" in m for m in warned), "the keep is logged"


@pytest.mark.parametrize("case", ["tail_too_long", "overlap_disagrees", "split_in_the_tail",
                                  "calendar_unreadable", "later_first_bar"])
def test_the_opt_in_refuses_unless_the_tail_is_provably_on_the_same_basis(tmp_path, case):
    sym = f"XK{case[:6].upper()}"
    truth = _truth(date.today() - timedelta(days=3))
    cached = _unadjusted_before(truth, SPLIT_DAY, 1.234)
    splits = [CalendarSplit(SPLIT_DAY, 1.234)]
    kw = {"lag": 4}
    if case == "tail_too_long":
        kw["lag"] = KEEP_TAIL_MAX_BARS + 1
    elif case == "overlap_disagrees":
        # The cache's recent bars are on ANOTHER basis than the vendor's (x1.01 over the last
        # 30 sessions): its tail cannot be spliced onto the fresh file.
        cached.loc[cached.index[-30:], ["Open", "High", "Low", "Close"]] *= 1.01
    elif case == "split_in_the_tail":
        splits = splits + [CalendarSplit(truth["Date"].iloc[-2].date(), 2.0)]
    elif case == "calendar_unreadable":
        kw["calendar_error"] = "FMP 503"
    elif case == "later_first_bar":
        kw["start_from"] = date(2023, 6, 1)
    path = _write_cache(sym, cached)
    before = open(path, "rb").read()
    p = _LaggingVendor(truth, splits, **kw)
    with pytest.raises(RuntimeError) as e:
        p.force_full_refetch(sym, "1d", provider_name="FMPOHLCVProvider", keep_newer_cached_tail=True)
    assert sym in str(e.value)
    assert open(path, "rb").read() == before, "a refused keep leaves the file byte-identical"
    assert read_full_fetch_marker(path) is None


def test_the_overlap_window_is_the_plateau_convention():
    assert KEEP_TAIL_OVERLAP_SESSIONS == 60 and 1 <= KEEP_TAIL_MAX_BARS <= 10
