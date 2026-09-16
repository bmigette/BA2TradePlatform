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
ATR series, the confirmed pivots) and then calls ``_chart_structure_core`` -- the same function
the reference calls, on the same window-local lists. Sharing the arithmetic is what makes the
equality a property of the code; ``tests/test_chart_structure_batch_equals_reference.py`` pins it
against the reference on synthetic and real histories anyway, because a refactor could break the
sharing without breaking an import.

WHAT IS ACTUALLY SHARED ACROSS ROWS (the speed-up):

* the per-bar validity checks (``_invalid_bar_checks``) run ONCE over the full history; a window
  reports the lowest bad index inside its own span, renumbered to its local 0..127;
* the confirmed pivots are found ONCE over the full history. A pivot at p is a strict extreme
  against its K neighbours on both sides, which is a LOCAL property of bars p-K..p+K, so the
  pivots a window sees are exactly the global pivots with ``s + K <= p <= e - K`` -- sliced, never
  recomputed, and NEVER a pivot the future confirms (that is
  ``test_adding_a_future_bar_changes_no_earlier_row``);
* the prior-range and channel reductions come from the sliced window, where they cost 20 points.

WHAT IS *NOT* SHARED, and why. ATR14 is a Wilder recursion SEEDED INSIDE EACH WINDOW (design
section 3.1 "stable initialization": a session's output is computed from exactly its last 128
eligible bars, so live and BT agree whatever prehistory each happens to hold). ATR[127] is the
divisor of every field here, so it is recomputed per window -- a global ATR would be a different
number and the profile would no longer be window-invariant. Section 3.3's "none of these fields
is recursive" is true of the STRUCTURE measurements and not of the ATR they are divided by; this
module resolves that by paying the 114-step recursion per row, which is the dominant cost and
still one pass over the history.
"""
from __future__ import annotations

from bisect import bisect_left, bisect_right
from typing import Callable, Dict, List, Optional

import numpy as np

from ba2_common.core.market_conditions import (
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
)

__all__ = ["BATCH_BY_PROFILE", "chart_structure_rows", "compute_chart_structure_batch"]


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

    hl, ll, cl = h.tolist(), l.tolist(), c.tolist()
    pivots = find_pivots(hl, ll, PIVOT_K)
    pivot_at = [p.index for p in pivots]

    last_local = WINDOW - 1
    for e in range(WINDOW - 1, n):
        s = e - WINDOW + 1
        # Lowest invalid bar inside THIS window, reported at its window-local index.
        k = bisect_left(bad_at, s)
        if k < len(bad_at) and bad_at[k] <= e:
            g = bad_at[k]
            out[e] = _all_structure(STATUS_INVALID_PRICES, _invalid_bar_label(checks, g, g - s))
            continue
        atr_last = float(atr14_wilder(h[s:e + 1], l[s:e + 1], c[s:e + 1])[last_local])
        if not atr_last > 0:
            out[e] = _all_structure(STATUS_INVALID_PRICES, f"atr<=0 at index {last_local}")
            continue
        lo = bisect_left(pivot_at, s + PIVOT_K)
        hi = bisect_right(pivot_at, e - PIVOT_K)
        local = [Pivot(p.index - s, p.kind, p.price) for p in pivots[lo:hi]]
        out[e] = _chart_structure_core(hl[s:e + 1], ll[s:e + 1], cl[s:e + 1], local, atr_last)
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
