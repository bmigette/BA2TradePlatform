"""Put a cached intraday history on the DAILY basis when the vendor cannot (or will not) do it.

WHEN THIS IS NEEDED. ``force_full_refetch(<intraday>)`` replaces an intraday file with the vendor's history. The
vendor's 5-minute endpoint serves old dates AS-TRADED for many symbols while its daily endpoint serves them
split-adjusted, so no refetch can make the two agree (``IntradayVendorBasisConflict``). The cross-interval check
(``cross_interval_basis``) measures the disagreement as clean piecewise-constant factors against the vendor-current
daily bars; this module turns those segments into a corrected frame, ONCE, with the origin of the data recorded.

THE RULE (one function, ``plan_rebase``; ``apply_plan`` applies it):

* PRICES. Open/High/Low/Close of every bar inside an off-basis segment are DIVIDED by the segment factor
  (factor = intraday / daily, measured as the median close ratio of the segment).
* FACTOR SOURCE. A measured factor is SNAPPED to the product of a contiguous run of the platform's split-calendar
  ratios (``fmp_history/mc_stock_split__<SYM>.json``, read offline) dated after the segment, when one matches
  within ``CALENDAR_TOL`` (0.5%): ``source = "split_calendar"``. Otherwise the measured value is used:
  ``source = "measured_only"`` (precision: the median of the segment's sessions, a few 0.1%).
* VOLUME. Multiplied by the factor ONLY for ``split_calendar`` factors, i.e. a real share split: the old file counts
  shares before the split, so the adjusted count is old * ratio (a 2-for-1 doubles it; a 1-for-10 reverse split
  divides it). A spin-off or an ADR-ratio change moves the PRICE by a factor but leaves the share count alone, so
  for a ``measured_only`` factor volume is left as it is and the segment carries ``volume_unadjusted = true``
  (surfaced by the health check and the launch preflight).
* REFUSED (``IntradayRebaseRefused``, with a code): class ``ok`` (nothing to do), ``noisy`` (``noisy``), too few
  sessions (``insufficient``), a segment that is not clean, MAD above ``SEGMENT_MAD_TOL`` (``unclean``: this is also
  how a re-used ticker looks), a burst whose session HIGH sits at the daily level (``bad_prints``: single bad bars,
  not a basis), a level below ``REBASE_MIN_LEVEL`` = 2% (``below_noise``: the correction would be as large as the
  error of the measurement and of the segment boundary, replacing one error with another), and a measured-only
  factor beyond 20x or below 1/20 (``implausible``: without a split-calendar source that is a different instrument).
* VERIFIED. The rebased frame is checked against the daily file with the shared check; the result must be ``ok``.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from ba2_providers.ohlcv import cross_interval_basis as cib

__all__ = ["IntradayRebaseRefused", "RebaseSegment", "RebasePlan", "REBASE_MIN_LEVEL", "CALENDAR_TOL",
           "MAX_MEASURED_ONLY_FACTOR", "TOOL_VERSION", "load_split_calendar", "plan_rebase", "apply_plan",
           "verify_rebased", "provenance_for", "provenance_from_note"]

TOOL_VERSION = "1"
#: |ln factor| below this is not rebased (see the module docstring, REFUSED).
REBASE_MIN_LEVEL = 0.02
#: a measured factor matches a product of calendar ratios within this relative error.
CALENDAR_TOL = 0.005
MAX_MEASURED_ONLY_FACTOR = 20.0


class IntradayRebaseRefused(RuntimeError):
    """A symbol cannot be rebased; ``code`` says why (ok, noisy, insufficient, unclean, bad_prints, below_noise,
    implausible, no_segments)."""

    def __init__(self, code: str, message: str):
        super().__init__(f"[{code}] {message}")
        self.code = code


@dataclass
class RebaseSegment:
    first_day: str                 # inclusive range of BAR dates the segment is applied to
    last_day: str                  # ("" first_day / last_day = unbounded on that side)
    sessions: int                  # comparable sessions the factor was measured on
    measured_factor: float
    factor: float                  # the factor applied (snapped or measured)
    source: str                    # "split_calendar" | "measured_only"
    calendar_splits: List[Tuple[str, float]] = field(default_factory=list)
    volume_adjusted: bool = False
    volume_unadjusted: bool = False
    mad: float = 0.0
    kind: str = "level"


@dataclass
class RebasePlan:
    symbol: str
    interval: str
    segments: List[RebaseSegment]
    check_before: Dict[str, Any]
    calendar_known: bool           # a split-calendar file exists for the symbol

    @property
    def sources(self) -> List[str]:
        return sorted({sg.source for sg in self.segments})

    def to_dict(self) -> dict:
        return {"symbol": self.symbol, "interval": self.interval, "calendar_known": self.calendar_known,
                "check_before": self.check_before, "segments": [asdict(sg) for sg in self.segments]}


# --------------------------------------------------------------------------------------------------
def load_split_calendar(symbol: str, cache_root: Optional[str] = None) -> Optional[List[Tuple[date, float]]]:
    """The platform's cached split calendar of ``symbol`` (``<cache>/fmp_history/mc_stock_split__SYM.json``, the
    file the market-condition source warms), or None when the symbol has none. Offline: no vendor call."""
    root = cache_root or cib._cache_root()
    path = os.path.join(root, "fmp_history", f"mc_stock_split__{symbol.upper()}.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    out = []
    for e in data.get("historical", []) if isinstance(data, dict) else []:
        try:
            num, den = float(e["numerator"]), float(e["denominator"])
            if num > 0 and den > 0:
                out.append((date.fromisoformat(e["date"]), num / den))
        except (KeyError, TypeError, ValueError):
            continue
    return sorted(out)


def _match_calendar(factor: float, after: date, splits: Sequence[Tuple[date, float]]):
    """``(snapped factor, [(date, ratio)])`` when ``factor`` equals the product of a contiguous run of calendar
    ratios dated after ``after`` (within CALENDAR_TOL), else None. Shortest run wins, then the latest."""
    cand = [(d, r) for d, r in splits if d > after and r != 1.0]
    best = None
    for i in range(len(cand)):
        prod = 1.0
        for j in range(i, min(len(cand), i + 6)):
            prod *= cand[j][1]
            if abs(prod / factor - 1.0) <= CALENDAR_TOL:
                run = cand[i:j + 1]
                key = (len(run), -i)
                if best is None or key < best[0]:
                    best = (key, prod, run)
    if best is None:
        return None
    return best[1], [(d.isoformat(), r) for d, r in best[2]]


def plan_rebase(result: "cib.BasisResult", splits: Optional[Sequence[Tuple[date, float]]]) -> RebasePlan:
    """The rebase plan for one symbol from its cross-interval result (measured over the WHOLE history against the
    vendor-current daily file). Raises ``IntradayRebaseRefused`` (see the module docstring)."""
    k = result.klass
    if k == cib.KLASS_OK:
        raise IntradayRebaseRefused("ok", f"{result.symbol}: the intraday file already agrees with the daily file")
    if k == cib.KLASS_NOISY:
        raise IntradayRebaseRefused("noisy", f"{result.symbol}: {result.reason or 'noisy'} -- no clean level to rebase "
                                             f"(a re-used ticker looks like this)")
    if k in cib.UNJUDGED_KLASSES:
        raise IntradayRebaseRefused("insufficient", f"{result.symbol}: {k} ({result.reason}) -- cannot be judged")
    if not result.segments:
        raise IntradayRebaseRefused("no_segments", f"{result.symbol}: class {k} without segments")
    segs = result.segments
    off = [sg for sg in segs if abs(np.log(sg.factor)) > cib.BASIS_TOL]
    if not off:
        raise IntradayRebaseRefused("ok", f"{result.symbol}: every segment is within the basis tolerance")
    for sg in off:
        if sg.kind == "burst_prints":
            raise IntradayRebaseRefused("bad_prints", f"{result.symbol}: {sg.first_day}..{sg.last_day} are single bad "
                                                      f"bars (session high at the daily level), not a basis")
        if sg.mad > cib.SEGMENT_MAD_TOL:
            raise IntradayRebaseRefused("unclean", f"{result.symbol}: segment {sg.first_day}..{sg.last_day} x{sg.factor:.4g} "
                                                   f"is not a clean level (MAD {sg.mad:.3f} > {cib.SEGMENT_MAD_TOL})")
        if abs(np.log(sg.factor)) < REBASE_MIN_LEVEL:
            raise IntradayRebaseRefused("below_noise", f"{result.symbol}: x{sg.factor:.4g} on {sg.first_day}..{sg.last_day} "
                                                       f"is below {np.exp(REBASE_MIN_LEVEL) - 1:.0%}: the correction would be "
                                                       f"as large as the measurement error; list it for exclusion")
    plan_segs: List[RebaseSegment] = []
    burst_plan = all(sg.kind.startswith("burst") for sg in segs)
    for i, sg in enumerate(segs):
        if abs(np.log(sg.factor)) <= cib.BASIS_TOL:
            continue
        if burst_plan:
            lo, hi = sg.first_day, sg.last_day
        else:
            lo = sg.first_day if i > 0 else ""
            hi = (date.fromisoformat(segs[i + 1].first_day) - pd.Timedelta(days=1)).isoformat() if i + 1 < len(segs) else ""
        m = _match_calendar(sg.factor, date.fromisoformat(sg.last_day), splits) if splits else None
        if m is None:
            if sg.factor > MAX_MEASURED_ONLY_FACTOR or sg.factor < 1.0 / MAX_MEASURED_ONLY_FACTOR:
                raise IntradayRebaseRefused(
                    "implausible", f"{result.symbol}: x{sg.factor:.4g} on {sg.first_day}..{sg.last_day} has no split-calendar "
                                   f"source and is beyond 1/{MAX_MEASURED_ONLY_FACTOR:g}..{MAX_MEASURED_ONLY_FACTOR:g}: "
                                   f"probably another instrument")
            plan_segs.append(RebaseSegment(lo, hi, sg.sessions, sg.factor, sg.factor, "measured_only", [], False, True,
                                           sg.mad, sg.kind))
        else:
            snapped, used = m
            plan_segs.append(RebaseSegment(lo, hi, sg.sessions, sg.factor, snapped, "split_calendar", used, True, False,
                                           sg.mad, sg.kind))
    before = {"klass": result.klass, "common_sessions": result.common_sessions, "factor": result.factor,
              "segments": [(s.first_day, s.last_day, s.sessions, round(s.factor, 6)) for s in segs]}
    return RebasePlan(result.symbol, "", plan_segs, before, splits is not None)


def _bar_days(frame: pd.DataFrame) -> pd.Series:
    return cib._naive_ny(frame["Date"]).dt.normalize()


def apply_plan(frame: pd.DataFrame, plan: RebasePlan) -> pd.DataFrame:
    """A COPY of ``frame`` with every planned segment rebased (see the module docstring). Bars outside the
    segments are untouched; the frame keeps every column (effective_date included)."""
    out = frame.copy()
    days = _bar_days(out)
    touched = pd.Series(False, index=out.index)
    for sg in plan.segments:
        m = pd.Series(True, index=out.index)
        if sg.first_day:
            m &= days >= pd.Timestamp(sg.first_day)
        if sg.last_day:
            m &= days <= pd.Timestamp(sg.last_day)
        if (m & touched).any():
            raise IntradayRebaseRefused("unclean", f"{plan.symbol}: overlapping rebase segments")
        for c in ("Open", "High", "Low", "Close"):
            out.loc[m, c] = out.loc[m, c].astype("float64") / sg.factor
        if sg.volume_adjusted and "Volume" in out.columns:
            out["Volume"] = out["Volume"].astype("float64")
            out.loc[m, "Volume"] = out.loc[m, "Volume"] * sg.factor
        touched |= m
    return out


def verify_rebased(symbol: str, interval: str, rebased: pd.DataFrame, daily: pd.DataFrame) -> "cib.BasisResult":
    """The shared check over the whole rebased frame against the daily frame (must come back ``ok``)."""
    store = cib.BasisStore("FMPOHLCVProvider", memo=None)
    return store.check_frame(symbol, interval, rebased, cib.WHOLE_HISTORY[0], cib.WHOLE_HISTORY[1], daily_frame=daily)


def count_affected(frame: pd.DataFrame, plan: RebasePlan) -> Tuple[int, int]:
    """``(bars, sessions)`` inside the planned segments."""
    days = _bar_days(frame)
    m = pd.Series(False, index=frame.index)
    for sg in plan.segments:
        mm = pd.Series(True, index=frame.index)
        if sg.first_day:
            mm &= days >= pd.Timestamp(sg.first_day)
        if sg.last_day:
            mm &= days <= pd.Timestamp(sg.last_day)
        m |= mm
    return int(m.sum()), int(days[m].nunique())


def provenance_for(plan: RebasePlan, *, daily_identity: Optional[Tuple[int, int]], backup: Optional[str],
                   post_check: Dict[str, Any], now: Optional[datetime] = None) -> dict:
    """The sidecar payload written next to a rebased file: the data's origin is never silent."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return {
        "kind": "rebased_by_tool", "tool": "tools/repair_intraday_basis.py", "tool_version": TOOL_VERSION,
        "applied_utc": now.isoformat(), "symbol": plan.symbol, "interval": plan.interval,
        "source": "split_calendar" if plan.sources == ["split_calendar"] else (
            "measured_only" if plan.sources == ["measured_only"] else "mixed"),
        "segments": [asdict(sg) for sg in plan.segments],
        "rule": "OHLC / factor; Volume * factor only when the factor is a product of split-calendar ratios",
        "daily_file_identity": list(daily_identity) if daily_identity else None,
        "backup": backup, "check_before": plan.check_before, "check_after": post_check,
    }


def provenance_from_note(note: Dict[str, Any], note_path: str, *, post_check: Dict[str, Any],
                         daily_identity: Optional[Tuple[int, int]], now: Optional[datetime] = None) -> dict:
    """Provenance for a file rescaled earlier by an ad-hoc script (``rescale_notes/<SYM>_5min.json``): the note's
    own segments are recorded verbatim, ADOPTED (not re-derived), only when the post-check of the file as it is now
    is ``ok``. That script multiplied volume by the factor in every segment; every adopted segment was a
    'clean calendar factor', which is exactly this module's volume rule."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    segs = []
    for sg in note.get("segments", []):
        snap = float(sg.get("snap", 1.0))
        if abs(np.log(snap)) <= cib.BASIS_TOL:
            continue
        cal = "calendar" in str(sg.get("verdict", ""))
        segs.append({"first_day": sg.get("a") or "", "last_day": sg.get("b") or "", "sessions": sg.get("n"),
                     "measured_factor": sg.get("measured"), "factor": snap,
                     "source": "split_calendar" if cal else "measured_only",
                     "volume_adjusted": True, "volume_unadjusted": False,
                     "volume_note": "" if cal else "ad-hoc script multiplied volume although the factor is not a calendar split"})
    return {
        "kind": "adopted_from_note", "tool": "tools/repair_intraday_basis.py adopt", "tool_version": TOOL_VERSION,
        "applied_utc": note.get("applied_utc"), "adopted_utc": now.isoformat(), "symbol": note.get("symbol"),
        "interval": "5min", "source": "split_calendar" if all(s["source"] == "split_calendar" for s in segs) else "mixed",
        "segments": segs, "rule": note.get("method"), "note_file": note_path, "note_source": note.get("source"),
        "daily_file_identity": list(daily_identity) if daily_identity else None,
        "backup": note.get("backup"), "backup_sha256": note.get("backup_sha256"),
        "check_before": None, "check_after": post_check,
    }
