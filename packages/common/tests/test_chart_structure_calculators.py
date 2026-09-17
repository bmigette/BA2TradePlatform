"""ta-structure-v1 chart-structure calculators (design 2026-09-15 sections 3.2 / 3.3, plan D8.14).

Every expected number here is derived by a straightforward independent loop in this file
(``_ref_*``), never by calling the function under test. The intermediate structures the design
names -- the confirmed pivot list and the alternating swing sequence -- are pinned as well as the
twelve fields, because a wrong pivot list that happens to produce the right distance on one
window is the failure this profile cannot afford.
"""
import math

import numpy as np
import pytest

from ba2_common.core.market_conditions import (
    CHANNEL_LOOKBACK,
    COMPUTE_BY_PROFILE,
    LEVEL_TOL_ATR,
    PIVOT_K,
    PROFILES,
    STATUS_INSUFFICIENT_HISTORY,
    STATUS_INVALID_PRICES,
    STATUS_VALID,
    STRUCTURE_CALC_VERSION,
    STRUCTURE_FIELDS,
    STRUCTURE_STATE_CODES,
    TA_STRUCTURE_V1,
    WINDOW,
    Pivot,
    _level_fields,
    _recent_swings,
    atr14_wilder,
    compute_chart_structure,
    field_spec,
    find_pivots,
    fit_channel,
    reduce_to_swings,
    swing_state,
)

# --------------------------------------------------------------------------------------------
# window builders
# --------------------------------------------------------------------------------------------


def _bars(closes, *, spread=1.0, highs=None, lows=None, volume=1000.0):
    """OHLCV arrays from closes: ``h = c + spread``, ``l = c - spread``, ``o = c`` (all valid)."""
    c = np.asarray(closes, dtype=float)
    h = c + spread if highs is None else np.asarray(highs, dtype=float)
    l = c - spread if lows is None else np.asarray(lows, dtype=float)
    o = c.copy()
    v = np.full(len(c), float(volume))
    return o, h, l, c, v


def _zigzag(points, seg):
    out = [float(points[0])]
    for a, b in zip(points, points[1:]):
        for i in range(1, seg + 1):
            out.append(float(a) + (float(b) - float(a)) * i / seg)
    return out


def _series(points, seg=5, n=WINDOW, step=0.05):
    """A zigzag through ``points`` (a turning point every ``seg`` bars), right-aligned in an
    ``n``-bar window behind a monotone ramp that CONTINUES into the first leg, so the junction is
    not itself a pivot. Returns ``(closes, pad)``; interior turning points sit at
    ``pad + seg*i`` for ``i`` in 1..len(points)-2."""
    zig = _zigzag(points, seg)
    pad = n - len(zig)
    assert pad >= 0, f"{len(zig)} bars of zigzag do not fit in {n}"
    d = step if points[1] > points[0] else -step
    prefix = [zig[0] - d * k for k in range(pad, 0, -1)]
    return prefix + zig, pad


# --------------------------------------------------------------------------------------------
# independent references
# --------------------------------------------------------------------------------------------


def _ref_pivots(h, l, k=PIVOT_K):
    """(index, kind, price) triples, naive double loop, high before low at the same index."""
    out = []
    n = len(h)
    for p in range(k, n - k):
        nb = [j for j in range(p - k, p + k + 1) if j != p]
        if all(h[p] > h[j] for j in nb):
            out.append((p, "high", h[p]))
        if all(l[p] < l[j] for j in nb):
            out.append((p, "low", l[p]))
    return out


def _ref_swings(pivots):
    """Collapse each maximal same-kind run to its earliest extreme (design 3.3 item 1)."""
    out = []
    for p in pivots:
        if out and out[-1][1] == p[1]:
            better = p[2] > out[-1][2] if p[1] == "high" else p[2] < out[-1][2]
            if better:
                out[-1] = p
        else:
            out.append(p)
    return out


def _ref_state(pivots):
    sw = _ref_swings(pivots)
    highs = [p[2] for p in sw if p[1] == "high"]
    lows = [p[2] for p in sw if p[1] == "low"]
    if len(highs) < 2 or len(lows) < 2:
        return "none"
    if highs[-1] > highs[-2] and lows[-1] > lows[-2]:
        return "bull"
    if highs[-1] < highs[-2] and lows[-1] < lows[-2]:
        return "bear"
    return "none"


def _ref_channel(y):
    n = len(y)
    xs = list(range(n))
    xb = sum(xs) / n
    yb = sum(y) / n
    sxy = sum((x - xb) * (v - yb) for x, v in zip(xs, y))
    sxx = sum((x - xb) * (x - xb) for x in xs)
    b = sxy / sxx
    a = yb - b * xb
    res = [v - (a + b * x) for x, v in zip(xs, y)]
    sd = math.sqrt(sum(e * e for e in res) / (n - 2))
    return a, b, sd


def _ref_breaks(pivots, closes, state):
    """``(bars_since_bos, bars_since_choch)``, recomputing the swing structure FROM SCRATCH at
    every session out of the pivots that session had confirmed (design 3.3 item 4)."""
    if state == "none":
        return None, None
    last = len(closes) - 1
    bos = choch = None
    for t in range(last, -1, -1):
        sw = _ref_swings([p for p in pivots if p[0] <= t - PIVOT_K])
        highs = [p[2] for p in sw if p[1] == "high"]
        lows = [p[2] for p in sw if p[1] == "low"]
        up = bool(highs) and closes[t] > highs[-1]
        down = bool(lows) and closes[t] < lows[-1]
        broke, changed = (up, down) if state == "bull" else (down, up)
        if bos is None and broke:
            bos = float(last - t)
        if choch is None and changed:
            choch = float(last - t)
    return bos, choch


def _triples(pivots):
    return [(p.index, p.kind, p.price) for p in pivots]


def _row(o, h, l, c, v):
    return compute_chart_structure(o, h, l, c, v).as_row()


# --------------------------------------------------------------------------------------------
# confirmed pivots (D8.14: absent K-1 sessions after the extreme, present K after)
# --------------------------------------------------------------------------------------------

def _spike_history(tie=False):
    """140 bars: a gentle monotone ramp with one strict 7-bar bump peaking at index 130."""
    c = [100.0 + 0.05 * i for i in range(140)]
    for j, d in zip(range(127, 134), [0.0, 1.0, 2.0, 3.0, 2.0, 1.0, 0.0]):
        c[j] += d
    if tie:
        c[129] = c[130]      # equal highs on both sides of the would-be pivot
    return c


def test_a_pivot_is_invisible_k_minus_1_sessions_after_the_extreme_and_visible_k_after():
    c = _spike_history()
    o, h, l, cc, v = _bars(c)
    early_end = 130 + PIVOT_K - 1                       # 2 sessions after the extreme
    late_end = 130 + PIVOT_K                            # 3 sessions after it

    def window(end):
        s = end - WINDOW + 1
        return [a[s:end + 1] for a in (o, h, l, cc, v)]

    early = window(early_end)
    late = window(late_end)
    assert [p.index + early_end - WINDOW + 1 for p in find_pivots(early[1].tolist(), early[2].tolist())] == []
    assert [p.index + late_end - WINDOW + 1 for p in find_pivots(late[1].tolist(), late[2].tolist())] == [130]

    early_row = _row(*early)
    late_row = _row(*late)
    # Two sessions after the extreme the level does not exist yet; three sessions after it does.
    assert early_row["structure_dist_resistance_atr"] is None
    assert early_row["structure_dist_resistance_atr_status"] == STATUS_INSUFFICIENT_HISTORY
    assert late_row["structure_dist_resistance_atr_status"] == STATUS_VALID
    atr = float(atr14_wilder(late[1], late[2], late[3])[WINDOW - 1])
    assert late_row["structure_dist_resistance_atr"] == pytest.approx((h[130] - c[130 + PIVOT_K]) / atr)
    assert late_row["structure_resistance_touches"] == 1.0


def test_equal_price_ties_are_not_pivots():
    c = _spike_history(tie=True)
    o, h, l, cc, v = _bars(c)
    end = 130 + PIVOT_K
    s = end - WINDOW + 1
    win = [a[s:end + 1] for a in (o, h, l, cc, v)]
    assert find_pivots(win[1].tolist(), win[2].tolist()) == []
    row = _row(*win)
    assert row["structure_dist_resistance_atr"] is None
    assert row["structure_dist_support_atr"] is None


def test_the_pivot_list_matches_an_independent_scan_and_is_high_before_low_at_one_index():
    closes, _ = _series([100, 112, 104, 118, 106, 120], seg=5)
    o, h, l, c, v = _bars(closes)
    assert _triples(find_pivots(h.tolist(), l.tolist())) == _ref_pivots(h.tolist(), l.tolist())
    # An OUTSIDE bar is both a pivot high and a pivot low, and the HIGH is listed first.
    hh, ll = [10.0] * 9, [1.0] * 9
    hh[4], ll[4] = 12.0, 0.5
    assert _triples(find_pivots(hh, ll)) == [(4, "high", 12.0), (4, "low", 0.5)]


# --------------------------------------------------------------------------------------------
# support / resistance levels and touches
# --------------------------------------------------------------------------------------------

def test_no_confirmed_pivot_above_the_close_makes_resistance_unknown_not_zero_and_not_the_extreme():
    closes, pad = _series([100, 110, 104, 118, 108, 122], seg=5)
    o, h, l, c, v = _bars(closes)
    row = _row(o, h, l, c, v)
    assert row["structure_dist_resistance_atr"] is None
    assert row["structure_dist_resistance_atr_status"] == STATUS_INSUFFICIENT_HISTORY
    assert row["structure_resistance_touches"] is None
    # There IS a higher high in the window -- the field is unknown anyway, never the extreme.
    assert float(h.max()) > float(c[-1])
    atr = float(atr14_wilder(h, l, c)[WINDOW - 1])
    assert row["structure_dist_support_atr"] == pytest.approx((c[-1] - l[pad + 4 * 5]) / atr)


def test_touch_counting_is_inclusive_at_the_exact_tolerance_boundary():
    # ``_level_fields`` takes ATR explicitly, which is the only way to place a pivot EXACTLY on
    # the +/- 0.25 ATR boundary: any price edit changes the ATR a whole window would produce.
    atr = 2.0
    tol = LEVEL_TOL_ATR * atr                      # 0.5
    close = 100.0
    pivots = [
        Pivot(10, "high", 110.0),                  # defines R
        Pivot(20, "high", 110.0 + tol),            # exactly on the boundary -> counts
        Pivot(30, "high", 110.0 + tol + 1e-9),     # just outside -> does not
        Pivot(40, "low", 90.0),                    # defines S
        Pivot(50, "low", 90.0 - tol),              # exactly on the boundary -> counts
        Pivot(60, "low", 90.0 - tol - 1e-9),       # just outside -> does not
    ]
    sup, res, sup_touch, res_touch = _level_fields(pivots, close, atr)
    assert res.value == pytest.approx(5.0) and res_touch.value == 2.0
    assert sup.value == pytest.approx(5.0) and sup_touch.value == 2.0


def test_equal_price_pivots_are_one_level_and_all_of_them_are_touches():
    pivots = [Pivot(10, "high", 110.0), Pivot(20, "high", 110.0), Pivot(30, "high", 110.0),
              Pivot(40, "low", 90.0)]
    sup, res, sup_touch, res_touch = _level_fields(pivots, 100.0, 2.0)
    assert res.value == pytest.approx(5.0)
    assert res_touch.value == 3.0
    assert sup_touch.value == 1.0        # the defining pivot always counts: minimum 1


# --------------------------------------------------------------------------------------------
# regression channel
# --------------------------------------------------------------------------------------------

def test_the_channel_fit_matches_an_independent_ols():
    closes, _ = _series([100, 112, 104, 118, 106, 120], seg=5)
    a, b, sd = fit_channel(closes[-CHANNEL_LOOKBACK:])
    ra, rb, rsd = _ref_channel(closes[-CHANNEL_LOOKBACK:])
    assert (a, b, sd) == pytest.approx((ra, rb, rsd))
    o, h, l, c, v = _bars(closes)
    atr = float(atr14_wilder(h, l, c)[WINDOW - 1])
    row = _row(o, h, l, c, v)
    assert row["channel_slope_20_atr"] == pytest.approx(rb / atr)
    assert row["channel_width_20_atr"] == pytest.approx(4.0 * rsd / atr)
    assert row["channel_pos_20"] == pytest.approx(
        (closes[-1] - (ra + (CHANNEL_LOOKBACK - 1) * rb - 2.0 * rsd)) / (4.0 * rsd))


def test_a_flat_channel_has_zero_dispersion_a_valid_zero_slope_and_unknown_width_and_position():
    closes = [100.0 + 0.3 * (i % 7) for i in range(WINDOW - CHANNEL_LOOKBACK)] + [111.0] * CHANNEL_LOOKBACK
    o, h, l, c, v = _bars(closes)
    row = _row(o, h, l, c, v)
    assert row["channel_slope_20_atr"] == 0.0
    assert row["channel_slope_20_atr_status"] == STATUS_VALID
    assert row["channel_width_20_atr"] is None and row["channel_pos_20"] is None
    assert row["channel_width_20_atr_status"] == STATUS_INVALID_PRICES
    assert row["channel_pos_20_status"] == STATUS_INVALID_PRICES


def test_a_close_outside_the_channel_gives_a_position_outside_0_1_and_is_not_clamped():
    base = [100.0 + 0.3 * (i % 7) for i in range(WINDOW - CHANNEL_LOOKBACK)]
    line = [100.0 + 0.5 * k for k in range(CHANNEL_LOOKBACK - 1)]
    closes = base + line + [line[-1] + 10.0]
    o, h, l, c, v = _bars(closes)
    row = _row(o, h, l, c, v)
    ra, rb, rsd = _ref_channel(closes[-CHANNEL_LOOKBACK:])
    expected = (closes[-1] - (ra + (CHANNEL_LOOKBACK - 1) * rb - 2.0 * rsd)) / (4.0 * rsd)
    assert row["channel_pos_20"] == pytest.approx(expected)
    assert row["channel_pos_20"] > 1.0


# --------------------------------------------------------------------------------------------
# prior-range breakout
# --------------------------------------------------------------------------------------------

def test_the_breakout_range_excludes_the_last_session():
    closes = [100.0 + 0.3 * (i % 7) for i in range(WINDOW)]
    highs = [x + 1.0 for x in closes]
    highs[-1] = 200.0                     # the window's highest high, in the EXCLUDED session
    lows = [x - 1.0 for x in closes]
    lows[-1] = 10.0                       # and its lowest low
    o, h, l, c, v = _bars(closes, highs=highs, lows=lows)
    atr = float(atr14_wilder(h, l, c)[WINDOW - 1])
    prior_hi = max(highs[WINDOW - 1 - CHANNEL_LOOKBACK:WINDOW - 1])
    prior_lo = min(lows[WINDOW - 1 - CHANNEL_LOOKBACK:WINDOW - 1])
    row = _row(o, h, l, c, v)
    assert row["close_vs_prior_high_20_atr"] == pytest.approx((closes[-1] - prior_hi) / atr)
    assert row["close_vs_prior_low_20_atr"] == pytest.approx((closes[-1] - prior_lo) / atr)
    assert row["close_vs_prior_high_20_atr"] != pytest.approx((closes[-1] - 200.0) / atr)
    assert prior_hi < 200.0 and prior_lo > 10.0


# --------------------------------------------------------------------------------------------
# swing structure
# --------------------------------------------------------------------------------------------

def test_the_alternating_reduction_keeps_the_highest_of_two_highs_between_two_lows():
    pivots = [Pivot(0, "low", 90.0), Pivot(5, "high", 110.0), Pivot(9, "high", 115.0),
              Pivot(14, "low", 95.0)]
    assert _triples(reduce_to_swings(pivots)) == [
        (0, "low", 90.0), (9, "high", 115.0), (14, "low", 95.0)]
    # and the LOWEST of two lows between two highs
    pivots = [Pivot(0, "high", 120.0), Pivot(5, "low", 95.0), Pivot(9, "low", 90.0),
              Pivot(14, "high", 118.0)]
    assert _triples(reduce_to_swings(pivots)) == [
        (0, "high", 120.0), (9, "low", 90.0), (14, "high", 118.0)]
    # a tie inside a run keeps the EARLIEST occurrence
    pivots = [Pivot(3, "high", 110.0), Pivot(8, "high", 110.0)]
    assert _triples(reduce_to_swings(pivots)) == [(3, "high", 110.0)]


def test_recent_swings_is_the_tail_of_the_full_reduction():
    rng = np.random.default_rng(20260916)
    for _ in range(25):
        closes = 100.0 + np.cumsum(rng.normal(0.0, 1.0, WINDOW))
        o, h, l, c, v = _bars(closes)
        pivots = find_pivots(h.tolist(), l.tolist())
        swings = reduce_to_swings(pivots)
        highs, lows = _recent_swings(pivots, 0, len(pivots) - 1, 2)
        ref_highs = [p.price for p in swings if p.kind == "high"][-2:][::-1]
        ref_lows = [p.price for p in swings if p.kind == "low"][-2:][::-1]
        assert highs == ref_highs and lows == ref_lows


@pytest.mark.parametrize("points,expected", [
    ([100, 110, 104, 118, 108, 122], "bull"),      # higher highs AND higher lows
    ([122, 108, 118, 104, 110, 100], "bear"),      # lower highs AND lower lows
    ([100, 110, 104, 118, 100, 112], "none"),      # higher high, LOWER low -> neither
])
def test_swing_structure_is_bull_bear_or_none(points, expected):
    closes, pad = _series(points, seg=5)
    o, h, l, c, v = _bars(closes)
    pivots = find_pivots(h.tolist(), l.tolist())
    ref = _ref_pivots(h.tolist(), l.tolist())
    assert _triples(pivots) == ref
    assert _triples(reduce_to_swings(pivots)) == _ref_swings(ref)
    assert swing_state(pivots) == expected == _ref_state(ref)
    row = _row(o, h, l, c, v)
    code = float(STRUCTURE_STATE_CODES.get(expected, 0))
    assert row["structure_state"] == code
    assert row["structure_state_status"] == STATUS_VALID


def test_equality_between_swings_is_neither_bull_nor_bear():
    pivots = [Pivot(3, "high", 110.0), Pivot(8, "low", 100.0),
              Pivot(13, "high", 110.0), Pivot(18, "low", 101.0)]     # equal highs
    assert swing_state(pivots) == "none"


def test_fewer_than_two_swings_of_either_kind_is_none_and_both_bars_since_are_unknown():
    closes = _spike_history()
    o, h, l, c, v = _bars(closes)
    end = 130 + PIVOT_K
    s = end - WINDOW + 1
    row = _row(*[a[s:end + 1] for a in (o, h, l, c, v)])   # exactly one pivot in the window
    assert row["structure_state"] == 0.0 and row["structure_state_status"] == STATUS_VALID
    assert row["structure_bars_since_bos"] is None and row["structure_bars_since_choch"] is None
    assert row["structure_bars_since_bos_status"] == STATUS_INSUFFICIENT_HISTORY


# --------------------------------------------------------------------------------------------
# break of structure / change of character
# --------------------------------------------------------------------------------------------

@pytest.mark.parametrize("points", [
    [100, 110, 104, 118, 108, 122],
    [122, 108, 118, 104, 110, 100],
    [100, 116, 102, 112, 106, 118],
])
def test_bos_and_choch_walk_with_the_pivots_each_session_had_confirmed(points):
    closes, _ = _series(points, seg=5)
    o, h, l, c, v = _bars(closes)
    pivots = find_pivots(h.tolist(), l.tolist())
    ref = _ref_pivots(h.tolist(), l.tolist())
    state = _ref_state(ref)
    bos, choch = _ref_breaks(ref, c.tolist(), state)
    row = _row(o, h, l, c, v)
    assert row["structure_bars_since_bos"] == bos
    assert row["structure_bars_since_choch"] == choch


def test_bos_and_choch_match_the_reference_walk_on_random_histories():
    rng = np.random.default_rng(1609)
    checked = 0
    for _ in range(12):
        closes = 100.0 + np.cumsum(rng.normal(0.0, 1.5, WINDOW))
        o, h, l, c, v = _bars(closes)
        ref = _ref_pivots(h.tolist(), l.tolist())
        state = _ref_state(ref)
        bos, choch = _ref_breaks(ref, c.tolist(), state)
        row = _row(o, h, l, c, v)
        assert row["structure_bars_since_bos"] == bos
        assert row["structure_bars_since_choch"] == choch
        if state != "none":
            checked += 1
    assert checked >= 3, "the random sample must exercise a directional structure"


def test_a_break_is_not_registered_before_the_swing_it_breaks_is_confirmed():
    # The most recent swing high is confirmed at index 130 (a 7-bar bump peaking there). A close
    # above it at session 131 or 132 must NOT count as a break of structure: at those sessions the
    # level does not exist yet. Pinned against the independent walk, which filters by p + K <= t.
    closes = _spike_history()
    o, h, l, c, v = _bars(closes)
    end = 139
    s = end - WINDOW + 1
    win = [a[s:end + 1] for a in (o, h, l, c, v)]
    pivots = find_pivots(win[1].tolist(), win[2].tolist())
    ref = _ref_pivots(win[1].tolist(), win[2].tolist())
    assert _triples(pivots) == ref
    state = _ref_state(ref)
    bos, choch = _ref_breaks(ref, win[3].tolist(), state)
    row = _row(*win)
    assert row["structure_bars_since_bos"] == bos
    assert row["structure_bars_since_choch"] == choch


# --------------------------------------------------------------------------------------------
# window-level failures
# --------------------------------------------------------------------------------------------

def test_a_non_positive_atr_makes_every_field_unknown():
    o, h, l, c, v = _bars([100.0] * WINDOW, spread=0.0)
    row = _row(o, h, l, c, v)
    for f in STRUCTURE_FIELDS:
        assert row[f] is None
        assert row[f"{f}_status"] == STATUS_INVALID_PRICES
    assert compute_chart_structure(o, h, l, c, v).dist_support.reason == f"atr<=0 at index {WINDOW - 1}"


def test_a_short_window_is_insufficient_history_and_a_long_one_is_a_programming_error():
    o, h, l, c, v = _bars([100.0 + i for i in range(WINDOW - 1)])
    row = _row(o, h, l, c, v)
    assert all(row[f] is None and row[f"{f}_status"] == STATUS_INSUFFICIENT_HISTORY
               for f in STRUCTURE_FIELDS)
    o, h, l, c, v = _bars([100.0 + i for i in range(WINDOW + 1)])
    with pytest.raises(ValueError, match="at most"):
        compute_chart_structure(o, h, l, c, v)


def test_one_bad_bar_fails_every_field_with_the_lowest_bad_index_named():
    closes = [100.0 + 0.3 * (i % 7) for i in range(WINDOW)]
    o, h, l, c, v = _bars(closes)
    c = c.copy()
    c[50] = float("nan")
    row = _row(o, h, l, c, v)
    for f in STRUCTURE_FIELDS:
        assert row[f] is None and row[f"{f}_status"] == STATUS_INVALID_PRICES
    assert "index 50" in compute_chart_structure(o, h, l, c, v).channel_pos.reason


def test_a_supplied_atr_series_must_match_the_window():
    closes = [100.0 + 0.3 * (i % 7) for i in range(WINDOW)]
    o, h, l, c, v = _bars(closes)
    assert compute_chart_structure(o, h, l, c, v, atr=atr14_wilder(h, l, c)).as_row() == _row(o, h, l, c, v)
    with pytest.raises(ValueError, match="atr length"):
        compute_chart_structure(o, h, l, c, v, atr=np.ones(5))


# --------------------------------------------------------------------------------------------
# registry (design 3.2 table)
# --------------------------------------------------------------------------------------------

_TABLE = [
    # name, short, kind, searched, value_min, value_max, value_step, ui_name
    ("structure_dist_support_atr", "dist-support", "numeric", True, 0.0, 5.0, 0.5, "Distance to support"),
    ("structure_dist_resistance_atr", "dist-resistance", "numeric", True, 0.0, 5.0, 0.5, "Distance to resistance"),
    ("structure_support_touches", "support-touches", "numeric", False, 1.0, 5.0, 1.0, "Support strength"),
    ("structure_resistance_touches", "resistance-touches", "numeric", False, 1.0, 5.0, 1.0, "Resistance strength"),
    ("channel_slope_20_atr", "chan-slope", "numeric", False, -0.30, 0.30, 0.05, "Channel slope"),
    ("channel_width_20_atr", "chan-width", "numeric", False, 1.0, 8.0, 0.5, "Channel width"),
    ("channel_pos_20", "chan-pos", "numeric", True, 0.0, 1.0, 0.1, "Position in channel"),
    ("close_vs_prior_high_20_atr", "vs-prior-high", "numeric", True, -3.0, 2.0, 0.25,
     "Close vs prior 20-session high"),
    ("close_vs_prior_low_20_atr", "vs-prior-low", "numeric", False, -2.0, 3.0, 0.25,
     "Close vs prior 20-session low"),
    ("structure_state", "structure", "categorical", True, None, None, None, "Swing structure"),
    ("structure_bars_since_bos", "bos", "numeric", False, 0.0, 60.0, 5.0,
     "Sessions since break of structure"),
    ("structure_bars_since_choch", "choch", "numeric", False, 0.0, 60.0, 5.0,
     "Sessions since change of character"),
]


def test_the_profile_registers_the_twelve_fields_of_the_design_table_in_order():
    assert PROFILES["ta-structure-v1"] is TA_STRUCTURE_V1
    assert TA_STRUCTURE_V1.calc_version == STRUCTURE_CALC_VERSION == "ta-structure-v1/calc-1"
    assert tuple(f.name for f in TA_STRUCTURE_V1.fields) == STRUCTURE_FIELDS
    assert [row[0] for row in _TABLE] == list(STRUCTURE_FIELDS)


@pytest.mark.parametrize("name,short,kind,searched,lo,hi,step,ui", _TABLE)
def test_each_field_spec_matches_the_design_table(name, short, kind, searched, lo, hi, step, ui):
    spec = field_spec(name)
    assert (spec.short, spec.kind, spec.searched, spec.ui_name) == (short, kind, searched, ui)
    assert (spec.value_min, spec.value_max, spec.value_step) == (lo, hi, step)


def test_exactly_five_fields_are_searched_and_they_are_the_design_subset():
    searched = {f.name for f in TA_STRUCTURE_V1.fields if f.searched}
    assert searched == {"structure_dist_support_atr", "structure_dist_resistance_atr",
                        "channel_pos_20", "close_vs_prior_high_20_atr", "structure_state"}


def test_structure_state_is_categorical_and_its_codes_exclude_none():
    spec = field_spec("structure_state")
    assert dict(spec.codes) == {"bull": 1, "bear": 2}
    assert "none" not in spec.codes
    assert (spec.value_min, spec.value_max, spec.value_step) == (None, None, None)
    assert spec.anchor_op is None and spec.anchor_value is None


def test_every_numeric_field_carries_an_anchor_inside_its_range():
    for spec in TA_STRUCTURE_V1.fields:
        if spec.kind != "numeric":
            continue
        assert spec.anchor_op in ("<", ">")
        assert spec.value_min <= spec.anchor_value <= spec.value_max


def test_the_profile_has_a_registered_calculator_producing_a_full_feature_row():
    closes, _ = _series([100, 112, 104, 118, 106, 120], seg=5)
    o, h, l, c, v = _bars(closes)
    row = COMPUTE_BY_PROFILE["ta-structure-v1"](o, h, l, c, v)
    assert set(row.by_field()) == set(STRUCTURE_FIELDS)
    assert set(row.calc_versions.values()) == {STRUCTURE_CALC_VERSION}


# --------------------------------------------------------------------------------------------
# golden rows (bit-for-bit)
# --------------------------------------------------------------------------------------------

#: ``field -> float.hex()`` (or None) for two crafted windows. These are not "whatever the code
#: printed": every field of both windows is independently checked by the tests above on the SAME
#: two series; the hex pins the last bit, so a change in a reduction order that keeps every
#: approx-comparison green still fails here.
_GOLDEN_POINTS = {
    "bull": [100, 110, 104, 118, 108, 122],
    "bear": [122, 108, 118, 104, 110, 100],
}
_GOLDEN = {
    "bear": {
        "structure_dist_support_atr": None,
        "structure_dist_resistance_atr": "0x1.eea465e48fb34p+1",
        "structure_support_touches": None,
        "structure_resistance_touches": "0x1.0000000000000p+0",
        "channel_slope_20_atr": "-0x1.c214971674204p-3",
        "channel_width_20_atr": "0x1.171bd657f4761p+2",
        "channel_pos_20": "0x1.145155a5a288cp-2",
        "close_vs_prior_high_20_atr": "-0x1.ab30e3a27c1adp+2",
        "close_vs_prior_low_20_atr": "-0x1.67bd616068826p-2",
        "structure_state": "0x1.0000000000000p+1",
        "structure_bars_since_bos": "0x0.0p+0",
        "structure_bars_since_choch": None,
    },
    "bull": {
        "structure_dist_support_atr": "0x1.32f58456765fcp+2",
        "structure_dist_resistance_atr": None,
        "structure_support_touches": "0x1.0000000000000p+0",
        "structure_resistance_touches": None,
        "channel_slope_20_atr": "0x1.83fbe2e32fa81p-3",
        "channel_width_20_atr": "0x1.25778f5b5bbdep+2",
        "channel_pos_20": "0x1.a3363db28c3c7p-1",
        "close_vs_prior_high_20_atr": "0x1.26ae419aaf13cp-1",
        "close_vs_prior_low_20_atr": "0x1.84d0968fa701ep+2",
        "structure_state": "0x1.0000000000000p+0",
        "structure_bars_since_bos": "0x0.0p+0",
        "structure_bars_since_choch": None,
    },
}


@pytest.mark.parametrize("key", sorted(_GOLDEN))
def test_golden_rows_are_bit_for_bit_stable(key):
    closes, _ = _series(_GOLDEN_POINTS[key], seg=5)
    o, h, l, c, v = _bars(closes)
    row = _row(o, h, l, c, v)
    got = {f: (None if row[f] is None else float(row[f]).hex()) for f in STRUCTURE_FIELDS}
    assert got == _GOLDEN[key]
    # Pinned HERE, next to the hex: a row whose bits changed is a new calculator, and the two
    # decisions -- re-paste the goldens, bump the calc version -- have to be taken together or
    # a warmed store will serve rows no longer produced by the version it is labelled with.
    assert row["calc_version"] == "ta-structure-v1/calc-1"
