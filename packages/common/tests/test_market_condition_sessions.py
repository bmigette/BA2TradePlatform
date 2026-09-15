"""prior_session_v1 calendar arithmetic (design section 4): ``prior_regular_session`` and
``regular_sessions_ending_at`` over the NYSE calendar."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

import pytest

from ba2_common.core.market_calendar import (
    NY_TZ,
    nyse_regular_sessions,
    prior_regular_session,
    regular_session_close_utc,
    regular_sessions_ending_at,
)


def _ny(d: date, hh: int, mm: int) -> datetime:
    return datetime.combine(d, time(hh, mm), tzinfo=NY_TZ)


@pytest.mark.parametrize("hh,mm", [(9, 30), (15, 45), (0, 1), (23, 59)])
def test_monday_decisions_read_friday(hh, mm):
    # 2025-06-09 is a Monday; 2025-06-06 the Friday before.
    assert prior_regular_session(_ny(date(2025, 6, 9), hh, mm)) == date(2025, 6, 6)


def test_after_the_close_still_reads_the_prior_session():
    # The policy is stable for the local date, including after the close.
    assert prior_regular_session(_ny(date(2025, 6, 10), 23, 59)) == date(2025, 6, 9)
    assert prior_regular_session(_ny(date(2025, 6, 10), 16, 30)) == date(2025, 6, 9)


def test_independence_day_weekend():
    # 2025-07-04 (Fri) holiday: Monday 07-07 reads Thursday 07-03.
    assert prior_regular_session(_ny(date(2025, 7, 7), 9, 30)) == date(2025, 7, 3)
    assert prior_regular_session(date(2025, 7, 7)) == date(2025, 7, 3)


def test_presidents_day():
    # 2025-02-17 (Mon) holiday: Tuesday 02-18 reads Friday 02-14.
    assert prior_regular_session(_ny(date(2025, 2, 18), 10, 0)) == date(2025, 2, 14)
    assert prior_regular_session(date(2025, 2, 18)) == date(2025, 2, 14)


def test_half_day_is_a_session():
    # 2025-11-28 closes at 13:00 ET -- still a regular session.
    assert prior_regular_session(date(2025, 12, 1)) == date(2025, 11, 28)
    assert regular_sessions_ending_at(date(2025, 11, 28), 1) == [date(2025, 11, 28)]
    assert regular_session_close_utc(date(2025, 11, 28)) == datetime(2025, 11, 28, 18, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize("monday,friday", [
    (date(2025, 3, 10), date(2025, 3, 7)),    # spring-forward Sunday 03-09
    (date(2025, 11, 3), date(2025, 10, 31)),  # fall-back Sunday 11-02
])
def test_dst_transition_weeks(monday, friday):
    assert prior_regular_session(_ny(monday, 9, 30)) == friday
    assert prior_regular_session(_ny(monday, 15, 45)) == friday
    assert prior_regular_session(monday) == friday


def test_close_utc_follows_dst():
    assert regular_session_close_utc(date(2025, 3, 7)) == datetime(2025, 3, 7, 21, 0, tzinfo=timezone.utc)
    assert regular_session_close_utc(date(2025, 3, 10)) == datetime(2025, 3, 10, 20, 0, tzinfo=timezone.utc)
    with pytest.raises(ValueError):
        regular_session_close_utc(date(2025, 3, 8))


def test_utc_input_is_converted_to_new_york_first():
    # 2025-06-10 02:00 UTC is still Monday 2025-06-09 22:00 in New York -> Friday.
    assert prior_regular_session(datetime(2025, 6, 10, 2, 0, tzinfo=timezone.utc)) == date(2025, 6, 6)


def test_naive_datetime_raises():
    with pytest.raises(ValueError, match="timezone-aware"):
        prior_regular_session(datetime(2025, 6, 9, 9, 30))


def test_daily_session_label():
    assert prior_regular_session(date(2025, 3, 10)) == date(2025, 3, 7)


def test_bt_label_and_intraday_decision_resolve_the_same_cutoff():
    label = date(2025, 3, 10)
    assert prior_regular_session(label) == prior_regular_session(_ny(label, 10, 0))


def test_sessions_ending_at_128():
    got = regular_sessions_ending_at(date(2025, 6, 30), 128)
    assert len(got) == 128 and len(set(got)) == 128
    assert got == sorted(got)
    assert got[-1] == date(2025, 6, 30)
    expected = [o.astimezone(NY_TZ).date() for o, _ in nyse_regular_sessions(got[0], got[-1])]
    assert got == expected


def test_sessions_ending_at_returns_a_fresh_list():
    a = regular_sessions_ending_at(date(2025, 6, 30), 5)
    a.pop()
    assert len(regular_sessions_ending_at(date(2025, 6, 30), 5)) == 5


def test_sessions_ending_at_a_non_session_raises():
    with pytest.raises(ValueError, match="not a regular"):
        regular_sessions_ending_at(date(2025, 7, 4), 10)
    with pytest.raises(ValueError, match="not a regular"):
        regular_sessions_ending_at(date(2025, 6, 28), 10)  # Saturday


def test_sessions_ending_at_large_n_extends_the_lookback():
    got = regular_sessions_ending_at(date(2025, 6, 30), 600)
    assert len(got) == 600 and got[-1] == date(2025, 6, 30)
    assert all(b > a for a, b in zip(got, got[1:]))
    assert (got[-1] - got[0]) > timedelta(days=600)


def test_sessions_ending_at_rejects_bad_input():
    with pytest.raises(ValueError):
        regular_sessions_ending_at(date(2025, 6, 30), 0)
    with pytest.raises(TypeError):
        regular_sessions_ending_at(datetime(2025, 6, 30, tzinfo=timezone.utc), 5)
