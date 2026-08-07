"""DeterministicScorer - ANALYST section (optional), pure calculators.

Scores analyst sentiment from DATED rating histories (FMP grades-historical /
FinnHub), reconstructed no-lookahead at as_of by the caller. Optional by
design: section weight defaults to 0 because ratings lag price action
(upgrades follow momentum -> double-count risk; memo §1/§7 open questions).
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import numpy as np

DEF_WINDOW_DAYS = 90
DEF_HALFLIFE_DAYS = 30

# FMP grades-historical buckets
_UP = ("strongBuy", "buy")
_DOWN = ("strongSell", "sell")
_HOLD = ("hold",)


def _bucket_counts(row: Dict[str, Any]) -> tuple:
    """(upgrades, holds, downgrades) from one grades-historical row."""
    def n(keys):
        return sum(int(row.get(k) or 0) for k in keys)
    return n(_UP), n(_HOLD), n(_DOWN)


def revision_momentum(grades_rows: List[Dict[str, Any]], as_of: datetime,
                      window_days: int = DEF_WINDOW_DAYS,
                      halflife_days: int = DEF_HALFLIFE_DAYS) -> Optional[Dict[str, Any]]:
    """Recency-weighted revision momentum in [-1, 1].

    `grades_rows` = FULL dated history (each row: date + bucket counts), any
    order. Rows with date > as_of are ignored (no lookahead). Weight of a row
    decays exponentially with halflife. Returns None when no usable rows.
    """
    if not grades_rows:
        return None
    cutoff = as_of - timedelta(days=window_days)
    decay = math.log(2.0) / max(1.0, float(halflife_days))

    w_up = w_down = w_hold = total = 0.0
    n_rows = 0
    for row in grades_rows:
        d = row.get("_parsed_date") or row.get("date")
        if isinstance(d, str):
            try:
                d = datetime.fromisoformat(d.replace("Z", "+00:00"))
            except ValueError:
                continue
        if d is None or not isinstance(d, datetime):
            continue
        if d.tzinfo is None and as_of.tzinfo is not None:
            from datetime import timezone
            d = d.replace(tzinfo=timezone.utc)
        if d > as_of or d < cutoff:
            continue
        up, hold, down = _bucket_counts(row)
        total_row = up + hold + down
        if total_row == 0:
            continue
        age_days = max(0.0, (as_of - d).total_seconds() / 86400.0)
        w = math.exp(-decay * age_days)
        w_up += w * up
        w_hold += w * hold
        w_down += w * down
        total += w * total_row
        n_rows += 1

    if total <= 0 or n_rows == 0:
        return None
    net = (w_up - w_down) / total
    coverage = min(1.0, total / 10.0)  # thin coverage attenuates conviction
    return {
        "score": float(np.clip(net * (0.5 + 0.5 * coverage), -1.0, 1.0)),
        "net_revision": float(net),
        "weighted_upgrades": float(w_up),
        "weighted_downgrades": float(w_down),
        "n_rows": n_rows,
    }
