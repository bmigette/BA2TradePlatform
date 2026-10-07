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
    mono = {"t": 1000.0, "last": None}
    monkeypatch.setattr(mdpi_mod, "_mono", lambda: mono["t"], raising=False)   # the memo's TTL clock follows the frozen one

    def set_now(ny_text: str) -> datetime:
        inst = pd.Timestamp(ny_text, tz=NY_TZ).to_pydatetime().astimezone(timezone.utc)
        if mono["last"] is not None:
            mono["t"] += (inst - mono["last"]).total_seconds()
        mono["last"] = inst
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
def _live_overlay_on():
    """Another test may have built a MemoizedOHLCVProvider (which switches the process to backtest mode)."""
    fb.set_live_overlay_enabled(True)
    yield
    fb.set_live_overlay_enabled(True)


@pytest.fixture(autouse=True)
def _clean_memos():
    def clear():
        for name in ("_SPLIT_BASIS_REPORTED", "_TOPUP_REFUSED", "_UNFINISHED_MEMO", "_FORMING_MISSING_LOGGED"):
            getattr(MarketDataProviderInterface, name, set()).clear()
    clear()
    yield
    clear()


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


def test_reads_inside_the_forming_bar_ttl_make_one_vendor_call_and_then_one_per_ttl(clock):
    # F3: the cache already holds the last FINAL bar and its file is >24 h old: the 09:31 top-up finds
    # only the forming bar, writes nothing (mtime stays old), and the memo throttles the next reads
    path = seed(truth(PREV), "2026-10-05 09:00")
    p = _Provider(with_forming(truth(D), D))
    clock("2026-10-06 09:31")
    p.get_ohlcv_data(SYMBOL, lookback_days=40, interval="1d")
    assert len(p.impl_calls) == 1
    assert os.path.getmtime(path) == ny_epoch("2026-10-05 09:00")           # nothing was written

    clock("2026-10-06 09:36")                                               # 5 min later: inside the TTL
    df = p.get_ohlcv_data(SYMBOL, lookback_days=40, interval="1d")
    df = p.get_ohlcv_data(SYMBOL, lookback_days=40, interval="1d")
    assert len(p.impl_calls) == 1                                           # the memo throttled both
    assert last_day(df) == D and df.iloc[-1].Volume == 4_000
    assert last_day(disk()) == PREV

    clock("2026-10-06 09:45")                                               # 14 min after the fetch: expired
    p.vendor = with_forming(truth(D), D).assign(Volume=lambda x: x.Volume.where(x.Date != pd.Timestamp(D), 9_000))
    df = p.get_ohlcv_data(SYMBOL, lookback_days=40, interval="1d")
    assert len(p.impl_calls) == 2 and df.iloc[-1].Volume == 9_000           # a fresh forming bar, not the 09:31 one
    assert last_day(disk()) == PREV
    assert getattr(mdpi_mod, 'FORMING_BAR_TTL_S', None) == 600.0


def test_a_restart_or_a_fresh_mtime_with_an_empty_memo_still_gets_todays_forming_bar(clock):
    # F3: exactly the state of the repaired files -- fresh by mtime (written today 06:30), memo empty
    # (process just started). A LATEST read during the session must fetch, memoize and NOT persist.
    path = seed(truth(PREV), "2026-10-06 06:30")
    before = open(path, "rb").read()
    p = _Provider(with_forming(truth(D), D))
    clock("2026-10-06 10:00")
    df = p.get_ohlcv_data(SYMBOL, lookback_days=40, interval="1d")
    assert len(p.impl_calls) == 1
    assert last_day(df) == D and df.iloc[-1].Volume == 4_000
    assert open(path, "rb").read() == before                                # the disk is untouched
    assert fb.forming_bar_status(df) == "present" and fb.last_bar_is_today(df, SYMBOL)


def test_during_the_session_a_missing_forming_bar_is_visible_to_the_caller(clock, monkeypatch):
    # F3: the vendor has no bar for today yet -> the frame's last row is an OLDER session. That must not
    # be silent: ERROR logged once per symbol per day, attrs says "missing", last_bar_is_today is False.
    seed(truth(PREV), "2026-10-06 06:30")
    p = _Provider(truth(PREV))                                              # no bar for D at all
    seen = []
    monkeypatch.setattr(mdpi_mod.logger, "error", lambda m, *a, **k: seen.append(str(m)))
    clock("2026-10-06 10:00")
    df = p.get_ohlcv_data(SYMBOL, lookback_days=40, interval="1d")
    assert fb.forming_bar_status(df) == "missing" and not fb.last_bar_is_today(df, SYMBOL)
    assert len(seen) == 1 and "no forming bar could be obtained" in seen[0] and SYMBOL in seen[0]
    clock("2026-10-06 10:20")
    p.get_ohlcv_data(SYMBOL, lookback_days=40, interval="1d")
    assert len(seen) == 1                                                   # once per symbol per day


def test_outside_a_session_nothing_is_expected_and_nothing_is_logged(clock, monkeypatch):
    seed(truth(PREV), "2026-10-06 06:30")
    seen = []
    monkeypatch.setattr(mdpi_mod.logger, "error", lambda m, *a, **k: seen.append(str(m)))
    clock("2026-10-06 08:00")                                               # before the open
    df = _Provider(truth(PREV)).get_ohlcv_data(SYMBOL, lookback_days=40, interval="1d")
    assert fb.forming_bar_status(df) == "not_expected" and seen == []
    assert last_day(df) == PREV


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


def test_once_the_session_is_final_the_snapshot_is_never_served_as_the_final_bar(clock):
    seed(truth(FRI), "2026-10-03 12:00")
    p = _Provider(with_forming(truth(D), D))
    clock("2026-10-06 09:31")
    p.get_ohlcv_data(SYMBOL, lookback_days=40, interval="1d")
    t = ny_epoch("2026-10-06 09:31")
    os.utime(native_cache.find_timeseries_path("_Provider", SYMBOL, "1d"), (t, t))
    clock("2026-10-06 20:30")                                # settled: the memo's snapshot is obsolete
    df = p.get_ohlcv_data(SYMBOL, lookback_days=40, interval="1d")
    assert last_day(df) == PREV and not (df["Volume"] == 4_000).any()      # never the 09:31 snapshot
    assert fb.forming_bar_status(df) == "not_expected"
    # the next session's first read fetches D's FINAL bar (and persists it) plus the new forming bar
    p.vendor = with_forming(truth(date(2026, 10, 7)), date(2026, 10, 7))
    clock("2026-10-07 09:35")
    df = p.get_ohlcv_data(SYMBOL, lookback_days=40, interval="1d")
    got = disk()
    assert last_day(got) == D and got[got["Date"] == pd.Timestamp(D)].iloc[0].Volume == truth(D).iloc[-1].Volume
    assert last_day(df) == date(2026, 10, 7)


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
    """CHARACTERIZATION (passes on the pre-fix code too): the guard still refuses what mtime cannot prove."""
    # same snapshot, but the file's mtime is later than the session's settlement (e.g. a copy/touch)
    path = contaminated("2026-10-06 22:00")
    before = open(path, "rb").read()
    p = _Provider(truth(date(2026, 10, 7)))
    clock("2026-10-07 09:35")

    with pytest.raises(OHLCVTopUpRefused, match="disagrees"):
        p._refresh_parquet_if_stale(pd.read_parquet(path), SYMBOL, "1d", "_Provider")
    assert open(path, "rb").read() == before


def test_a_backdated_mtime_before_the_session_is_not_a_proof(clock):
    """CHARACTERIZATION (passes on the pre-fix code too): a backdated mtime must not trigger the heal."""
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
    """CHARACTERIZATION (passes on the pre-fix code too): the switch must not change an ordinary append."""
    path = seed(truth(FRI), "2026-10-03 12:00")
    monkeypatch.setenv("BA2_OHLCV_TOPUP_FULL_REFETCH", "0")
    clock("2026-10-07 09:00")
    _Provider(truth(D)).get_ohlcv_data(SYMBOL, lookback_days=40, interval="1d")
    assert last_day(disk()) == D                              # appended exactly as without the switch


def test_the_default_still_replaces_a_split_history(clock):
    """CHARACTERIZATION (passes on the pre-fix code too): without the switch a split replaces the history."""
    cached, p = _split_world()
    path = seed(cached, "2026-10-03 12:00")
    clock("2026-10-07 09:00")
    p._refresh_parquet_if_stale(pd.read_parquet(path), SYMBOL, "1d", "_Provider")
    assert read_full_fetch_marker(path) is not None


@pytest.mark.parametrize("value", ["false", "maybe", "2", ""])
def test_a_bad_switch_value_raises_on_every_top_up_not_only_on_a_split(clock, monkeypatch, value):
    # F4: validated ONCE at the top of _verified_tail_topup. Before, "false" silently meant "not additive"
    # in the appendable branch.
    path = seed(truth(FRI), "2026-10-03 12:00")
    before = open(path, "rb").read()
    monkeypatch.setenv("BA2_OHLCV_TOPUP_FULL_REFETCH", value)
    clock("2026-10-07 09:00")
    with pytest.raises(ValueError, match="BA2_OHLCV_TOPUP_FULL_REFETCH"):
        _Provider(truth(D))._refresh_parquet_if_stale(pd.read_parquet(path), SYMBOL, "1d", "_Provider")
    assert open(path, "rb").read() == before


def provisional_copy(df: pd.DataFrame, idx: int) -> pd.DataFrame:
    """``df`` with the bar at ``idx`` replaced by a PROVISIONAL one in the guard's sense: an open that differs
    from the vendor's by more than the equality tolerance but lies inside the vendor's day range (and within
    3 %), high/low/close equal, volume below the vendor's."""
    out = df.copy()
    r = out.loc[idx]
    out.loc[idx, "Open"] = round(r.Low + 0.2 * (r.Open - r.Low), 3)
    out.loc[idx, "Volume"] = int(r.Volume * 0.5)
    assert abs(out.loc[idx, "Open"] - r.Open) / r.Open > 0.005          # outside the 0.5 % equality tolerance
    return out


def _topup(path):
    return _Provider(truth(D))._refresh_parquet_if_stale(pd.read_parquet(path), SYMBOL, "1d", "_Provider")


def test_additive_only_switch_never_replaces_a_cached_bar_but_the_default_does(clock, monkeypatch):
    # F5: a REAL provisional bar (the previous version changed a Close by 0.1 %, inside the equality
    # tolerance, and passed with the protection deleted)
    cached = provisional_copy(truth(FRI), len(truth(FRI)) - 3)
    stuck_day = cached["Date"].iloc[-3]
    clock("2026-10-07 09:00")

    # default: the guard classifies it provisional and REPLACES it with the vendor's bar
    path = seed(cached, "2026-10-03 12:00")
    _topup(path)
    got = disk()
    assert got[got["Date"] == stuck_day].iloc[0].Open == pytest.approx(
        truth(FRI)[truth(FRI)["Date"] == stuck_day].iloc[0].Open)

    # additive-only: the cached bar is kept exactly, and the new bars are still appended after it
    path = seed(cached, "2026-10-03 12:00")
    monkeypatch.setenv("BA2_OHLCV_TOPUP_FULL_REFETCH", "0")
    _topup(path)
    got = disk()
    kept = got[got["Date"] == stuck_day].iloc[0]
    mine = cached[cached["Date"] == stuck_day].iloc[0]
    assert kept.Open == mine.Open and kept.Volume == mine.Volume
    assert last_day(got) == D


def test_the_additive_refusal_names_the_heal_exception(clock, monkeypatch):
    cached, p = _split_world()
    path = seed(cached, "2026-10-03 12:00")
    monkeypatch.setenv("BA2_OHLCV_TOPUP_FULL_REFETCH", "0")
    clock("2026-10-07 09:00")
    with pytest.raises(OHLCVTopUpRefused) as e:
        p._refresh_parquet_if_stale(pd.read_parquet(path), SYMBOL, "1d", "_Provider")
    assert "DEFERRED" in str(e.value) and "still healed" in str(e.value)


def test_a_provably_partial_newest_bar_is_still_healed_in_an_additive_run(clock, monkeypatch):
    # F4: the documented exception to "strictly additive"
    contaminated("2026-10-06 09:32")
    monkeypatch.setenv("BA2_OHLCV_TOPUP_FULL_REFETCH", "0")
    clock("2026-10-07 09:35")
    p = _Provider(pd.concat([truth(D), truth(date(2026, 10, 7)).iloc[[-1]]], ignore_index=True))
    p.get_ohlcv_data(SYMBOL, lookback_days=40, interval="1d")
    got = disk()
    row = got[got["Date"] == pd.Timestamp(D)].iloc[0]
    want = truth(D).iloc[-1]
    assert row.Volume == want.Volume and row.Open == pytest.approx(want.Open)


# --------------------------------------------------------------------------- F1: a refused top-up stays loud
def test_a_refused_top_up_raises_on_every_read_and_never_serves_a_mixed_basis_frame(clock, monkeypatch):
    cached, _p = _split_world()
    path = seed(cached, "2026-10-03 12:00")                      # old basis (x2)
    before = open(path, "rb").read()
    # the vendor serves the NEW basis incl. today's forming bar; the calendar lists a split => a full
    # re-fetch is called for; the additive switch refuses it (a guard refusal behaves the same way)
    p = _Provider(with_forming(truth(D), D), [CalendarSplit(PREV, 2.0)])
    monkeypatch.setenv("BA2_OHLCV_TOPUP_FULL_REFETCH", "0")
    clock("2026-10-06 09:31")
    with pytest.raises(OHLCVTopUpRefused):
        p.get_ohlcv_data(SYMBOL, lookback_days=40, interval="1d")
    assert not getattr(MarketDataProviderInterface, "_UNFINISHED_MEMO", {}).get(("_Provider", SYMBOL, "1d"))   # not memoized

    for when in ("2026-10-06 09:35", "2026-10-06 09:50"):        # second and third reads, same process
        clock(when)
        with pytest.raises(OHLCVTopUpRefused):
            p.get_ohlcv_data(SYMBOL, lookback_days=40, interval="1d")
    assert open(path, "rb").read() == before                      # the disk was never touched


def test_a_guard_refusal_of_a_basis_mismatch_also_stays_loud_on_the_second_read(clock):
    # same, without the switch: an unexplained disagreement (a settled bar 6 % off, no split to explain it)
    cached = truth(FRI).copy()
    cached.loc[cached.index[-4], "Close"] = round(cached.loc[cached.index[-4], "Close"] * 1.06, 3)
    seed(cached, "2026-10-03 12:00")
    p = _Provider(with_forming(truth(D), D))
    clock("2026-10-06 09:31")
    with pytest.raises(OHLCVTopUpRefused):
        p.get_ohlcv_data(SYMBOL, lookback_days=40, interval="1d")
    clock("2026-10-06 09:34")
    with pytest.raises(OHLCVTopUpRefused):
        p.get_ohlcv_data(SYMBOL, lookback_days=40, interval="1d")


# --------------------------------------------------------------------------- F2: intraday throttling
def _five_minute_world():
    prev_bars = pd.date_range("2026-10-05 09:30", "2026-10-05 15:55", freq="5min")
    cached = minute_frame(prev_bars)
    cached["effective_date"] = cached["Date"]
    d = os.path.join(native_cache.CACHE_FOLDER, "_Provider")
    os.makedirs(d, exist_ok=True)
    f = os.path.join(d, f"{SYMBOL}_5min.parquet")
    cached.to_parquet(f, index=False)
    return prev_bars, f


def test_n_intraday_reads_inside_one_bar_make_one_vendor_call(clock):
    prev_bars, f = _five_minute_world()
    p = _Provider(minute_frame(list(prev_bars) + list(pd.date_range("2026-10-06 09:30", "2026-10-06 09:40", freq="5min"))))
    clock("2026-10-06 09:41")
    df = None
    for _ in range(6):
        df = p.get_ohlcv_data(SYMBOL, lookback_days=5, interval="5min")
    assert len(p.impl_calls) == 1
    assert pd.Timestamp(df["Date"].max()).tz_localize(None) == pd.Timestamp("2026-10-06 09:40")   # live sees the forming bar
    on_disk = pd.read_parquet(f)
    assert pd.Timestamp(on_disk["Date"].max()) == pd.Timestamp("2026-10-06 09:35")                # the disk does not

    clock("2026-10-06 09:47")                                    # 6 min later: past one bar interval
    p.get_ohlcv_data(SYMBOL, lookback_days=5, interval="5min")
    assert len(p.impl_calls) == 2


# --------------------------------------------------------------------------- backtest processes
def test_a_backtest_process_never_overlays_or_force_fetches_a_forming_bar(clock):
    path = seed(truth(PREV), "2026-10-06 06:30")
    before = open(path, "rb").read()
    p = _Provider(with_forming(truth(D), D))
    clock("2026-10-06 10:00")                                    # session open, file fresh by mtime
    fb.set_live_overlay_enabled(False)                           # what MemoizedOHLCVProvider's constructor does
    df = p.get_ohlcv_data(SYMBOL, lookback_days=40, interval="1d")   # end_date=None => is_latest, like DeterministicScorer's
    assert last_day(df) == PREV and p.impl_calls == []           # no forced fetch, no overlay: the old behaviour
    assert fb.forming_bar_status(df) == "not_expected"
    assert open(path, "rb").read() == before
    # and an overlay memoized earlier in the same process is not served either
    p._remember_unfinished_bars(with_forming(truth(D), D), SYMBOL, "1d", "_Provider")
    assert last_day(p.get_ohlcv_data(SYMBOL, lookback_days=40, interval="1d")) == PREV


# --------------------------------------------------------------------------- heal: the live frame keeps its bar
def test_a_healed_bar_is_not_lost_from_the_live_frame_when_the_vendor_cannot_replace_it(clock):
    path = contaminated("2026-10-06 09:32")
    before = open(path, "rb").read()
    clock("2026-10-07 09:35")
    # (a) the vendor has nothing newer than the day before the healed bar
    df = _Provider(truth(PREV))._refresh_parquet_if_stale(pd.read_parquet(path), SYMBOL, "1d", "_Provider")
    assert last_day(df) == D and open(path, "rb").read() == before

    # (b) the fetch fails
    class Boom(_Provider):
        def _get_ohlcv_data_impl(self, *a, **k):
            raise RuntimeError("vendor down")
    df = Boom(truth(D))._refresh_parquet_if_stale(pd.read_parquet(path), SYMBOL, "1d", "_Provider")
    assert last_day(df) == D and open(path, "rb").read() == before


# --------------------------------------------------------------------------- minor: writer return, marker
def test_write_timeseries_reports_when_it_wrote_nothing(clock):
    clock("2026-10-06 09:31")
    df = truth(D)
    df["effective_date"] = df["Date"]
    assert native_cache.write_timeseries("_Provider", "XRET", "1d", df) is True
    assert native_cache.write_timeseries("_Provider", "XNEWB", "1d", df.iloc[[-1]]) is False


def test_a_cold_fill_that_wrote_no_file_records_no_marker(clock, monkeypatch):
    clock("2026-10-06 09:31")
    seen = []
    monkeypatch.setattr(mdpi_mod.logger, "info", lambda m, *a, **k: seen.append(str(m)))
    _Provider(truth(D))._record_cold_full_fetch("_Provider", "XNOFILE", "1d")
    assert any("no file was written" in m for m in seen)
    assert not os.path.exists(os.path.join(native_cache.CACHE_FOLDER, "_Provider", "_split_basis"))


# --------------------------------------------------------------------------- F7: the Senate price store
def test_the_senate_price_store_never_persists_a_forming_row(clock):
    import json
    from ba2_providers import fmp_common
    clock("2026-10-06 09:31")
    payload = [{"date": "2026-10-06", "open": 90.0, "high": 90.0, "low": 85.0, "close": 85.0, "volume": 4000},
               {"date": "2026-10-05", "open": 50.0, "high": 52.0, "low": 49.0, "close": 51.0, "volume": 9_000_000},
               {"date": "2025-01-02", "open": 40.0, "high": 41.0, "low": 39.0, "close": 40.5, "volume": 1_000_000}]
    got = fmp_common._fmp_history_disk_read_or_fetch("historical_price_full", "XSEN", lambda: [dict(r) for r in payload], 7)
    assert [r["date"] for r in got] == ["2026-10-06", "2026-10-05", "2025-01-02"]       # the caller still gets every row
    f = os.path.join(fmp_common._fmp_history_cache_dir(), "historical_price_full__XSEN.json")
    assert [r["date"] for r in json.load(open(f))] == ["2026-10-05", "2025-01-02"]       # the file never holds the forming one
    clock("2026-10-06 20:30")                                                              # after settlement the row is final
    os.unlink(f)
    fmp_common._fmp_history_disk_read_or_fetch("historical_price_full", "XSEN", lambda: [dict(r) for r in payload], 7)
    assert [r["date"] for r in json.load(open(f))] == ["2026-10-06", "2026-10-05", "2025-01-02"]


def test_other_fmp_history_namespaces_are_untouched_by_the_finality_rule(clock):
    import json
    from ba2_providers import fmp_common
    clock("2026-10-06 09:31")
    rows = [{"date": "2026-10-06", "x": 1}]
    fmp_common._fmp_history_disk_read_or_fetch("some_other_namespace", "XOTH", lambda: rows, 7)
    f = os.path.join(fmp_common._fmp_history_cache_dir(), "some_other_namespace__XOTH.json")
    assert json.load(open(f)) == rows
