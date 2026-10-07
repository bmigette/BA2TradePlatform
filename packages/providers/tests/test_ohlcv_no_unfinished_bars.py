"""An unfinished OHLCV bar is never written to the cache, by any writer (2026-10-07).

The 09:31 New York top-up used to append the vendor's FORMING bar of the day to the shared parquet:
one tick (O == H, L == C), frozen there, read by every backtest as history, and the tail guard then
refused every later top-up. ``ba2_common.core.ohlcv_final_bars`` is the rule (daily: session close +
4 h, 13:00 ET close on a half day; intraday: the interval has ended); ``native_cache.write_timeseries``
the one writer that enforces it. Live still SEES the forming bar, from the process memo.

Time is frozen per test (the frozen 'now' moves ``ohlcv_final_bars.now_utc`` and the
``datetime`` of the provider module), file mtimes are set with ``os.utime``.
"""
from __future__ import annotations

import importlib
import os
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from ba2_common.core import native_cache, ohlcv_final_bars as fb
from ba2_common.core.interfaces.MarketDataProviderInterface import MarketDataProviderInterface
from ba2_common.core.market_calendar import NY_TZ, nyse_regular_sessions
from ba2_common.core.ohlcv_topup_guard import OHLCVTopUpRefused
from ba2_common.core.split_basis import CalendarSplit, read_full_fetch_marker
from ba2_providers.ohlcv.FMPOHLCVProvider import FMPOHLCVProvider

mdpi_mod = importlib.import_module("ba2_common.core.interfaces.MarketDataProviderInterface")
provider_utils = importlib.import_module("ba2_common.core.provider_utils")
PROVIDER = "FMPOHLCVProvider"
SYMBOL = "XFRM"

D = date(2026, 10, 6)            # Tuesday: the session whose bar is forming at 09:31 ET
PREV = date(2026, 10, 5)
FRI = date(2026, 10, 2)


def _sessions(a: date, b: date):
    return [o.astimezone(NY_TZ).date() for o, _c in nyse_regular_sessions(a, b)]


def ny_epoch(text: str) -> float:
    return pd.Timestamp(text, tz=NY_TZ).timestamp()


class _Provider(FMPOHLCVProvider):
    """The real FMP provider with the network replaced by a frame. Daily dates come back as tz-aware
    UTC midnights like ``_fetch_daily_data``; ``vendor`` is swapped by the test as the day goes on."""

    def __new__(cls, *a, **k):
        return object.__new__(cls)

    def __init__(self, vendor, splits=()):
        super().__init__(api_key="test-key")
        self.vendor, self.splits, self.impl_calls = vendor, list(splits), []

    def _get_ohlcv_data_impl(self, symbol, start_date, end_date, interval="1d"):
        self.impl_calls.append((symbol, start_date, end_date, interval))
        d = self.vendor
        if interval == "1d":
            out = d[(d["Date"] >= pd.Timestamp(start_date.date()))
                    & (d["Date"] <= pd.Timestamp(end_date.date()))].reset_index(drop=True).copy()
            out["Date"] = pd.to_datetime(out["Date"]).dt.tz_localize("UTC")
            return out
        # FMP intraday: New York wall-clock text parsed with utc=True (tagged UTC, not converted)
        out = d[(d["Date"] >= pd.Timestamp(start_date.date()))
                & (d["Date"] <= pd.Timestamp(end_date.date()) + pd.Timedelta(days=1))].reset_index(drop=True).copy()
        out["Date"] = pd.to_datetime(out["Date"]).dt.tz_localize("UTC")
        return out

    def _split_calendar(self, symbol, interval):
        return list(self.splits)


class _FrozenDatetime(datetime):
    instant = None            # tz-aware UTC

    @classmethod
    def now(cls, tz=None):
        return cls.instant.astimezone(tz) if tz else cls.instant.astimezone().replace(tzinfo=None)

    @classmethod
    def utcnow(cls):
        return cls.instant.replace(tzinfo=None)


@pytest.fixture
def clock(monkeypatch):
    """``clock('2026-10-06 09:31')`` freezes 'now' at that New York wall-clock time."""
    def set_now(ny_text: str) -> datetime:
        inst = pd.Timestamp(ny_text, tz=NY_TZ).to_pydatetime().astimezone(timezone.utc)
        _FrozenDatetime.instant = inst
        monkeypatch.setattr(fb, "now_utc", lambda: _FrozenDatetime.instant)
        monkeypatch.setattr(mdpi_mod, "datetime", _FrozenDatetime)
        monkeypatch.setattr(provider_utils, "datetime", _FrozenDatetime)   # validate_date_range's 'now'
        return inst
    return set_now


@pytest.fixture(autouse=True)
def _own_cache(tmp_path, monkeypatch):
    """A private cache folder per test (the providers conftest shares one for the whole session)."""
    import ba2_common.config as bcfg
    cache = str(tmp_path / "cache")
    monkeypatch.setattr(bcfg, "CACHE_FOLDER", cache)
    monkeypatch.setattr(native_cache, "CACHE_FOLDER", cache)
    monkeypatch.setattr(native_cache, "_CACHE_ROOT", os.path.join(cache, "datasets", "cache"))


@pytest.fixture(autouse=True)
def _clean_memos():
    for memo in (MarketDataProviderInterface._SPLIT_BASIS_REPORTED, MarketDataProviderInterface._TOPUP_REFUSED,
                 MarketDataProviderInterface._UNFINISHED_MEMO):
        memo.clear()
    yield
    for memo in (MarketDataProviderInterface._SPLIT_BASIS_REPORTED, MarketDataProviderInterface._TOPUP_REFUSED,
                 MarketDataProviderInterface._UNFINISHED_MEMO):
        memo.clear()


# --------------------------------------------------------------------------- data
SESSIONS = _sessions(date(2024, 1, 2), date(2026, 12, 31))


def _full() -> pd.DataFrame:
    rng = np.random.default_rng(7)
    n = len(SESSIONS)
    c = 100 * np.exp(np.cumsum(rng.normal(0.0002, 0.01, n)))
    o = c * (1 + rng.normal(0, 0.002, n))
    return pd.DataFrame({"Date": pd.to_datetime(SESSIONS), "Open": o.round(3),
                         "High": (np.maximum(o, c) * 1.01).round(3), "Low": (np.minimum(o, c) * 0.99).round(3),
                         "Close": c.round(3), "Volume": rng.integers(5_000_000, 9_000_000, n)})


FULL = _full()


def truth(through: date) -> pd.DataFrame:
    """The vendor's FINAL daily bars through ``through`` (inclusive)."""
    return FULL[FULL["Date"] <= pd.Timestamp(through)].reset_index(drop=True).copy()


def with_forming(df: pd.DataFrame, day: date) -> pd.DataFrame:
    """``df`` with the bar of ``day`` replaced by a one-tick forming snapshot whose open/high lie
    OUTSIDE the final day's range (what froze AMD on 2026-09-28)."""
    out = df.copy()
    i = out.index[out["Date"] == pd.Timestamp(day)][0]
    final = out.loc[i]
    out.loc[i, ["Open", "High", "Low", "Close", "Volume"]] = [final.High * 1.02, final.High * 1.02,
                                                              final.Close, final.Close, 4_000]
    return out


def seed(df: pd.DataFrame, mtime_ny: str, symbol: str = SYMBOL, provider: str = "_Provider") -> str:
    """Write ``df`` as the cache file and stamp its mtime."""
    out = df.copy()
    out["effective_date"] = out["Date"]
    path = os.path.join(native_cache.CACHE_FOLDER, provider)
    os.makedirs(path, exist_ok=True)
    out.to_parquet(os.path.join(path, f"{symbol}_1d.parquet"), index=False)   # raw: bypasses the rule
    p = native_cache.find_timeseries_path(provider, symbol, "1d")
    t = ny_epoch(mtime_ny)
    os.utime(p, (t, t))
    return p


def disk(symbol=SYMBOL, provider="_Provider") -> pd.DataFrame:
    return pd.read_parquet(native_cache.find_timeseries_path(provider, symbol, "1d"))


def last_day(df) -> date:
    return pd.Timestamp(df["Date"].max()).date()


# --------------------------------------------------------------------------- live: 09:31 ET
def test_a_top_up_at_0931_ny_does_not_persist_todays_bar_but_live_still_sees_it(clock):
    cached = truth(FRI)
    path = seed(cached, "2026-10-03 12:00")
    vendor = with_forming(truth(D), D)
    clock("2026-10-06 09:31")
    p = _Provider(vendor)

    df = p.get_ohlcv_data(SYMBOL, lookback_days=40, interval="1d")

    # the disk stops at the last FINAL session (the vendor's 10-05 bar was appended)
    on_disk = disk()
    assert last_day(on_disk) == PREV
    assert not (on_disk["Date"] == pd.Timestamp(D)).any()
    assert len(on_disk) == len(cached) + 1
    # live still sees today's forming bar, the one-tick snapshot, from memory
    assert last_day(df) == D
    last = df.iloc[-1]
    assert last.Open == last.High and last.Low == last.Close and last.Volume == 4_000
    assert "effective_date" not in df.columns
    # the file is a normal one the reader can slice
    assert native_cache.read_timeseries("_Provider", SYMBOL, "1d", as_of=None)["Date"].max() == pd.Timestamp(PREV)


def test_a_second_live_read_the_same_day_makes_no_vendor_call_and_still_returns_the_forming_bar(clock):
    # the cache already holds the last FINAL bar and its file is >24 h old: the 09:31 top-up finds
    # only the forming bar, writes nothing (mtime stays old), and the memo must stand in for it
    path = seed(truth(PREV), "2026-10-05 09:00")
    p = _Provider(with_forming(truth(D), D))
    clock("2026-10-06 09:31")
    p.get_ohlcv_data(SYMBOL, lookback_days=40, interval="1d")
    assert len(p.impl_calls) == 1
    assert os.path.getmtime(path) == ny_epoch("2026-10-05 09:00")           # nothing was written

    clock("2026-10-06 11:00")
    df = p.get_ohlcv_data(SYMBOL, lookback_days=40, interval="1d")

    assert len(p.impl_calls) == 1                                           # the memo throttled it
    assert last_day(df) == D and df.iloc[-1].Volume == 4_000
    assert last_day(disk()) == PREV

    # control: without the memo (e.g. after a restart) the vendor is asked again
    MarketDataProviderInterface._UNFINISHED_MEMO.clear()
    p.get_ohlcv_data(SYMBOL, lookback_days=40, interval="1d")
    assert len(p.impl_calls) == 2


def test_the_next_day_appends_the_vendors_final_bar_and_the_tail_guard_never_trips(clock, ):
    seed(truth(FRI), "2026-10-03 12:00")
    p = _Provider(with_forming(truth(D), D))
    clock("2026-10-06 09:31")
    p.get_ohlcv_data(SYMBOL, lookback_days=40, interval="1d")
    t = ny_epoch("2026-10-06 09:31")
    os.utime(native_cache.find_timeseries_path("_Provider", SYMBOL, "1d"), (t, t))   # the frozen clock's write time

    # next morning the vendor serves the FINAL bar for D (different from the snapshot) + a new forming one
    final_d = truth(D)
    p.vendor = with_forming(truth(date(2026, 10, 7)), date(2026, 10, 7))
    clock("2026-10-07 09:35")
    df = p.get_ohlcv_data(SYMBOL, lookback_days=40, interval="1d")

    on_disk = disk()
    assert last_day(on_disk) == D
    row = on_disk[on_disk["Date"] == pd.Timestamp(D)].iloc[0]
    want = final_d[final_d["Date"] == pd.Timestamp(D)].iloc[0]
    assert (row.Open, row.High, row.Low, row.Close) == pytest.approx((want.Open, want.High, want.Low, want.Close))
    assert MarketDataProviderInterface._TOPUP_REFUSED == {}
    assert last_day(df) == date(2026, 10, 7)               # and live sees the new forming bar
    # nothing before the new bars changed
    old = pd.concat([truth(FRI), truth(PREV)[truth(PREV)["Date"] > pd.Timestamp(FRI)]], ignore_index=True)
    got = on_disk[on_disk["Date"] <= pd.Timestamp(PREV)].reset_index(drop=True)
    assert list(got["Date"]) == list(old["Date"])
    assert got["Close"].to_numpy() == pytest.approx(old["Close"].to_numpy())


def test_the_bar_of_a_stale_memo_is_not_served_once_it_is_final(clock):
    seed(truth(FRI), "2026-10-03 12:00")
    p = _Provider(with_forming(truth(D), D))
    clock("2026-10-06 09:31")
    p.get_ohlcv_data(SYMBOL, lookback_days=40, interval="1d")
    clock("2026-10-06 20:30")                                # settled: the snapshot must not be served as final
    df = p.get_ohlcv_data(SYMBOL, lookback_days=40, interval="1d")
    assert last_day(df) == PREV


# --------------------------------------------------------------------------- close + delay, half day, weekend
def test_a_top_up_at_close_plus_the_settlement_delay_persists_todays_bar(clock):
    seed(truth(FRI), "2026-10-03 12:00")
    p = _Provider(truth(D))
    clock("2026-10-06 19:59")
    p.get_ohlcv_data(SYMBOL, lookback_days=40, interval="1d")
    assert last_day(disk()) == PREV                          # one minute before close + 4 h

    seed(truth(FRI), "2026-10-03 12:00")                     # back to the stale file, run at 20:00:01
    clock("2026-10-06 20:00")
    p = _Provider(truth(D))
    df = p.get_ohlcv_data(SYMBOL, lookback_days=40, interval="1d")
    assert last_day(disk()) == D and last_day(df) == D


def test_a_half_day_is_final_at_17_et(clock):
    fri_before = date(2026, 11, 25)                          # Wednesday before Thanksgiving
    half = date(2026, 11, 27)                                # 13:00 ET close
    seed(truth(fri_before), "2026-11-26 12:00")
    clock("2026-11-27 16:59")
    p = _Provider(truth(half))
    p.get_ohlcv_data(SYMBOL, lookback_days=40, interval="1d")
    assert last_day(disk()) == fri_before

    seed(truth(fri_before), "2026-11-26 12:00")
    clock("2026-11-27 17:01")
    p = _Provider(truth(half))
    p.get_ohlcv_data(SYMBOL, lookback_days=40, interval="1d")
    assert last_day(disk()) == half


def test_a_weekend_run_persists_friday_and_a_holiday_run_the_session_before(clock):
    fri = date(2026, 10, 9)
    seed(truth(date(2026, 10, 7)), "2026-10-08 12:00")
    clock("2026-10-10 12:00")                                # Saturday
    p = _Provider(truth(fri))
    p.get_ohlcv_data(SYMBOL, lookback_days=40, interval="1d")
    assert last_day(disk()) == fri

    wed = date(2026, 11, 25)
    seed(truth(date(2026, 11, 23)), "2026-11-24 12:00", symbol="XHOL")
    clock("2026-11-26 12:00")                                # Thanksgiving: no session, no bar
    p = _Provider(truth(wed))
    p.get_ohlcv_data("XHOL", lookback_days=40, interval="1d")
    assert last_day(disk("XHOL")) == wed


# --------------------------------------------------------------------------- split-triggered full re-fetch
def test_a_split_triggered_full_refetch_during_the_session_does_not_persist_todays_bar(clock):
    split_day = PREV
    final = truth(D)
    # the cache holds the pre-split history AS TRADED (x2); the vendor serves it split-adjusted
    cached = truth(FRI).copy()
    for col in ("Open", "High", "Low", "Close"):
        cached[col] = (cached[col] * 2.0).round(3)
    path = seed(cached, "2026-10-03 12:00")
    p = _Provider(with_forming(final, D), [CalendarSplit(split_day, 2.0)])
    clock("2026-10-06 09:31")

    df = p.get_ohlcv_data(SYMBOL, lookback_days=40, interval="1d")

    on_disk = disk()
    assert read_full_fetch_marker(path) is not None
    assert last_day(on_disk) == PREV                         # replaced history, minus the forming bar
    assert not (on_disk["Date"] == pd.Timestamp(D)).any()
    assert read_full_fetch_marker(path)["last_bar"] == PREV.isoformat()   # marker: the last FINAL session
    assert len(on_disk) == len(final) - 1
    assert last_day(df) == D                                 # live still sees the forming bar


def test_force_full_refetch_returns_and_writes_final_bars_only(clock):
    p = _Provider(with_forming(truth(D), D))
    clock("2026-10-06 09:31")
    out = p.force_full_refetch(SYMBOL, "1d", provider_name="_Provider")
    assert last_day(out) == PREV and last_day(disk()) == PREV


# --------------------------------------------------------------------------- self-heal / loud refusal
def contaminated(snapshot_written_ny: str):
    """A file written by the pre-fix code: the bar of D is the one-tick snapshot, newest on disk."""
    return seed(with_forming(truth(D), D), snapshot_written_ny)


def test_a_contaminated_newest_bar_self_heals_when_the_mtime_proves_it(clock):
    path = contaminated("2026-10-06 09:32")                  # written mid-session of D
    final = truth(D)
    p = _Provider(with_forming(truth(date(2026, 10, 7)), date(2026, 10, 7)))
    p.vendor = pd.concat([final, truth(date(2026, 10, 7)).iloc[[-1]]], ignore_index=True)
    clock("2026-10-07 09:35")

    p.get_ohlcv_data(SYMBOL, lookback_days=40, interval="1d")

    row = disk()[disk()["Date"] == pd.Timestamp(D)].iloc[0]
    want = final[final["Date"] == pd.Timestamp(D)].iloc[0]
    assert (row.Open, row.High, row.Low, row.Close, row.Volume) == pytest.approx(
        (want.Open, want.High, want.Low, want.Close, want.Volume))
    assert MarketDataProviderInterface._TOPUP_REFUSED == {}
    assert path == native_cache.find_timeseries_path("_Provider", SYMBOL, "1d")


def test_a_contaminated_bar_the_mtime_cannot_prove_is_still_refused_loudly(clock):
    # same snapshot, but the file's mtime is later than the session's settlement (e.g. a copy/touch)
    path = contaminated("2026-10-06 22:00")
    before = open(path, "rb").read()
    p = _Provider(truth(date(2026, 10, 7)))
    clock("2026-10-07 09:35")

    with pytest.raises(OHLCVTopUpRefused, match="disagrees"):
        p._refresh_parquet_if_stale(pd.read_parquet(path), SYMBOL, "1d", "_Provider")
    assert open(path, "rb").read() == before


def test_a_backdated_mtime_before_the_session_is_not_a_proof(clock):
    path = contaminated("2026-10-03 12:00")                  # mtime BEFORE the bar's own session
    p = _Provider(truth(date(2026, 10, 7)))
    clock("2026-10-07 09:35")
    with pytest.raises(OHLCVTopUpRefused):
        p._refresh_parquet_if_stale(pd.read_parquet(path), SYMBOL, "1d", "_Provider")


# --------------------------------------------------------------------------- backtest-side reads
def test_a_backtest_side_read_never_sees_an_unfinished_bar(clock):
    seed(truth(FRI), "2026-10-03 12:00")
    p = _Provider(with_forming(truth(D), D))
    clock("2026-10-06 09:31")
    p.get_ohlcv_data(SYMBOL, lookback_days=40, interval="1d")      # the live write

    # a backtest (pinned end_date) is served from the file only, in every spelling of the slice
    bt = _Provider(truth(D)).get_ohlcv_data(SYMBOL, start_date=datetime(2026, 9, 1),
                                            end_date=datetime(2026, 10, 5, 23, 59), interval="1d")
    assert last_day(bt) == PREV
    assert native_cache.read_timeseries("_Provider", SYMBOL, "1d", as_of=None)["Date"].max() == pd.Timestamp(PREV)
    assert native_cache.read_timeseries("_Provider", SYMBOL, "1d", as_of=datetime(2026, 10, 6, 13, 0))["Date"].max() \
        == pd.Timestamp(PREV)
    assert (pd.read_parquet(native_cache.find_timeseries_path("_Provider", SYMBOL, "1d"))["Date"]
            <= pd.Timestamp(PREV)).all()


def test_a_cold_fill_during_the_session_writes_final_bars_only(clock):
    clock("2026-10-06 09:31")
    p = _Provider(with_forming(truth(D), D))
    df = p.get_ohlcv_data("XCOLD", lookback_days=40, interval="1d")
    assert last_day(disk("XCOLD")) == PREV and last_day(df) == D


# --------------------------------------------------------------------------- 5-minute cache
def minute_frame(stamps, base=100.0):
    d = pd.to_datetime(stamps)
    return pd.DataFrame({"Date": d, "Open": base, "High": base + 1, "Low": base - 1, "Close": base + .5,
                         "Volume": 1000})


def test_a_forming_five_minute_bar_is_not_persisted_and_is_fetched_again_once_final(clock):
    prev_bars = pd.date_range("2026-10-05 09:30", "2026-10-05 15:55", freq="5min")
    cached = minute_frame(prev_bars)
    cached["effective_date"] = cached["Date"]
    d = os.path.join(native_cache.CACHE_FOLDER, "_Provider")
    os.makedirs(d, exist_ok=True)
    cached.to_parquet(os.path.join(d, f"{SYMBOL}_5min.parquet"), index=False)

    p = _Provider(minute_frame(list(prev_bars) + [pd.Timestamp("2026-10-06 09:30")]))
    clock("2026-10-06 09:31")
    df = p._refresh_parquet_if_stale(pd.read_parquet(os.path.join(d, f"{SYMBOL}_5min.parquet")),
                                     SYMBOL, "5min", "_Provider")
    on_disk = pd.read_parquet(os.path.join(d, f"{SYMBOL}_5min.parquet"))
    assert pd.Timestamp(on_disk["Date"].max()) == pd.Timestamp("2026-10-05 15:55")       # 09:30 bar still forming
    assert pd.Timestamp(df["Date"].max()).tz_localize(None) == pd.Timestamp("2026-10-06 09:30")   # live sees it

    # ten minutes later the 09:30 and 09:35 bars are final and are fetched (the top-up resumes at
    # the last PERSISTED bar + 5 min, not after the forming one)
    p.vendor = minute_frame(list(prev_bars) + list(pd.date_range("2026-10-06 09:30", "2026-10-06 09:40", freq="5min")),
                            base=101.0)
    clock("2026-10-06 09:41")
    p._refresh_parquet_if_stale(pd.read_parquet(os.path.join(d, f"{SYMBOL}_5min.parquet")),
                                SYMBOL, "5min", "_Provider")
    on_disk = pd.read_parquet(os.path.join(d, f"{SYMBOL}_5min.parquet"))
    assert pd.Timestamp(on_disk["Date"].max()) == pd.Timestamp("2026-10-06 09:35")       # 09:40 still forming
    assert not on_disk["Date"].duplicated().any()


# --------------------------------------------------------------------------- the one writer
def test_write_timeseries_is_the_single_choke_point(clock):
    clock("2026-10-06 09:31")
    df = truth(D)
    df["effective_date"] = df["Date"]
    native_cache.write_timeseries("_Provider", "XDIRECT", "1d", df)
    assert last_day(disk("XDIRECT")) == PREV
    # a write that would leave nothing writes nothing (no empty file shadowing a cold fill)
    only_today = df.iloc[[-1]]
    native_cache.write_timeseries("_Provider", "XNEW", "1d", only_today)
    assert native_cache.find_timeseries_path("_Provider", "XNEW", "1d") is None


def test_the_legacy_csv_cache_obeys_the_same_rule(clock, tmp_path):
    clock("2026-10-06 09:31")
    p = _Provider(with_forming(truth(D), D))
    target = str(tmp_path / "legacy.csv")
    assert p._save_final_bars_cache(truth(D), SYMBOL, "1d", target)
    saved = pd.read_csv(target, parse_dates=["Date"])
    assert last_day(saved) == PREV


# --------------------------------------------------------------------------- additive-only refresh switch
def _split_world():
    cached = truth(FRI).copy()
    for col in ("Open", "High", "Low", "Close"):
        cached[col] = (cached[col] * 2.0).round(3)          # the pre-split basis
    return cached, _Provider(truth(D), [CalendarSplit(PREV, 2.0)])


def test_additive_only_switch_refuses_a_split_replacement_and_writes_nothing(clock, monkeypatch):
    cached, p = _split_world()
    path = seed(cached, "2026-10-03 12:00")
    before = open(path, "rb").read()
    monkeypatch.setenv("BA2_OHLCV_TOPUP_FULL_REFETCH", "0")
    clock("2026-10-07 09:00")
    with pytest.raises(OHLCVTopUpRefused, match="DEFERRED"):
        p._refresh_parquet_if_stale(pd.read_parquet(path), SYMBOL, "1d", "_Provider")
    assert open(path, "rb").read() == before and read_full_fetch_marker(path) is None
    assert not [c for c in p.impl_calls if c[1].year < 2025]        # no 15-year re-fetch was even asked for


def test_additive_only_switch_leaves_an_ordinary_top_up_alone(clock, monkeypatch):
    path = seed(truth(FRI), "2026-10-03 12:00")
    monkeypatch.setenv("BA2_OHLCV_TOPUP_FULL_REFETCH", "0")
    clock("2026-10-07 09:00")
    _Provider(truth(D)).get_ohlcv_data(SYMBOL, lookback_days=40, interval="1d")
    assert last_day(disk()) == D                              # appended exactly as without the switch


def test_the_default_still_replaces_and_a_bad_switch_value_is_refused(clock, monkeypatch):
    cached, p = _split_world()
    path = seed(cached, "2026-10-03 12:00")
    clock("2026-10-07 09:00")
    monkeypatch.setenv("BA2_OHLCV_TOPUP_FULL_REFETCH", "maybe")
    with pytest.raises(ValueError, match="BA2_OHLCV_TOPUP_FULL_REFETCH"):
        p._refresh_parquet_if_stale(pd.read_parquet(path), SYMBOL, "1d", "_Provider")
    monkeypatch.delenv("BA2_OHLCV_TOPUP_FULL_REFETCH")
    p._refresh_parquet_if_stale(pd.read_parquet(path), SYMBOL, "1d", "_Provider")
    assert read_full_fetch_marker(path) is not None            # replaced, as before


def test_additive_only_switch_never_rewrites_a_cached_bar_even_a_provisional_looking_one(clock, monkeypatch):
    # a cached bar inside the guard's provisional tolerance would normally be replaced by the vendor's
    cached = truth(FRI).copy()
    cached.loc[cached.index[-3], "Close"] = round(cached.loc[cached.index[-3], "Close"] * 1.001, 3)
    path = seed(cached, "2026-10-03 12:00")
    monkeypatch.setenv("BA2_OHLCV_TOPUP_FULL_REFETCH", "0")
    clock("2026-10-07 09:00")
    _Provider(truth(D)).get_ohlcv_data(SYMBOL, lookback_days=40, interval="1d")
    got = disk()
    old = got[got["Date"] <= pd.Timestamp(FRI)].reset_index(drop=True)
    assert old["Close"].to_numpy() == pytest.approx(cached["Close"].to_numpy(), abs=0) and last_day(got) == D
