"""Pure signal math for the PremiumSeller expert (spec §4-§5).

Every function returns None on insufficient/invalid input — the caller treats
None as "cannot evaluate" and SKIPS the trade or the gate (never a fabricated
number; project convention: no silent fallbacks for money/vol values).
"""
from __future__ import annotations

import math
from datetime import date, timedelta
from typing import Iterable, List, Optional

IV_RANK_MIN_POINTS = 20

# FMP grades-historical grade strings -> ordinal score (5 best .. 1 worst).
# Unknown strings score None: the rating floor only excludes KNOWN-bad names.
_GRADE_SCORE = {
    "strong buy": 5.0, "buy": 4.0, "outperform": 4.0, "overweight": 4.0,
    "positive": 4.0, "market outperform": 4.0,
    "neutral": 3.0, "hold": 3.0, "market perform": 3.0, "equal weight": 3.0,
    "sector perform": 3.0, "peer perform": 3.0,
    "sell": 2.0, "underperform": 2.0, "underweight": 2.0, "negative": 2.0,
    "strong sell": 1.0,
}


def iv_rank(history: Iterable[Optional[float]], current: Optional[float]) -> Optional[float]:
    """IVR: % of historical points <= current IV (0-100). None when < 20 valid
    points or current is None — the gate must fail closed (caller skips the
    filter decision per its own rule)."""
    vals = [v for v in history if v is not None]
    if current is None or len(vals) < IV_RANK_MIN_POINTS:
        return None
    below = sum(1 for v in vals if v <= current)
    return 100.0 * below / len(vals)


def realized_vol_annualized(closes: List[float], window: int) -> Optional[float]:
    """Annualized stdev of log returns over the last `window` closes (0-1 scale)."""
    if len(closes) < window + 1:
        return None
    seg = closes[-(window + 1):]
    rets = [math.log(seg[i] / seg[i - 1]) for i in range(1, len(seg)) if seg[i - 1] > 0 and seg[i] > 0]
    if len(rets) < 2:
        return None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(var * 252)


def sma(values: List[float], n: int) -> Optional[float]:
    if len(values) < n:
        return None
    return sum(values[-n:]) / n


def grade_score(grade: Optional[str]) -> Optional[float]:
    if not grade:
        return None
    return _GRADE_SCORE.get(str(grade).strip().lower())


def analyst_counts_score(row: Optional[dict]) -> Optional[float]:
    """Weighted analyst-consensus score (1-5) from a grades-historical row's
    aggregate counts. None when the row carries no usable counts.

    Key spellings mirror FMPRating._GRADES_FIELD_ALIASES (stable/grades-historical
    rows carry a ``date`` plus analystRatings* counts — no per-grade strings)."""
    if not row:
        return None
    def _n(*keys):
        for k in keys:
            v = row.get(k)
            if isinstance(v, (int, float)):
                return float(v)
        return 0.0
    sb = _n("analystRatingsStrongBuy", "analystRatingsStrongbuy", "strongBuy")
    b = _n("analystRatingsbuy", "analystRatingsBuy", "buy")
    h = _n("analystRatingsHold", "hold")
    s = _n("analystRatingsSell", "sell")
    ss = _n("analystRatingsStrongSell", "strongSell")
    total = sb + b + h + s + ss
    if total <= 0:
        return None
    return (5.0 * sb + 4.0 * b + 3.0 * h + 2.0 * s + 1.0 * ss) / total


def earnings_within(report_dates: Iterable[date], as_of: date, window_days: int) -> bool:
    """True iff any (eventual) report date falls inside (as_of, as_of + window_days].

    Uses REPORTED dates as the approximation of the scheduled date (spec §9):
    schedules drift by a few days, immaterial for a 30-45 DTE exclusion window.
    """
    end = as_of + timedelta(days=window_days)
    return any(as_of < d <= end for d in report_dates)
