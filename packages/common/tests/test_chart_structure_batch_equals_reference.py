"""The ta-structure-v1 batch form reproduces the per-window reference EXACTLY (design 3.3, D8.15).

"Agree exactly" is the requirement, not "agree to a tolerance": the batch is what warmup writes
into the feature store and the reference is what the contract says the store must contain, so a
single differing bit is a silently different strategy.

The sharpest test here is :func:`test_adding_a_future_bar_changes_no_earlier_row` -- a batch that
took its pivots from the whole history (rather than from the pivots each window had CONFIRMED)
passes the equality test on a fixed history and fails this one.
"""
import math
import os
import time

import numpy as np
import pytest

from ba2_common.core.market_conditions import (
    CHANNEL_LOOKBACK,
    PIVOT_K,
    STATUS_INVALID_PRICES,
    STRUCTURE_FIELDS,
    WINDOW,
    compute_chart_structure,
    fit_channel,
)
from ba2_common.core.market_conditions_batch import (
    chart_structure_rows,
    compute_chart_structure_batch,
)

_AAPL = os.path.expanduser("~/Documents/ba2/common/cache/FMPOHLCVProvider/AAPL_1d.parquet")


def _ohlcv(closes, *, spread=1.0):
    c = np.asarray(closes, dtype=float)
    return c.copy(), c + spread, c - spread, c, np.full(len(c), 1000.0)


def _random_walk(n=600, seed=11):
    rng = np.random.default_rng(seed)
    return _ohlcv(100.0 + np.cumsum(rng.normal(0.0, 1.2, n)))


def _trend_with_noise(n=600, seed=22):
    rng = np.random.default_rng(seed)
    base = np.linspace(50.0, 180.0, n)
    return _ohlcv(base + rng.normal(0.0, 2.5, n))


def _degenerate(n=600, seed=33):
    """Flat runs (ties -> no pivots, sigma == 0 channels), a zero-range stretch that drives ATR to
    zero, and a gap. Every unusual branch of the profile in one history."""
    rng = np.random.default_rng(seed)
    c = list(100.0 + np.cumsum(rng.normal(0.0, 1.0, 200)))
    c += [c[-1]] * 160                       # a long flat run
    c += list(c[-1] + np.cumsum(rng.normal(0.0, 1.5, 100)))
    c += [c[-1] + 40.0]                      # a gap
    c += list(c[-1] + np.cumsum(rng.normal(0.0, 0.8, n - len(c))))
    c = np.asarray(c, dtype=float)
    o, h, l, cc, v = _ohlcv(c)
    h = h.copy()
    l = l.copy()
    h[200:340] = cc[200:340]                 # zero-range bars -> ATR decays to 0 inside the run
    l[200:340] = cc[200:340]
    return o, h, l, cc, v


_HISTORIES = {
    "random_walk": _random_walk,
    "trend_with_noise": _trend_with_noise,
    "degenerate": _degenerate,
}


def _reference_rows(o, h, l, c, v):
    return [None if e < WINDOW - 1
            else compute_chart_structure(*[a[e - WINDOW + 1:e + 1] for a in (o, h, l, c, v)])
            for e in range(len(c))]


@pytest.mark.parametrize("name", sorted(_HISTORIES))
def test_batch_equals_reference_for_every_session_and_field(name):
    o, h, l, c, v = _HISTORIES[name]()
    got = chart_structure_rows(o, h, l, c, v)
    want = _reference_rows(o, h, l, c, v)
    assert len(got) == len(want) == len(c)
    for e, (a, b) in enumerate(zip(got, want)):
        assert a == b, f"{name}: row {e} differs\nbatch={a}\nref  ={b}"
    # ... and the values are bit-identical, not merely "equal dataclasses" by some loose rule.
    for e in range(WINDOW - 1, len(c)):
        for field, obs in got[e].by_field().items():
            ref = want[e].by_field()[field]
            assert obs.status == ref.status and obs.reason == ref.reason
            if obs.value is None:
                assert ref.value is None
            else:
                assert obs.value.hex() == ref.value.hex()


def test_the_degenerate_history_actually_exercises_the_unusual_branches():
    o, h, l, c, v = _degenerate()
    rows = [r for r in chart_structure_rows(o, h, l, c, v) if r is not None]
    statuses = {f: {r.by_field()[f].status for r in rows} for f in STRUCTURE_FIELDS}
    assert STATUS_INVALID_PRICES in statuses["channel_width_20_atr"], "no sigma == 0 window"
    assert any(all(o_.status == STATUS_INVALID_PRICES for o_ in r.by_field().values())
               and "atr<=0" in r.dist_support.reason for r in rows), "no atr<=0 window"
    assert any(r.structure_state.value == 0.0 for r in rows), "no 'none' structure"
    assert any(r.dist_resistance.value is not None for r in rows), "no resistance ever resolved"
    assert any(r.dist_resistance.value is None and r.dist_resistance.status != STATUS_INVALID_PRICES
               for r in rows), "no window without a resistance above the close"


def test_rows_before_the_first_full_window_are_absent():
    o, h, l, c, v = _random_walk(n=WINDOW + 3)
    rows = chart_structure_rows(o, h, l, c, v)
    assert rows[:WINDOW - 1] == [None] * (WINDOW - 1)
    assert all(r is not None for r in rows[WINDOW - 1:])
    assert chart_structure_rows(*_random_walk(n=WINDOW - 1)) == [None] * (WINDOW - 1)


def test_an_invalid_bar_only_fails_the_windows_that_contain_it():
    o, h, l, c, v = _random_walk(n=500)
    c = c.copy()
    c[300] = -1.0                       # non-positive close
    got = chart_structure_rows(o, h, l, c, v)
    want = _reference_rows(o, h, l, c, v)
    assert got == want
    assert got[299].dist_support.status != STATUS_INVALID_PRICES
    assert got[300].dist_support.status == STATUS_INVALID_PRICES
    assert "index" in got[300].dist_support.reason
    # The reason is renumbered to the WINDOW's own indices, never the history's.
    assert got[300].dist_support.reason.startswith(f"index {WINDOW - 1} (close non-positive")
    assert got[300 + WINDOW - 1].dist_support.reason.startswith("index 0 (close non-positive")
    assert got[300 + WINDOW].dist_support.status != STATUS_INVALID_PRICES


def test_adding_a_future_bar_changes_no_earlier_row():
    o, h, l, c, v = _random_walk(n=400)
    base = chart_structure_rows(o, h, l, c, v)
    # A new bar that is a strict extreme: a batch that confirmed pivots from the whole history
    # would let it rewrite the last PIVOT_K rows.
    for extra in (float(c.max()) + 25.0, float(c.min()) - 25.0):
        o2, h2, l2, c2, v2 = _ohlcv(np.append(c, extra))
        grown = chart_structure_rows(o2, h2, l2, c2, v2)
        assert grown[:len(c)] == base
    # Many bars at once, still no rewrite of the past.
    rng = np.random.default_rng(7)
    o3, h3, l3, c3, v3 = _ohlcv(np.append(c, c[-1] + np.cumsum(rng.normal(0.0, 3.0, 40))))
    assert chart_structure_rows(o3, h3, l3, c3, v3)[:len(c)] == base


def test_the_feature_row_form_matches_the_values_form():
    o, h, l, c, v = _random_walk(n=300)
    values = chart_structure_rows(o, h, l, c, v)
    rows = compute_chart_structure_batch(o, h, l, c, v)
    assert len(rows) == len(values)
    for a, b in zip(rows, values):
        if b is None:
            assert a is None
            continue
        assert dict(a.by_field()) == b.by_field()
        assert set(a.calc_versions.values()) == {b.calc_version}


@pytest.mark.skipif(not os.path.exists(_AAPL), reason="the local FMP daily cache has no AAPL")
def test_batch_equals_reference_on_a_real_symbol():
    import pandas as pd

    df = pd.read_parquet(_AAPL).sort_values("Date").tail(1200)
    o, h, l, c, v = (df[k].to_numpy(dtype=float) for k in ("Open", "High", "Low", "Close", "Volume"))
    assert chart_structure_rows(o, h, l, c, v) == _reference_rows(o, h, l, c, v)


def test_cold_build_and_lookup_cost_are_reported(capsys):
    """D8.15: measure, print, and refuse a cost that is obviously off (not a benchmark gate)."""
    if os.path.exists(_AAPL):
        import pandas as pd

        df = pd.read_parquet(_AAPL).sort_values("Date")
        o, h, l, c, v = (df[k].to_numpy(dtype=float)
                         for k in ("Open", "High", "Low", "Close", "Volume"))
        label = f"AAPL ({len(c)} bars)"
    else:
        o, h, l, c, v = _random_walk(n=3800)
        label = f"synthetic ({len(c)} bars)"

    t0 = time.perf_counter()
    rows = chart_structure_rows(o, h, l, c, v)
    cold_ms = (time.perf_counter() - t0) * 1e3

    built = [r for r in rows if r is not None]
    t0 = time.perf_counter()
    reps = 20000
    for i in range(reps):
        built[i % len(built)].by_field()["channel_pos_20"].value
    lookup_us = (time.perf_counter() - t0) / reps * 1e6

    with capsys.disabled():
        print(f"\n[ta-structure-v1] cold batch build {label}: {cold_ms:.1f} ms "
              f"({cold_ms / max(1, len(built)):.3f} ms/row, {len(built)} rows); "
              f"stored-row field lookup {lookup_us:.3f} us")
    assert cold_ms / max(1, len(built)) < 5.0, "a batch row must cost well under a millisecond"
    assert lookup_us < 20.0
    assert math.isfinite(cold_ms)


def test_a_sub_span_batch_equals_the_full_history_batch_for_the_rows_it_covers():
    """What the store build relies on: it runs the batch over the span ONE month of rows needs,
    not the whole cached history, and the rows must be the same ones."""
    o, h, l, c, v = _random_walk(n=600)
    full = chart_structure_rows(o, h, l, c, v)
    lo, hi = 200, 260                      # rows ending at 327..386 (each needs bars lo..hi+126)
    span = [a[lo:hi + WINDOW] for a in (o, h, l, c, v)]
    part = chart_structure_rows(*span)
    for e in range(WINDOW - 1, len(part)):
        assert part[e] == full[e + lo]


# --------------------------------------------------------------------------------------------
# the one deliberate deviation from design 3.3's "batch form" recipe, as EVIDENCE
# --------------------------------------------------------------------------------------------
def _cumulative_sum_channel_fit(closes: np.ndarray):
    """Design 3.3's prescribed channel: "regression via cumulative sums of x, y, x^2, xy over
    the rolling 20". One pass of running sums over the whole history, then each window's fit by
    subtraction, with sigma from the normal-equation identity
    ``Sum e^2 = Syy - a*Sy - b*Sxy``.

    Lives in the test, not in the module: it is the thing being ruled OUT."""
    c = np.asarray(closes, dtype=float)
    n = len(c)
    js = np.arange(n, dtype=np.float64)
    sy = np.concatenate(([0.0], np.cumsum(c)))
    sjy = np.concatenate(([0.0], np.cumsum(js * c)))
    syy = np.concatenate(([0.0], np.cumsum(c * c)))
    m = float(CHANNEL_LOOKBACK)
    sx = m * (m - 1) / 2.0
    sxx = (m - 1) * m * (2 * m - 1) / 6.0
    out = []
    for e in range(CHANNEL_LOOKBACK - 1, n):
        s = e - CHANNEL_LOOKBACK + 1
        y = sy[e + 1] - sy[s]
        jy = sjy[e + 1] - sjy[s]
        yy = syy[e + 1] - syy[s]
        xy = jy - s * y
        b = (m * xy - sx * y) / (m * sxx - sx * sx)
        a = (y - b * sx) / m
        out.append((a, b, math.sqrt(max(yy - a * y - b * xy, 0.0) / (CHANNEL_LOOKBACK - 2))))
    return out


def test_cumulative_sum_ols_is_not_bit_exact_so_the_batch_fits_per_session():
    """WHY the batch deviates from design 3.3 on this one structure.

    "Agree exactly" is the requirement, and the cumulative-sum form does not: every row differs,
    and the error is not in the last bit. sigma falls out of a cancellation
    (``Syy - a*Sy - b*Sxy``) between quantities of order price^2 while the residuals are of order
    a few ticks, and the running sums span the whole history rather than twenty points. A drift
    of ~1e-7 relative in a value the GA compares against a threshold is a different decision, not
    a rounding detail -- so the batch re-fits per session with the same ``math.fsum`` reductions
    the reference uses, and this test is the evidence rather than an omission.
    """
    histories = {name: build()[3] for name, build in sorted(_HISTORIES.items())}
    if os.path.exists(_AAPL):
        import pandas as pd

        histories["AAPL"] = pd.read_parquet(_AAPL).sort_values("Date")["Close"].to_numpy(float)

    worst = 0.0
    for name, closes in histories.items():
        got = _cumulative_sum_channel_fit(closes)
        differing = 0
        for k, e in enumerate(range(CHANNEL_LOOKBACK - 1, len(closes))):
            want = fit_channel(closes[e - CHANNEL_LOOKBACK + 1:e + 1].tolist())
            if got[k] != want:
                differing += 1
                for g, w in zip(got[k], want):
                    worst = max(worst, abs(g - w) / (abs(w) or 1.0))
        assert differing, f"{name}: the cumulative-sum fit was bit-exact here -- re-open the choice"
    assert worst > 1e-9, f"the drift is only {worst:.3e}; re-open the choice if it is this small"


def test_the_rolling_level_lists_survive_a_row_the_batch_declines_to_compute():
    """The sorted level lists slide with the window even for rows that are skipped (an invalid
    bar, a non-positive ATR). Leaving the state behind on a skip would silently mis-level every
    later row -- and 'later' is where the trades are."""
    o, h, l, c, v = _random_walk(n=600)
    c = c.copy()
    c[300] = float("nan")                  # every window containing bar 300 is skipped
    got = chart_structure_rows(o, h, l, c, v)
    want = _reference_rows(o, h, l, c, v)
    assert got == want
    after = got[300 + WINDOW]
    assert after.dist_support.status != STATUS_INVALID_PRICES or after.dist_resistance.value is None
    assert any(r is not None and r.dist_resistance.value is not None
               for r in got[300 + WINDOW:]), "no level resolved after the skipped run"
