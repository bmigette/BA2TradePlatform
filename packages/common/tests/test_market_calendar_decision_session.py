"""The single BT/live decision-session rule (BT/live option parity plan, step A1).

data_session = prior_regular_session(decision_label), with
live label = NY date of the instant, BT label = next_regular_session(bar_date).
"""
from datetime import date, datetime, time, timedelta, timezone

import pytest

from ba2_common.core import market_calendar
from ba2_common.core.market_calendar import (
    NY_TZ,
    backtest_decision_label,
    decision_data_session,
    live_decision_label,
    next_regular_session,
    prior_regular_session,
    regular_session_dates,
)

SESSIONS = regular_session_dates(date(2024, 1, 1), date(2025, 12, 31))


def _ny(day: date, hh: int, mm: int) -> datetime:
    return datetime.combine(day, time(hh, mm), tzinfo=NY_TZ)


def test_backtest_label_half_day_friday_to_monday():
    assert backtest_decision_label(date(2025, 11, 28)) == date(2025, 12, 1)


def test_backtest_label_christmas_eve_skips_holiday():
    assert backtest_decision_label(date(2025, 12, 24)) == date(2025, 12, 26)


def test_backtest_label_refuses_non_session_bar():
    # A BT bar on a Saturday is a data bug, not a Monday decision.
    with pytest.raises(ValueError):
        backtest_decision_label(date(2025, 6, 7))
    with pytest.raises(ValueError):
        backtest_decision_label(date(2025, 12, 25))


def test_next_regular_session_is_strictly_after():
    assert next_regular_session(date(2025, 6, 9)) == date(2025, 6, 10)


def test_backtest_bar_reads_its_own_session():
    assert len(SESSIONS) > 480
    for d in SESSIONS:
        assert decision_data_session(backtest_decision_label(d)) == d


@pytest.mark.parametrize("hh,mm", [(9, 35), (15, 55)])
def test_live_reads_prior_session(hh, mm):
    for s in SESSIONS:
        assert decision_data_session(live_decision_label(_ny(s, hh, mm))) == prior_regular_session(s)


def test_bt_live_equivalence():
    for s in SESSIONS[1:]:
        bt = decision_data_session(backtest_decision_label(prior_regular_session(s)))
        live = decision_data_session(live_decision_label(_ny(s, 9, 35)))
        assert bt == live


def test_live_saturday_reads_friday():
    assert decision_data_session(live_decision_label(_ny(date(2025, 6, 7), 12, 0))) == date(2025, 6, 6)


def test_live_label_refuses_naive_datetime():
    with pytest.raises(ValueError):
        live_decision_label(datetime(2025, 6, 9, 9, 35))


def test_live_label_refuses_plain_date():
    with pytest.raises(TypeError):
        live_decision_label(date(2025, 6, 9))


def test_next_regular_session_refuses_datetime():
    with pytest.raises(TypeError):
        next_regular_session(_ny(date(2025, 6, 9), 9, 35))


def test_backtest_label_refuses_datetime():
    with pytest.raises(TypeError):
        backtest_decision_label(_ny(date(2025, 6, 9), 9, 35))


def test_live_label_is_the_new_york_date_not_utc():
    # 02:00Z on the 10th is 22:00 ET on Monday the 9th.
    moment = datetime(2025, 6, 10, 2, 0, tzinfo=timezone.utc)
    assert live_decision_label(moment) == date(2025, 6, 9)
    assert decision_data_session(live_decision_label(moment)) == date(2025, 6, 6)


def test_prior_session_v1_is_stable_after_the_close():
    # A decision after session D's close still reads prior(D), never D itself.
    d = date(2025, 6, 9)
    assert decision_data_session(live_decision_label(_ny(d, 16, 30))) == date(2025, 6, 6)


def test_decision_data_session_refuses_an_instant():
    with pytest.raises(TypeError):
        decision_data_session(_ny(date(2025, 6, 9), 9, 35))


def test_next_regular_session_grows_the_table_forward():
    market_calendar._clear_session_table()
    try:
        far = date.today().replace(year=date.today().year + 20)
        nxt = next_regular_session(far)
        assert far < nxt <= far + timedelta(days=5)
        assert nxt.weekday() < 5
    finally:
        market_calendar._clear_session_table()


def test_next_regular_session_grows_the_table_backward():
    market_calendar._clear_session_table()
    try:
        # Fri 1985-06-07 -> Mon 1985-06-10, before the default 1990 table start.
        assert next_regular_session(date(1985, 6, 7)) == date(1985, 6, 10)
    finally:
        market_calendar._clear_session_table()


def test_next_regular_session_refuses_a_day_before_the_table():
    with pytest.raises(ValueError):
        next_regular_session(date(1800, 1, 1))


# --------------------------------------------------------------------------- is_regular_session
@pytest.mark.parametrize("day,expected", [
    (date(2025, 6, 3), True),      # an ordinary Tuesday
    (date(2025, 11, 28), True),    # half day (day after Thanksgiving) is a session
    (date(2025, 12, 24), True),    # half day
    (date(2025, 6, 7), False),     # Saturday
    (date(2025, 6, 8), False),     # Sunday
    (date(2025, 7, 4), False),     # Independence Day
    (date(2025, 12, 25), False),   # Christmas
])
def test_is_regular_session(day, expected):
    assert market_calendar.is_regular_session(day) is expected


def test_is_regular_session_agrees_with_the_session_list_over_two_years():
    sessions = set(SESSIONS)
    day = date(2024, 1, 1)
    while day <= date(2025, 12, 31):
        assert market_calendar.is_regular_session(day) is (day in sessions), day
        day += timedelta(days=1)


def test_is_regular_session_refuses_a_datetime():
    with pytest.raises(TypeError):
        market_calendar.is_regular_session(_ny(date(2025, 6, 3), 10, 0))


def test_is_regular_session_does_not_call_schedule_once_the_table_is_built(monkeypatch):
    market_calendar.is_regular_session(date(2025, 6, 3))          # table spans 2024-2025 now
    calls = []
    cal = market_calendar._nyse_calendar()
    real = cal.schedule
    monkeypatch.setattr(cal, "schedule", lambda *a, **k: calls.append(a) or real(*a, **k))
    day = date(2024, 1, 1)
    while day <= date(2025, 12, 31):
        market_calendar.is_regular_session(day)
        day += timedelta(days=1)
    assert calls == []
