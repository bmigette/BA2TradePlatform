"""The finality rule: which OHLCV bars may be persisted (``ba2_common.core.ohlcv_final_bars``).

A daily bar is final at ``regular close + 4 h`` (13:00 ET close on a half day), America/New_York;
an intraday bar when its interval has ended; anything the NYSE calendar does not cover takes the
calendar-free upper bound ``D + 1 day + 12 h + 4 h`` UTC (never early, only late).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pandas as pd
import pytest

from ba2_common.core import ohlcv_final_bars as fb
from ba2_common.core import market_calendar as mc

NY = mc.NY_TZ


def ny(text: str) -> datetime:
    """An America/New_York wall-clock time as a tz-aware UTC instant."""
    return pd.Timestamp(text, tz=NY).to_pydatetime().astimezone(timezone.utc)


def daily(*days: str) -> pd.DataFrame:
    return pd.DataFrame({"Date": pd.to_datetime(list(days)), "Open": 1.0, "High": 2.0, "Low": 0.5,
                         "Close": 1.5, "Volume": 10})


def unfinished(df, symbol, interval, now):
    return [str(pd.Timestamp(d).date()) if interval == "1d" else str(d)
            for d in fb.split_unfinished(df, symbol, interval, now)[1]["Date"]]


# --------------------------------------------------------------------------- daily, regular day
def test_regular_day_is_final_exactly_four_hours_after_the_close():
    df = daily("2026-10-05", "2026-10-06", "2026-10-07")
    assert unfinished(df, "AAPL", "1d", ny("2026-10-07 09:31")) == ["2026-10-07"]
    # Oct 6 closed 16:00 ET; final at 20:00 ET, not a minute before
    assert unfinished(df, "AAPL", "1d", ny("2026-10-06 19:59")) == ["2026-10-06", "2026-10-07"]
    assert unfinished(df, "AAPL", "1d", ny("2026-10-06 20:00")) == ["2026-10-07"]
    assert unfinished(df, "AAPL", "1d", ny("2026-10-07 20:00")) == []


def test_session_final_at_is_close_plus_settle():
    assert fb.session_final_at("AAPL", date(2026, 10, 6)) == ny("2026-10-06 20:00")
    assert fb.SETTLE_AFTER_CLOSE == timedelta(hours=4)


def test_half_day_closes_at_13_et_so_is_final_at_17_et():
    day = date(2026, 11, 27)                       # Friday after Thanksgiving: 13:00 ET close
    assert fb.session_final_at("AAPL", day) == ny("2026-11-27 17:00")
    df = daily("2026-11-27")
    assert unfinished(df, "AAPL", "1d", ny("2026-11-27 16:59")) == ["2026-11-27"]
    assert unfinished(df, "AAPL", "1d", ny("2026-11-27 17:00")) == []


@pytest.mark.parametrize("day,closes_utc", [
    ("2026-03-06", "2026-03-06 21:00"),            # EST, the Friday before the spring-forward weekend
    ("2026-03-09", "2026-03-09 20:00"),            # EDT, the Monday after
    ("2026-10-30", "2026-10-30 20:00"),            # EDT, before fall-back
    ("2026-11-02", "2026-11-02 21:00"),            # EST, the Monday after fall-back
])
def test_dst_boundary_closes_are_the_new_york_closes(day, closes_utc):
    close = mc.regular_session_close_utc(date.fromisoformat(day))
    assert close == datetime.fromisoformat(closes_utc).replace(tzinfo=timezone.utc)
    assert fb.session_final_at("AAPL", date.fromisoformat(day)) == close + timedelta(hours=4)


def test_a_weekend_run_persists_friday_and_nothing_is_forming():
    df = daily("2026-10-08", "2026-10-09")         # Thu, Fri
    assert unfinished(df, "AAPL", "1d", ny("2026-10-10 12:00")) == []      # Saturday noon
    assert unfinished(df, "AAPL", "1d", ny("2026-10-09 15:00")) == ["2026-10-09"]   # Friday session


def test_a_holiday_run_persists_the_prior_session():
    # Thanksgiving Thursday 2026-11-26: no session, no bar. Wednesday's bar is long final at noon.
    df = daily("2026-11-25")
    assert unfinished(df, "AAPL", "1d", ny("2026-11-26 12:00")) == []


def test_a_bar_dated_on_a_non_session_day_takes_the_calendar_free_bound():
    # Saturday 2026-10-10 (a crypto / forex style bar): not an NYSE session -> D + 1 day + 16 h UTC
    assert fb.session_final_at("BTCUSD", date(2026, 10, 10)) == datetime(2026, 10, 11, 16, 0, tzinfo=timezone.utc)
    df = daily("2026-10-10")
    assert unfinished(df, "BTCUSD", "1d", ny("2026-10-10 20:30")) == ["2026-10-10"]   # NYSE rule would say final
    assert unfinished(df, "BTCUSD", "1d", datetime(2026, 10, 11, 16, 0, tzinfo=timezone.utc)) == []


@pytest.mark.parametrize("symbol", ["0700.HK", "VOD.L", "005930.KS", "000001.SZ", "7203.T"])
def test_a_non_nyse_symbol_never_uses_the_nyse_calendar(symbol):
    # never guessed: final only when no exchange on Earth can still be trading local date D
    day = date(2026, 10, 6)
    assert fb.session_final_at(symbol, day) == datetime(2026, 10, 7, 16, 0, tzinfo=timezone.utc)
    df = daily("2026-10-06")
    assert unfinished(df, symbol, "1d", datetime(2026, 10, 7, 15, 59, tzinfo=timezone.utc)) == ["2026-10-06"]
    assert unfinished(df, symbol, "1d", datetime(2026, 10, 7, 16, 0, tzinfo=timezone.utc)) == []


def test_the_nyse_calendar_being_unavailable_falls_back_to_the_bound_and_says_so(monkeypatch):
    def boom(day):
        raise mc.MarketCalendarUnavailable("no calendar")
    seen = []
    monkeypatch.setattr(fb, "is_regular_session", boom)
    monkeypatch.setattr(fb.logger, "error", lambda msg, *a, **k: seen.append(str(msg)))
    got = fb.session_final_at("AAPL", date(2026, 10, 6))
    assert got == datetime(2026, 10, 7, 16, 0, tzinfo=timezone.utc)      # later than 20:00 ET, never earlier
    assert got > ny("2026-10-06 20:00")
    assert seen and "calendar unavailable" in seen[0]


def test_tz_aware_utc_midnight_daily_labels_read_as_their_utc_date():
    df = pd.DataFrame({"Date": pd.to_datetime(["2026-10-06", "2026-10-07"]).tz_localize("UTC"),
                       "Open": 1.0, "High": 1.0, "Low": 1.0, "Close": 1.0, "Volume": 1})
    keep, gone = fb.split_unfinished(df, "AAPL", "1d", ny("2026-10-07 09:31"))
    assert len(keep) == 1 and len(gone) == 1


def test_old_bars_never_touch_the_calendar(monkeypatch):
    monkeypatch.setattr(fb, "is_regular_session", lambda d: (_ for _ in ()).throw(AssertionError("calendar used")))
    df = daily("2005-01-03", "2020-03-16")
    keep, gone = fb.split_unfinished(df, "AAPL", "1d", ny("2026-10-07 09:31"))
    assert len(keep) == 2 and gone.empty


# --------------------------------------------------------------------------- intraday
def intraday(*stamps: str, tz=None) -> pd.DataFrame:
    d = pd.to_datetime(list(stamps))
    if tz:
        d = d.tz_localize(tz)
    return pd.DataFrame({"Date": d, "Open": 1.0, "High": 1.0, "Low": 1.0, "Close": 1.0, "Volume": 1})


def test_a_five_minute_bar_is_final_when_its_interval_has_ended():
    df = intraday("2026-10-07 09:25", "2026-10-07 09:30", "2026-10-07 09:35")
    keep, gone = fb.split_unfinished(df, "SPY", "5min", ny("2026-10-07 09:31"))
    assert list(keep["Date"].dt.strftime("%H:%M")) == ["09:25"]               # 09:30 bar ends 09:35
    keep, gone = fb.split_unfinished(df, "SPY", "5m", ny("2026-10-07 09:35"))
    assert list(keep["Date"].dt.strftime("%H:%M")) == ["09:25", "09:30"]      # spellings are aliases


def test_tz_tagged_intraday_labels_are_read_as_new_york_wall_clock():
    # FMP intraday text is New York wall-clock parsed with utc=True: the tag is a lie, the text is NY
    df = intraday("2026-10-07 09:30", "2026-10-07 09:25", tz="UTC")
    keep, gone = fb.split_unfinished(df, "SPY", "5min", ny("2026-10-07 09:31"))
    assert list(keep["Date"].dt.strftime("%H:%M")) == ["09:25"] and len(gone) == 1


def test_intraday_of_a_non_nyse_symbol_is_read_at_the_latest_offset_on_earth():
    df = intraday("2026-10-07 09:30")
    # label + 12 h + 5 min is the earliest this bar can be final anywhere
    assert fb.split_unfinished(df, "0700.HK", "5min", datetime(2026, 10, 7, 21, 34, tzinfo=timezone.utc))[1].shape[0] == 1
    assert fb.split_unfinished(df, "0700.HK", "5min", datetime(2026, 10, 7, 21, 35, tzinfo=timezone.utc))[1].empty


def test_the_ambiguous_fall_back_hour_is_read_as_the_later_instant():
    # 2026-11-01 01:30 happens twice in New York; the later (EST) one is 06:30 UTC
    df = intraday("2026-11-01 01:30")
    assert fb.split_unfinished(df, "SPY", "5min", datetime(2026, 11, 1, 6, 34, tzinfo=timezone.utc))[1].shape[0] == 1
    assert fb.split_unfinished(df, "SPY", "5min", datetime(2026, 11, 1, 6, 35, tzinfo=timezone.utc))[1].empty


# --------------------------------------------------------------------------- weekly / monthly / unknown
def test_weekly_and_monthly_bars_are_final_only_after_their_period():
    wk = daily("2026-10-05")                        # week of Mon Oct 5 .. Sun Oct 11
    assert fb.split_unfinished(wk, "AAPL", "1wk", ny("2026-10-10 12:00"))[1].shape[0] == 1
    assert fb.split_unfinished(wk, "AAPL", "1wk", datetime(2026, 10, 12, 16, 0, tzinfo=timezone.utc))[1].empty
    mo = daily("2026-09-01")
    assert fb.split_unfinished(mo, "AAPL", "1mo", datetime(2026, 10, 1, 15, 59, tzinfo=timezone.utc))[1].shape[0] == 1
    assert fb.split_unfinished(mo, "AAPL", "1mo", datetime(2026, 10, 1, 16, 0, tzinfo=timezone.utc))[1].empty


def test_an_interval_without_a_rule_is_refused_loudly_not_guessed():
    with pytest.raises(fb.UnknownIntervalError):
        fb.split_unfinished(daily("2026-10-06"), "AAPL", "3h", ny("2026-10-07 09:31"))


def test_a_frame_without_a_date_column_is_refused():
    with pytest.raises(ValueError, match="Date"):
        fb.split_unfinished(pd.DataFrame({"Close": [1.0]}), "AAPL", "1d", ny("2026-10-07 09:31"))


def test_nothing_unfinished_returns_the_same_frame_object():
    df = daily("2026-09-01")
    keep, gone = fb.split_unfinished(df, "AAPL", "1d", ny("2026-10-07 09:31"))
    assert keep is df and gone.empty


# --------------------------------------------------------------------------- written_before_final
def test_a_write_inside_the_bars_own_session_proves_a_snapshot():
    day = date(2026, 10, 6)
    assert fb.written_before_final("AAPL", day, ny("2026-10-06 09:31"))
    assert fb.written_before_final("AAPL", day, ny("2026-10-06 19:59"))
    assert not fb.written_before_final("AAPL", day, ny("2026-10-06 20:00"))      # settled
    assert not fb.written_before_final("AAPL", day, ny("2026-10-06 09:00"))      # before the open: proves nothing
    assert not fb.written_before_final("AAPL", day, ny("2026-10-03 12:00"))      # backdated / inconsistent


# --------------------------------------------------------------------------- live helpers
def test_forming_session_day_is_the_open_to_settlement_window():
    assert fb.forming_session_day("AAPL", ny("2026-10-06 09:29")) is None                   # before the open
    assert fb.forming_session_day("AAPL", ny("2026-10-06 09:30")) == date(2026, 10, 6)
    assert fb.forming_session_day("AAPL", ny("2026-10-06 19:59")) == date(2026, 10, 6)      # still settling
    assert fb.forming_session_day("AAPL", ny("2026-10-06 20:00")) is None
    assert fb.forming_session_day("AAPL", ny("2026-10-10 12:00")) is None                   # Saturday
    assert fb.forming_session_day("AAPL", ny("2026-11-26 12:00")) is None                   # Thanksgiving
    assert fb.forming_session_day("AAPL", ny("2026-11-27 16:59")) == date(2026, 11, 27)     # half day: settles at 17:00
    assert fb.forming_session_day("AAPL", ny("2026-11-27 17:00")) is None
    assert fb.forming_session_day("0700.HK", ny("2026-10-06 10:00")) is None                # no NYSE calendar: never guessed


def test_last_final_session_day():
    assert fb.last_final_session_day("AAPL", ny("2026-10-07 09:31")) == date(2026, 10, 6)
    assert fb.last_final_session_day("AAPL", ny("2026-10-06 19:59")) == date(2026, 10, 5)
    assert fb.last_final_session_day("AAPL", ny("2026-10-10 12:00")) == date(2026, 10, 9)
    assert fb.last_final_session_day("0700.HK", ny("2026-10-07 09:31")) is None


def test_last_bar_is_today_and_the_status_attribute():
    f = daily("2026-10-05", "2026-10-06")
    assert fb.last_bar_is_today(f, "AAPL", ny("2026-10-06 10:00")) is True
    assert fb.last_bar_is_today(f, "AAPL", ny("2026-10-07 10:00")) is False
    assert fb.last_bar_is_today(f.iloc[0:0], "AAPL", ny("2026-10-06 10:00")) is False
    assert fb.forming_bar_status(f) is None
    f.attrs[fb.FORMING_STATUS_ATTR] = "missing"
    assert fb.forming_bar_status(f) == "missing"


def test_the_live_overlay_switch_defaults_off_and_round_trips():
    """OPT-IN (2026-10-07 live review): no forced in-session vendor fetch, no forming-bar overlay."""
    assert fb.live_overlay_enabled() is False
    fb.set_live_overlay_enabled(True)
    try:
        assert fb.live_overlay_enabled() is True
    finally:
        fb.set_live_overlay_enabled(False)


def test_final_mask_of_a_long_intraday_frame_only_judges_the_tail():
    idx = pd.date_range("2018-10-01 09:30", periods=150_000, freq="5min")
    big = pd.DataFrame({"Date": idx, "Open": 1.0, "High": 1.0, "Low": 1.0, "Close": 1.0, "Volume": 1})
    now = ny("2118-01-01 12:00")                                     # far after every bar: all final
    assert fb.final_mask(big, "SPY", "5min", now).all()
    tail_now = pd.Timestamp(idx[-1] + pd.Timedelta(minutes=1)).tz_localize(NY).to_pydatetime().astimezone(timezone.utc)
    m = fb.final_mask(big, "SPY", "5min", tail_now)
    assert m.iloc[:-1].all() and not m.iloc[-1]                      # only the last bar is still forming


def test_settle_after_close_is_one_documented_constant():
    import inspect
    src = inspect.getsource(fb)
    assert fb.SETTLE_AFTER_CLOSE == timedelta(hours=4)
    import re
    assert len(re.findall(r"^SETTLE_AFTER_CLOSE = ", src, re.M)) == 1 and "Measured on FMP" in src
