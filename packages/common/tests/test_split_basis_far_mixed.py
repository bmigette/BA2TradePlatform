"""The mixed-basis scan beyond the 30-day window (review r7, APH 2026-09-28).

APH's cache was rewritten with the bars from 2026-07-01 already adjusted for the 2:1 split whose
ex-date is 2026-09-03, applied 64 sessions early. The ratio rule (at 09-03) and the 30-day
mixed-basis window both passed it; only the option basis guard refused. The scan now also
catches, up to ``MIXED_BASIS_FAR_WINDOW_DAYS`` from a calendar split, a one-day move whose size
MATCHES that split's factor (within ``MIXED_BASIS_FAR_MATCH`` in log). Measured on the 789 local
files (753 stage-2 + 97 stage-1 names): the only such move within 365 days of a matching split is APH 2026-07-01.
Real moves stay allowed: a large move of another size, a matching move further away, and a move
acknowledged in the overrides. No network, synthetic series.
"""
from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pytest

from ba2_common.core.split_basis import (
    MIXED_BASIS_FAR_MATCH, MIXED_BASIS_FAR_WINDOW_DAYS, VERDICT_CONSISTENT, VERDICT_MIXED_BASIS,
    CalendarSplit, check_split_basis,
)

SPLIT = date(2026, 9, 3)


def _series(first=date(2025, 1, 2), last=date(2026, 9, 25), px=160.0, seed=3):
    days = np.arange(np.datetime64(first), np.datetime64(last) + 1, dtype="datetime64[D]")
    days = days[np.is_busday(days)]
    rng = np.random.default_rng(seed)
    c = px * np.exp(np.cumsum(rng.normal(0, 0.004, len(days))))
    return days, c


def _adjusted(days, c, factor=2.0, split=SPLIT):
    """A correctly split-adjusted file is one continuous series (the factor is already applied
    to every bar before the ex-date)."""
    return c.copy()


def _check(days, c, splits, **kw):
    return check_split_basis(days, c, c, c, c, splits, symbol="XTEST", **kw)


def _with_step(days, c, day, ratio):
    c = c.copy()
    c[days >= np.datetime64(day)] *= ratio
    return c


def test_the_constants():
    assert MIXED_BASIS_FAR_WINDOW_DAYS == 365 and MIXED_BASIS_FAR_MATCH == pytest.approx(0.04)


def test_the_aph_shape_is_mixed_basis():
    """The bars before 2026-07-01 as first fetched (as traded, x2 the adjusted level), the bars
    appended from 07-01 already adjusted for the 09-03 split: x0.488 on 07-01, a smooth ex-date."""
    days, c = _series()
    good = _adjusted(days, c)
    assert [x.verdict for x in _check(days, good, [CalendarSplit(SPLIT, 2.0)])] == [VERDICT_CONSISTENT]
    early = date(2026, 7, 1)
    bad = good.copy()
    bad[days < np.datetime64(early)] *= 2.0
    bad = bad * np.where(days >= np.datetime64(early), 0.976, 1.0)       # the day's own -2.4%
    (chk,) = _check(days, bad, [CalendarSplit(SPLIT, 2.0)])
    assert chk.verdict == VERDICT_MIXED_BASIS and chk.checked_bar == early
    assert "2026-07-01" in chk.reason and "x2" in chk.reason


def test_a_move_of_another_size_far_from_the_split_is_a_real_move():
    days, c = _series()
    good = _with_step(days, _adjusted(days, c), date(2026, 7, 1), 0.60)   # a -40% crash
    assert [x.verdict for x in _check(days, good, [CalendarSplit(SPLIT, 2.0)])] == [VERDICT_CONSISTENT]


def test_a_matching_move_beyond_the_far_window_is_a_real_move():
    days, c = _series(first=date(2024, 1, 2))
    day = SPLIT - timedelta(days=MIXED_BASIS_FAR_WINDOW_DAYS + 10)
    good = _with_step(days, _adjusted(days, c), day, 0.5)
    assert [x.verdict for x in _check(days, good, [CalendarSplit(SPLIT, 2.0)])] == [VERDICT_CONSISTENT]


def test_an_acknowledged_matching_move_stays_acknowledged():
    days, c = _series()
    day = date(2026, 5, 4)
    moved = _with_step(days, _adjusted(days, c), day, 0.5)
    i = int(np.flatnonzero(days == np.datetime64(day))[0])
    r = moved[i] / moved[i - 1]
    (chk,) = _check(days, moved, [CalendarSplit(SPLIT, 2.0)], acknowledged_jumps={day: r})
    assert chk.verdict == VERDICT_CONSISTENT
    (chk,) = _check(days, moved, [CalendarSplit(SPLIT, 2.0)], acknowledged_jumps={})
    assert chk.verdict == VERDICT_MIXED_BASIS


def test_a_reverse_split_applied_early_is_caught_too():
    """1-for-10 (ratio 0.1): the bars before the early adjustment are 1/10 of the adjusted level."""
    days, c = _series(px=2.0)
    bad = _adjusted(days, c, factor=0.1)
    bad[days < np.datetime64(date(2026, 5, 1))] *= 0.1
    (chk,) = _check(days, bad, [CalendarSplit(SPLIT, 0.1)])
    assert chk.verdict == VERDICT_MIXED_BASIS
