"""BT-vs-live parity of the SPY regime leaf (plan 2026-10-24 Task 11): ONE rule, TWO callers.

* BACKTEST publishes ``calendar.at(price_source.scan_cutoff_date(decision))``;
* LIVE publishes ``classify_benchmark_regime(knowable_closes(dates, closes, now))`` with ``now`` the
  real instant -- exactly the call ``TradeManager._publish_benchmark_regime`` makes (its own
  behaviour, caching and failure handling are tested in ``tests/test_trademanager_benchmark_regime.py``,
  which also drives the real method against the same calendar).

For the same closes and the same decision instant both must give the SAME regime and therefore the
same answer from every mode of both leaves: Friday close, the weekend, the 2026-11-26 holiday and the
2026-11-27 half day (close 13:00 New York).

The BT clock labels a decision with exchange-local wall time tagged UTC; live uses the true instant.
"""
from __future__ import annotations

from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo

import numpy as np
import pytest

from ba2_common.core import benchmark_regime as br
from ba2_common.core.market_calendar import regular_sessions_ending_at
from ba2_common.core.TradeConditions import SpyTrendRegimeCondition, SpyVolRegimeCondition

from app.services.backtest.price_source import AsOfPriceSource

NY = ZoneInfo("America/New_York")
FIELDS = [(SpyTrendRegimeCondition, br.TREND_FIELD), (SpyVolRegimeCondition, br.VOL_FIELD)]


def _series(n: int = 1100, last: date = date(2026, 12, 31)):
    dates = list(regular_sessions_ending_at(last, n))
    rng = np.random.default_rng(3)
    # vol clusters so the vol axis visits more than one state
    shock = rng.normal(0, 1, len(dates)) * np.where(np.sin(np.arange(len(dates)) / 40.0) > 0.4, 0.02, 0.006)
    closes = (400.0 * np.exp(np.cumsum(shock + 0.0003))).tolist()
    return dates, closes


DATES, CLOSES = _series()
CAL = br.BenchmarkRegimeCalendar(DATES, CLOSES)


def _cond(cls, code):
    return cls(account=None, instrument_name="AAPL", expert_recommendation=None,
               operator_str="==", value=float(code))


def _answers(regime):
    br.set_benchmark_regime(regime)
    out = {}
    for cls, field in FIELDS:
        for mode, code in br.CODES_BY_FIELD[field].items():
            c = _cond(cls, code)
            out[(field, mode)] = (c.evaluate(), c.last_status)
    return out


def bt_regime(wall: datetime, interval: str) -> br.BenchmarkRegime:
    ps = AsOfPriceSource(ohlcv_provider=None, interval=interval)
    return CAL.at(ps.scan_cutoff_date(wall))


def live_regime(now_utc: datetime) -> br.BenchmarkRegime:
    dates, closes = br.knowable_closes(DATES, CLOSES, now_utc)
    return br.classify_benchmark_regime(closes, session=dates[-1])


def wall(day: date, hh: int, mm: int) -> datetime:
    return datetime(day.year, day.month, day.day, hh, mm, tzinfo=timezone.utc)


def true_instant(day: date, hh: int, mm: int) -> datetime:
    return datetime.combine(day, time(hh, mm), tzinfo=NY).astimezone(timezone.utc)


@pytest.fixture(autouse=True)
def _clean():
    br.reset_benchmark_regime()
    yield
    br.reset_benchmark_regime()


@pytest.mark.parametrize("cls,field", FIELDS)
def test_every_mode_every_day_same_result_bt_and_live(cls, field):
    for i in range(210, len(DATES), 7):                       # stride keeps it fast; Task 2 covers every day
        br.set_benchmark_regime(CAL.at(DATES[i]))             # BT path
        bt = {m: _cond(cls, c).evaluate() for m, c in br.CODES_BY_FIELD[field].items()}
        br.set_benchmark_regime(br.classify_benchmark_regime(CLOSES[:i + 1]))   # live path
        live = {m: _cond(cls, c).evaluate() for m, c in br.CODES_BY_FIELD[field].items()}
        assert bt == live, (i, DATES[i])


def test_the_series_exercises_more_than_one_state_per_axis():
    # a parity test over a constant regime would prove nothing
    regimes = [CAL.at(d) for d in DATES[300::5]]
    assert len({r.trend for r in regimes}) >= 3
    assert len({r.vol for r in regimes}) >= 2


# (label, local date, hh, mm, newest session the decision may use)
INTRADAY_CASES = [
    ("fri open", date(2026, 10, 23), 9, 30, date(2026, 10, 22)),
    ("fri mid-session", date(2026, 10, 23), 12, 0, date(2026, 10, 22)),
    ("fri just before the close", date(2026, 10, 23), 15, 55, date(2026, 10, 22)),
    ("fri after the close", date(2026, 10, 23), 16, 5, date(2026, 10, 23)),
    ("saturday", date(2026, 10, 24), 12, 0, date(2026, 10, 23)),
    ("sunday", date(2026, 10, 25), 20, 0, date(2026, 10, 23)),
    ("monday open", date(2026, 10, 26), 9, 30, date(2026, 10, 23)),
    ("wed before thanksgiving, after close", date(2026, 11, 25), 16, 5, date(2026, 11, 25)),
    ("thanksgiving holiday", date(2026, 11, 26), 10, 0, date(2026, 11, 25)),
    ("thanksgiving holiday, evening", date(2026, 11, 26), 20, 0, date(2026, 11, 25)),
    ("half day, mid-session", date(2026, 11, 27), 12, 59, date(2026, 11, 25)),
    ("half day, after the 13:00 close", date(2026, 11, 27), 13, 5, date(2026, 11, 27)),
    ("half day, evening", date(2026, 11, 27), 17, 0, date(2026, 11, 27)),
    ("monday after the half day", date(2026, 11, 30), 9, 30, date(2026, 11, 27)),
]


@pytest.mark.parametrize("label,day,hh,mm,session", INTRADAY_CASES, ids=[c[0] for c in INTRADAY_CASES])
def test_intraday_decision_sees_the_same_close_as_a_live_run_at_that_instant(label, day, hh, mm, session):
    bt = bt_regime(wall(day, hh, mm), "5min")
    live = live_regime(true_instant(day, hh, mm))
    assert bt.session == session, (label, "backtest", bt.session)
    assert live.session == session, (label, "live", live.session)
    assert bt == live
    assert _answers(bt) == _answers(live)


def test_a_decision_never_reads_its_own_days_unfinished_close():
    # 09:30 on 2026-10-23: the 10-23 close does not exist yet, on either path
    day = date(2026, 10, 23)
    assert bt_regime(wall(day, 9, 30), "5min").session != day
    assert live_regime(true_instant(day, 9, 30)).session != day


@pytest.mark.parametrize("day", [date(2026, 10, 23), date(2026, 11, 25), date(2026, 11, 27)])
def test_daily_clock_decision_on_a_sessions_close_matches_live_after_that_close(day):
    """The daily clock decides on D's close (bar D); live reproduces it only after D finished."""
    bt = bt_regime(wall(day, 0, 0), "1d")
    live = live_regime(true_instant(day, 17, 0))
    assert bt.session == day and bt == live
    assert _answers(bt) == _answers(live)


def test_weekend_and_holiday_decisions_use_the_last_session_on_the_daily_clock_too():
    # a daily bar is never stamped on a weekend/holiday, but the lookup must still resolve to the last session
    for day, session in ((date(2026, 10, 24), date(2026, 10, 23)), (date(2026, 11, 26), date(2026, 11, 25))):
        assert bt_regime(wall(day, 0, 0), "1d").session == session
        assert live_regime(true_instant(day, 17, 0)).session == session
