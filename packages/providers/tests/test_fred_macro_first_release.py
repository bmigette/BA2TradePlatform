"""Macro signal inputs are visible only once FRED had first published them -- BT == live.

The bug (code review 2026-09-26): the daily FRED series DeterministicScorer reads (VIXCLS,
BAA10Y, T10Y3M) were cut on their OBSERVATION date against a backtest bar's midnight-UTC
``as_of``, so bar d saw rows dated d. Bar d is the live decision made at ~09:30 ET on the next
session N(d) (it fills at N(d)'s open), and live at that moment had less: the real fetch at
13:32Z on 2026-09-25 ended VIXCLS 09-22, BAA10Y 09-23, T10Y3M 09-24 -- the backtest's bar 09-24
saw 09-24 on all three.

The fix is one rule in one function, used by both paths: a row is visible to a decision
labelled L iff its FIRST-RELEASE date on FRED is strictly before L (L = the New York date live,
N(d) for a daily bar d). These tests pin: no lookahead, monthly vintage behaviour, the BT/live
parity of that one function, the refusals, and the ALFRED assembly of first-release dates.

No network: every payload is synthetic (or a fake FRED endpoint).
"""
import json
import os
from datetime import date, datetime, timedelta, timezone

import pandas as pd
import pytest

from ba2_common.core.market_calendar import NotARegularSession, next_regular_session
from ba2_providers.macro import fred_series as fs

FETCHED_2026_09_25 = "2026-09-25T13:32:46+00:00"


@pytest.fixture(autouse=True)
def _isolated_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(fs, "CACHE_FOLDER", str(tmp_path))
    fs.reset_cache()
    yield
    fs.reset_cache()


def _write(series_id, rows, *, first_vintage="2010-11-22", fetched_at=None, fmt=True):
    os.makedirs(os.path.join(fs.CACHE_FOLDER, "fred"), exist_ok=True)
    doc = {"series_id": series_id,
           "fetched_at": fetched_at or datetime.now(timezone.utc).isoformat(),
           "observations": [{"date": d, "value": v, "realtime_start": rt} for d, v, rt in rows]}
    if fmt:
        doc.update({"availability": fs.AVAIL_FIRST_RELEASE,
                    "format": fs.CACHE_FORMAT_FIRST_RELEASE, "first_vintage": first_vintage})
    with open(fs.cache_path(series_id), "w", encoding="utf-8") as fh:
        json.dump(doc, fh)


def _bar(d: str) -> datetime:
    """A daily backtest bar as the engine stamps it: midnight UTC."""
    y, m, dd = map(int, d.split("-"))
    return datetime(y, m, dd, tzinfo=timezone.utc)


def _live_at(monkeypatch, instant: datetime):
    """Make ``as_of=None`` the live decision at *instant*."""
    monkeypatch.setattr(fs, "_live_decision_instant", lambda: instant)


def _open_utc(session: date) -> datetime:
    """~09:30 ET on *session*, the live DS schedule (13:30Z in summer)."""
    return datetime(session.year, session.month, session.day, 13, 30, tzinfo=timezone.utc)


def _last_date(series):
    return str(series.index[-1].date())


# --------------------------------------------------------------------------- #
# The decision label
# --------------------------------------------------------------------------- #
class TestTheDecisionLabel:
    def test_a_daily_bar_is_the_decision_of_the_next_session(self):
        assert fs.decision_label(_bar("2026-09-24")) == date(2026, 9, 25)
        assert fs.decision_label("2026-09-24") == date(2026, 9, 25)
        assert fs.decision_label(date(2026, 9, 24)) == date(2026, 9, 25)

    def test_a_friday_bar_is_mondays_decision(self):
        assert fs.decision_label(_bar("2026-09-25")) == date(2026, 9, 28)

    def test_a_bar_before_a_holiday_skips_it(self):
        # 2024-07-04 is a NYSE holiday: bar 07-03 is the decision of 07-05.
        assert fs.decision_label(_bar("2024-07-03")) == date(2024, 7, 5)

    def test_a_non_session_stamp_is_the_next_sessions_decision(self):
        """Same rule as a session bar (data through that day, next open) -- not a refusal:
        a vendor bar on a closure must not fail a whole run over the macro overlay."""
        assert fs.decision_label(_bar("2026-09-26")) == date(2026, 9, 28)   # a Saturday
        assert fs.decision_label(_bar("2024-07-04")) == date(2024, 7, 5)    # a holiday

    def test_the_label_agrees_with_the_platform_bt_label_on_every_session(self):
        from ba2_common.core.market_calendar import (backtest_decision_label,
                                                     regular_session_dates)
        for d in regular_session_dates(date(2020, 1, 1), date(2025, 12, 31)):
            assert fs.decision_label(_bar(d.isoformat())) == backtest_decision_label(d), d

    def test_an_intraday_instant_is_its_new_york_date(self):
        assert fs.decision_label(datetime(2026, 9, 25, 13, 30, tzinfo=timezone.utc)) == \
            date(2026, 9, 25)
        # 01:00 UTC on 09-26 is 21:00 ET on 09-25.
        assert fs.decision_label(datetime(2026, 9, 26, 1, 0, tzinfo=timezone.utc)) == \
            date(2026, 9, 25)

    def test_live_uses_the_live_decision_instant(self, monkeypatch):
        _live_at(monkeypatch, datetime(2026, 9, 25, 13, 30, tzinfo=timezone.utc))
        assert fs.decision_label(None) == date(2026, 9, 25)


# --------------------------------------------------------------------------- #
# No lookahead
# --------------------------------------------------------------------------- #
class TestNoLookahead:
    def test_a_print_first_published_on_d_plus_1_is_invisible_on_bar_d(self):
        """BAA10Y obs d appears in the vintage of d+1. Bar d (= decision of d+1) must not see
        it: that vintage is released DURING d+1, after the 09:30 ET decision."""
        _write("BAA10Y", [("2026-09-22", "1.42", "2026-09-23"),
                          ("2026-09-23", "1.39", "2026-09-24"),
                          ("2026-09-24", "1.39", "2026-09-25")], first_vintage="2014-01-27",
               fetched_at="2026-09-28T15:00:00+00:00")
        assert _last_date(fs.get_series_as_of("BAA10Y", _bar("2026-09-23"))) == "2026-09-22"
        assert _last_date(fs.get_series_as_of("BAA10Y", _bar("2026-09-24"))) == "2026-09-23"
        # The Friday bar is Monday's decision: the 09-25 vintage (obs 09-24) is visible then.
        assert _last_date(fs.get_series_as_of("BAA10Y", _bar("2026-09-25"))) == "2026-09-24"

    def test_a_print_published_the_evening_of_d_is_visible_to_bar_d(self):
        """T10Y3M obs d is in vintage d (Treasury's par yields, the evening of d); the
        decision of N(d) at 09:30 ET had it."""
        _write("T10Y3M", [("2026-09-23", "0.92", "2026-09-23"),
                          ("2026-09-24", "0.94", "2026-09-24")], first_vintage="2014-01-27")
        assert _last_date(fs.get_series_as_of("T10Y3M", _bar("2026-09-24"))) == "2026-09-24"

    def test_a_stalled_series_shows_only_what_was_published(self):
        """VIXCLS stalled after its 09-22 vintage: obs 09-23/09-24 published LATE (say 09-28)
        stay invisible to every decision before that, however old the observation is."""
        _write("VIXCLS", [("2026-09-22", "14.21", "2026-09-22"),
                          ("2026-09-23", "15.00", "2026-09-28"),
                          ("2026-09-24", "15.10", "2026-09-28")],
               fetched_at="2026-09-29T12:00:00+00:00")
        assert _last_date(fs.get_series_as_of("VIXCLS", _bar("2026-09-24"))) == "2026-09-22"
        assert _last_date(fs.get_series_as_of("VIXCLS", _bar("2026-09-25"))) == "2026-09-22"
        assert _last_date(fs.get_series_as_of("VIXCLS", _bar("2026-09-28"))) == "2026-09-24"

    def test_the_measured_2026_09_25_live_fetch_is_exactly_what_bar_09_24_sees(self, monkeypatch):
        """The confirming evidence, as a regression: live at 09:30 ET on 2026-09-25 held VIXCLS
        through 09-22, BAA10Y through 09-23, T10Y3M through 09-24 -- and so must bar 09-24."""
        _write("VIXCLS", [("2026-09-21", "14.87", "2026-09-21"),
                          ("2026-09-22", "14.21", "2026-09-22")], fetched_at=FETCHED_2026_09_25)
        _write("BAA10Y", [("2026-09-22", "1.42", "2026-09-23"),
                          ("2026-09-23", "1.39", "2026-09-24"),
                          ("2026-09-24", "1.39", "2026-09-25")],
               first_vintage="2014-01-27", fetched_at="2026-09-26T16:00:00+00:00")
        _write("T10Y3M", [("2026-09-23", "0.92", "2026-09-23"),
                          ("2026-09-24", "0.94", "2026-09-24"),
                          ("2026-09-25", "0.93", "2026-09-25")],
               first_vintage="2014-01-27", fetched_at="2026-09-26T16:00:00+00:00")
        want = {"VIXCLS": "2026-09-22", "BAA10Y": "2026-09-23", "T10Y3M": "2026-09-24"}
        _live_at(monkeypatch, datetime(2026, 9, 25, 13, 30, tzinfo=timezone.utc))
        for sid, last in want.items():
            bt = fs.get_series_as_of(sid, _bar("2026-09-24"))
            live = fs.get_series_as_of(sid, None)
            assert _last_date(bt) == last, sid
            pd.testing.assert_series_equal(bt, live)


# --------------------------------------------------------------------------- #
# Monthly series: the real release calendar (ALFRED first release), not a guessed lag
# --------------------------------------------------------------------------- #
class TestMonthlyVintages:
    ROWS = [("2023-11-01", "3.7", "2023-12-08"),
            ("2023-12-01", "3.7", "2024-01-05"),
            ("2024-01-01", "3.7", "2024-02-02")]     # Employment Situation, Fri 08:30 ET

    def test_a_release_the_morning_of_L_is_invisible_on_L_in_both_paths(self, monkeypatch):
        _write("UNRATE", self.ROWS, first_vintage="1960-03-15")
        # Bar Thu 02-01 == live Fri 02-02 09:30 ET: the 08:30 ET release is dated L -> hidden.
        _live_at(monkeypatch, _open_utc(date(2024, 2, 2)))
        bt = fs.get_series_as_of("UNRATE", _bar("2024-02-01"))
        assert _last_date(bt) == "2023-12-01"
        pd.testing.assert_series_equal(bt, fs.get_series_as_of("UNRATE", None))

    def test_it_is_visible_from_the_next_decision_on(self, monkeypatch):
        _write("UNRATE", self.ROWS, first_vintage="1960-03-15")
        # Bar Fri 02-02 == live Mon 02-05.
        _live_at(monkeypatch, _open_utc(date(2024, 2, 5)))
        bt = fs.get_series_as_of("UNRATE", _bar("2024-02-02"))
        assert _last_date(bt) == "2024-01-01"
        pd.testing.assert_series_equal(bt, fs.get_series_as_of("UNRATE", None))

    def test_the_observation_month_never_decides_visibility(self):
        """January is dated 2024-01-01 -- weeks before anyone could know it."""
        _write("UNRATE", self.ROWS, first_vintage="1960-03-15")
        # December was published 2024-01-05, so bar 01-02 (decision of 01-03) still ends at
        # November; nothing in January sees January.
        for bar, last in (("2024-01-02", "2023-11-01"), ("2024-01-17", "2023-12-01"),
                          ("2024-01-31", "2023-12-01")):
            assert _last_date(fs.get_series_as_of("UNRATE", _bar(bar))) == last, bar


# --------------------------------------------------------------------------- #
# BT/live parity: the same function, the same answer, for the same decision
# --------------------------------------------------------------------------- #
def test_every_daily_bar_equals_the_live_decision_it_stands_for(monkeypatch):
    """For every session d of a quarter, with a mix of same-day, next-day and late releases:
    ``get_series_as_of(sid, bar d)`` == ``get_series_as_of(sid, None)`` at 09:30 ET on N(d)."""
    days = pd.bdate_range("2024-01-02", "2024-03-28")
    rows = []
    for i, d in enumerate(days):
        lag = (0, 1, 1, 3)[i % 4]                    # same day, next day, weekend-ish, late
        rt = (d + pd.Timedelta(days=lag)).date().isoformat()
        rows.append((d.date().isoformat(), f"{10 + i * 0.1:.2f}", rt))
    _write("BAA10Y", rows, first_vintage="2014-01-27")
    checked = 0
    for d in days:
        bar = d.date()
        try:
            n_d = next_regular_session(bar)
            fs.decision_label(_bar(bar.isoformat()))
        except NotARegularSession:
            continue                                 # bdate_range includes holidays
        _live_at(monkeypatch, _open_utc(n_d))
        bt = fs.get_series_as_of("BAA10Y", _bar(bar.isoformat()))
        live = fs.get_series_as_of("BAA10Y", None)
        recorded_live = fs.get_series_as_of("BAA10Y", _open_utc(n_d))
        pd.testing.assert_series_equal(bt, live)
        pd.testing.assert_series_equal(bt, recorded_live)
        visible = set(bt.index.strftime("%Y-%m-%d"))
        for obs, _value, rt in rows:
            assert (obs in visible) == (date.fromisoformat(rt) < n_d), (bar, obs, rt)
        checked += 1
    assert checked > 50


# --------------------------------------------------------------------------- #
# Refusals: availability that cannot be established is refused, never guessed
# --------------------------------------------------------------------------- #
class TestRefusals:
    def test_an_old_observation_date_format_file_is_refused(self):
        """The pre-fix format: every row's realtime_start is the FETCH vintage. Reading it as a
        first-release date would hide all history; reading the observation date would leak."""
        _write("VIXCLS", [("2024-03-14", "14.4", "2026-09-23")], fmt=False)
        with pytest.raises(fs.MacroAvailabilityUnknown, match="first-release format"):
            fs.get_series_as_of("VIXCLS", _bar("2024-03-15"))

    def test_a_decision_on_or_before_the_first_vintage_is_refused(self):
        """Rows in the first vintage are stamped with it: public NO LATER than that, maybe
        earlier. Before it, nothing is provably public -- not an empty series, a refusal."""
        _write("BAA10Y", [("2014-01-24", "2.35", "2014-01-27")], first_vintage="2014-01-27")
        with pytest.raises(fs.MacroAvailabilityUnknown, match="first recorded vintage"):
            fs.get_series_as_of("BAA10Y", _bar("2014-01-24"))    # label 01-27
        assert len(fs.get_series_as_of("BAA10Y", _bar("2014-01-27"))) == 1

    def test_a_decision_after_the_fetch_day_is_refused(self):
        """Vintages published after the fetch are unknown: a backtest must not run on fewer
        rows than live had."""
        _write("T10Y3M", [("2026-09-24", "0.94", "2026-09-24")],
               first_vintage="2014-01-27", fetched_at=FETCHED_2026_09_25)
        assert len(fs.get_series_as_of("T10Y3M", _bar("2026-09-24"))) == 1   # label 09-25
        with pytest.raises(fs.MacroAvailabilityUnknown, match="before the decision label"):
            fs.get_series_as_of("T10Y3M", _bar("2026-09-25"))              # label 09-28

    def test_a_fetch_time_without_a_timezone_is_refused(self):
        _write("T10Y3M", [("2026-09-24", "0.94", "2026-09-24")],
               first_vintage="2014-01-27", fetched_at="2026-09-25T13:32:46")
        with pytest.raises(fs.MacroAvailabilityUnknown, match="fetched on None"):
            fs.get_series_as_of("T10Y3M", _bar("2026-09-24"))

    def test_a_row_without_a_first_release_date_is_refused(self):
        _write("VIXCLS", [("2024-03-14", "14.4", None)])
        with pytest.raises(fs.MacroAvailabilityUnknown, match="first-release date"):
            fs.get_series_as_of("VIXCLS", _bar("2024-03-15"))

    def test_a_memo_seeded_without_its_header_is_refused(self):
        fs._MEM["VIXCLS"] = [{"date": "2024-03-14", "value": "14.4",
                              "realtime_start": "2024-03-14"}]
        with pytest.raises(fs.MacroAvailabilityUnknown, match="first-release format"):
            fs.get_series_as_of("VIXCLS", _bar("2024-03-15"))

    def test_the_refusal_is_not_an_oserror(self):
        """``absorb_if_benign`` swallows the OSError family; this must propagate."""
        assert not issubclass(fs.MacroAvailabilityUnknown, OSError)


# --------------------------------------------------------------------------- #
# Assembling first-release dates from ALFRED (fake FRED endpoint)
# --------------------------------------------------------------------------- #
class _FakeFred:
    """ALFRED semantics as measured 2026-09-26: ``output_type=4`` reports a row only in the
    window holding its first release and never a row of the very first vintage; a plain
    real-time window returns each row's value periods clamped to the window."""

    def __init__(self, vintages, history):
        # history: {obs date: [(vintage it took this value, value), ...]} ascending
        self.vintages = vintages
        self.history = history
        self.calls = []

    def _periods(self, d, rt_start, rt_end):
        out = []
        changes = self.history.get(d, [])
        for i, (v_from, value) in enumerate(changes):
            v_to = (self.vintages[self.vintages.index(changes[i + 1][0]) - 1]
                    if i + 1 < len(changes) else "9999-12-31")
            lo, hi = max(v_from, rt_start), min(v_to, rt_end)
            if lo <= hi:
                out.append({"date": d, "value": value, "realtime_start": lo, "realtime_end": hi})
        return out

    def __call__(self, url, params, sid):
        self.calls.append(dict(params))
        if url == fs.API_VINTAGEDATES_URL:
            off = params["offset"]
            return {"count": len(self.vintages), "vintage_dates": self.vintages[off:off + 2]}
        rs, re_ = params.get("realtime_start"), params.get("realtime_end")
        if params.get("output_type") == 4:
            rows = []
            for d, changes in self.history.items():
                v_first = changes[0][0]
                if v_first != self.vintages[0] and rs <= v_first <= re_:
                    rows.append({"date": d, "value": changes[0][1], "realtime_start": v_first,
                                 "realtime_end": re_})
            return {"observations": rows}
        if rs is None:                                   # the current vintage
            return {"observations": [{"date": d, "value": ch[-1][1]}
                                     for d, ch in self.history.items()]}
        dates = [params["observation_start"]] if "observation_start" in params else self.history
        return {"observations": [p for d in dates for p in self._periods(d, rs, re_)]}


def test_first_release_dates_are_assembled_from_alfred(monkeypatch):
    vintages = ["2020-01-06", "2020-01-07", "2020-01-08", "2020-01-09", "2020-01-10"]
    history = {
        "2019-12-31": [("2020-01-06", "1.0")],                  # in the first vintage
        "2020-01-06": [("2020-01-07", "2.0")],                  # next-day release
        "2020-01-07": [("2020-01-07", "3.0"), ("2020-01-09", "3.5")],   # revised later
        "2020-01-08": [("2020-01-09", "."), ("2020-01-10", "4.0")],     # "." then filled
    }
    fake = _FakeFred(vintages, history)
    monkeypatch.setattr(fs, "_fred_get", fake)
    monkeypatch.setattr(fs, "_VINTAGE_WINDOW", 2)               # force several windows

    rows, header = fs._fetch_first_release("VIXCLS", "KEY")
    got = {r["date"]: (r["realtime_start"], r["value"]) for r in rows}
    assert got == {
        "2019-12-31": ("2020-01-06", "1.0"),
        "2020-01-06": ("2020-01-07", "2.0"),
        "2020-01-07": ("2020-01-07", "3.0"),     # the FIRST-RELEASE value, not the revision
        "2020-01-08": ("2020-01-10", "4.0"),     # first published as a number on 01-10
    }
    assert header["first_vintage"] == "2020-01-06"
    assert header["last_vintage"] == "2020-01-10"
    assert header["late_filled"] == ["2020-01-08"]
    windows = [(c["realtime_start"], c["realtime_end"]) for c in fake.calls
               if c.get("output_type") == 4]
    assert windows == [("2020-01-06", "2020-01-07"), ("2020-01-08", "2020-01-09"),
                       ("2020-01-10", "9999-12-31")]


def test_a_row_no_vintage_can_date_refuses_the_whole_fetch(monkeypatch):
    fake = _FakeFred(["2020-01-06", "2020-01-07"], {"2020-01-06": [("2020-01-07", "2.0")]})
    real_call = fake.__call__

    def lying(url, params, sid):
        out = real_call(url, params, sid)
        if params.get("realtime_start") is None and url == fs.API_URL:
            out["observations"].append({"date": "2020-01-03", "value": "9.9"})  # undated
        return out

    monkeypatch.setattr(fs, "_fred_get", lying)
    with pytest.raises(RuntimeError, match="availability is unknown"):
        fs._fetch_first_release("VIXCLS", "KEY")


def test_refresh_writes_the_first_release_header_and_leaves_dgs3mo_alone(monkeypatch):
    fake = _FakeFred(["2020-01-06", "2020-01-07"], {"2020-01-06": [("2020-01-07", "2.0")]})
    monkeypatch.setattr(fs, "_fred_get", fake)
    fs.refresh_series("VIXCLS", "KEY")
    doc = json.load(open(fs.cache_path("VIXCLS"), encoding="utf-8"))
    assert doc["format"] == fs.CACHE_FORMAT_FIRST_RELEASE
    assert doc["first_vintage"] == "2020-01-06"
    assert list(doc)[-1] == "observations", "the header precedes the rows (the planner reads it)"

    fs.refresh_series("DGS3MO", "KEY")
    doc = json.load(open(fs.cache_path("DGS3MO"), encoding="utf-8"))
    assert set(doc) == {"series_id", "fetched_at", "vintage", "observations"}, (
        "the option BS-rate file must keep exactly its old header")
    assert all(c.get("output_type") is None for c in fake.calls[-1:])


def test_a_refresh_reuses_the_rows_it_already_walked(monkeypatch):
    fake = _FakeFred(["2020-01-06", "2020-01-07", "2020-01-08"],
                     {"2020-01-06": [("2020-01-07", "."), ("2020-01-08", "2.0")]})
    monkeypatch.setattr(fs, "_fred_get", fake)
    fs.refresh_series("VIXCLS", "KEY")
    walks = [c for c in fake.calls if "observation_start" in c]
    assert walks, "the late-filled row was walked the first time"
    fake.calls.clear()
    fs.refresh_series("VIXCLS", "KEY")
    assert not [c for c in fake.calls if "observation_start" in c], "walked again"
    doc = json.load(open(fs.cache_path("VIXCLS"), encoding="utf-8"))
    assert doc["observations"][0]["realtime_start"] == "2020-01-08"
