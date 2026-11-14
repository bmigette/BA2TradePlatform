"""FMP grades-historical rows -> rating-bucket counts: ONE reader for every expert.

FMP ``stable/grades-historical`` rows carry the verbose names ``analystRatingsStrongBuy`` /
``analystRatingsBuy`` / ``analystRatingsHold`` / ``analystRatingsSell`` /
``analystRatingsStrongSell`` (live responses and the backtest disk cache are the same raw JSON).
FMP has also spelled the buy bucket ``analystRatingsbuy``, and the consensus endpoints use the
short names (``strongBuy`` ...), so every spelling maps here. FMPRating's as-of reconstruction
and DeterministicScorer's grades leg both read a row through ``grade_bucket_counts`` -- a second,
private mapping is how DS ended up reading short names only and scoring no row at all.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

_logger = logging.getLogger(__name__)

#: bucket -> accepted field names, first present wins.
GRADES_FIELD_ALIASES: Dict[str, tuple] = {
    "strongBuy": ("analystRatingsStrongBuy", "strongBuy"),
    "buy": ("analystRatingsBuy", "analystRatingsbuy", "buy"),
    "hold": ("analystRatingsHold", "hold"),
    "sell": ("analystRatingsSell", "sell"),
    "strongSell": ("analystRatingsStrongSell", "strongSell"),
}

_WARNED_SHAPES: set = set()


def grade_bucket_counts(row: Dict[str, Any]) -> Optional[Dict[str, int]]:
    """``{strongBuy, buy, hold, sell, strongSell}`` counts of one grades row, or None.

    None when any bucket has no field under a known name or a non-integer value: that is a
    schema change, not "zero analysts", and is never read as zeros. A real zero count is 0.
    """
    out: Dict[str, int] = {}
    for bucket, names in GRADES_FIELD_ALIASES.items():
        value = next((row[n] for n in names if row.get(n) is not None), None)
        if value is None:
            return None
        try:
            out[bucket] = int(value)
        except (TypeError, ValueError):
            return None
    return out


def warn_unrecognised_row(where: str, row: Dict[str, Any]) -> None:
    """WARN once per (caller, field set) about a grades row ``grade_bucket_counts`` refused."""
    shape = (where, tuple(sorted(str(k) for k in row)))
    if shape in _WARNED_SHAPES:
        return
    _WARNED_SHAPES.add(shape)
    _logger.warning(f"{where}: grades-historical row without readable rating buckets "
                    f"(fields {list(shape[1])}); the row is not scored. A changed FMP schema "
                    f"needs a new alias in ba2_experts.analyst_grades.GRADES_FIELD_ALIASES")
