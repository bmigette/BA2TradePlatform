"""Window assembly for the market-condition gates (design section 4): exactly WINDOW bars on
exactly the WINDOW regular sessions ending at the requested session, or an explicit status."""
from __future__ import annotations

from datetime import date, datetime, timezone

import numpy as np
import pytest

from ba2_common.core.market_calendar import regular_sessions_ending_at
from ba2_common.core.market_condition_source import (
    assemble_window,
    normalized_window_bytes,
    window_digest,
)
from ba2_common.core.market_conditions import (
    STATUS_INSUFFICIENT_HISTORY,
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


def test_exact_window_from_a_longer_series():
    d, o, h, l, c, v = _series(_sessions())
    res = assemble_window(d, o, h, l, c, v, SESSION)
    assert res.status == STATUS_VALID and res.ok
    assert list(res.dates) == regular_sessions_ending_at(SESSION, WINDOW)
    assert all(len(a) == WINDOW and a.dtype == np.float64 for a in res.arrays())
    end = int(np.flatnonzero(d == np.datetime64(SESSION))[0])
    np.testing.assert_array_equal(res.c, c[end - WINDOW + 1:end + 1])


def test_unsorted_input_and_date_objects_are_accepted():
    days = _sessions()
    d, o, h, l, c, v = _series(days)
    perm = np.random.default_rng(1).permutation(len(days))
    res = assemble_window([days[i] for i in perm], o[perm], h[perm], l[perm], c[perm], v[perm], SESSION)
    assert res.ok
    ref = assemble_window(d, o, h, l, c, v, SESSION)
    for a, b in zip(res.arrays(), ref.arrays()):
        np.testing.assert_array_equal(a, b)


def test_young_listing_is_insufficient_history():
    days = regular_sessions_ending_at(SESSION, 60)
    res = assemble_window(*_series(days), SESSION)
    assert res.status == STATUS_INSUFFICIENT_HISTORY
    assert "60 of 128" in res.reason


def test_series_starting_after_the_session_is_insufficient_history():
    res = assemble_window(*_series([date(2025, 7, 1), date(2025, 7, 2)]), SESSION)
    assert res.status == STATUS_INSUFFICIENT_HISTORY


def test_gap_inside_the_window_is_missing_session():
    days = _sessions()
    hole = regular_sessions_ending_at(SESSION, 50)[0]
    res = assemble_window(*_series([d for d in days if d != hole]), SESSION)
    assert res.status == STATUS_MISSING_SESSION
    assert str(hole) in res.reason


def test_missing_requested_session_is_missing_session_not_an_older_one():
    days = [d for d in _sessions() if d != SESSION]
    res = assemble_window(*_series(days), SESSION)
    assert res.status == STATUS_MISSING_SESSION
    assert str(SESSION) in res.reason


def test_leading_hole_with_earlier_bars_is_missing_session():
    req = regular_sessions_ending_at(SESSION, WINDOW)
    days = [d for d in _sessions() if d != req[0]]
    res = assemble_window(*_series(days), SESSION)
    assert res.status == STATUS_MISSING_SESSION


def test_stale_series_is_missing_session():
    days = regular_sessions_ending_at(date(2024, 6, 28), 200)
    res = assemble_window(*_series(days), SESSION)
    assert res.status == STATUS_MISSING_SESSION


def test_identical_duplicate_collapses_conflicting_duplicate_fails():
    days = _sessions()
    d, o, h, l, c, v = _series(days)
    j = int(np.flatnonzero(d == np.datetime64(date(2025, 5, 1)))[0])
    dup = lambda a: np.insert(a, j, a[j])  # noqa: E731
    res = assemble_window(dup(d), dup(o), dup(h), dup(l), dup(c), dup(v), SESSION)
    assert res.ok
    c2 = dup(c).copy()
    c2[j] += 0.5
    res = assemble_window(dup(d), dup(o), dup(h), dup(l), c2, dup(v), SESSION)
    assert res.status == STATUS_MISSING_SESSION and "conflicting" in res.reason


def test_non_session_bar_is_missing_session():
    days = _sessions() + [date(2025, 6, 28)]  # a Saturday
    res = assemble_window(*_series(sorted(days)), SESSION)
    assert res.status == STATUS_MISSING_SESSION and "not a regular session" in res.reason


def test_invalid_prices_are_left_to_the_calculator():
    d, o, h, l, c, v = _series(_sessions())
    c = c.copy()
    c[int(np.flatnonzero(d == np.datetime64(SESSION))[0])] = np.nan
    res = assemble_window(d, o, h, l, c, v, SESSION)
    assert res.ok
    assert compute_market_conditions(*res.arrays()).adx.status == STATUS_INVALID_PRICES


def test_ambiguous_dates_and_non_sessions_are_refused():
    d, o, h, l, c, v = _series(_sessions())
    stamps = [datetime(2025, 1, 2, 14, 30, tzinfo=timezone.utc)] * len(d)
    with pytest.raises(ValueError, match="ambiguous"):
        assemble_window(stamps, o, h, l, c, v, SESSION)
    with pytest.raises(ValueError, match="not a regular"):
        assemble_window(d, o, h, l, c, v, date(2025, 6, 29))
    with pytest.raises(ValueError, match="equal lengths"):
        assemble_window(d[:-1], o, h, l, c, v, SESSION)


def test_window_digest_is_stable_and_content_sensitive():
    res = assemble_window(*_series(_sessions()), SESSION)
    a = window_digest(*res.arrays())
    assert a == window_digest(*[x.copy() for x in res.arrays()])
    assert a.startswith("sha256:") and len(a) == len("sha256:") + 64
    raw = normalized_window_bytes(*res.arrays())
    assert len(raw) == WINDOW * 5 * 8
    assert np.frombuffer(raw, dtype="<f8").reshape(WINDOW, 5)[-1, 3] == res.c[-1]
    c = res.c.copy()
    c[0] = np.nextafter(c[0], np.inf)
    assert window_digest(res.o, res.h, res.l, c, res.v) != a
