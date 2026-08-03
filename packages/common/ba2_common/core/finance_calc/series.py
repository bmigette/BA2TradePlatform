"""
Vendored from FinanceHarness (https://github.com/Yijia-Xiao/FinanceHarness),
Apache-2.0 license. Modified for BA2TradePlatform: ToolSpec/ToolResponse layer
removed, ToolError replaced with ValueError, imports localized.

Shared numeric-series helpers for the compute tools — pure, no network."""

from __future__ import annotations


def percentile(sorted_xs: list[float], q: float) -> float:
    """Linear-interpolation percentile (``q`` in [0, 1]) over an ALREADY-SORTED list.

    The caller sorts (so a hot path that already holds a sorted series doesn't re-sort).
    """
    idx = q * (len(sorted_xs) - 1)
    lo = int(idx)
    if lo + 1 >= len(sorted_xs):
        return sorted_xs[-1]
    return sorted_xs[lo] * (1 - (idx - lo)) + sorted_xs[lo + 1] * (idx - lo)
