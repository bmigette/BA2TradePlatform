"""The scan-visibility rule (``metric_store.visible_scan_date`` / ``scan_cutoff_date``).

A scan dated S is visible at decision instant T  <=>  every session dated <= S has FINISHED at T
(S strictly before the earliest session not yet finished). Both grids of the screener store are
checked: the OLD store (Wednesday scans 2020-2022, Saturday scans 2023-2026) and the REBUILT one
(Saturdays only). The daily clock (S <= D) must stay exactly as before.
"""
from datetime import date, datetime, timezone

import pytest

from ba2_providers.screener.metric_store import scan_cutoff_date, visible_scan_date

WED = ["2024-01-03", "2024-01-10", "2024-01-17", "2024-01-24"]            # a Wednesday grid
SAT = ["2023-12-30", "2024-01-06", "2024-01-13", "2024-01-20"]            # a Saturday grid


def at(y, m, d, hh, mm):
    return datetime(y, m, d, hh, mm, tzinfo=timezone.utc)      # exchange-local wall time, labelled UTC


@pytest.mark.parametrize("grid, instant, expected", [
    # --- Saturday scans: visible from MONDAY's first minute (Friday finished), not one session later
    (SAT, at(2024, 1, 8, 8, 0), "2024-01-06"),        # Mon pre-market
    (SAT, at(2024, 1, 8, 10, 0), "2024-01-06"),       # Mon 10:00
    (SAT, at(2024, 1, 8, 16, 5), "2024-01-06"),       # Mon after the close
    (SAT, at(2024, 1, 9, 10, 0), "2024-01-06"),       # Tue: still the Jan 6 scan (next is Jan 13)
    (SAT, at(2024, 1, 5, 10, 0), "2023-12-30"),       # Fri 10:00: the Jan 6 scan is in the future
    (SAT, at(2024, 1, 6, 12, 0), "2024-01-06"),       # the Saturday itself (Friday finished)
    (SAT, at(2024, 1, 13, 12, 0), "2024-01-13"),
    # --- Wednesday scans: NOT visible on the day until the close
    (WED, at(2024, 1, 10, 8, 0), "2024-01-03"),       # Wed pre-market
    (WED, at(2024, 1, 10, 10, 0), "2024-01-03"),      # Wed 10:00: Wednesday's session not finished
    (WED, at(2024, 1, 10, 15, 55), "2024-01-03"),     # Wed last bar
    (WED, at(2024, 1, 10, 16, 5), "2024-01-10"),      # Wed after the close: visible
    (WED, at(2024, 1, 11, 10, 0), "2024-01-10"),      # Thu 10:00: visible
    (WED, at(2024, 1, 3, 10, 0), None),               # the very first scan is not yet visible
    # --- holiday Monday (2024-01-15 MLK) behaves like a weekend: Saturday scan Jan 13 visible Tue Jan 16
    (SAT, at(2024, 1, 15, 10, 0), "2024-01-13"),
    (SAT, at(2024, 1, 16, 10, 0), "2024-01-13"),
    # a scan dated ON the holiday: visible from the next session's open (no session <= it is unfinished)
    (["2024-01-12", "2024-01-15"], at(2024, 1, 16, 10, 0), "2024-01-15"),
    (["2024-01-12", "2024-01-15"], at(2024, 1, 12, 16, 5), "2024-01-15"),   # holiday-dated: content = Friday close, known at 16:05
    (["2024-01-12", "2024-01-15"], at(2024, 1, 12, 10, 0), None),
    # --- half day (Fri 2023-11-24 closes 13:00): a scan dated that Friday is visible from 13:00
    (["2023-11-22", "2023-11-24"], at(2023, 11, 24, 12, 55), "2023-11-22"),
    (["2023-11-22", "2023-11-24"], at(2023, 11, 24, 13, 0), "2023-11-24"),
])
def test_intraday_visibility(grid, instant, expected):
    assert visible_scan_date(grid, instant, intraday=True) == expected


@pytest.mark.parametrize("grid", [WED, SAT])
@pytest.mark.parametrize("instant", [at(2024, 1, 8, 8, 0), at(2024, 1, 10, 10, 0), at(2024, 1, 10, 16, 5),
                                     at(2024, 1, 11, 10, 0), at(2024, 1, 15, 10, 0)])
def test_daily_clock_is_unchanged_scan_le_the_bar_date(grid, instant):
    """execution_interval=1d: a decision stamped D uses D's close by convention, so the scan dated
    <= D stays visible, exactly the pre-fix reading (newest scan <= the bar's own date)."""
    legacy = [d for d in grid if d <= instant.date().isoformat()]
    assert visible_scan_date(grid, instant, intraday=False) == (legacy[-1] if legacy else None)
    assert scan_cutoff_date(instant, intraday=False) == instant.date()


def test_scan_cutoff_is_the_day_before_the_earliest_unfinished_session():
    assert scan_cutoff_date(at(2024, 1, 8, 10, 0), intraday=True) == date(2024, 1, 7)    # Mon 10:00 -> Sunday
    assert scan_cutoff_date(at(2024, 1, 10, 10, 0), intraday=True) == date(2024, 1, 9)   # Wed 10:00 -> Tuesday
    assert scan_cutoff_date(at(2024, 1, 10, 16, 0), intraday=True) == date(2024, 1, 10)  # at the close
    assert scan_cutoff_date(at(2024, 1, 13, 12, 0), intraday=True) == date(2024, 1, 15)  # Sat: Monday holiday -> Tuesday starts
