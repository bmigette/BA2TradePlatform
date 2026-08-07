"""Post-earnings-announcement-drift (PEAD) calculators, shared and pure.

Two consumers with DIFFERENT shapes need the same underlying maths, so it lives
here once rather than being reimplemented per expert (the reimplementation is
what let DeterministicScorer's growth calculation silently diverge from its own
unit-tested one):

  * ``FMPEarningsDrift`` wants a BOOLEAN entry signal -- "did this name just beat
    by more than X%, recently enough to still be drifting?"
  * ``DeterministicScorer`` wants a CONTINUOUS score in [-1, 1] to weight against
    its other sections. Feeding a thresholded boolean into a weighted average
    throws away exactly the information the weighting is there to use.

Point-in-time: an earnings ANNOUNCEMENT is public on its report date (unlike a
financial statement, which is only knowable once FILED), so filtering rows on
``report_date <= as_of`` is the correct no-lookahead rule. Every function here
applies it defensively rather than trusting the caller's provider window.

Evidence base: Ball & Brown (1968), Bernard & Thomas (1989/1990). The drift is
strongest in the first few weeks after the announcement and decays over roughly
a quarter, which is why the score is recency-decayed rather than a flat window.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

# --- Defaults (mirrored by the consuming experts' ExpertSetting defaults) -----
DEF_DRIFT_WINDOW_DAYS = 75     # beyond ~a quarter the drift is spent
DEF_HALFLIFE_DAYS = 25         # decay of the drift's remaining strength
DEF_SCALE_PCT = 5.0            # a 5% surprise saturates the tanh
DEF_MIN_SUE_HISTORY = 6        # quarters needed before standardizing


def _as_utc(d: datetime) -> datetime:
    """Force tz-aware UTC so naive/aware operands never mix (this codebase has
    already lost an indicator to exactly that TypeError)."""
    return d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d.astimezone(timezone.utc)


def surprise_percent(reported: Optional[float],
                     estimated: Optional[float]) -> Optional[float]:
    """EPS surprise as a percentage of the |estimate|.

    Dividing by the ABSOLUTE estimate keeps the sign meaningful when the
    consensus itself is negative: a loss of 0.10 against an expected loss of
    0.50 is a BEAT, which a plain (r-e)/e would report with the wrong sign.
    """
    if reported is None or estimated is None:
        return None
    try:
        reported, estimated = float(reported), float(estimated)
    except (TypeError, ValueError):
        return None
    if estimated == 0 or not (math.isfinite(reported) and math.isfinite(estimated)):
        return None
    return (reported - estimated) / abs(estimated) * 100.0


def parse_report_date(row: Dict[str, Any]) -> Optional[datetime]:
    """Announcement date of one past-earnings row, tz-aware UTC."""
    raw = row.get("report_date") or row.get("fiscal_date_ending") or row.get("date")
    if not raw:
        return None
    try:
        return _as_utc(datetime.fromisoformat(str(raw).split("T")[0]))
    except (TypeError, ValueError):
        return None


def surprise_history(rows: Optional[List[Dict[str, Any]]],
                     as_of: datetime) -> List[Tuple[datetime, float]]:
    """[(report_date, surprise_pct)] for rows ANNOUNCED on or before as_of,
    newest first. Rows without both an actual and an estimate are dropped -- the
    provider emits scheduled-but-unreported quarters with an estimate and no
    actual, and treating a missing actual as 0.0 would invent a 100% miss."""
    if not rows:
        return []
    as_of = _as_utc(as_of)
    out: List[Tuple[datetime, float]] = []
    for row in rows:
        d = parse_report_date(row)
        if d is None or d > as_of:
            continue
        pct = row.get("surprise_percent")
        if pct is None:
            reported, estimated = row.get("reported_eps"), row.get("estimated_eps")
            # The provider coerces a missing EPS to 0; a real 0.0 actual and a
            # missing one are indistinguishable downstream, so an unreported
            # quarter must be dropped rather than scored as a total miss.
            if not reported or not estimated:
                continue
            pct = surprise_percent(reported, estimated)
        if pct is None or not math.isfinite(float(pct)):
            continue
        out.append((d, float(pct)))
    out.sort(key=lambda t: t[0], reverse=True)
    return out


def standardized_surprise(rows: Optional[List[Dict[str, Any]]], as_of: datetime,
                          min_history: int = DEF_MIN_SUE_HISTORY) -> Optional[float]:
    """SUE: the latest surprise expressed in standard deviations of this name's
    OWN past surprises.

    A 3% beat means something very different for a utility that never deviates
    than for a biotech that swings 40% a quarter; standardizing is what makes
    the signal comparable across names. Returns None when there is not enough
    history to estimate the dispersion.
    """
    hist = surprise_history(rows, as_of)
    if len(hist) < min_history:
        return None
    latest = hist[0][1]
    prior = [p for _, p in hist[1:]]
    mean = sum(prior) / len(prior)
    var = sum((p - mean) ** 2 for p in prior) / (len(prior) - 1) if len(prior) > 1 else 0.0
    sd = math.sqrt(var)
    if sd <= 0 or not math.isfinite(sd):
        return None
    return (latest - mean) / sd


def pead_score(rows: Optional[List[Dict[str, Any]]], as_of: datetime,
               s: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """Continuous PEAD score in [-1, 1], or None when there is nothing to score.

    None (not 0.0) matters: 0.0 is a real "reported exactly in line" reading, and
    a caller that renormalizes over available sections must be able to tell the
    two apart.

    score = tanh(magnitude / scale) * recency_decay, where magnitude is the SUE
    when enough history exists (comparable across names) and the raw surprise %
    otherwise, and recency_decay halves every `earnings_halflife_days` until the
    drift window closes.
    """
    s = s or {}
    window = float(s.get("earnings_drift_window_days", DEF_DRIFT_WINDOW_DAYS))
    halflife = float(s.get("earnings_halflife_days", DEF_HALFLIFE_DAYS))
    scale_pct = float(s.get("earnings_scale_pct", DEF_SCALE_PCT))
    use_sue = bool(s.get("earnings_use_sue", True))

    hist = surprise_history(rows, as_of)
    if not hist:
        return None
    report_date, surprise_pct = hist[0]
    days_since = max(0.0, (_as_utc(as_of) - report_date).total_seconds() / 86400.0)
    if window > 0 and days_since > window:
        return None  # drift is spent; this is "no signal", not "a bad signal"

    sue = standardized_surprise(rows, as_of) if use_sue else None
    if sue is not None:
        magnitude = math.tanh(sue / 2.0)          # ~2 sigma saturates
    else:
        magnitude = math.tanh(surprise_pct / scale_pct) if scale_pct > 0 else 0.0

    decay = math.exp(-math.log(2.0) * days_since / halflife) if halflife > 0 else 1.0
    score = max(-1.0, min(1.0, magnitude * decay))
    return {
        "score": float(score),
        "surprise_pct": round(surprise_pct, 2),
        "sue": round(sue, 3) if sue is not None else None,
        "days_since_report": round(days_since, 1),
        "report_date": report_date.date().isoformat(),
        "decay": round(decay, 3),
        "n_history": len(hist),
        "basis": "sue" if sue is not None else "surprise_pct",
    }
