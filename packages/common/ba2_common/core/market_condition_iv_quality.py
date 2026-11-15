"""Chain QUALITY of an ``iv-v1`` snapshot: the holes ``insufficient_history`` would otherwise hide.

WHY (review 2026-11-15). ``iv-v1`` reports a chain that exists but cannot define a value as
``insufficient_history`` -- a legitimately undefined observation, which launch-time window
coverage (``market_condition_reader._status_problems``) exempts, the way it exempts a leading
``missing_session`` run as "not listed yet". Both exemptions are right for single sessions and
wrong for a symbol whose chain is BROKEN: a missing expiry partition, blank or truncated quotes, a
tree that starts years after the stock's bars. Such a symbol would refuse every iv gate for months
and the GA would read that as the strategy not firing there.

THE MEASURE. For one symbol and one implied field (IV30, IV60), over the sessions where the stock
HAS a usable OHLCV window (``underlying_realized_vol_20`` valid -- before listing and after
delisting nothing is expected of the chain either), count the sessions where the field is UNKNOWN
for any reason, the rate, and the longest contiguous run of them, with its dates. A leading run
(the option tree starting after the bars) is a run like any other, so it is measured here instead
of passing as "pre-listing".

WHERE IT IS USED
* the store build records the whole-snapshot measure in each symbol's coverage record (an
  ``iv_quality`` entry; chain-profile manifests only, the OHLCV profiles' manifests are untouched)
  and prints the worst symbols;
* ``tools/warm_market_conditions.py verify`` refuses a chain manifest whose record breaches the
  thresholds;
* the launcher measures over exactly the sessions the RUN reads (mapped statuses) and refuses.

Thresholds default to ``MAX_RUN`` / ``MAX_RATE``; both callers take explicit overrides and a list
of ACCEPTED symbols, which are reported but not refused.
"""
from __future__ import annotations

from datetime import date
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from ba2_common.core.market_conditions import (
    FIELD_ATM_IV_30, FIELD_ATM_IV_60, FIELD_REALIZED_VOL_20, STATUS_VALID, STATUSES,
)

__all__ = ["MAX_RUN", "MAX_RATE", "QUALITY_FIELDS", "quality_from_statuses", "quality_problems",
           "quality_for_window", "quality_from_manifest", "worst_symbols"]

#: Longest tolerated run (sessions) of an unknown implied field while the stock trades.
MAX_RUN = 20
#: Highest tolerated share of such sessions.
MAX_RATE = 0.40
QUALITY_FIELDS = (FIELD_ATM_IV_30, FIELD_ATM_IV_60)
_VALID_CODE = STATUSES.index(STATUS_VALID)


def quality_from_statuses(sessions: Sequence[date], statuses: Mapping[str, Sequence[str]]) -> Dict[str, Any]:
    """``{field: {eligible, unknown, rate, longest_run, run_first, run_last}}`` for ``QUALITY_FIELDS``.

    ``statuses`` maps field name -> one status string per session (``None`` = no row at all,
    which counts as unknown). Only sessions where ``underlying_realized_vol_20`` is valid count."""
    rv = statuses[FIELD_REALIZED_VOL_20]
    out: Dict[str, Any] = {}
    for field in QUALITY_FIELDS:
        col = statuses[field]
        eligible = unknown = best = cur = 0
        cur_first = best_first = best_last = None
        for s, st, rv_st in zip(sessions, col, rv):
            if rv_st != STATUS_VALID:
                continue                       # no bars: nothing is expected of the chain
            eligible += 1
            if st == STATUS_VALID:
                cur, cur_first = 0, None
                continue
            unknown += 1
            if cur == 0:
                cur_first = s
            cur += 1
            if cur > best:
                best, best_first, best_last = cur, cur_first, s
        out[field] = {"eligible": eligible, "unknown": unknown,
                      "rate": round(unknown / eligible, 6) if eligible else 0.0,
                      "longest_run": best,
                      "run_first": best_first.isoformat() if best_first else None,
                      "run_last": best_last.isoformat() if best_last else None}
    return out


def quality_problems(quality_by_symbol: Mapping[str, Mapping[str, Any]], *, max_run: int = MAX_RUN,
                     max_rate: float = MAX_RATE, accept: Iterable[str] = ()) -> Tuple[List[str], List[str]]:
    """``(refused, accepted)`` messages: one per symbol breaching a threshold on any field.
    A symbol in ``accept`` is reported in ``accepted`` instead of refused."""
    accept = {str(s).upper() for s in accept}
    refused: List[str] = []
    accepted: List[str] = []
    for sym in sorted(quality_by_symbol):
        bad = []
        for field, q in sorted(quality_by_symbol[sym].items()):
            if q["longest_run"] > max_run or q["rate"] > max_rate:
                bad.append(f"{field} unknown on {q['unknown']}/{q['eligible']} trading sessions "
                           f"({q['rate']:.0%}), longest run {q['longest_run']} "
                           f"({q['run_first']}..{q['run_last']})")
        if bad:
            (accepted if sym.upper() in accept else refused).append(f"{sym}: " + "; ".join(bad))
    return refused, accepted


def worst_symbols(quality_by_symbol: Mapping[str, Mapping[str, Any]], n: int = 10) -> List[Tuple[str, int, float]]:
    """The ``n`` symbols with the longest unknown run (then the highest rate) over QUALITY_FIELDS."""
    rows = []
    for sym, q in quality_by_symbol.items():
        run = max(v["longest_run"] for v in q.values())
        rate = max(v["rate"] for v in q.values())
        rows.append((sym, run, rate))
    return sorted(rows, key=lambda r: (-r[1], -r[2], r[0]))[:n]


def quality_from_manifest(manifest: Mapping[str, Any]) -> Dict[str, Dict[str, Any]]:
    """The whole-snapshot measure the build recorded (``iv_quality`` coverage entries)."""
    out: Dict[str, Dict[str, Any]] = {}
    for sym, rec in (manifest.get("coverage") or {}).items():
        for exc in rec.get("exceptions") or ():
            if isinstance(exc, Mapping) and exc.get("kind") == "iv_quality":
                out.setdefault(sym, {})[exc["field"]] = {k: exc[k] for k in (
                    "eligible", "unknown", "rate", "longest_run", "run_first", "run_last")}
    return out


def quality_for_window(mapped: Any, universe: Iterable[str], start: Any, end: Any,
                       intraday: bool = False) -> Dict[str, Dict[str, Any]]:
    """The measure over exactly the sessions a run over ``[start, end]`` reads (the same session
    rule as ``market_condition_reader.window_coverage_problems``), from the mapped statuses."""
    from ba2_common.core.market_calendar import (
        backtest_decision_label, decision_data_session, prior_regular_session, regular_session_dates,
    )
    from ba2_common.core.market_condition_reader import _as_date

    first, last = _as_date(start), _as_date(end)
    bars = regular_session_dates(first, last)
    if not bars:
        return {}
    want_first = decision_data_session(backtest_decision_label(bars[0]))
    want_last = decision_data_session(backtest_decision_label(bars[-1]))
    if intraday:
        want_first = prior_regular_session(bars[0])
        want_last = bars[-2] if len(bars) > 1 else want_first
    sessions = regular_session_dates(want_first, want_last)
    fields = list(mapped.fields)
    idx = {f: fields.index(f) for f in (*QUALITY_FIELDS, FIELD_REALIZED_VOL_20)}
    covered = set(mapped.symbols())
    out: Dict[str, Dict[str, Any]] = {}
    for sym in sorted({str(s).upper() for s in universe}):
        if sym not in covered:
            continue                            # missing_coverage refuses these already
        present, codes = mapped.statuses_for_sessions(sym, sessions)
        statuses = {f: [STATUSES[c] if (p and c >= 0) else None for p, c in zip(present, codes[:, j])]
                    for f, j in idx.items()}
        out[sym] = quality_from_statuses(sessions, statuses)
    return out
