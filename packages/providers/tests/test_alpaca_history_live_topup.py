"""Top-up core: finished sessions, continuity, split table, seam (synthetic frames, fake fetcher, no network)."""
from datetime import date, datetime, timedelta, timezone

import pandas as pd
import pytest

from ba2_common.core.market_calendar import regular_session_dates
from ba2_providers.ohlcv.alpaca_history import live_topup
from ba2_providers.ohlcv.alpaca_history.live_topup import (
    CHUNK_SESSIONS, PendingCorporateAction, SymbolNotInHistory, TopUpGap, topup_frame)


def utc(y, m, d, h=12):
    return datetime(y, m, d, h, tzinfo=timezone.utc)


def daily_cached(last: str, n: int = 5, close: float = 10.0) -> pd.DataFrame:
    days = pd.bdate_range(end=last, periods=n)
    return pd.DataFrame({"Date": days, "Open": close, "High": close + 1, "Low": close - 1, "Close": close,
                         "Volume": 1000, "effective_date": days})


def raw_daily(days, px=10.0) -> pd.DataFrame:
    d = pd.to_datetime(list(days))
    return pd.DataFrame({"Date": d, "Open": px, "High": px + 1, "Low": px - 1, "Close": px, "Volume": 1000.0})


def raw_5min(days, per_day=78, px=10.0) -> pd.DataFrame:
    rows = []
    for d in days:
        for i in range(per_day):
            rows.append(pd.Timestamp(d) + pd.Timedelta(hours=9, minutes=30 + 5 * i))
    t = pd.DatetimeIndex(rows)
    return pd.DataFrame({"Date": t, "Open": px, "High": px + 1, "Low": px - 1, "Close": px, "Volume": 100.0})


class Fetcher:
    def __init__(self, frame):
        self.frame, self.calls = frame, []

    def __call__(self, start, end_excl):
        self.calls.append((start, end_excl))
        d = pd.to_datetime(self.frame["Date"]).dt.normalize()
        return self.frame[(d >= pd.Timestamp(start)) & (d < pd.Timestamp(end_excl))]


@pytest.fixture
def errors(monkeypatch):
    seen = []

    class L:
        def error(self, msg, *a, **k):
            seen.append(msg)

        def __getattr__(self, name):
            return lambda *a, **k: None
    monkeypatch.setattr(live_topup, "logger", L())
    return seen


def run(cached, fetcher, now, interval="1d", splits=(), spinoffs=(), symbol="AAA"):
    return topup_frame(cached, fetcher, splits, spinoffs, interval=interval, now_utc=now, symbol=symbol)


def test_contiguous_append_over_a_weekend_boundary():
    cached = daily_cached("2026-10-16")                                   # Friday
    f = Fetcher(raw_daily(regular_session_dates(date(2026, 10, 19), date(2026, 10, 23))))
    merged, rep = run(cached, f, utc(2026, 10, 24))                      # Saturday
    assert [d.date().isoformat() for d in merged["Date"].iloc[-5:]] == [
        "2026-10-19", "2026-10-20", "2026-10-21", "2026-10-22", "2026-10-23"]
    assert f.calls == [(date(2026, 10, 19), date(2026, 10, 24))]
    assert rep["appended"] == 5 and merged["Date"].is_monotonic_increasing
    assert list(merged.columns) == list(cached.columns) and str(merged["Volume"].dtype) == "int64"


def test_holiday_and_half_day_are_not_gaps():
    cached = daily_cached("2026-11-24")                                   # Tuesday
    f = Fetcher(raw_daily(["2026-11-25", "2026-11-27"]))                  # 26th Thanksgiving closed, 27th half day
    merged, rep = run(cached, f, utc(2026, 11, 30, 15))                  # Monday 10:00 NY, Monday not final
    assert rep["sessions"] == [date(2026, 11, 25), date(2026, 11, 27)] and len(merged) == 7


def test_only_final_sessions_are_requested():
    cached = daily_cached("2026-10-15")
    f = Fetcher(raw_daily(["2026-10-16", "2026-10-19"]))
    # Monday 19th 21:00 UTC = 17:00 NY: Monday closed at 16:00, not yet final (+4h); Friday 16th is.
    merged, rep = run(cached, f, utc(2026, 10, 19, 21))
    assert rep["sessions"] == [date(2026, 10, 16)] and f.calls == [(date(2026, 10, 16), date(2026, 10, 17))]


def test_no_new_session_means_no_fetch():
    cached = daily_cached("2026-10-16")
    f = Fetcher(raw_daily([]))
    merged, rep = run(cached, f, utc(2026, 10, 19, 14))                  # Monday morning: Friday is already there
    assert merged is cached and f.calls == [] and rep["appended"] == 0


def test_a_missing_session_in_the_middle_appends_nothing_and_logs_error(errors):
    cached = daily_cached("2026-10-16")
    f = Fetcher(raw_daily(["2026-10-19", "2026-10-20", "2026-10-22", "2026-10-23"]))   # 21st missing
    with pytest.raises(TopUpGap) as e:
        run(cached, f, utc(2026, 10, 24))
    assert e.value.missing == [date(2026, 10, 21)]
    assert errors and "AAA" in errors[0] and "1d" in errors[0] and "2026-10-21" in errors[0]


def test_a_missing_first_session_is_a_gap_too(errors):
    cached = daily_cached("2026-10-16")
    f = Fetcher(raw_daily(["2026-10-20", "2026-10-21", "2026-10-22", "2026-10-23"]))
    with pytest.raises(TopUpGap) as e:
        run(cached, f, utc(2026, 10, 24))
    assert e.value.missing == [date(2026, 10, 19)]


def test_already_present_session_is_not_reappended():
    cached = daily_cached("2026-10-19")
    f = Fetcher(raw_daily(["2026-10-19", "2026-10-20"]))                  # the vendor also answers a cached day
    merged, rep = run(cached, f, utc(2026, 10, 21, 14))
    assert merged["Date"].duplicated().sum() == 0 and rep["sessions"] == [date(2026, 10, 20)]
    # a fetcher that ignores the window and returns a cached day must not duplicate it
    class Greedy(Fetcher):
        def __call__(self, s, e):
            return self.frame
    merged, _ = run(cached, Greedy(raw_daily(["2026-10-16", "2026-10-19", "2026-10-20"])), utc(2026, 10, 21, 14))
    assert merged["Date"].duplicated().sum() == 0 and merged["Date"].is_monotonic_increasing


def test_a_session_outside_the_finished_range_refuses():
    cached = daily_cached("2026-10-16")
    f = Fetcher(raw_daily(["2026-10-19", "2026-10-20"]))

    class Forming(Fetcher):
        def __call__(self, s, e):
            return raw_daily(["2026-10-19", "2026-10-20", "2026-10-21"])         # 21st is forming/future
    with pytest.raises(live_topup.TopUpRefused, match="outside"):
        run(cached, Forming(None), utc(2026, 10, 21, 14))


def test_events_after_the_bar_divide_prices_and_multiply_volume():
    cached = daily_cached("2026-10-16", close=5.0)
    raw = raw_daily(["2026-10-19", "2026-10-20", "2026-10-21"], px=10.0)
    raw.loc[raw["Date"] >= "2026-10-21", ["Open", "High", "Low", "Close"]] = 5.0     # 2:1 split on the 21st (raw)
    merged, _ = run(cached, Fetcher(raw), utc(2026, 10, 22, 14), splits=[("2026-10-21", 2.0)])
    tail = merged.iloc[-3:]
    assert tail["Close"].tolist() == [5.0, 5.0, 5.0]
    assert tail["Volume"].tolist() == [2000, 2000, 1000]


def test_unexplained_2to1_seam_raises_pending_corporate_action():
    cached = daily_cached("2026-10-16", close=10.0)
    with pytest.raises(PendingCorporateAction):
        run(cached, Fetcher(raw_daily(["2026-10-19"], px=5.0)), utc(2026, 10, 20, 14))


def test_explained_split_seam_is_accepted_when_table_has_it():
    cached = daily_cached("2026-10-16", close=5.0)          # already re-based for the accepted event (file basis invariant)
    merged, _ = run(cached, Fetcher(raw_daily(["2026-10-19"], px=5.0)), utc(2026, 10, 20, 14),
                    splits=[("2026-10-19", 2.0)])
    assert len(merged) == 6                       # seam on the event date: the bar on the event date is not adjusted


def test_symbol_not_in_history():
    with pytest.raises(SymbolNotInHistory):
        run(daily_cached("2026-10-16").iloc[0:0], Fetcher(raw_daily([])), utc(2026, 10, 24))


@pytest.mark.parametrize("behind", [60, 250])
def test_a_long_gap_is_appended_contiguously_in_bounded_chunks(behind):
    last = regular_session_dates(date(2024, 1, 2), date(2026, 10, 23))[-(behind + 1)]
    cached = daily_cached(last.isoformat())
    exp = regular_session_dates(last, date(2026, 10, 23))[1:]
    assert len(exp) == behind
    f = Fetcher(raw_daily(exp))
    merged, rep = run(cached, f, utc(2026, 10, 24))
    assert rep["sessions"] == exp and rep["appended"] == behind
    assert merged["Date"].is_monotonic_increasing and not merged["Date"].duplicated().any()
    assert len(f.calls) == -(-behind // CHUNK_SESSIONS)                      # ceil(behind / 30) requests
    sizes = [len(regular_session_dates(a, b - timedelta(days=1))) for a, b in f.calls]
    assert max(sizes) <= CHUNK_SESSIONS and sum(sizes) == behind


def test_one_failing_chunk_appends_nothing():
    last = regular_session_dates(date(2025, 1, 2), date(2026, 10, 23))[-101]
    cached = daily_cached(last.isoformat())
    exp = regular_session_dates(last, date(2026, 10, 23))[1:]
    inner = Fetcher(raw_daily(exp))

    def flaky(start, end):
        if len(inner.calls) == 2:
            raise RuntimeError("HTTP 500 on chunk 3")
        return inner(start, end)
    with pytest.raises(RuntimeError, match="chunk 3"):
        run(cached, flaky, utc(2026, 10, 24))
    assert len(cached) == 5                                                  # the cached frame itself is untouched


def test_a_missing_session_in_a_late_chunk_appends_nothing(errors):
    last = regular_session_dates(date(2025, 1, 2), date(2026, 10, 23))[-101]
    cached = daily_cached(last.isoformat())
    exp = regular_session_dates(last, date(2026, 10, 23))[1:]
    with pytest.raises(TopUpGap):
        run(cached, Fetcher(raw_daily([d for d in exp if d != exp[75]])), utc(2026, 10, 24))
    assert errors


def test_a_split_inside_a_long_range_is_applied_like_a_short_one():
    last = regular_session_dates(date(2025, 1, 2), date(2026, 10, 23))[-101]
    cached = daily_cached(last.isoformat(), close=20.0)
    exp = regular_session_dates(last, date(2026, 10, 23))[1:]
    split_day = exp[60]
    raw = raw_daily(exp, px=40.0)
    raw.loc[pd.to_datetime(raw["Date"]) >= pd.Timestamp(split_day), ["Open", "High", "Low", "Close"]] /= 2     # raw price halves
    merged, _ = run(cached, Fetcher(raw), utc(2026, 10, 24), splits=[(split_day.isoformat(), 2.0)])
    new = merged.iloc[len(cached):]
    assert new["Close"].round(6).eq(20.0).all()                              # pre-event bars divided, post-event as traded: one basis
    with pytest.raises(PendingCorporateAction):                              # the same range with the split NOT accepted
        run(cached, Fetcher(raw), utc(2026, 10, 24))


def test_5min_appends_whole_sessions_regular_hours():
    days = ["2026-10-19", "2026-10-20"]
    base = raw_5min(["2026-10-16"])
    cached = base.assign(effective_date=base["Date"])
    f = Fetcher(raw_5min(days))
    merged, rep = run(cached, f, utc(2026, 10, 21, 14), interval="5min")
    assert rep["appended"] == 156 and merged["Date"].is_monotonic_increasing
    assert merged["Volume"].dtype == "float64"


def test_5min_thin_symbol_with_few_bars_per_session_is_accepted_but_a_missing_session_is_not(errors):
    base = raw_5min(["2026-10-16"])
    cached = base.assign(effective_date=base["Date"])
    thin = raw_5min(["2026-10-19", "2026-10-20"], per_day=3)                   # no trades in most slots
    merged, rep = run(cached, Fetcher(thin), utc(2026, 10, 21, 14), interval="5min")
    assert rep["appended"] == 6
    with pytest.raises(TopUpGap):
        run(cached, Fetcher(raw_5min(["2026-10-20"], per_day=3)), utc(2026, 10, 21, 14), interval="5min")
    assert any("5min" in m for m in errors)


def test_5min_half_day_needs_no_slots_after_the_early_close():
    base = raw_5min(["2026-11-24"])
    cached = base.assign(effective_date=base["Date"])
    f = Fetcher(pd.concat([raw_5min(["2026-11-25"]), raw_5min(["2026-11-27"], per_day=42)], ignore_index=True))   # 09:30..12:55
    merged, rep = run(cached, f, utc(2026, 11, 30, 15), interval="5min")
    assert rep["sessions"] == [date(2026, 11, 25), date(2026, 11, 27)]
