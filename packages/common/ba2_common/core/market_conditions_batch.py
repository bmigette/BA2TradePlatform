"""Full-history batch form of the ``ta-structure-v1`` chart-structure profile (design
``docs/plans/2026-09-15-option-market-condition-genes-design.md`` section 3.3,
"Precomputation, batch form").

WHY IT EXISTS. Every field of section 3.2 is precomputed once per (symbol, session) at warmup
and read back by one array lookup at decision time. The per-window reference
(``market_conditions.compute_chart_structure``) is the contract; this module produces the SAME
rows for a whole cached history in one pass, so a warmup that has ten years of bars does not pay
the per-window set-up cost ten thousand times.

THE ONE RULE. ``batch == reference``, exactly -- not "to within a tolerance". That is why this
module does not re-derive a single measurement: it prepares the per-window inputs (validity, the
ATR series, the confirmed pivots, the level lists, the prior range) and then calls
``_chart_structure_core`` -- the same function the reference calls, on the same window-local
lists. Sharing the arithmetic is what makes the equality a property of the code;
``tests/test_chart_structure_batch_equals_reference.py`` pins it against the reference on
synthetic and real histories anyway, because a refactor could break the sharing without breaking
an import.

THE ROLLING STRUCTURES SECTION 3.3 PRESCRIBES, and what each is allowed to be:

* **pivots from K-shifted comparisons, confirmed at p + K** -- found ONCE over the full history.
  A pivot is a strict extreme against bars p-K..p+K, a purely LOCAL property, so the pivots a
  window sees are exactly the global ones with ``s + K <= p <= e - K``: sliced, never recomputed,
  and never a pivot the future confirms (that is
  ``test_adding_a_future_bar_changes_no_earlier_row``).
* **nearest-level queries against the sorted confirmed levels** -- two sorted price lists are
  carried across the sliding window (``insort`` what session ``e`` confirms, drop what leaves at
  ``s``) and handed to the shared ``levels_from_sorted``. Both queries are BISECTS on a sorted
  list: selections, not arithmetic, so they return the same element ``min``/``max`` would.
* **prior range via rolling max/min shifted by one** -- one vectorised pass per array, read as
  ``win[e - 20]`` for the row ending at ``e``. Also a selection: the maximum of twenty floats is
  the same float however it is found.
* **regression via cumulative sums of x, y, x^2, xy over the rolling 20** -- NOT USED, and this
  is the one deliberate deviation from section 3.3. It was implemented and measured against the
  fsum reference (``test_cumulative_sum_ols_is_not_bit_exact_so_the_batch_fits_per_session``):
  EVERY row differs, by up to 4.3e-07 relative on AAPL, because sigma comes out of the
  ``Syy - a*Sy - b*Sxy`` cancellation and the running sums span the whole history rather than
  twenty points. The plan's instruction is explicit that correctness beats the shortcut, and a
  4e-07 drift in a value the GA compares against a threshold is a different decision, not a
  rounding detail. The channel is therefore re-fitted per session with the same ``math.fsum``
  reductions the reference uses, and the test above exists so the deviation is evidence rather
  than an omission.

WHAT IS *NOT* SHARED, and why. ATR14 is a Wilder recursion SEEDED INSIDE EACH WINDOW (design
section 3.1 "stable initialization": a session's output is computed from exactly its last 128
eligible bars, so live and BT agree whatever prehistory each happens to hold). ATR[127] is the
divisor of every field here, so it is recomputed per window -- a global ATR would be a different
number and the profile would no longer be window-invariant. Section 3.3's "none of these fields
is recursive" is true of the STRUCTURE measurements and not of the ATR they are divided by; this
module pays the 114-step recursion per row, which is the dominant cost and the reason the
measured cold build is 75 us/row (~0.3 s for a fully-warmed symbol) rather than section 3.3's
estimated ~10 ms per SYMBOL. The measured figures are recorded in
``reports/strategy_research/market_conditions_bench_2026-09-16.md`` section 6b.
"""
from __future__ import annotations

from bisect import bisect_left, insort
from typing import Callable, Dict, List, Optional

import numpy as np

from ba2_common.core.market_conditions import (
    CHANNEL_LOOKBACK,
    PIVOT_HIGH,
    PIVOT_K,
    STATUS_INVALID_PRICES,
    TA_STRUCTURE_V1,
    WINDOW,
    ChartStructureValues,
    FeatureRow,
    Pivot,
    _all_structure,
    _chart_structure_core,
    _f64,
    _invalid_bar_checks,
    _invalid_bar_label,
    atr14_wilder,
    find_pivots,
    levels_from_sorted,
)

__all__ = ["BATCH_BY_PROFILE", "chart_structure_rows", "compute_chart_structure_batch"]


def _rolling_prior_range(h: np.ndarray, l: np.ndarray) -> tuple:
    """``(max H, min L)`` over each ``CHANNEL_LOOKBACK``-bar window, indexed by its FIRST bar.

    The row ending at ``e`` reads ``out[e - CHANNEL_LOOKBACK]``, which is the range of the
    sessions before it -- "shifted by one" in section 3.3's phrasing, and the reason a close
    above it is a breakout of a range that existed before the close."""
    from numpy.lib.stride_tricks import sliding_window_view

    return (sliding_window_view(h, CHANNEL_LOOKBACK).max(axis=1),
            sliding_window_view(l, CHANNEL_LOOKBACK).min(axis=1))


def chart_structure_rows(o, h, l, c, v) -> List[Optional[ChartStructureValues]]:
    """One entry per bar of the supplied history, aligned to the input index.

    ``out[e]`` is what ``compute_chart_structure`` returns for the window of the ``WINDOW`` bars
    ENDING at ``e`` -- and ``None`` for ``e < WINDOW - 1``, where no such window exists. The
    caller decides which rows its calendar considers valid (design section 4); this function
    knows only bars.
    """
    o, h, l, c, v = _f64(o), _f64(h), _f64(l), _f64(c), _f64(v)
    lengths = {len(o), len(h), len(l), len(c), len(v)}
    if len(lengths) != 1:
        raise ValueError(f"OHLCV arrays must have equal lengths, got {sorted(lengths)}")
    n = len(c)
    out: List[Optional[ChartStructureValues]] = [None] * n
    if n < WINDOW:
        return out

    checks = _invalid_bar_checks(o, h, l, c, v)
    any_bad = np.zeros(n, dtype=bool)
    for _, mask in checks:
        any_bad |= mask
    bad_at = np.flatnonzero(any_bad).tolist()
    next_bad = 0                      # bad_at[next_bad:] are the bad bars at or after ``s``

    hl, ll, cl = h.tolist(), l.tolist(), c.tolist()
    pivots = find_pivots(hl, ll)
    roll_hi, roll_lo = _rolling_prior_range(h, l)

    #: The confirmed levels of the CURRENT window, sorted: ``insort`` what each session confirms,
    #: drop what falls out of the window behind it. ``added`` and ``dropped`` bound the slice of
    #: ``pivots`` these lists hold, which is also the pivot list the swing/break walk needs.
    highs_sorted: List[float] = []
    lows_sorted: List[float] = []
    added = dropped = 0

    last_local = WINDOW - 1
    for e in range(WINDOW - 1, n):
        s = e - WINDOW + 1
        # Slide the confirmed-level window FIRST, before any skip: a row this function declines
        # to compute must not leave the rolling state describing some earlier window.
        while added < len(pivots) and pivots[added].index <= e - PIVOT_K:
            p = pivots[added]
            insort(highs_sorted if p.kind == PIVOT_HIGH else lows_sorted, p.price)
            added += 1
        while dropped < added and pivots[dropped].index < s + PIVOT_K:
            p = pivots[dropped]
            prices = highs_sorted if p.kind == PIVOT_HIGH else lows_sorted
            i = bisect_left(prices, p.price)
            if i >= len(prices) or prices[i] != p.price:
                # The sliding invariant is that whatever was inserted is still there to remove.
                # Deleting "whatever bisect landed on" instead would take a DIFFERENT price out of
                # the level list and mis-level every later row, quietly.
                raise AssertionError(
                    f"market-condition batch: pivot {p.kind} {p.price!r} at index {p.index} is "
                    f"not in the {p.kind} level list of the window ending at {e}; the sliding "
                    f"window state is broken")
            del prices[i]
            dropped += 1
        while next_bad < len(bad_at) and bad_at[next_bad] < s:
            next_bad += 1

        # Lowest invalid bar inside THIS window, reported at its window-local index.
        if next_bad < len(bad_at) and bad_at[next_bad] <= e:
            g = bad_at[next_bad]
            out[e] = _all_structure(STATUS_INVALID_PRICES, _invalid_bar_label(checks, g, g - s))
            continue
        atr_last = float(atr14_wilder(h[s:e + 1], l[s:e + 1], c[s:e + 1])[last_local])
        if not atr_last > 0:
            out[e] = _all_structure(STATUS_INVALID_PRICES, f"atr<=0 at index {last_local}")
            continue
        local = [Pivot(p.index - s, p.kind, p.price) for p in pivots[dropped:added]]
        out[e] = _chart_structure_core(
            hl[s:e + 1], ll[s:e + 1], cl[s:e + 1], local, atr_last,
            levels=levels_from_sorted(highs_sorted, lows_sorted, cl[e], atr_last),
            prior_range=(float(roll_hi[e - CHANNEL_LOOKBACK]), float(roll_lo[e - CHANNEL_LOOKBACK])))
    return out


def compute_chart_structure_batch(o, h, l, c, v) -> List[Optional[FeatureRow]]:
    """:func:`chart_structure_rows` as the field-generic ``FeatureRow`` the store writes."""
    return [None if r is None else r.to_feature_row() for r in chart_structure_rows(o, h, l, c, v)]


#: profile name -> ``fn(o, h, l, c, v) -> [FeatureRow | None]``, one entry per input bar, aligned
#: to the input index (``None`` before the first full window). The store build looks a profile up
#: here and falls back to the per-window ``COMPUTE_BY_PROFILE`` calculator when it is absent, so
#: adding a batch form is a registration rather than a new code path in the builder.
BATCH_BY_PROFILE: Dict[str, Callable[..., List[Optional[FeatureRow]]]] = {
    TA_STRUCTURE_V1.name: compute_chart_structure_batch,
}
