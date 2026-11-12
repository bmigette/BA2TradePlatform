"""Split-adjusted analyst price targets (one rule for every consumer, live and backtest).

FMP ``v4/price-target`` rows carry ``priceTarget`` AS PUBLISHED (unsplit: NVDA 2024-05-23 =
1200) and ``adjPriceTarget`` adjusted to TODAY's split basis (= 120). Prices everywhere in the
platform (backtest bars, live account quotes) are on today's basis, so the only comparable
target is ``adjPriceTarget``. Measured 2026-11: 85,771 cached rows over 5,046 symbols, none
lacking ``adjPriceTarget``.

Rule for a row WITHOUT a usable ``adjPriceTarget`` (missing, null, non-numeric, non-positive):
the row is SKIPPED, never replaced by the unsplit ``priceTarget``. Callers count the skips and
call :func:`log_skipped_unadjusted` so the loss is visible.
"""
from __future__ import annotations

import math
from typing import Any, Mapping, Optional

from ba2_common.logger import logger


def split_adjusted_target(row: Mapping[str, Any]) -> Optional[float]:
    """The split-adjusted target of a price-target row, or None when it has none."""
    raw = row.get("adjPriceTarget")
    if raw is None:
        return None
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) and v > 0 else None


def log_skipped_unadjusted(where: str, skipped: int) -> None:
    """One WARNING per call site when rows were skipped for lacking ``adjPriceTarget``."""
    if skipped:
        logger.warning(f"{where}: skipped {skipped} price-target row(s) without a usable "
                       f"adjPriceTarget (the unsplit priceTarget is never substituted)")
