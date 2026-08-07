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
DEF_TARGET_WINDOW_DAYS = 90
DEF_TARGET_SCALE = 0.20      # 20% implied upside saturates the tanh
# A "consensus" resting on one or two analysts is noise dressed as agreement --
# the same degenerate-thin-coverage trap FMPRating guards with
# min_price_targets_per_quarter. Below this the target leg is dropped entirely.
DEF_MIN_TARGETS = 3
DEF_AW_GRADES = 0.5          # sub-weights inside the ANALYST section
DEF_AW_TARGETS = 0.5

# FMP grades-historical buckets
_UP = ("strongBuy", "buy")
_DOWN = ("strongSell", "sell")
_HOLD = ("hold",)


def _as_utc(d: datetime) -> datetime:
    """Force a datetime to tz-aware UTC so naive/aware operands never mix."""
    from datetime import timezone
    return d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d.astimezone(timezone.utc)


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
    # FMP rows carry tz-aware dates while a backtest as_of is often naive (and
    # vice versa); comparing the two raises TypeError mid-bar. Normalise ONCE,
    # both directions, before any date math -- the tz-mismatch class of bug has
    # already silently killed an indicator in this codebase once.
    as_of = _as_utc(as_of)
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
        d = _as_utc(d)
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


def price_target_drift(rows: Optional[List[Dict[str, Any]]], as_of: datetime,
                       current_price: Optional[float],
                       window_days: int = DEF_TARGET_WINDOW_DAYS,
                       scale: float = DEF_TARGET_SCALE,
                       min_targets: int = DEF_MIN_TARGETS) -> Optional[Dict[str, Any]]:
    """Analyst price-target signal in [-1, 1] from DATED individual targets.

    Two legs, both computable without lookahead because each row carries its own
    ``publishedDate`` (unlike FMP's estimate endpoints, which expose only a
    current snapshot keyed by the FUTURE fiscal period they estimate -- there is
    no dated estimate history on this plan, so EPS-revision momentum is simply
    not backtestable):

      upside   consensus (mean target in the window) vs the as_of price
      drift    recent half of the window vs the older half -- are the targets
               being RAISED or cut? A high but stale target is not the same
               signal as one being marked up right now.

    Returns None (not 0.0) when coverage is too thin to mean anything: a
    consensus built from one or two analysts is the degenerate case FMPRating
    guards against, and the caller renormalizes over the sections it does have.
    """
    if not rows or not current_price or current_price <= 0:
        return None
    as_of = _as_utc(as_of)
    floor = as_of - timedelta(days=int(window_days))

    dated: List[tuple] = []
    for r in rows:
        raw = r.get("publishedDate") or r.get("date")
        target = r.get("priceTarget")
        if raw is None or target is None:
            continue
        try:
            d = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            continue
        d = _as_utc(d)
        if d > as_of or d <= floor:
            continue
        try:
            t = float(target)
        except (TypeError, ValueError):
            continue
        if t > 0 and math.isfinite(t):
            dated.append((d, t))

    if len(dated) < max(1, int(min_targets)):
        return None
    dated.sort(key=lambda x: x[0])
    targets = [t for _, t in dated]
    consensus = sum(targets) / len(targets)
    upside = consensus / float(current_price) - 1.0
    upside_score = math.tanh(upside / scale) if scale > 0 else 0.0

    # Revision drift: newer half vs older half of the SAME window.
    half = len(targets) // 2
    drift_score = 0.0
    if half >= 1:
        older = sum(targets[:half]) / half
        newer = sum(targets[half:]) / (len(targets) - half)
        if older > 0:
            drift_score = math.tanh((newer / older - 1.0) / scale)

    score = float(np.clip(0.5 * upside_score + 0.5 * drift_score, -1.0, 1.0))
    return {
        "score": score,
        "consensus_target": round(consensus, 2),
        "upside_pct": round(upside * 100.0, 2),
        "drift_score": round(drift_score, 3),
        "n_targets": len(targets),
    }


def analyst_section_score(grades_rows: Optional[List[Dict[str, Any]]],
                          target_rows: Optional[List[Dict[str, Any]]],
                          as_of: datetime, current_price: Optional[float],
                          s: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """Blend the two ANALYST legs (rating revisions + price-target drift).

    Either leg may be None; the surviving one carries the section, and when both
    are missing the section itself is None so the composite renormalizes rather
    than scoring a fabricated neutral 0.
    """
    s = s or {}
    legs: Dict[str, tuple] = {}
    rm = revision_momentum(grades_rows, as_of,
                           int(s.get("analyst_window_days", DEF_WINDOW_DAYS)),
                           int(s.get("analyst_recency_halflife_days", DEF_HALFLIFE_DAYS)))
    if rm is not None:
        legs["grades"] = (float(s.get("aw_grades", DEF_AW_GRADES)), rm["score"], rm)
    pt = price_target_drift(target_rows, as_of, current_price,
                            int(s.get("analyst_target_window_days", DEF_TARGET_WINDOW_DAYS)),
                            float(s.get("analyst_target_scale", DEF_TARGET_SCALE)),
                            int(s.get("analyst_min_targets", DEF_MIN_TARGETS)))
    if pt is not None:
        legs["targets"] = (float(s.get("aw_targets", DEF_AW_TARGETS)), pt["score"], pt)

    total_w = sum(w for w, _, _ in legs.values())
    if not legs or total_w <= 0:
        return None
    score = sum(w * v for w, v, _ in legs.values()) / total_w
    return {
        "score": float(np.clip(score, -1.0, 1.0)),
        "legs": {k: {"weight": w, "score": v, "detail": d} for k, (w, v, d) in legs.items()},
        "n_legs": len(legs),
    }
