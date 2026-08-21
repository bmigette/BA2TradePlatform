"""ba2_common.core.market_calendar: the offline NYSE regular-session fallback.

EVERY test here pins an explicit instant. A market-hours test that reads the wall
clock passes at 11:00 and fails at 17:00, and the failure looks like a code bug.

The dates are real 2026 NYSE facts, verified against pandas_market_calendars
5.3.0 offline: 2026-08-19 is an ordinary Wednesday (09:30-16:00 EDT); 2026-07-03
is the OBSERVED Independence Day holiday (July 4 falls on a Saturday); 2026-01-19
is MLK Day; 2026-11-26 is Thanksgiving and 2026-11-27 is the half day after it,
closing at 13:00 EST.
"""
from datetime import datetime, timezone

import pytest

from ba2_common.core.account_types import MARKET_HOURS_SOURCE_FALLBACK
from ba2_common.core.market_calendar import (
    NY_TZ,
    MarketCalendarUnavailable,
    clear_nyse_calendar_cache,
    nyse_market_hours,
    nyse_regular_sessions,
)


def _ny(year, month, day, hour=0, minute=0, second=0) -> datetime:
    """A frozen instant in exchange-local time."""
    return datetime(year, month, day, hour, minute, second, tzinfo=NY_TZ)


# ---------------------------------------------------------------------------
# Open / closed
# ---------------------------------------------------------------------------

def test_weekday_mid_session_is_open():
    hours = nyse_market_hours(_ny(2026, 8, 19, 12, 0))

    assert hours.is_open is True
    assert hours.source == MARKET_HOURS_SOURCE_FALLBACK
    assert hours.open_at == _ny(2026, 8, 19, 9, 30)
    assert hours.close_at == _ny(2026, 8, 19, 16, 0)
    assert hours.as_of == _ny(2026, 8, 19, 12, 0)


def test_the_fallback_publishes_no_broker_status_and_stays_known():
    """`status` is the BROKER's word; the calendar has none. `is_known` is True --
    a fallback answer is a real answer, not a failure."""
    hours = nyse_market_hours(_ny(2026, 8, 19, 12, 0))

    assert hours.status is None
    assert hours.is_known is True


def test_saturday_is_closed_and_points_at_monday():
    hours = nyse_market_hours(_ny(2026, 8, 22, 12, 0))

    assert hours.is_open is False
    assert hours.next_open == _ny(2026, 8, 24, 9, 30)


def test_sunday_is_closed():
    assert nyse_market_hours(_ny(2026, 8, 23, 12, 0)).is_open is False


def test_a_real_nyse_holiday_is_closed():
    """2026-07-04 is a Saturday, so Independence Day is OBSERVED on Friday the 3rd.
    A plain weekday check would call this an ordinary trading day."""
    hours = nyse_market_hours(_ny(2026, 7, 3, 12, 0))

    assert hours.is_open is False
    assert hours.next_open == _ny(2026, 7, 6, 9, 30)


def test_martin_luther_king_day_is_closed():
    assert nyse_market_hours(_ny(2026, 1, 19, 12, 0)).is_open is False


def test_thanksgiving_is_closed():
    assert nyse_market_hours(_ny(2026, 11, 26, 12, 0)).is_open is False


# ---------------------------------------------------------------------------
# Half day -- the case a hardcoded 16:00 gets wrong
# ---------------------------------------------------------------------------

def test_half_day_is_open_before_the_early_close():
    hours = nyse_market_hours(_ny(2026, 11, 27, 12, 0))

    assert hours.is_open is True
    assert hours.close_at == _ny(2026, 11, 27, 13, 0)


def test_half_day_is_closed_after_the_early_close():
    """13:00 ET, not 16:00. Submitting into this hour is what the gate prevents."""
    hours = nyse_market_hours(_ny(2026, 11, 27, 14, 0))

    assert hours.is_open is False
    assert hours.next_open == _ny(2026, 11, 30, 9, 30)


# ---------------------------------------------------------------------------
# The exact boundary minutes
# ---------------------------------------------------------------------------

def test_one_second_before_the_open_is_closed():
    assert nyse_market_hours(_ny(2026, 8, 19, 9, 29, 59)).is_open is False


def test_the_opening_instant_is_open():
    """Open is INCLUSIVE (open <= now), matching tastytrade/utils.py:56."""
    assert nyse_market_hours(_ny(2026, 8, 19, 9, 30, 0)).is_open is True


def test_one_second_before_the_close_is_open():
    assert nyse_market_hours(_ny(2026, 8, 19, 15, 59, 59)).is_open is True


def test_the_closing_instant_is_closed():
    """Close is EXCLUSIVE (now < close). 16:00:00 is already shut."""
    assert nyse_market_hours(_ny(2026, 8, 19, 16, 0, 0)).is_open is False


def test_the_half_day_closing_instant_is_closed():
    assert nyse_market_hours(_ny(2026, 11, 27, 13, 0, 0)).is_open is False


# ---------------------------------------------------------------------------
# next_open / next_close / next_transition
# ---------------------------------------------------------------------------

def test_while_open_the_next_transition_is_todays_close():
    hours = nyse_market_hours(_ny(2026, 8, 19, 12, 0))

    assert hours.next_close == _ny(2026, 8, 19, 16, 0)
    assert hours.next_open == _ny(2026, 8, 20, 9, 30)
    assert hours.next_transition == _ny(2026, 8, 19, 16, 0)


def test_before_the_open_the_next_transition_is_todays_open():
    hours = nyse_market_hours(_ny(2026, 8, 19, 7, 0))

    assert hours.next_open == _ny(2026, 8, 19, 9, 30)
    assert hours.next_transition == _ny(2026, 8, 19, 9, 30)


def test_when_shut_the_session_bounds_describe_the_upcoming_session():
    hours = nyse_market_hours(_ny(2026, 8, 22, 12, 0))

    assert hours.open_at == hours.next_open
    assert hours.close_at == hours.next_close


# ---------------------------------------------------------------------------
# Input contract, session listing, failure
# ---------------------------------------------------------------------------

def test_a_naive_datetime_is_refused_rather_than_guessed():
    """Reading a naive instant as UTC shifts the 09:30/16:00 boundary by 4 hours."""
    with pytest.raises(ValueError, match="timezone-aware"):
        nyse_market_hours(datetime(2026, 8, 19, 12, 0))


def test_a_utc_instant_is_accepted_and_answered_in_exchange_terms():
    """16:00 UTC on 2026-08-19 is 12:00 EDT -- mid-session."""
    hours = nyse_market_hours(datetime(2026, 8, 19, 16, 0, tzinfo=timezone.utc))

    assert hours.is_open is True


def test_sessions_omit_weekends_and_holidays_entirely():
    sessions = nyse_regular_sessions(_ny(2026, 7, 1).date(), _ny(2026, 7, 7).date())
    days = sorted(o.astimezone(NY_TZ).date().isoformat() for o, _ in sessions)

    # Wed 1st, Thu 2nd, Mon 6th, Tue 7th. No Fri 3rd (observed holiday), no weekend.
    assert days == ["2026-07-01", "2026-07-02", "2026-07-06", "2026-07-07"]


def test_a_missing_calendar_package_raises_rather_than_claiming_open(monkeypatch):
    """Fail LOUD here; the callers fail CLOSED. Never silently "open".

    Setting the entry to None makes CPython's import machinery raise ImportError
    ("import of pandas_market_calendars halted; None in sys.modules"), which is a
    faithful stand-in for the package being absent.
    """
    import sys

    clear_nyse_calendar_cache()
    monkeypatch.setitem(sys.modules, "pandas_market_calendars", None)
    try:
        with pytest.raises(MarketCalendarUnavailable):
            nyse_market_hours(_ny(2026, 8, 19, 12, 0))
    finally:
        # monkeypatch restores sys.modules, but the memo must not survive either way.
        clear_nyse_calendar_cache()


# ---------------------------------------------------------------------------
# next_open / next_close are STRICTLY future -- found by mutation
# ---------------------------------------------------------------------------

def test_next_open_at_the_opening_instant_is_TOMORROWS_open_not_now():
    """MarketHours documents next_open/next_close as "both STRICTLY FUTURE
    instants". Found by mutation: relaxing `o > now_utc` to `o >= now_utc`
    survived every other test in this file.

    It matters downstream: MarketHours.next_transition is the seam's cache-expiry
    key, so a next_open equal to `now` makes the entry expire the instant it is
    taken -- refetching on every single call at exactly 09:30:00.
    """
    at_the_bell = _ny(2026, 8, 19, 9, 30, 0)
    hours = nyse_market_hours(at_the_bell)

    assert hours.is_open is True
    assert hours.next_open == _ny(2026, 8, 20, 9, 30)   # tomorrow, not now
    assert hours.next_open > at_the_bell
    assert hours.next_transition == _ny(2026, 8, 19, 16, 0)   # today's close


def test_next_close_at_the_closing_instant_is_TOMORROWS_close_not_now():
    """The mirror case: `c >= now_utc` also survived every other test."""
    at_the_close = _ny(2026, 8, 19, 16, 0, 0)
    hours = nyse_market_hours(at_the_close)

    assert hours.is_open is False
    assert hours.next_close == _ny(2026, 8, 20, 16, 0)
    assert hours.next_close > at_the_close
