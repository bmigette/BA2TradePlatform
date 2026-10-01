"""Gap-fill policy ``"previous"`` (operator decision 2026-09-30): a missing or invalid bar inside
a symbol's own listed range is replaced by the previous VALID bar's OHLC (never the next one --
that would be look-ahead). Default (no ``gap_fill``) is unchanged, byte-for-byte."""
from __future__ import annotations

from datetime import date

import numpy as np
import pytest

from ba2_common.core.market_calendar import regular_sessions_ending_at
from ba2_common.core.market_condition_source import (
    GAP_FILL_PREVIOUS,
    assemble_window,
    assemble_window_filled,
    fill_gaps_previous,
    validate_gap_fill,
)
from ba2_common.core.market_conditions import (
    STATUS_INVALID_PRICES,
    STATUS_MISSING_SESSION,
    STATUS_VALID,
    WINDOW,
    compute_market_conditions,
)

SESSION = date(2025, 6, 30)


def _series(days):
    n = len(days)
    c = 100.0 + np.arange(n, dtype=float) * 0.1
    return (np.array(days, dtype="datetime64[D]"), c - 0.05, c + 1.0, c - 1.0, c, np.full(n, 1e6))


def _sessions(n=WINDOW + 40, end=date(2025, 7, 15)):
    return regular_sessions_ending_at(end, n)


def test_validate_gap_fill_rejects_unknown_policy():
    validate_gap_fill(None)
    validate_gap_fill(GAP_FILL_PREVIOUS)
    with pytest.raises(ValueError, match="unknown gap_fill policy"):
        validate_gap_fill("next")


def test_fills_a_missing_session_with_the_previous_valid_bar():
    days = _sessions()
    hole = regular_sessions_ending_at(SESSION, 50)[0]
    d, o, h, l, c, v = _series(days)
    j = int(np.flatnonzero(d == np.datetime64(hole))[0])
    keep = np.arange(len(d)) != j
    res = fill_gaps_previous(d[keep], o[keep], h[keep], l[keep], c[keep], v[keep])
    assert hole in res.missing_filled
    assert hole in res.filled_sessions
    assert res.invalid_filled == ()
    assert res.unfilled_hole_at_start is None
    i = int(np.flatnonzero(res.dates == np.datetime64(hole))[0])
    # Carried forward: identical OHLC to the previous (surviving) bar, volume forced to 0.
    prev = j - 1
    assert res.o[i] == o[prev] and res.h[i] == h[prev] and res.l[i] == l[prev] and res.c[i] == c[prev]
    assert res.v[i] == 0.0


def test_fills_an_invalid_bar_with_the_previous_valid_bar():
    days = _sessions()
    d, o, h, l, c, v = _series(days)
    j = int(np.flatnonzero(d == np.datetime64(SESSION))[0])
    h2 = h.copy()
    h2[j] = l[j] - 1.0  # high < low: invalid
    res = fill_gaps_previous(d, o, h2, l, c, v)
    assert SESSION in res.invalid_filled
    assert res.missing_filled == ()
    i = int(np.flatnonzero(res.dates == np.datetime64(SESSION))[0])
    assert res.h[i] == h[j - 1] and res.o[i] == o[j - 1]
    assert res.v[i] == 0.0


def test_fills_consecutive_holes_by_carrying_the_last_valid_bar():
    days = _sessions()
    d, o, h, l, c, v = _series(days)
    req = regular_sessions_ending_at(SESSION, 60)
    holes = set(req[10:13])  # three consecutive sessions
    keep = np.array([dd.astype(object) not in holes for dd in d])
    res = fill_gaps_previous(d[keep], o[keep], h[keep], l[keep], c[keep], v[keep])
    for hole in holes:
        assert hole in res.missing_filled
    # all three carry the SAME previous bar (the one right before the run of holes)
    before = sorted(set(d[keep].tolist()) - set())  # noqa: F841 -- just for readability
    prev_idx = int(np.flatnonzero(d == np.datetime64(min(holes)))[0]) - 1
    for hole in holes:
        i = int(np.flatnonzero(res.dates == np.datetime64(hole))[0])
        assert res.c[i] == c[prev_idx]
        assert res.v[i] == 0.0


def test_hole_at_series_start_is_never_filled_when_missing():
    days = _sessions()
    d, o, h, l, c, v = _series(days)
    keep = np.arange(len(d)) != 0  # drop the very first bar
    res = fill_gaps_previous(d[keep], o[keep], h[keep], l[keep], c[keep], v[keep])
    # The output's own span simply starts one session later -- nothing is fabricated before it.
    assert res.dates[0] == d[1]
    assert res.unfilled_hole_at_start is None
    assert res.missing_filled == () and res.invalid_filled == ()


def test_hole_at_series_start_is_never_filled_when_invalid():
    days = _sessions()
    d, o, h, l, c, v = _series(days)
    h2 = h.copy()
    h2[0] = l[0] - 1.0  # the very first bar is itself invalid, no earlier bar exists
    res = fill_gaps_previous(d, o, h2, l, c, v)
    assert res.unfilled_hole_at_start == d[0].astype(object)
    assert res.invalid_filled == () and res.missing_filled == ()
    # left exactly as given: still invalid
    assert res.h[0] == h2[0]


def test_conflicting_duplicate_bars_are_refused():
    days = _sessions()
    d, o, h, l, c, v = _series(days)
    j = 10
    dup = lambda a: np.insert(a, j, a[j])  # noqa: E731
    c2 = dup(c).copy()
    c2[j] += 1.0
    with pytest.raises(ValueError, match="conflicting"):
        fill_gaps_previous(dup(d), dup(o), dup(h), dup(l), c2, dup(v))


def test_identical_duplicate_bars_collapse():
    days = _sessions()
    d, o, h, l, c, v = _series(days)
    j = 10
    dup = lambda a: np.insert(a, j, a[j])  # noqa: E731
    res = fill_gaps_previous(dup(d), dup(o), dup(h), dup(l), dup(c), dup(v))
    assert len(res.dates) == len(d)
    assert res.filled_sessions == ()


def test_bar_off_the_regular_session_calendar_is_refused():
    days = _sessions() + [date(2025, 6, 28)]  # a Saturday
    with pytest.raises(ValueError, match="not a regular session"):
        fill_gaps_previous(*_series(sorted(days)))


def test_volume_is_always_zero_for_a_filled_bar_never_carried():
    days = _sessions()
    d, o, h, l, c, v = _series(days)
    v2 = v.copy()
    v2[:] = 777.0
    keep = np.arange(len(d)) != 5
    res = fill_gaps_previous(d[keep], o[keep], h[keep], l[keep], c[keep], v2[keep])
    i = int(np.flatnonzero(res.dates == d[5])[0])
    assert res.v[i] == 0.0
    # a filled bar never registers a NEW invalid_prices row through its (zero) volume
    assert np.isfinite(res.v[i]) and res.v[i] >= 0


# ---------------------------------------------------------------------------
# assemble_window_filled: the shared BT/live entry point
# ---------------------------------------------------------------------------
def test_assemble_window_filled_default_is_unchanged():
    d, o, h, l, c, v = _series(_sessions())
    ref = assemble_window(d, o, h, l, c, v, SESSION)
    got = assemble_window_filled(d, o, h, l, c, v, SESSION)
    assert got.status == ref.status
    for a, b in zip(got.arrays(), ref.arrays()):
        np.testing.assert_array_equal(a, b)


def test_assemble_window_filled_previous_turns_a_missing_session_window_valid():
    days = _sessions()
    hole = regular_sessions_ending_at(SESSION, 50)[0]
    d, o, h, l, c, v = _series(days)
    keep = d != np.datetime64(hole)
    unfilled = assemble_window(d[keep], o[keep], h[keep], l[keep], c[keep], v[keep], SESSION)
    assert unfilled.status == STATUS_MISSING_SESSION
    filled = assemble_window_filled(d[keep], o[keep], h[keep], l[keep], c[keep], v[keep], SESSION,
                                    gap_fill=GAP_FILL_PREVIOUS)
    assert filled.ok


def test_assemble_window_filled_previous_turns_an_invalid_row_valid():
    d, o, h, l, c, v = _series(_sessions())
    j = int(np.flatnonzero(d == np.datetime64(SESSION))[0])
    h2 = h.copy()
    h2[j] = l[j] - 1.0
    unfilled = assemble_window(d, o, h2, l, c, v, SESSION)
    assert unfilled.ok
    assert compute_market_conditions(*unfilled.arrays()).adx.status == STATUS_INVALID_PRICES
    filled = assemble_window_filled(d, o, h2, l, c, v, SESSION, gap_fill=GAP_FILL_PREVIOUS)
    assert filled.ok
    mc = compute_market_conditions(*filled.arrays())
    assert mc.adx.status == STATUS_VALID
    assert mc.trend_slope.status == STATUS_VALID


def test_assemble_window_filled_rejects_unknown_policy():
    d, o, h, l, c, v = _series(_sessions())
    with pytest.raises(ValueError, match="unknown gap_fill policy"):
        assemble_window_filled(d, o, h, l, c, v, SESSION, gap_fill="next")


def test_absent_session_after_an_invalid_first_bar_is_emitted_as_an_invalid_row_not_a_crash():
    sessions = regular_sessions_ending_at(date(2025, 7, 15), 6)
    keep = [sessions[0]] + sessions[2:]                # session 2 absent
    d, o, h, l, c, v = _series(keep)
    h = h.copy()
    h[0] = l[0] - 1.0                                  # first bar present but invalid
    res = fill_gaps_previous(d, o, h, l, c, v)
    assert len(res.dates) == len(sessions)
    assert res.unfilled_hole_at_start == sessions[0]
    assert np.isnan(res.c[1]) and np.isnan(res.v[1])   # the absent session: explicitly invalid
    assert res.filled_sessions == ()                   # nothing was carried
    assert not np.isnan(res.c[2])                      # later valid bars are untouched
