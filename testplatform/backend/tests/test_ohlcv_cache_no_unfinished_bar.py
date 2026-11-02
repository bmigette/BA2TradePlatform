"""``ba2-test fetch-cache`` (``extend_ohlcv_cache``) never writes an unfinished bar.

Run during the session with ``end`` = today, the vendor answers with today's FORMING bar. The shared
cache the backtests read must keep stopping at the last FINAL session, on every branch of
``extend_ohlcv_cache``: cold fill, the verified daily tail top-up, a split-triggered full re-fetch,
and the 5-minute right extension (``ba2_common.core.ohlcv_final_bars``).
"""
from __future__ import annotations

from datetime import date, datetime, timezone

import numpy as np
import pandas as pd
import pytest

from ba2_common.core import native_cache, ohlcv_final_bars as fb
from ba2_common.core.interfaces.MarketDataProviderInterface import MarketDataProviderInterface
from ba2_common.core.market_calendar import NY_TZ, nyse_regular_sessions
from ba2_common.core.split_basis import CalendarSplit, read_full_fetch_marker
from ba2_providers.ohlcv.FMPOHLCVProvider import FMPOHLCVProvider

D = date(2026, 10, 6)       # the session forming at 09:31 ET
PREV = date(2026, 10, 5)
FRI = date(2026, 10, 2)
SESSIONS = [o.astimezone(NY_TZ).date() for o, _c in nyse_regular_sessions(date(2024, 1, 2), D)]


def _full() -> pd.DataFrame:
    rng = np.random.default_rng(3)
    n = len(SESSIONS)
    c = 80 * np.exp(np.cumsum(rng.normal(0.0002, 0.01, n)))
    o = c * (1 + rng.normal(0, 0.002, n))
    return pd.DataFrame({"Date": pd.to_datetime(SESSIONS), "Open": o.round(3),
                         "High": (np.maximum(o, c) * 1.01).round(3), "Low": (np.minimum(o, c) * 0.99).round(3),
                         "Close": c.round(3), "Volume": rng.integers(5_000_000, 9_000_000, n)})


FULL = _full()


def truth(through: date) -> pd.DataFrame:
    return FULL[FULL["Date"] <= pd.Timestamp(through)].reset_index(drop=True).copy()


def forming_today() -> pd.DataFrame:
    out = truth(D)
    out.loc[out.index[-1], ["Open", "High", "Low", "Close", "Volume"]] = [90.0, 90.0, 85.0, 85.0, 3_000]
    return out


class FakeFMP(FMPOHLCVProvider):
    def __new__(cls, *a, **k):
        return object.__new__(cls)

    def __init__(self, vendor, splits=()):
        super().__init__(api_key="test-key")
        self.vendor, self.splits = vendor, list(splits)

    def _get_ohlcv_data_impl(self, symbol, start_date, end_date, interval="1d"):
        v = self.vendor
        out = v[(v["Date"] >= pd.Timestamp(start_date.date()))
                & (v["Date"] <= pd.Timestamp(end_date.date()) + (pd.Timedelta(days=1) if interval != "1d" else pd.Timedelta(0)))
                ].reset_index(drop=True).copy()
        out["Date"] = out["Date"].dt.tz_localize("UTC")
        return out

    def _split_calendar(self, symbol, interval):
        return list(self.splits)


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    import ba2_common.config as cfg
    import ba2_common.core.native_cache as nc_src
    monkeypatch.setattr(cfg, "CACHE_FOLDER", str(tmp_path), raising=False)
    monkeypatch.setattr(nc_src, "CACHE_FOLDER", str(tmp_path), raising=False)
    monkeypatch.setattr(native_cache, "CACHE_FOLDER", str(tmp_path), raising=False)
    inst = pd.Timestamp("2026-10-06 09:31", tz=NY_TZ).to_pydatetime().astimezone(timezone.utc)
    monkeypatch.setattr(fb, "now_utc", lambda: inst)
    MarketDataProviderInterface._TOPUP_REFUSED.clear()
    getattr(MarketDataProviderInterface, "_UNFINISHED_MEMO", set()).clear()
    yield
    MarketDataProviderInterface._TOPUP_REFUSED.clear()
    getattr(MarketDataProviderInterface, "_UNFINISHED_MEMO", set()).clear()


def _seed(symbol, df, interval="1d"):
    out = df.copy()
    out["effective_date"] = out["Date"]
    native_cache.write_timeseries("FakeFMP", symbol, interval, out)
    return native_cache.find_timeseries_path("FakeFMP", symbol, interval)


def _last(path) -> pd.Timestamp:
    return pd.Timestamp(pd.read_parquet(path)["Date"].max())


def test_fetch_cache_tail_top_up_during_the_session_stops_at_the_last_final_session():
    from app.services.ohlcv_cache_provider import wrap_with_cache
    path = _seed("XTAIL", truth(FRI))
    prov = wrap_with_cache(FakeFMP(forming_today()))

    out = prov.extend_ohlcv_cache("XTAIL", datetime(2026, 6, 15), datetime(2026, 10, 6), "1d")

    assert _last(path) == pd.Timestamp(PREV)
    assert not (pd.read_parquet(path)["Date"] == pd.Timestamp(D)).any()
    assert out["Date"].max() == pd.Timestamp(PREV)       # the in-memory merge never took it as history either


def test_fetch_cache_after_close_plus_delay_writes_the_final_bar(monkeypatch):
    from app.services.ohlcv_cache_provider import wrap_with_cache
    inst = pd.Timestamp("2026-10-06 20:01", tz=NY_TZ).to_pydatetime().astimezone(timezone.utc)
    monkeypatch.setattr(fb, "now_utc", lambda: inst)
    path = _seed("XLATE", truth(FRI))
    wrap_with_cache(FakeFMP(truth(D))).extend_ohlcv_cache("XLATE", datetime(2026, 6, 15), datetime(2026, 10, 6), "1d")
    assert _last(path) == pd.Timestamp(D)


def test_fetch_cache_cold_fill_during_the_session_writes_final_bars_only():
    from app.services.ohlcv_cache_provider import wrap_with_cache
    prov = wrap_with_cache(FakeFMP(forming_today()))
    prov.extend_ohlcv_cache("XCOLD", datetime(2026, 6, 15), datetime(2026, 10, 6), "1d")
    path = native_cache.find_timeseries_path("FakeFMP", "XCOLD", "1d")
    assert _last(path) == pd.Timestamp(PREV)


def test_a_forced_full_refetch_during_the_session_writes_no_unfinished_bar_and_marks_the_last_final_session():
    from app.services.ohlcv_cache_provider import wrap_with_cache
    cached = truth(FRI).copy()
    for col in ("Open", "High", "Low", "Close"):
        cached[col] = (cached[col] * 2.0).round(3)              # the pre-split basis
    path = _seed("XSPLIT", cached)
    prov = wrap_with_cache(FakeFMP(forming_today(), [CalendarSplit(PREV, 2.0)]))

    prov.extend_ohlcv_cache("XSPLIT", datetime(2026, 6, 15), datetime(2026, 10, 6), "1d")

    assert _last(path) == pd.Timestamp(PREV)
    assert read_full_fetch_marker(path)["last_bar"] == PREV.isoformat()


def test_fetch_cache_5min_during_the_session_does_not_persist_the_forming_bar():
    from app.services.ohlcv_cache_provider import wrap_with_cache
    prev_bars = pd.date_range("2026-10-05 09:30", "2026-10-05 15:55", freq="5min")
    bars = list(prev_bars) + [pd.Timestamp("2026-10-06 09:30")]
    vendor = pd.DataFrame({"Date": pd.to_datetime(bars), "Open": 1.0, "High": 2.0, "Low": 0.5, "Close": 1.5,
                           "Volume": 100})
    path = _seed("XM5", vendor[vendor["Date"] < pd.Timestamp("2026-10-05 12:00")], interval="5min")
    prov = wrap_with_cache(FakeFMP(vendor))

    prov.extend_ohlcv_cache("XM5", datetime(2026, 10, 1), datetime(2026, 10, 6, 23, 59), "5min")

    assert _last(path) == pd.Timestamp("2026-10-05 15:55")         # the 09:30 bar ends at 09:35 > 09:31


# --------------------------------------------------------------------------- backtest processes
def test_building_a_backtest_provider_turns_the_live_overlay_off_and_hermetic_reads_never_touch_the_inner_provider(tmp_path):
    """Reviewer's question: can a BACKTEST process ever see an overlaid forming bar? DeterministicScorer's
    read is ``get_ohlcv_data(end_date=replay_now(None))`` (= LATEST on the inner provider), so:
    (1) the hermetic reader (``cached_only=True``) never calls the inner ``get_ohlcv_data`` at all -- it
    reads the parquet; (2) constructing ANY MemoizedOHLCVProvider switches the process's live overlay and
    live forming-bar fetch off, so even a non-hermetic wrapper's LATEST read gets the pre-fix behaviour."""
    from app.services.backtest.price_source import MemoizedOHLCVProvider

    fb.set_live_overlay_enabled(True)
    path = _seed("XBT", truth(PREV))
    inner = FakeFMP(forming_today())

    def _trip(self, *a, **k):
        raise AssertionError("a hermetic backtest read reached the inner provider")
    # the cache is keyed on the inner provider's CLASS name: keep it "FakeFMP"
    Tripwire = type("FakeFMP", (FakeFMP,), {"get_ohlcv_data": _trip})
    hermetic = MemoizedOHLCVProvider(Tripwire(forming_today()), datetime(2026, 1, 1), datetime(2026, 10, 6), "1d",
                                     cached_only=True)
    assert fb.live_overlay_enabled() is False                      # constructing it flipped the process switch
    df, _dates = hermetic._load("XBT", "1d")
    assert df["Date"].max() == pd.Timestamp(PREV)                  # the parquet only: no forming bar

    # non-hermetic wrapper: a LATEST inner read neither overlays nor force-fetches the forming bar
    MarketDataProviderInterface._UNFINISHED_MEMO.clear()
    inner._remember_unfinished_bars(forming_today(), "XBT", "1d", "FakeFMP")
    latest = inner.get_ohlcv_data("XBT", lookback_days=40, interval="1d")
    assert pd.Timestamp(latest["Date"].max()).tz_localize(None) == pd.Timestamp(PREV)
    assert path == native_cache.find_timeseries_path("FakeFMP", "XBT", "1d")
    fb.set_live_overlay_enabled(False)      # the process default (ON is an explicit opt-in)
