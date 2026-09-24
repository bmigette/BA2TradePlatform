"""Behaviour-distinct TOP-N selection over an optimization's ``all_results``.

WHY THIS EXISTS. A converged GA fills its top ranks with genomes that differ only in INERT genes
(thresholds of disabled gates, exits that never fire). Stage-1 option job 1 at gen 34: the top 20
by fitness were 11 distinct parameter sets that all produced the identical backtest (fitness
14.129, +1588%, -34.3% maxDD, 351 trades). The end-of-job persist
(``ba2test_launcher._rank_measured_candidates``) dedupes on the fitness VALUE, which collapses
exact clones but still lets through strategies that trade the same way with a score a hair
apart. This module picks the best N that actually BEHAVE differently; the re-run + persist
itself stays in ``ba2test_launcher._persist_top_backtests`` (see tools/persist_distinct_topn.py).

BEHAVIOUR FINGERPRINT = ``(trades, round(total_return, 2), round(max_drawdown, 2))``, from the
trial record the GA already wrote -- no re-run is needed to select. Identical fingerprint = same
behaviour: the highest-fitness genome is kept (ties: first seen) and the rest are counted as its
``clones``.

MIN-DIFFERENCE TOLERANCES (``Tolerances``). After the exact collapse, a candidate must ALSO
differ from EVERY already-selected pick on at least ONE axis:

  * return   -- ``|a - b| / max(|a|, |b|) >= return_rel_pct`` percent (default 5%, relative);
  * drawdown -- ``| |a| - |b| | >= dd_pts`` percentage points (default 2 pts, absolute);
  * trades   -- ``|a - b| / max(a, b) >= trades_rel_pct`` percent (default 5%, relative).

A candidate within tolerance on ALL THREE axes of some pick is a near-duplicate of the first
such pick (highest fitness) and is dropped, counted in that pick's ``near_duplicates``. A
tolerance of 0 IGNORES that axis (it can no longer make two genomes distinct); all three at 0
switches the near-duplicate screen off, so only exact fingerprints collapse. The defaults are
deliberately conservative: two strategies inside 5% of each other's return, 2 points of drawdown
and 5% of trade count are not worth two re-runs.

RELATIVE RETURN NEAR ZERO. The return axis is relative to ``max(|a|, |b|)``, so between two
small returns a tiny absolute gap is a large relative one (+0.5% vs +1.0% is 50% apart): such
genomes always count as distinct on that axis, whatever the drawdown/trade axes say. That is
harmless for the ranks worth persisting (their returns are far from zero), but a screen over
break-even genomes will keep more near-duplicates than the tolerance suggests.

EXCLUDED, never picks: non-measurements (``is_measured_result`` false: stalled, missing or
non-numeric fitness), the four fitness sentinels (zero-trade / low-trade / wiped-out / stalled),
records without a numeric ``total_return``/``max_drawdown`` (no fingerprint), and genomes whose
``trades`` is below ``min_trades`` (default 30; pass 0 to allow them explicitly).

``trades`` -- and therefore this gate -- counts ROUND-TRIP ROWS, one per LEG on options (it is
the engine's ``total_trades``), not structures. ">= 30 round-trip rows" is a basic sanity floor
only: the option fitnesses' own gate counts completed STRUCTURES (per year for the annual gates,
over the whole window for ``option_car_target_soft30``) and is stricter -- a 4-leg genome
passes this floor with 8 bets.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app.services.strategy_fitness import (
    LOW_TRADE_SENTINEL,
    STALLED_SENTINEL,
    WIPED_OUT_SENTINEL,
    ZERO_TRADE_SENTINEL,
    _calendar_year_returns,
    _parse_dt,
    is_measured_result,
)

_SENTINELS = frozenset({ZERO_TRADE_SENTINEL, LOW_TRADE_SENTINEL, WIPED_OUT_SENTINEL,
                        STALLED_SENTINEL})

# Keys ``_persist_top_backtests`` ADDS to a genome's params when it writes
# ``Backtest.strategy_params`` (fixed expert settings + the decoded rule trees). Stripping them
# recovers the raw genome, which is what an already-persisted row is matched on.
PERSIST_ADDED_PARAM_KEYS = ("expertFixedSettings", "entryRules", "exitRules")

DEFAULT_MIN_TRADES = 30   # round-trip ROWS (one per leg on options), not structures


@dataclass(frozen=True)
class Tolerances:
    """Minimum difference from every selected pick for a candidate to count as distinct.
    See the module docstring: 0 ignores an axis; all three 0 disables the screen."""
    return_rel_pct: float = 5.0
    dd_pts: float = 2.0
    trades_rel_pct: float = 5.0


@dataclass
class DistinctPick:
    rank: int
    fitness: float
    total_return: float
    car: Optional[float]
    max_drawdown: float
    trades: int
    clones: int
    params: Dict[str, Any]
    key: Optional[str]
    near_duplicates: int = 0
    fingerprint: Tuple[int, float, float] = field(default=(0, 0.0, 0.0))


def behaviour_fingerprint(trades, total_return, max_drawdown) -> Tuple[int, float, float]:
    return (int(trades), round(float(total_return), 2), round(float(max_drawdown), 2))


def params_key(params: Optional[Dict[str, Any]]) -> str:
    """Canonical key of a raw genome (the persisted-row additions stripped)."""
    import json
    raw = {k: v for k, v in (params or {}).items() if k not in PERSIST_ADDED_PARAM_KEYS}
    return json.dumps(raw, sort_keys=True, default=str)


def annualise(total_return_pct: Optional[float], years: Optional[float]) -> Optional[float]:
    """Compound annual growth (%) of a total return (%) over ``years``; None when unknown."""
    if total_return_pct is None or not years or years <= 0:
        return None
    growth = 1.0 + float(total_return_pct) / 100.0
    if growth <= 0:
        return -100.0
    return (growth ** (1.0 / years) - 1.0) * 100.0


def _num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _rel_pct(a: float, b: float) -> float:
    den = max(abs(a), abs(b))
    return 0.0 if den == 0 else abs(a - b) / den * 100.0


def _is_distinct(fp_a, fp_b, tol: Tolerances) -> bool:
    """True when ``fp_a`` differs from ``fp_b`` on at least one ENABLED (> 0) axis. With every
    axis disabled the screen is off and any two (non-identical) fingerprints are distinct."""
    if tol.return_rel_pct <= 0 and tol.dd_pts <= 0 and tol.trades_rel_pct <= 0:
        return True
    ta, ra, da = fp_a
    tb, rb, db = fp_b
    return ((tol.return_rel_pct > 0 and _rel_pct(ra, rb) >= tol.return_rel_pct)
            or (tol.dd_pts > 0 and abs(abs(da) - abs(db)) >= tol.dd_pts)
            or (tol.trades_rel_pct > 0 and _rel_pct(ta, tb) >= tol.trades_rel_pct))


def select_behaviour_distinct(results: Optional[Sequence[Dict[str, Any]]], n: int, *,
                              min_trades: int = DEFAULT_MIN_TRADES,
                              tolerances: Tolerances = Tolerances(),
                              years: Optional[float] = None,
                              stats: Optional[Dict[str, int]] = None) -> List[DistinctPick]:
    """The best ``n`` behaviour-distinct genomes of ``results`` (an ``all_results`` list),
    highest fitness first. ``min_trades`` is a floor on round-trip ROWS (legs on options), see
    the module docstring. ``years`` (the run window) makes each pick carry its CAR, compounded
    from the GA record's total return. ``stats``, when given, is filled with the exclusion
    counts."""
    counts = {"excluded_unmeasured": 0, "excluded_low_trades": 0, "excluded_no_metrics": 0,
              "eligible": 0, "distinct_behaviours": 0}
    valid = []
    for r in results or []:
        if (not is_measured_result(r) or not isinstance(r.get("params"), dict)
                or r["fitness"] in _SENTINELS):
            counts["excluded_unmeasured"] += 1
            continue
        if not (_num(r.get("total_return")) and _num(r.get("max_drawdown"))
                and _num(r.get("trades"))):
            counts["excluded_no_metrics"] += 1
            continue
        if int(r["trades"]) < int(min_trades):
            counts["excluded_low_trades"] += 1
            continue
        valid.append(r)
    counts["eligible"] = len(valid)

    # Stable sort: equal fitness keeps input order, so a tie keeps the first seen.
    valid.sort(key=lambda r: r["fitness"], reverse=True)

    groups: Dict[Tuple[int, float, float], List[Any]] = {}   # fp -> [record, clone count]
    for r in valid:
        fp = behaviour_fingerprint(r["trades"], r["total_return"], r["max_drawdown"])
        if fp in groups:
            groups[fp][1] += 1
        else:
            groups[fp] = [r, 0]
    counts["distinct_behaviours"] = len(groups)

    picks: List[DistinctPick] = []
    for fp, (r, clones) in groups.items():       # dict order == best-fitness-first
        twin = next((p for p in picks if not _is_distinct(fp, p.fingerprint, tolerances)), None)
        if twin is not None:
            twin.near_duplicates += 1
            continue
        if len(picks) >= n:
            # Keep scanning (without picking) only so near-duplicate counts cover the whole
            # list; a new distinct behaviour past n is simply not selected.
            continue
        picks.append(DistinctPick(
            rank=len(picks) + 1, fitness=float(r["fitness"]),
            total_return=float(r["total_return"]),
            car=annualise(r["total_return"], years),
            max_drawdown=float(r["max_drawdown"]), trades=int(r["trades"]), clones=clones,
            params=r["params"], key=r.get("key"), fingerprint=fp))
    if stats is not None:
        stats.update(counts)
    return picks


def labelled_calendar_year_returns(equity_curve) -> List[Tuple[str, float]]:
    """``strategy_fitness._calendar_year_returns`` with a year label on each value.

    The fitness helper returns bare values and merges a <6-month stub at either end into its
    neighbour. When no merge happened (the value count equals the number of calendar years the
    curve spans -- always the case for a full-year window such as 2020-01-01..2025-12-31) each
    value is labelled with its year; otherwise the labels are positional (``Y1``, ``Y2``...), so
    a merged stub is never mislabelled as a calendar year."""
    values = _calendar_year_returns(equity_curve)
    if not values:
        return []
    years = sorted({d.year for d in (_parse_dt(p.get("date")) for p in equity_curve or [])
                    if d is not None})
    if len(years) == len(values):
        return [(str(y), v) for y, v in zip(years, values)]
    return [(f"Y{i}", v) for i, v in enumerate(values, start=1)]
