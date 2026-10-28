"""Stage-1 gate, ``report.md`` and ``dashboard.html`` of the ML exploration v2 grid (design section 4, "Gate to stage 2").

``gate`` reads the finished rows of ``results.jsonl`` (``ml_grid.run_cell_seed``) and decides, per cell, whether it goes
on to stage 2. The design's rule, averaged over a cell's seeds:

* it beats BOTH baselines (naive momentum and the linear model of the same track / store / symbols / target kind /
  horizon) and its shuffle control, in at least ``GATE_MIN_REGIMES`` (4) of the six regimes, including ``2022bear`` or
  ``2020crash``. The rule is not relaxed for a regime the cell cannot be scored in at all (no seed has a value because
  no block of that regime was fitted): that regime is "n/a" (``unavailable`` of the record, ``available`` False in its
  regime entry; shown as n/a everywhere), it never counts, and a cell that fails the rule says "regimes unavailable:
  2019, 2020crash" in its reasons. A cell scored in only four regimes must beat the baselines in all four;
* its primary metric (selection: quintile spread, timing: excess; both already net of ``cost_bps`` = 7) is positive;
* the shuffle control's p-value is at most ``GATE_MAX_P``;
* at most ``GATE_MAX_SURVIVORS`` (10) survivors PER TRACK, ranked by seed mean minus seed std.

What the rows allow, and the choices made where the design is silent (also listed in ``report.md``):

* A regime "beats the baseline" when the cell's seed-mean regime value of the primary metric is above the baseline's in
  that regime, for every baseline found. Several baseline cells can match; those with the cell's own feature groups are
  preferred and the strongest one (highest seed-mean primary) is used.
* A baseline is the naive / linear cell of the same track, store, symbols, target kind and horizon (its own groups
  first, then the strongest). When a family has none for the cell's target (``rank``: no linear; ``quantile``: no
  naive or linear; ``terciles``: no naive) the strongest cell of that family with the same track / store / symbols /
  horizon and ANY target is the FALLBACK: the track metrics depend only on the score ranking, so it is comparable.
  The record says which baseline was used and whether it was a fallback (``baselines``), the gate table shows
  "<family> baseline: fallback (<kind>)". A family with no cell at all on that track / horizon is still a reason.
* A regime "beats the shuffle control" when the cell's regime value is above that regime's null mean. Per seed the
  null is the HIGHEST of the gating controls' per-regime null means (``controls["by_regime"][regime]["null_means"]``:
  on the timing track the plain and the within-block time shuffle), and the per-seed nulls are then averaged; that is
  the seed-mean value against the mean of the per-seed strictest nulls. The p shown per regime (report, heatmap
  tooltip) is the median over seeds of the larger-p control's, which is also the control behind the whole-period p
  (the value ``factor_effects`` reports) and does not gate per regime: the significance is judged once, on the whole
  period. A row without per-regime controls (written before them) FAILS the regime check with ``OLD_ROW_WARNING``
  (``strict_old_rows``, the default); with ``strict_old_rows=False`` that seed falls back to its own whole-period null,
  the cell carries the warning and survivors that rely on it are marked.
* A cell with fewer finished seeds than planned is "pending" only while the run is live (``live=True``); once the run
  has ended it fails with "k of n seeds finished (errors)" (or "(missing)" when no seed errored).
* ``GATE_MAX_P`` is not in the design; 0.05 is the conventional choice. With the default 20 control seeds the smallest
  attainable p is 1/21, so 0.05 means "beats every shuffle".
* Coverage (``min_coverage``, 0.8) excludes a cell before the gate is read; the threshold is not in the design either.
* A near-constant model (more than ``LOW_ITER_FLAG_SHARE`` of its blocks stopped at <= ``ml_grid.LOW_ITER`` boosting
  rounds) is FLAGGED ("near-constant k%"), not excluded: it cannot beat its shuffle control anyway. The exclusion stays
  available as ``max_low_iter_share`` (default None).
"""
from __future__ import annotations

import hashlib
import html as _html
import json
import logging
import math
import os
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

import numpy as np
import pandas as pd

from app.services import ml_grid as G
from app.services import ml_walkforward as W

logger = logging.getLogger(__name__)

#: the six regimes of the gate (``ml_walkforward.REGIMES`` without the pre-evaluation ``pre2019``)
GATE_REGIMES: Tuple[str, ...] = tuple(r for r in W.REGIMES if r != "pre2019")
HARD_REGIMES: Tuple[str, ...] = ("2022bear", "2020crash")
GATE_MIN_REGIMES = 4
GATE_MAX_SURVIVORS = 10
GATE_COST_BPS = 7.0
GATE_MAX_P = 0.05
GATE_MIN_COVERAGE = 0.8
GATE_MAX_LOW_ITER_SHARE: Optional[float] = None       # no exclusion by near-constant models: they are flagged
LOW_ITER_FLAG_SHARE = 0.5
BASELINE_FAMILIES: Tuple[str, ...] = ("naive", "linear")
OLD_ROW_WARNING = "per-regime control missing (old row)"
#: a live rebuild waits at least this long, and ``THROTTLE_FACTOR`` x the duration of the last build
THROTTLE_MIN_SECONDS = 60.0
THROTTLE_FACTOR = 10.0
#: ``write_atomic`` tries ``os.replace`` this often when the target is held open (Windows), with a short pause
REPLACE_TRIES = 5
REPLACE_PAUSE_SECONDS = 0.05
_now = time.monotonic                           # module-level so the tests can drive the clock
_sleep = time.sleep
#: the metrics the heatmap shows per track (the first is the gate's primary)
HEATMAP_METRICS: Dict[str, Tuple[str, ...]] = {
    "selection": ("quintile_spread_net", "rank_ic"),
    "timing": ("timing_excess_net", "timing_sharpe_net"),
}
HEATMAP_MAX_ROWS = 60
DASHBOARD_TITLE = "ML Grid Stage 1"


def _num(x: Any) -> float:
    """``x`` as a float, NaN for None / non-numbers."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return float("nan")
    return v


def _finite(x: Any) -> bool:
    return isinstance(x, (int, float, np.floating, np.integer)) and math.isfinite(float(x))


def _opt(x: float) -> Optional[float]:
    return float(x) if math.isfinite(x) else None


def _cfg(row: Dict[str, Any], key: str) -> Any:
    """A config field of a row, None when absent (rows of older versions / minimal rows)."""
    cfg = row["config"]
    return cfg[key] if key in cfg else None


# ================================================================== the gate
def _match_key(row: Dict[str, Any], loose: bool = False) -> Tuple:
    """What a baseline must share with a cell: track, store (its signature when the row has one), symbols, target kind
    and horizon (and the barrier ratio, which only barrier targets use). ``loose``: without the target kind and ratio
    (the fallback match: the track metrics depend only on the score ranking, so a baseline of another target on the
    same track / store / symbols / horizon is comparable)."""
    ident = row["identity"] if "identity" in row and isinstance(row["identity"], dict) else {}
    sig = ident["store_signature"] if "store_signature" in ident else None
    tgt = row["config"]["target"]
    syms = _cfg(row, "symbols")
    base = (row["track"], sig if sig else _cfg(row, "store"), tuple(syms) if isinstance(syms, list) else syms)
    if loose:
        return base + (tgt["horizon"],)
    return base + (tgt["kind"], tgt["horizon"], tgt["ratio"] if "ratio" in tgt and tgt["kind"] in G._BARRIER_KINDS else None)


def _seed_mean_regimes(rows: Sequence[Dict[str, Any]], metric: str) -> Dict[str, float]:
    """Per regime, the mean over the seeds of the metric's regime mean; NaN when any seed lacks a finite value."""
    out: Dict[str, float] = {}
    for reg in ("all",) + GATE_REGIMES:
        vals = []
        for r in rows:
            m = r["regimes"][metric] if "regimes" in r and metric in r["regimes"] else {}
            vals.append(_num(m[reg]["mean"]) if reg in m else float("nan"))
        out[reg] = float(np.mean(vals)) if vals and all(math.isfinite(v) for v in vals) else float("nan")
    return out


def _unavailable_regimes(rows: Sequence[Dict[str, Any]], metric: str) -> List[str]:
    """The gate regimes that NO seed of the cell has a finite value for: a block of that regime was never fitted (the
    training window does not reach back, or every block was skipped), so the cell cannot be scored there at all. A regime
    one seed lacks is not structural: it stays an ordinary NaN (not counted)."""
    out: List[str] = []
    for reg in GATE_REGIMES:
        have = []
        for r in rows:
            m = r["regimes"][metric] if "regimes" in r and metric in r["regimes"] else {}
            have.append(reg in m and math.isfinite(_num(m[reg]["mean"])))
        if rows and not any(have):
            out.append(reg)
    return out


def _null_mean(rows: Sequence[Dict[str, Any]]) -> float:
    """Seed mean of the mean of the gating shuffle control's null (NaN when a seed has none)."""
    vals = []
    for r in rows:
        c = r["controls"] if "controls" in r and isinstance(r["controls"], dict) else {}
        name = c["primary"] if "primary" in c else None
        vals.append(_num(c[name]["mean"]) if name and name in c and isinstance(c[name], dict) and "mean" in c[name]
                    else float("nan"))
    return float(np.mean(vals)) if vals and all(math.isfinite(v) for v in vals) else float("nan")


def _seed_regime_nulls(row: Dict[str, Any]) -> Optional[Dict[str, Dict[str, Any]]]:
    """Per regime, for ONE row: ``null`` (the highest of the gating controls' null means; NaN when any is not finite),
    ``std`` / ``p_value`` / ``control`` (those of the larger-p control, for display). None for an old row without
    ``controls["by_regime"]``."""
    c = row["controls"] if "controls" in row and isinstance(row["controls"], dict) else {}
    if "by_regime" not in c or not isinstance(c["by_regime"], dict):
        return None
    out: Dict[str, Dict[str, Any]] = {}
    for reg in GATE_REGIMES:
        e = c["by_regime"][reg] if reg in c["by_regime"] else None
        if e is None:
            out[reg] = {"null": float("nan"), "std": None, "p_value": None, "control": None}
            continue
        means = e["null_means"] if "null_means" in e else {e["control"]: e["null_mean"]}
        vals = [_num(v) for v in means.values()]
        out[reg] = {"null": max(vals) if vals and all(math.isfinite(v) for v in vals) else float("nan"),
                    "std": e["null_std"], "p_value": e["p_value"], "control": e["control"]}
    return out


def _whole_null_of(row: Dict[str, Any]) -> float:
    """One row's whole-period null mean of its gating control (NaN when it has none)."""
    c = row["controls"] if "controls" in row and isinstance(row["controls"], dict) else {}
    name = c["primary"] if "primary" in c else None
    return _num(c[name]["mean"]) if name and name in c and isinstance(c[name], dict) and "mean" in c[name] \
        else float("nan")


def _regime_controls(rows: Sequence[Dict[str, Any]]) -> Tuple[Dict[str, Dict[str, Any]], int]:
    """Per regime, aggregated over the seeds, and the number of old rows (seeds without per-regime controls).

    ``null_mean``: the mean over the seeds of each seed's null (its per-regime strictest null, or for an old row its
    whole-period null); ``null_std``: mean of the finite stds; ``p_value``: median over the seeds that have one;
    ``control``: the first seed's."""
    per = [_seed_regime_nulls(r) for r in rows]
    whole = [_whole_null_of(r) for r in rows]
    out: Dict[str, Dict[str, Any]] = {}
    for reg in GATE_REGIMES:
        nulls = [x[reg]["null"] if x is not None else w for x, w in zip(per, whole)]
        stds = [float(x[reg]["std"]) for x in per if x is not None and _finite(x[reg]["std"])]
        ps = [float(x[reg]["p_value"]) for x in per if x is not None and _finite(x[reg]["p_value"])]
        ctl = next((x[reg]["control"] for x in per if x is not None and x[reg]["control"]), None)
        out[reg] = {"null_mean": float(np.mean(nulls)) if nulls and all(math.isfinite(v) for v in nulls) else float("nan"),
                    "null_std": float(np.mean(stds)) if stds else None,
                    "p_value": float(np.median(ps)) if ps else None, "control": ctl}
    return out, sum(1 for x in per if x is None)


def _parse_label(label: str) -> Optional[Dict[str, Any]]:
    """The factor levels out of a row / error label ``"<track> <kind> h<H> <family> <train_months> <groups> <blocks>"``
    (``ml_grid._label_of``), None when it is not one."""
    parts = label.split()
    if len(parts) != 7 or parts[0] not in G.TRACKS or not parts[2].startswith("h") or not parts[2][1:].isdigit():
        return None
    return {"track": parts[0], "target": parts[1], "horizon": int(parts[2][1:]), "family": parts[3],
            "train_months": int(parts[4]) if parts[4].isdigit() else parts[4], "groups": parts[5]}


def _cost_bps(rows: Sequence[Dict[str, Any]]) -> float:
    vals = []
    for r in rows:
        ident = r["identity"] if "identity" in r and isinstance(r["identity"], dict) else {}
        st = ident["settings"] if "settings" in ident else {}
        if "cost_bps" in st:
            vals.append(_num(st["cost_bps"]))
    return min(vals) if vals else float("nan")


def _fmt(x: float, nd: int = 4) -> str:
    return "n/a" if not math.isfinite(x) else f"{x:+.{nd}f}"


def gate(cell_results: Union[str, Path, Sequence[Dict[str, Any]]], *, max_p: float = GATE_MAX_P,
         min_regimes: int = GATE_MIN_REGIMES, max_survivors: int = GATE_MAX_SURVIVORS,
         min_coverage: float = GATE_MIN_COVERAGE, max_low_iter_share: Optional[float] = GATE_MAX_LOW_ITER_SHARE,
         min_cost_bps: float = GATE_COST_BPS, live: bool = False,
         errors: Optional[Sequence[Dict[str, Any]]] = None, strict_old_rows: bool = True,
         cancelled: bool = False) -> Dict[str, Any]:
    """Apply the stage-1 gate to ``cell_results`` (rows, or a path to ``results.jsonl``).

    ``live``: the run is still going, so a cell with unfinished seeds is "pending"; False (the default: the run has
    ended) makes it a failure "k of n seeds finished (errors)". ``errors``: the rows of ``errors.jsonl``; a seed in
    them without a later result row is an error, and a cell with errors but no result row at all is listed too.
    ``cancelled``: the run was cancelled, so its unfinished cells are "incomplete (cancelled)" (a cell with errors
    stays a failure). ``strict_old_rows``: a seed whose row has no per-regime controls fails the regime check (default);
    False lets that seed fall back to its whole-period null, with a warning (``uses_fallback``).

    Returns ``{"cells", "survivors", "by_track", "params", "effects"}``. ``cells``: one record per cell with ``key``,
    ``track``, ``label``, the factor levels (``target`` ``horizon`` ``family`` ``train_months`` ``groups``), ``mean`` /
    ``std`` / ``score`` (= mean - std) of the primary over the seeds, ``n_seeds``, ``p_value`` (median over seeds of the
    gating control), ``null_mean``, ``regimes`` (per regime: ``value``, ``naive``, ``linear``, ``beats_baseline``,
    ``beats_control``, ``counted``, and the regime's control ``null_mean`` / ``null_std`` / ``p_value`` /
    ``control``), ``whole`` (the same for the whole period), ``warnings`` (e.g. ``OLD_ROW_WARNING``; they do not fail
    a cell), ``regimes_beaten``, ``passed`` (every condition met), ``survivor`` (passed and in
    the top ``max_survivors`` of its track), ``rank`` (among the track's passing cells), ``state`` (survivor / capped /
    fail / pending / excluded / baseline / not gated), ``reasons`` (one string per failed condition) and ``note``.
    ``survivors``: the survivors of both tracks, each track best first; ``by_track`` the same per track; ``effects``:
    the ``factor_effects`` tables per track (coverage filter only: near-constant models are still in the factor tables);
    ``excluded``: per track the cells the gate dropped (coverage; near-constant models only when
    ``max_low_iter_share`` is set) with the reason. ``low_iter_share`` / ``near_constant`` flag a cell whose models
    stopped almost at once in more than ``LOW_ITER_FLAG_SHARE`` of the blocks."""
    rows = G.read_results(cell_results) if isinstance(cell_results, (str, Path)) else list(cell_results)
    last: Dict[Tuple[str, int], Dict[str, Any]] = {}
    for r in rows:
        last[(r["cell_key"], int(r["seed"]))] = r
    by_cell: Dict[str, List[Dict[str, Any]]] = {}
    for (key, _), r in sorted(last.items(), key=lambda kv: (kv[0][0], kv[0][1])):
        by_cell.setdefault(key, []).append(r)
    err_rows: Dict[str, List[Dict[str, Any]]] = {}
    for e in (errors or []):
        if (e["cell_key"], int(e["seed"])) not in last:                        # a later success resolves an error
            err_rows.setdefault(e["cell_key"], []).append(e)
    params = {"max_p": max_p, "min_regimes": min_regimes, "max_survivors": max_survivors, "regimes": list(GATE_REGIMES),
              "hard_regimes": list(HARD_REGIMES), "cost_bps": min_cost_bps, "min_coverage": min_coverage,
              "max_low_iter_share": max_low_iter_share, "low_iter_flag_share": LOW_ITER_FLAG_SHARE,
              "strict_old_rows": strict_old_rows}
    # The factor tables and the baselines see every cell with enough coverage; the gate also drops near-constant models.
    effects, kept, excluded, excluded_tables = {}, {}, {}, {}
    for track in G.TRACKS:
        fx = G.factor_effects(rows, track, min_coverage, None)
        effects[track] = fx
        for _, c in fx["cells"].iterrows():
            kept[c["cell_key"]] = c
        gated = fx if max_low_iter_share is None else G.factor_effects(rows, track, min_coverage, max_low_iter_share)
        excluded_tables[track] = gated["excluded"]
        for _, c in gated["excluded"].iterrows():
            excluded[c["cell_key"]] = c["reason"]
    metric_of = lambda track: G.PRIMARY[track]                                                 # noqa: E731
    # baselines: kept cells of the baseline families, by match key
    pools: Dict[Tuple, List[Dict[str, Any]]] = {}          # same target kind (exact match)
    loose_pools: Dict[Tuple, List[Dict[str, Any]]] = {}    # any target (the fallback)
    for key, rs in by_cell.items():
        r0 = rs[0]
        if r0["config"]["family"] in BASELINE_FAMILIES and key in kept and r0["config"]["target"]["kind"] != "volatility":
            ok = [r for r in rs if r["status"] == "ok"]
            item = {"key": key, "family": r0["config"]["family"], "groups": tuple(r0["config"]["groups"]),
                    "kind": r0["config"]["target"]["kind"], "mean": float(kept[key]["mean"]),
                    "regimes": _seed_mean_regimes(ok, metric_of(r0["track"]))}
            pools.setdefault(_match_key(r0), []).append(item)
            loose_pools.setdefault(_match_key(r0, loose=True), []).append(item)
    records: List[Dict[str, Any]] = []
    for key, rs in by_cell.items():
        r0 = rs[0]
        cfg = r0["config"]
        track = r0["track"]
        ok = [r for r in rs if r["status"] == "ok"]
        rec: Dict[str, Any] = {
            "key": key, "track": track, "label": r0["label"] if "label" in r0 else G._label_of(cfg),
            "target": cfg["target"]["kind"], "horizon": cfg["target"]["horizon"], "family": cfg["family"],
            "train_months": cfg["train_months"], "groups": "+".join(cfg["groups"]), "n_seeds": len(ok),
            "mean": float("nan"), "std": float("nan"), "score": float("nan"), "p_value": float("nan"),
            "null_mean": float("nan"), "low_iter_share": None, "near_constant": False, "regimes": {},
            "whole": None, "warnings": [], "old_rows": 0, "uses_fallback": False, "regimes_beaten": 0,
            "unavailable": [], "passed": False, "survivor": False, "rank": None, "state": "fail", "reasons": [],
            "note": "", "baseline_keys": {}, "baselines": {}}
        records.append(rec)
        if cfg["target"]["kind"] == "volatility":
            rec.update(state="not gated", reasons=["volatility target: a sizing model with no trading shuffle "
                                                   "control, not gated"])
            continue
        if cfg["family"] in BASELINE_FAMILIES:
            rec.update(state="baseline", reasons=[f"{cfg['family']} is a baseline family, not a candidate"])
            if key in kept:
                c = kept[key]
                rec.update(mean=float(c["mean"]), std=float(c["std"]), score=float(c["score"]))
            continue
        c = kept[key] if key in kept else None
        if c is not None:
            rec.update(mean=float(c["mean"]), std=float(c["std"]), score=float(c["score"]),
                       p_value=float(c["p_value"]))
            share = float(c["low_iter_share"])
            if math.isfinite(share):
                rec.update(low_iter_share=share, near_constant=share > LOW_ITER_FLAG_SHARE)
        if key in excluded:
            rec.update(state="excluded", reasons=[f"excluded: {excluded[key]}"])
            continue
        if c is None:
            rec.update(state="excluded", reasons=["excluded: no usable seed (no primary metric)"])
            continue
        rec["null_mean"] = _null_mean(ok)
        reasons: List[str] = []
        planned = max([int(r["seeds_run"]) for r in ok if "seeds_run" in r] or [len(ok)])
        pending = len(ok) < planned
        if pending:
            suffix = ("so far" if live else "(errors)" if key in err_rows else
                      "(cancelled)" if cancelled else "(missing)")
            reasons.append(f"{len(ok)} of {planned} seeds finished {suffix}")
        elif not math.isfinite(rec["std"]):
            reasons.append("a single seed: no seed std, so no mean - std to rank by")
        # --- baselines
        cell_regs = _seed_mean_regimes(ok, metric_of(track))
        unavailable = _unavailable_regimes(ok, metric_of(track))
        rec["unavailable"] = unavailable
        pool = pools[_match_key(r0)] if _match_key(r0) in pools else []
        loose = loose_pools[_match_key(r0, loose=True)] if _match_key(r0, loose=True) in loose_pools else []
        strongest = lambda cs: max(cs, key=lambda b: (b["mean"] if math.isfinite(b["mean"]) else -math.inf, b["key"]))  # noqa: E731
        base: Dict[str, Dict[str, Any]] = {}
        for fam in BASELINE_FAMILIES:
            cands = [b for b in pool if b["family"] == fam]
            same = [b for b in cands if b["groups"] == tuple(cfg["groups"])]
            cands = same or cands
            if cands:                                         # 1. the cell's own target: its own groups first, then the strongest
                base[fam] = {**strongest(cands), "fallback": False}
                continue
            cands = [b for b in loose if b["family"] == fam]
            if cands:                                         # 2. any target: the strongest
                base[fam] = {**strongest(cands), "fallback": True}
            else:
                reasons.append(f"missing baseline: no {fam} cell with the same track / store / symbols / horizon "
                               f"(any target)")
        rec["baseline_keys"] = {f: b["key"] for f, b in base.items()}
        rec["baselines"] = {f: {"key": b["key"], "fallback": b["fallback"], "kind": b["kind"]} for f, b in base.items()}
        # --- regimes
        beaten, hard = 0, False
        ctl, old_rows = _regime_controls(ok)
        rec["old_rows"] = old_rows
        ctl_name = next((r["controls"]["primary"] for r in ok if isinstance(r["controls"], dict)
                         and "primary" in r["controls"]), None)
        strict_fail = old_rows > 0 and strict_old_rows
        if old_rows and not strict_old_rows:
            rec["warnings"].append(OLD_ROW_WARNING)
            rec["uses_fallback"] = True
        rec["whole"] = {"value": _opt(cell_regs["all"]), "null_mean": _opt(rec["null_mean"]),
                        "null_std": None, "p_value": _opt(rec["p_value"]), "control": ctl_name}
        for reg in GATE_REGIMES:
            v = cell_regs[reg]
            rc = ctl[reg]
            info = {"value": _opt(v), "available": reg not in unavailable, "naive": None, "linear": None,
                    "null_mean": _opt(rc["null_mean"]), "null_std": rc["null_std"], "p_value": rc["p_value"],
                    "control": rc["control"]}
            vs_base = bool(base) and math.isfinite(v)
            for fam, b in base.items():
                bv = b["regimes"][reg]
                info[fam] = _opt(bv)
                vs_base = vs_base and math.isfinite(bv) and v > bv
            vs_ctl = (math.isfinite(v) and math.isfinite(rc["null_mean"]) and v > rc["null_mean"]
                      and not strict_fail)
            counted = vs_base and vs_ctl
            info.update(beats_baseline=bool(vs_base), beats_control=bool(vs_ctl), counted=bool(counted))
            rec["regimes"][reg] = info
            beaten += counted
            hard = hard or (counted and reg in HARD_REGIMES)
        rec["regimes_beaten"] = int(beaten)
        if strict_fail:
            reasons.append(OLD_ROW_WARNING)
        elif base:
            if beaten < min_regimes:
                reasons.append(f"beats the baselines and the shuffle control in only {beaten} of {len(GATE_REGIMES)} "
                               f"regimes (need {min_regimes})")
            if not hard:
                reasons.append(f"does not beat them in {' or '.join(HARD_REGIMES)}")
            if unavailable and (beaten < min_regimes or not hard):
                reasons.append(f"regimes unavailable: {', '.join(unavailable)}")
        # --- costs, significance
        if not (math.isfinite(rec["mean"]) and rec["mean"] > 0):
            reasons.append(f"not positive after costs: primary mean {_fmt(rec['mean'])} after {min_cost_bps:g} bps")
        charged = _cost_bps(ok)
        if math.isfinite(charged) and charged < min_cost_bps:
            reasons.append(f"the run charged {charged:g} bps, below the gate's {min_cost_bps:g} bps")
        if not math.isfinite(rec["p_value"]):
            reasons.append("no shuffle-control p-value")
        elif rec["p_value"] > max_p:
            reasons.append(f"shuffle control p={rec['p_value']:.3f} > {max_p:g}")
        rec["reasons"] = reasons
        rec["passed"] = not reasons
        if pending and live:
            rec["state"] = "pending"
        elif pending and cancelled and key not in err_rows:
            rec["state"] = "incomplete"
        else:
            rec["state"] = "fail" if reasons else "pass"
    for key, errs in err_rows.items():                       # every seed of the cell errored: nothing in the results
        if key in by_cell:
            continue
        lv = _parse_label(errs[0]["label"] if "label" in errs[0] else "")
        if lv is None or lv["family"] in BASELINE_FAMILIES or lv["target"] == "volatility":
            continue
        records.append({"key": key, "label": errs[0]["label"], "n_seeds": 0, "mean": float("nan"),
                        "std": float("nan"), "score": float("nan"), "p_value": float("nan"),
                        "null_mean": float("nan"), "low_iter_share": None, "near_constant": False, "regimes": {},
                        "whole": None, "warnings": [], "old_rows": 0, "uses_fallback": False, "regimes_beaten": 0,
                        "unavailable": [], "passed": False, "survivor": False, "rank": None,
                        "state": "pending" if live else "fail", "note": "", "baseline_keys": {}, "baselines": {},
                        "reasons": ["no seed finished (errors)"], **lv})
    # --- survivors per track
    by_track: Dict[str, List[Dict[str, Any]]] = {t: [] for t in G.TRACKS}
    for track in G.TRACKS:
        ranked = sorted((r for r in records if r["track"] == track and r["passed"]),
                        key=lambda r: (-r["score"], r["key"]))
        for i, r in enumerate(ranked, 1):
            r["rank"] = i
            if i <= max_survivors:
                r["survivor"], r["state"] = True, "survivor"
                by_track[track].append(r)
            else:
                r["state"] = "capped"
                r["note"] = f"passes every condition but ranks {i} of {len(ranked)}; only the top {max_survivors} go on"
    state_order = {"survivor": 0, "capped": 1, "pass": 1, "pending": 2, "incomplete": 2, "fail": 3, "excluded": 4, "baseline": 5,
                   "not gated": 6}
    records.sort(key=lambda r: (G.TRACKS.index(r["track"]), state_order[r["state"]],
                                -(r["score"] if math.isfinite(r["score"]) else -math.inf), r["key"]))
    return {"cells": records, "survivors": [r for t in G.TRACKS for r in by_track[t]], "by_track": by_track,
            "params": params, "effects": effects, "excluded": excluded_tables}


# ================================================================== run state
SUMMARY_NAME, REPORT_NAME, DASHBOARD_NAME = "run_summary.json", "report.md", "dashboard.html"


def write_atomic(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` through a UNIQUE temp file in the same directory and ``os.replace``: a reader (the
    artifact publisher, an editor) never sees a half-written file, and two writers never share a temp name. A
    ``PermissionError`` (the target is open in another program on Windows) is retried ``REPLACE_TRIES`` times with a
    short pause; whatever fails, the temp file is deleted."""
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        for attempt in range(REPLACE_TRIES):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if attempt == REPLACE_TRIES - 1:
                    raise
                _sleep(REPLACE_PAUSE_SECONDS * (attempt + 1))
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def read_run_summary(out_dir: Union[str, Path]) -> Dict[str, Any]:
    """``run_summary.json`` of ``out_dir`` ({} when missing or unreadable)."""
    path = Path(out_dir) / SUMMARY_NAME
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, json.JSONDecodeError):
        return {}


PLANNED_NAME = "planned_keys.txt"


def _planned_digest(keys: Sequence[str]) -> str:
    return hashlib.sha1("\n".join(sorted(keys)).encode("utf-8")).hexdigest()


def write_planned(out_dir: Union[str, Path], keys: Sequence[str]) -> str:
    """Write the run's planned ``cell_key:seed`` tasks (sorted, one per line) to ``planned_keys.txt`` once and return
    their hash, which ``run_summary.json`` carries (with the count) instead of the list."""
    ordered = sorted(keys)
    write_atomic(Path(out_dir) / PLANNED_NAME, "\n".join(ordered) + ("\n" if ordered else ""))
    return _planned_digest(ordered)


def _read_planned(path: Path) -> Optional[List[str]]:
    try:
        return path.read_text(encoding="utf-8").split()
    except OSError:
        return None


_SLIM_METRICS = frozenset(m for ms in HEATMAP_METRICS.values() for m in ms) | frozenset(G.PRIMARY.values())
_SLIM_CONFIG = ("track", "store", "symbols", "groups", "target", "family", "train_months", "blocks")


def slim_row(r: Dict[str, Any]) -> Dict[str, Any]:
    """The fields of a ``results.jsonl`` row that the gate, ``factor_effects`` and the renderers read, in the row's own
    layout (so they accept it unchanged): per-regime MEANS of the gate / heatmap metrics, the controls' p-values, whole
    period null means and ``by_regime``, the identity bits the baseline match and the cost check use. The block audit,
    the metrics, the timings and the symbol lists are dropped."""
    out: Dict[str, Any] = {k: r[k] for k in ("cell_key", "track", "label", "status", "n_blocks", "n_blocks_fitted",
                                             "seeds_run", "low_iter_share") if k in r}
    out["seed"] = int(r["seed"])
    if "primary" in r:
        out["primary"] = dict(r["primary"])
    cfg = r["config"] if "config" in r and isinstance(r["config"], dict) else {}
    out["config"] = {k: cfg[k] for k in _SLIM_CONFIG if k in cfg}
    ident = r["identity"] if "identity" in r and isinstance(r["identity"], dict) else {}
    settings = ident["settings"] if "settings" in ident and isinstance(ident["settings"], dict) else {}
    out["identity"] = {**({"store_signature": ident["store_signature"]} if "store_signature" in ident else {}),
                       "settings": {k: settings[k] for k in ("cost_bps",) if k in settings}}
    ver = r["versions"] if "versions" in r and isinstance(r["versions"], dict) else {}
    out["versions"] = {k: ver[k] for k in ("code",) if k in ver}
    reg = r["regimes"] if "regimes" in r and isinstance(r["regimes"], dict) else {}
    out["regimes"] = {m: {name: {"mean": e["mean"], **({"n": e["n"]} if "n" in e else {})} for name, e in reg[m].items()}
                      for m in _SLIM_METRICS if m in reg}
    ctl = r["controls"] if "controls" in r and isinstance(r["controls"], dict) else {}
    slim_ctl: Dict[str, Any] = {k: ctl[k] for k in ("primary", "p_value", "p_values", "by_regime", "skipped") if k in ctl}
    names = set(ctl["p_values"]) if "p_values" in ctl and isinstance(ctl["p_values"], dict) else set()
    if "primary" in ctl and ctl["primary"]:
        names.add(ctl["primary"])
    for n in names:
        if n in ctl and isinstance(ctl[n], dict) and "mean" in ctl[n]:
            slim_ctl[n] = {"mean": ctl[n]["mean"]}
    out["controls"] = slim_ctl
    return out


def _slim_error(e: Dict[str, Any]) -> Dict[str, Any]:
    return {"cell_key": e["cell_key"], "seed": int(e["seed"]), "label": e["label"] if "label" in e else ""}


class JsonlTail:
    """An append-only JSONL file read INCREMENTALLY: ``update`` parses only the bytes added since the last call (a line
    cut short by a write in progress waits for its newline), keeps ``keep(row)`` per ``key(slim)`` (the last row of a
    key wins) and starts over when the file shrank or its head changed (it was rewritten). An unparseable line is
    skipped and counted; only the first is logged."""
    HEAD = 256

    def __init__(self, path: Union[str, Path], keep: Any, key: Any) -> None:
        self.path, self.keep, self.key = Path(path), keep, key
        self.items: Dict[Any, Dict[str, Any]] = {}
        self.parsed_bytes = 0
        self.n_parsed = 0
        self.bad_lines = 0
        self._head = b""

    def _reset(self) -> None:
        self.items.clear()
        self.parsed_bytes = self.n_parsed = self.bad_lines = 0
        self._head = b""

    def _accept(self, line: bytes) -> bool:
        try:
            slim = self.keep(json.loads(line))
            self.items[self.key(slim)] = slim
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            self.bad_lines += 1
            if self.bad_lines == 1:
                logger.warning(f"{self.path}: a line is not valid JSON (an interrupted write?); ignored "
                               f"(further bad lines are only counted)")
            return False
        self.n_parsed += 1
        return True

    def update(self) -> None:
        try:
            size = self.path.stat().st_size
        except FileNotFoundError:
            self._reset()
            return
        if size < self.parsed_bytes:
            self._reset()
        elif self.parsed_bytes:
            with open(self.path, "rb") as fh:
                if fh.read(len(self._head)) != self._head:
                    self._reset()
        if size == self.parsed_bytes:
            return
        with open(self.path, "rb") as fh:
            fh.seek(self.parsed_bytes)
            data = fh.read()
        lines = data.split(b"\n")
        tail = lines.pop()                                    # after the last newline: complete only if it parses
        consumed = 0
        for line in lines:
            consumed += len(line) + 1
            if line.strip():
                self._accept(line)
        if tail.strip():
            try:
                json.loads(tail)
            except json.JSONDecodeError:
                pass                                          # a write in progress: wait for the rest
            else:
                self._accept(tail)
                consumed += len(tail)
        if not self.parsed_bytes and consumed:
            self._head = data[:min(self.HEAD, consumed)]
        self.parsed_bytes += consumed

    def rows(self) -> List[Dict[str, Any]]:
        return list(self.items.values())


class StateCache:
    """The parsed state of one grid output directory between rebuilds: the slim rows and errors (incremental), and the
    time bookkeeping of the rebuild throttle."""

    def __init__(self, out_dir: Union[str, Path]) -> None:
        out = Path(out_dir)
        self.results = JsonlTail(out / "results.jsonl", slim_row, lambda s: (s["cell_key"], s["seed"]))
        self.errors = JsonlTail(out / "errors.jsonl", _slim_error, lambda s: (s["cell_key"], s["seed"]))
        self.last_start: Optional[float] = None
        self.last_duration = 0.0
        self._planned_path = out / PLANNED_NAME
        self._planned_for: Optional[str] = None                 # the hash the cached list belongs to
        self._planned: Optional[set] = None

    def planned(self, digest: Optional[str]) -> Optional[set]:
        """The planned ``cell_key:seed`` set of the run whose hash is ``digest`` (``run_summary.json``): read from
        ``planned_keys.txt`` ONCE per cache (again only when the hash changes), None when there is none or the file
        does not match the hash (another run's)."""
        if not digest:
            return None
        if self._planned_for != digest:
            keys = _read_planned(self._planned_path)
            self._planned_for = digest
            self._planned = set(keys) if keys is not None and _planned_digest(keys) == digest else None
        return self._planned

    def due(self, now: float) -> bool:
        """Whether a live (non-final) rebuild may run now: at most every max(60 s, 10 x the last build's duration)."""
        return (self.last_start is None
                or now - self.last_start >= max(THROTTLE_MIN_SECONDS, THROTTLE_FACTOR * self.last_duration))


_CACHES: Dict[str, StateCache] = {}


def get_cache(out_dir: Union[str, Path]) -> StateCache:
    return _CACHES.setdefault(str(Path(out_dir).resolve()), StateCache(out_dir))


def drop_cache(out_dir: Union[str, Path]) -> None:
    _CACHES.pop(str(Path(out_dir).resolve()), None)


def write_run_summary(out_dir: Union[str, Path], summary: Dict[str, Any]) -> None:
    """Merge ``summary`` (JSON-able run facts: planned_tasks, done, errors, pool_rebuilds, ...) into ``run_summary.json``."""
    out = Path(out_dir)
    path = out / SUMMARY_NAME
    cur: Dict[str, Any] = {}
    if path.exists():
        try:
            cur = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            cur = {}
    cur.update(G._sanitize(summary))
    write_atomic(path, json.dumps(cur, indent=1, sort_keys=True))


def run_is_live(out_dir: Union[str, Path]) -> bool:
    """True while a live run holds ``out_dir`` (its lock file names a running process)."""
    try:
        G.check_lock(Path(out_dir))
    except G.GridLockedError:
        return True
    return False


def collect_state(out_dir: Union[str, Path], summary: Optional[Dict[str, Any]] = None,
                  live: Optional[bool] = None, cache: Optional[StateCache] = None,
                  strict_old_rows: bool = True) -> Dict[str, Any]:
    """Everything the report and the dashboard show, read from ``out_dir``: the (slim) rows, the unresolved errors, the
    run summary (``run_summary.json`` merged with ``summary``), the gate and the run identity. ``live``: the run is
    still going (None: a live lock in ``out_dir`` says so); only then is an unfinished cell "pending". ``cache``: a
    ``StateCache`` that parses only what was appended since its last use (default: a fresh one, a full parse)."""
    out = Path(out_dir)
    c = cache if cache is not None else StateCache(out)
    c.results.update()
    c.errors.update()
    rows, err_rows = c.results.rows(), c.errors.rows()
    keys = set(c.results.items)
    unresolved = set(c.errors.items) - keys
    merged = {**read_run_summary(out), **(G._sanitize(summary) if summary else {})}
    is_live = run_is_live(out) if live is None else live
    codes = sorted({r["versions"]["code"] for r in rows if "code" in r["versions"]})
    planned = c.planned(merged["planned_hash"] if "planned_hash" in merged else None)
    if planned:
        done = {f"{k}:{sd}" for k, sd in keys | unresolved}
        n_progress, n_planned = len(planned & done), len(planned)
    else:
        n_progress = len(keys) + len(unresolved)
        n_planned = (merged["planned_count"] if "planned_count" in merged else
                     merged["planned_tasks"] if "planned_tasks" in merged else None)
    cancelled = bool(merged["cancelled"]) if "cancelled" in merged else False
    return {"out_dir": out, "rows": rows, "n_rows": len(keys), "n_errors": len(unresolved), "summary": merged,
            "n_progress": n_progress, "n_planned": n_planned, "live": is_live,
            "gate": gate(rows, live=is_live, errors=err_rows, strict_old_rows=strict_old_rows, cancelled=cancelled),
            "identity": {"dir": out.name, "code": G.library_versions()["code"], "row_codes": codes,
                         "updated": datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S UTC%z")}}


def _excluded_counts(state: Dict[str, Any]) -> Dict[str, int]:
    return {t: int(len(state["gate"]["excluded"][t])) for t in G.TRACKS}


# ================================================================== report.md
def _md_cell(x: Any) -> str:
    """A table cell: pipes, backticks and ``<`` escaped (a label or reason must not open a tag or a code span)."""
    return str(x).replace("|", "\\|").replace("`", "\\`").replace("<", "&lt;").replace("\n", " ")


def _md_table(headers: Sequence[str], body: Sequence[Sequence[Any]]) -> str:
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    lines += ["| " + " | ".join(_md_cell(c) for c in r) + " |" for r in body]
    return "\n".join(lines)


def _cell_text(c: Dict[str, Any]) -> str:
    return f"{c['target']} h{c['horizon']} {c['family']} {c['train_months']}m {c['groups']}"


def _mean_std(c: Dict[str, Any]) -> str:
    if not math.isfinite(c["mean"]):
        return "n/a"
    return f"{c['mean']:+.4f} ± " + (f"{c['std']:.4f}" if math.isfinite(c["std"]) else "n/a")


REPORT_REGIME_ROWS = 100

#: what a reader of the stage-1 factor tables must know before comparing levels
CONFOUNDING_NOTE = (
    "The stage-1 core grid is unbalanced, so a factor's mean is not its effect: (1) target x family availability: some "
    "targets exist for some families only (`rank` has no linear baseline, `quantile` no naive or linear, `terciles` no naive; "
    "the darts families take `rel_return` / `quantile` only and every deep family needs half-year blocks), so a target level and a family level share "
    "cells; (2) the feature groups are confounded with tsai: a tsai cell reads the 1h panel (`panel_1h`) and no store group, so "
    "a groups level that holds tsai cells measures the architecture, not the features; (3) the horizon is fixed (5) in the "
    "core grid, so its effect is not estimated at all. A level mean is therefore the mean over whatever cells happened to "
    "carry it; read the within-family tables below and the common-period column before ranking levels.")


def _regime_text(r: Dict[str, Any]) -> str:
    """``value vs null (p=..)`` of one regime of a gate record, a check mark when the regime counted."""
    if r["value"] is None:
        return "n/a"
    null = "n/a" if r["null_mean"] is None else f"{r['null_mean']:+.4f}"
    p = "n/a" if r["p_value"] is None else f"{r['p_value']:.3f}"
    return f"{r['value']:+.4f} vs null {null} (p={p}){' ✓' if r['counted'] else ''}"


def _beaten_text(c: Dict[str, Any]) -> str:
    """``"4 of 6"``; the regimes the cell could not be scored in are named: ``"4 of 6 (n/a: 2019, 2020crash)"``."""
    na = c["unavailable"] if "unavailable" in c else []
    return f"{c['regimes_beaten']} of 6" + (f" (n/a: {', '.join(na)})" if na else "")


def _baseline_text(c: Dict[str, Any]) -> str:
    """"naive baseline: fallback (<kind>)" for every baseline taken from another target (empty when none was)."""
    return "; ".join(f"{fam} baseline: fallback ({b['kind']})" for fam, b in c["baselines"].items() if b["fallback"])


def _flag_text(c: Dict[str, Any]) -> str:
    return f"near-constant {100 * c['low_iter_share']:.0f}%" if c["near_constant"] else ""


def _why(c: Dict[str, Any]) -> str:
    """Reasons (failed conditions), then warnings (fallbacks that do not fail the cell), else the cap note."""
    return "; ".join(c["reasons"] + c["warnings"]) or c["note"]


def _p_text(c: Dict[str, Any]) -> str:
    return f"{c['p_value']:.3f}" if math.isfinite(c["p_value"]) else "n/a"


def render_report(state: Dict[str, Any]) -> str:
    g, ident, s = state["gate"], state["identity"], state["summary"]
    p = g["params"]
    exc = _excluded_counts(state)
    rcodes = ", ".join("`" + c + "`" for c in ident["row_codes"]) or "n/a"
    out = ["# ML grid stage 1 report", "",
           f"Run: `{ident['dir']}` · code `{ident['code']}` · rows computed with code {rcodes} · "
           f"generated {ident['updated']}", "", "## Run summary", ""]
    planned = s["planned_tasks"] if "planned_tasks" in s else None
    if "pool_rebuilds_total" in s and "pool_rebuilds" in s:
        rebuilds = f"{s['pool_rebuilds']} in the last run, {s['pool_rebuilds_total']} in total"
    else:
        rebuilds = s["pool_rebuilds"] if "pool_rebuilds" in s else "unknown (not recorded)"
    out.append(_md_table(["item", "value"], [
        ["rows done", f"{state['n_progress']}" + (f" of {state['n_planned']} planned" if state["n_planned"] else "")],
        ["rows in results.jsonl", state["n_rows"]],
        ["errors (unresolved)", state["n_errors"]],
        ["pool_rebuilds", rebuilds],
        ["excluded cells", f"{sum(exc.values())} ({', '.join(f'{t} {n}' for t, n in exc.items())})"],
        ["cancelled", s["cancelled"] if "cancelled" in s else "unknown"]]))
    low = (f" or more than {p['max_low_iter_share']:g} of blocks near-constant"
           if p["max_low_iter_share"] is not None else "")
    out += ["", "## Gate rules", "",
            f"A candidate cell passes when, on the seed mean, it beats BOTH baselines (naive momentum and linear, same "
            f"track / store / symbols / target / horizon; else the strongest of that family on the same track / store / "
            f"symbols / horizon with any target, marked fallback) and its shuffle control in at least {p['min_regimes']} of the "
            f"{len(p['regimes'])} regimes ({', '.join(p['regimes'])}), including {' or '.join(p['hard_regimes'])}; its "
            f"primary metric is positive after {p['cost_bps']:g} bps; the shuffle control's p (median over seeds) is "
            f"at most {p['max_p']:g}. Cells are ranked by seed mean − seed std; at most {p['max_survivors']} survivors "
            f"per track. Cells with coverage under {p['min_coverage']:g}{low} are excluded first; a cell whose models "
            f"were near-constant in more than {p['low_iter_flag_share']:g} of the blocks carries a \"near-constant k%\" "
            f"flag but is not excluded. A regime counts as "
            f"beating the shuffle control when its value is above that regime's null mean (the control's mean over the "
            f"shuffles of the same regime metric); the significance is the whole-period p. A cell with unfinished "
            f"seeds is pending only while the run is live; after it, it fails.", "", "## Survivors", ""]
    if not g["survivors"]:
        out += ["No cell passed the gate. The factor-effect tables below are the deliverable; the exploration stops "
                "here (design section 4).", ""]
    else:
        for track in G.TRACKS:
            sv = g["by_track"][track]
            out += [f"### {track}", ""]
            if not sv:
                out += ["No survivor.", ""]
                continue
            out += [_md_table(["rank", "cell", "key", "seed mean ± std", "mean − std", "regimes beaten", "p", "notes"],
                              [[c["rank"], _cell_text(c), c["key"], _mean_std(c), _fmt(c["score"]),
                                _beaten_text(c), _p_text(c),
                                "old-row fallback" if c["uses_fallback"] else ""] for c in sv]), ""]
    common = g["effects"][G.TRACKS[0]]["common_start"]
    out += ["## Reading the factor tables", "", CONFOUNDING_NOTE, "",
            "## Factor effects", "",
            "Mean of the cell-level seed means per factor level (cells kept by the coverage filters), with the spread "
            "across cells. Primary metric: selection = quintile spread net of cost, timing = timing excess net of cost. "
            f"**common mean** (common period from {common}): the same metric over the regimes from {common} on only, "
            f"pooled by their trading days and the same for every cell, so families scored over different spans (a long "
            f"window cannot score 2019 or the 2020 crash) are compared on one period; `n/a` when no cell of the level can "
            f"score all of those regimes.", ""]
    for track in G.TRACKS:
        f = g["effects"][track]["factors"]
        out += [f"### {track} ({G.PRIMARY[track]})", ""]
        if not len(f):
            out += ["No finished cells.", ""]
            continue
        out += [_md_table(["factor", "level", "cells", "mean", "std", "median", "share > 0", "common mean"],
                          [[fac, lev, int(r["n_cells"]), _fmt(r["mean"]),
                            f"{r['std']:.4f}" if math.isfinite(r["std"]) else "n/a", _fmt(r["median"]),
                            f"{r['share_positive']:.2f}", _fmt(r["common_mean"])] for (fac, lev), r in f.iterrows()]), ""]
    out += ["## Factors within each family", "",
            "Each factor again, inside ONE family, so its effect is not mixed with the family's (the grid is not "
            "balanced, see above). Same metrics: mean of the cell-level seed means and the common mean.", ""]
    for track in G.TRACKS:
        b = g["effects"][track]["by_family"]
        out += [f"### {track} ({G.PRIMARY[track]})", ""]
        if not len(b):
            out += ["No finished cells.", ""]
            continue
        for family in b.index.get_level_values("family").unique():
            sub = b.xs(family, level="family")
            out += [f"#### {family}", "",
                    _md_table(["factor", "level", "cells", "mean", "common mean"],
                              [[fac, lev, int(r["n_cells"]), _fmt(r["mean"]), _fmt(r["common_mean"])]
                               for (fac, lev), r in sub.iterrows()]), ""]
    out += ["## Gate table", ""]
    for track in G.TRACKS:
        cs = [c for c in g["cells"] if c["track"] == track]
        out += [f"### {track}", ""]
        if not cs:
            out += ["No finished cells.", ""]
            continue
        out += [_md_table(["rank", "cell", "key", "seed mean ± std", "mean − std", "regimes beaten", "p", "result",
                           "flags", "baseline", "reasons"],
                          [[c["rank"] or "", _cell_text(c), c["key"], _mean_std(c), _fmt(c["score"]),
                            _beaten_text(c) if c["regimes"] else "", _p_text(c), c["state"].upper(),
                            _flag_text(c), _baseline_text(c), _why(c)] for c in cs]), ""]
    out += ["## Per-regime control", "",
            "Per regime: the cell's seed-mean value of the primary metric against that regime's shuffle-control null "
            "(mean over the shuffles of the same regime metric; the control with the larger p on the timing track), "
            "and the median over seeds of the regime's one-sided p. A check mark means the regime counted (above both "
            "baselines and the null). The regime p is shown, it does not gate; the whole-period p does.", ""]
    for track in G.TRACKS:
        cs = [c for c in g["cells"] if c["track"] == track and c["regimes"]]
        out += [f"### {track}", ""]
        if not cs:
            out += ["No gated cells.", ""]
            continue
        top = cs[:REPORT_REGIME_ROWS]
        out += [_md_table(["key", "cell"] + list(GATE_REGIMES),
                          [[c["key"], _cell_text(c)] + [_regime_text(c["regimes"][r]) for r in GATE_REGIMES]
                           for c in top]), ""]
        if len(cs) > len(top):
            out += [f"Top {len(top)} of {len(cs)} cells by gate state and rank.", ""]
    out += ["## Excluded cells", ""]
    any_ex = False
    for track in G.TRACKS:
        ex = g["excluded"][track]
        if len(ex):
            any_ex = True
            out += [f"### {track}", "", _md_table(
                ["key", "reason", "cell"],
                [[r["cell_key"], r["reason"], f"{r['target']} h{r['horizon']} {r['family']} {r['train_months']}m "
                                              f"{r['groups']}"] for _, r in ex.iterrows()]), ""]
    if not any_ex:
        out += ["None.", ""]
    return "\n".join(out) + "\n"


# ================================================================== dashboard.html
LIGHT = {"bg": "#fbfbfa", "fg": "#1b1b1a", "muted": "#5c5b57", "card": "#ffffff", "border": "#dcdbd5",
         "accent": "#6250d6", "pos": "#2a78d6", "neg": "#eb6834", "neutral": "#f0efec", "good": "#0c8a0c",
         "bad": "#d03b3b", "pending": "#b7791f", "bar-track": "#ecebe6"}
DARK = {"bg": "#161615", "fg": "#efeee9", "muted": "#a9a8a0", "card": "#1f1f1d", "border": "#3a3a37",
        "accent": "#9a8cf5", "pos": "#3987e5", "neg": "#d95926", "neutral": "#383835", "good": "#2fbf2f",
        "bad": "#e05555", "pending": "#e0a020", "bar-track": "#2a2a28"}


def _tokens(d: Dict[str, str], scheme: str) -> str:
    return "".join(f"--{k}: {v}; " for k, v in d.items()) + f"color-scheme: {scheme}"


CSS = """
:root { %(light)s }
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) { %(dark)s }
}
:root[data-theme="dark"] { %(dark)s }
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--fg);
  font: 14px/1.45 ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
  padding-block: 20px 40px; padding-left: max(16px, env(safe-area-inset-left, 0px));
  padding-right: max(16px, env(safe-area-inset-right, 0px)); font-variant-numeric: tabular-nums; }
main { max-width: 1200px; margin: 0 auto; }
h1 { font-size: 22px; margin: 0 0 4px; }
h2 { font-size: 16px; margin: 28px 0 8px; padding-bottom: 4px; border-bottom: 1px solid var(--border); }
h3 { font-size: 14px; margin: 18px 0 6px; color: var(--accent); }
p, li { overflow-wrap: anywhere; }
.muted { color: var(--muted); }
.small { font-size: 12px; }
.ident { color: var(--muted); font-size: 12px; overflow-wrap: anywhere; }
.tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(130px, 1fr)); gap: 10px; margin-top: 14px; }
.tile { background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 10px 12px; min-width: 0; }
.tile .k { font-size: 11px; text-transform: uppercase; letter-spacing: .04em; color: var(--muted); }
.tile .v { font-size: 22px; font-weight: 600; margin-top: 2px; }
.tile .s { font-size: 12px; color: var(--muted); overflow-wrap: anywhere; }
.scroll { overflow-x: auto; max-width: 100%%; border: 1px solid var(--border); border-radius: 8px; background: var(--card); }
table { border-collapse: collapse; width: 100%%; font-size: 12px; }
th, td { padding: 4px 8px; text-align: right; white-space: nowrap; border-bottom: 1px solid var(--border); }
th { color: var(--muted); font-weight: 600; }
th.l, td.l { text-align: left; }
td.wrap { white-space: normal; min-width: 220px; max-width: 420px; text-align: left; overflow-wrap: anywhere; }
tr:last-child td { border-bottom: 0; }
td.sticky, th.sticky { position: sticky; left: 0; background: var(--card); text-align: left; z-index: 1; }
.hm { background: color-mix(in srgb, var(--hc) var(--m), var(--neutral)); color: var(--fg); min-width: 52px; }
.hm.pos { --hc: var(--pos); }
.hm.neg { --hc: var(--neg); }
.hm.na { background: var(--card); color: var(--muted); }
.hm.all { border-left: 2px solid var(--border); }
.legend { display: flex; gap: 8px; align-items: center; font-size: 12px; color: var(--muted); margin: 6px 0; flex-wrap: wrap; }
.legend .sw { width: 18px; height: 12px; border-radius: 2px; border: 1px solid var(--border); display: inline-block; }
.sw.p { background: var(--pos); } .sw.z { background: var(--neutral); } .sw.n { background: var(--neg); }
.chip { --c: var(--muted); display: inline-block; padding: 1px 8px; border-radius: 999px; font-size: 11px;
  font-weight: 600; border: 1px solid var(--c); color: var(--fg); background: color-mix(in srgb, var(--c) 16%%, var(--card)); }
.chip[data-state="survivor"] { --c: var(--good); }
.chip[data-state="capped"] { --c: var(--good); }
.chip[data-state="fail"] { --c: var(--bad); }
.chip[data-state="pending"] { --c: var(--pending); }
.chip[data-state="excluded"] { --c: var(--muted); }
.chip[data-state="baseline"] { --c: var(--accent); }
.chip[data-state="not gated"] { --c: var(--muted); }
.chip[data-state="flag"] { --c: var(--pending); }
.chip[data-state="incomplete"] { --c: var(--pending); }
.fx { display: grid; grid-template-columns: minmax(90px, 1.1fr) minmax(0, 3fr) minmax(70px, auto); gap: 4px 10px;
  align-items: center; font-size: 12px; }
.fx .grp { grid-column: 1 / -1; color: var(--muted); font-weight: 600; margin-top: 8px; }
.bar-track { position: relative; height: 12px; background: var(--bar-track); border-radius: 3px; min-width: 0; }
.bar-track::after { content: ""; position: absolute; left: 50%%; top: -2px; bottom: -2px; width: 1px; background: var(--muted); }
.bar { position: absolute; top: 0; bottom: 0; border-radius: 3px; }
.bar.pos { background: var(--pos); }
.bar.neg { background: var(--neg); }
.dots { display: inline-flex; gap: 2px; vertical-align: middle; margin-left: 6px; }
.dot { width: 7px; height: 7px; border-radius: 2px; background: var(--border); display: inline-block; }
.dot.on { background: var(--good); }
.dot.na { background: repeating-linear-gradient(45deg, var(--border), var(--border) 2px, transparent 2px, transparent 4px); }
.note { background: var(--card); border: 1px solid var(--border); border-left: 3px solid var(--accent); border-radius: 6px;
  padding: 8px 12px; margin: 10px 0; }
.empty { color: var(--muted); padding: 12px 0; }
""" % {"light": _tokens(LIGHT, "light"), "dark": _tokens(DARK, "dark")}


def _e(x: Any) -> str:
    return _html.escape(str(x), quote=True)


_CHIP_GLYPH = {"survivor": "&#10003;", "capped": "&#8805;", "fail": "&#10007;", "pending": "&#8230;",
               "excluded": "&#8856;", "baseline": "&#9670;", "not gated": "&#8211;", "flag": "&#9888;",
               "incomplete": "&#8943;"}
_CHIP_LABEL = {"survivor": "survivor", "capped": "over cap", "fail": "fail", "pending": "pending",
               "excluded": "excluded", "baseline": "baseline", "not gated": "not gated", "flag": "flag",
               "incomplete": "incomplete (cancelled)"}


def _chip(state: str) -> str:
    return f'<span class="chip" data-state="{_e(state)}">{_CHIP_GLYPH[state]} {_e(_CHIP_LABEL[state])}</span>'


def _flag_chip(c: Dict[str, Any]) -> str:
    """The "near-constant k%" flag chip of a cell (empty when it is not flagged)."""
    if not c["near_constant"]:
        return ""
    return f' <span class="chip" data-state="flag">{_CHIP_GLYPH["flag"]} {_e(_flag_text(c))}</span>'


def _short(c: Dict[str, Any]) -> str:
    return f"{c['target']} h{c['horizon']} · {c['family']} · {c['train_months']}m · {c['groups']}"


def _heat_cell(v: float, scale: float, all_col: bool, tip: str = "") -> str:
    cls = "all" if all_col else ""
    title = f' title="{_e(tip)}"' if tip else ""
    if not math.isfinite(v):
        return f'<td class="hm na {cls}"{title}>&ndash;</td>'
    m = min(1.0, abs(v) / scale) * 65.0 if scale > 0 else 0.0
    return f'<td class="hm {"pos" if v >= 0 else "neg"} {cls}" style="--m:{m:.0f}%"{title}>{v:+.3f}</td>'


def _control_tip(label: str, e: Dict[str, Any]) -> str:
    """Tooltip of a primary-metric heat cell: the value, the control's null there (mean ± std) and the p."""
    if e["value"] is None:
        if "available" in e and not e["available"]:
            return f"{label}: n/a (no block of this regime was fitted)"
        return f"{label}: not enough rows"
    parts = [f"{label}: {e['value']:+.4f}"]
    if e["null_mean"] is not None:
        sd = "" if e["null_std"] is None else f" ± {e['null_std']:.4f}"
        p = "p n/a" if e["p_value"] is None else f"p={e['p_value']:.3f}"
        ctl = e["control"] if e["control"] else "control"
        parts.append(f"{ctl} null {e['null_mean']:+.4f}{sd}, {p}")
    return " | ".join(parts)


def _heatmap(state: Dict[str, Any], track: str) -> str:
    g = state["gate"]
    last: Dict[Tuple[str, int], Dict[str, Any]] = {}
    for r in state["rows"]:
        last[(r["cell_key"], int(r["seed"]))] = r
    by_key: Dict[str, List[Dict[str, Any]]] = {}
    for (key, _), r in last.items():
        if r["status"] == "ok" and r["track"] == track:
            by_key.setdefault(key, []).append(r)
    cells = [c for c in g["cells"] if c["track"] == track and c["key"] in by_key and c["state"] != "not gated"]
    base = [c for c in cells if c["state"] == "baseline"][:4]
    rest = [c for c in cells if c["state"] != "baseline"]
    shown = base + rest[:HEATMAP_MAX_ROWS]
    if not shown:
        return '<p class="empty">No finished cells yet.</p>'
    metrics = HEATMAP_METRICS[track]
    cols = ("all",) + GATE_REGIMES
    data = {c["key"]: {m: _seed_mean_regimes(by_key[c["key"]], m) for m in metrics} for c in shown}
    scales = {}
    for m in metrics:
        vals = [abs(v) for c in shown for v in data[c["key"]][m].values() if math.isfinite(v)]
        scales[m] = float(np.percentile(vals, 95)) if vals else 0.0
    head1 = '<tr><th class="sticky" rowspan="2">cell</th><th rowspan="2">gate</th>' + "".join(
        f'<th colspan="{len(cols)}">{_e(m)}</th>' for m in metrics) + "</tr>"
    head2 = "<tr>" + "".join(f"<th>{_e(c)}</th>" for _ in metrics for c in cols) + "</tr>"
    body = []
    for c in shown:
        def tip(m: str, col: str, c: Dict[str, Any] = c) -> str:
            if m != metrics[0] or not c["regimes"]:
                return f"{col} {m}"
            return _control_tip(col, c["whole"] if col == "all" else c["regimes"][col])
        tds = "".join(_heat_cell(data[c["key"]][m][col], scales[m], col == "all", tip(m, col))
                      for m in metrics for col in cols)
        body.append(f'<tr class="hmrow"><td class="sticky">{_e(_short(c))} <span class="muted small">{_e(c["key"])}'
                    f'</span></td><td>{_chip(c["state"])}{_flag_chip(c)}</td>{tds}</tr>')
    note = ""
    if len(rest) > HEATMAP_MAX_ROWS:
        note = (f'<p class="muted small">Showing the top {HEATMAP_MAX_ROWS} of {len(rest)} cells by gate rank '
                f'(and the baselines); the full list is in results.jsonl and report.md.</p>')
    legend = ('<div class="legend"><span class="sw n"></span> negative <span class="sw z"></span> zero '
              '<span class="sw p"></span> positive &middot; seed-mean value per regime, colour scaled per metric '
              '(95th percentile of |value|)</div>')
    return f'{legend}<div class="scroll"><table>{head1}{head2}{"".join(body)}</table></div>{note}'


def _fx_bars(state: Dict[str, Any], track: str) -> str:
    f = state["gate"]["effects"][track]["factors"]
    if not len(f):
        return '<p class="empty">No finished cells yet.</p>'
    scale = max([abs(v) for v in f["mean"] if math.isfinite(v)] or [0.0])
    out, cur = ['<div class="fx">'], None
    for (fac, lev), r in f.iterrows():
        if fac != cur:
            out.append(f'<div class="grp">{_e(fac)}</div>')
            cur = fac
        v, sd = float(r["mean"]), float(r["std"])
        w = min(50.0, 50.0 * abs(v) / scale) if scale > 0 and math.isfinite(v) else 0.0
        left = 50.0 if v >= 0 else 50.0 - w
        sdt = "n/a" if not math.isfinite(sd) else f"{sd:.3f}"
        out.append(f'<div>{_e(lev)} <span class="muted small">n={int(r["n_cells"])}</span></div>'
                   f'<div class="bar-track" title="{_e(fac)}={_e(lev)}: mean {v:+.4f}">'
                   f'<span class="bar {"pos" if v >= 0 else "neg"}" style="left:{left:.1f}%;width:{w:.1f}%"></span></div>'
                   f'<div>{v:+.4f}<span class="muted small"> ±{sdt}</span></div>')
    out.append("</div>")
    return "".join(out)


GATE_TABLE_MAX_ROWS = 300


def _dot(regime: str, e: Dict[str, Any]) -> str:
    """One regime dot of the gate table: green when the regime counted, grey when it did not, hatched (n/a) when the cell
    could not be scored in it at all."""
    if not e["available"]:
        return f'<i class="dot na" title="{_e(regime)}: n/a"></i>'
    return f'<i class="dot{" on" if e["counted"] else ""}" title="{_e(regime)}"></i>'


def _gate_table(state: Dict[str, Any], track: str) -> str:
    cs = [c for c in state["gate"]["cells"] if c["track"] == track]
    if not cs:
        return '<p class="empty">No finished cells yet.</p>'
    rows = []
    for c in cs[:GATE_TABLE_MAX_ROWS]:
        if c["regimes"]:
            dots = "".join(_dot(r, c["regimes"][r]) for r in GATE_REGIMES)
            beat = f'{c["regimes_beaten"]}/6<span class="dots">{dots}</span>'
        else:
            beat = "&ndash;"
        why = _why(c)
        bl = _baseline_text(c)
        rows.append(f'<tr><td>{c["rank"] or ""}</td><td class="l">{_e(_short(c))} <span class="muted small">'
                    f'{_e(c["key"])}</span></td><td>{_e(_mean_std(c))}</td><td>{_e(_fmt(c["score"]))}</td>'
                    f'<td>{beat}</td><td>{_e(_p_text(c))}</td><td class="l">{_chip(c["state"])}{_flag_chip(c)}</td>'
                    f'<td class="wrap small">{_e(why)}'
                    f'{f"<br><span class=muted>{_e(bl)}</span>" if bl else ""}</td></tr>')
    head = ('<tr><th>#</th><th class="l">cell</th><th>seed mean ± std</th><th>mean − std</th><th>regimes beaten</th>'
            '<th>p</th><th class="l">gate</th><th class="l">reasons</th></tr>')
    note = (f'<p class="muted small">Showing {GATE_TABLE_MAX_ROWS} of {len(cs)} cells (best states first); '
            f'report.md has all of them.</p>') if len(cs) > GATE_TABLE_MAX_ROWS else ""
    return f'<div class="scroll"><table>{head}{"".join(rows)}</table></div>{note}'


def _tile(k: str, v: str, s: str = "") -> str:
    return f'<div class="tile"><div class="k">{_e(k)}</div><div class="v">{v}</div><div class="s">{s}</div></div>'


def render_dashboard(state: Dict[str, Any]) -> str:
    """The self-contained artifact page (no doctype / html / head / body, no external resource, colours as tokens)."""
    g, ident, s = state["gate"], state["identity"], state["summary"]
    n, planned = state["n_progress"], state["n_planned"]
    prog = f"{n}/{planned}" if planned else f"{n}"
    pct = f"{100.0 * n / planned:.0f}% of the planned (cell, seed) tasks" if planned else "planned total unknown"
    exc = _excluded_counts(state)
    surv = len(g["survivors"])
    cands = len([c for c in g["cells"] if c["state"] not in ("baseline", "not gated")])
    rebuilds = s["pool_rebuilds_total"] if "pool_rebuilds_total" in s else (
        s["pool_rebuilds"] if "pool_rebuilds" in s else "n/a")
    old_cells = [c for c in g["cells"] if c["old_rows"]]
    old_surv = sum(1 for c in g["survivors"] if c["uses_fallback"])
    tiles = "".join([
        _tile("Progress", _e(prog), _e(pct)),
        _tile("Errors", _e(state["n_errors"]), "unresolved (cell, seed) tasks"),
        _tile("Survivors", _e(surv), _e(" · ".join(f"{t} {len(g['by_track'][t])}" for t in G.TRACKS))),
        _tile("Cells", _e(cands), _e(f"candidates seen, {sum(1 for c in g['cells'] if c['near_constant'])} near-constant")),
        _tile("Excluded", _e(sum(exc.values())), "low block coverage"),
        _tile("Old rows", _e(len(old_cells)), _e(f"cells with rows lacking per-regime controls; {old_surv} survivors "
                                                  f"rely on the fallback")),
        _tile("Pool rebuilds", _e(rebuilds), "worker pool restarts")])
    rc = ", ".join(ident["row_codes"]) or "n/a"
    out = [f"<title>{_e(DASHBOARD_TITLE)}</title>", '<meta charset="utf-8">',
           '<meta name="claude:viewport-fit-sides" content="cover">',
           f"<style>{CSS}</style>", "<main>", f"<h1>{_e(DASHBOARD_TITLE)}</h1>",
           f'<div class="ident">Run <b>{_e(ident["dir"])}</b> &middot; code {_e(ident["code"])} (rows computed with '
           f'{_e(rc)}) &middot; Last updated {_e(ident["updated"])}</div>',
           f'<div class="tiles">{tiles}</div>']
    if not state["rows"]:
        out.append('<p class="empty">No finished rows yet. The page fills in as the grid runs.</p>')
    if surv:
        items = "".join(f'<li>{_e(c["track"])} #{c["rank"]}: {_e(_short(c))} <span class="muted small">{_e(c["key"])}'
                        f' &middot; {_e(_mean_std(c))}</span>'
                        f'{" <b>(old-row fallback)</b>" if c["uses_fallback"] else ""}</li>' for c in g["survivors"])
        out.append(f'<h2>Survivors</h2><div class="note"><ul>{items}</ul></div>')
    elif state["rows"]:
        out.append('<div class="note">No cell has passed the gate (yet).</div>')
    for title, fn in (("Heatmap: cells &times; metrics by regime", _heatmap), ("Factor effects", _fx_bars),
                      ("Gate", _gate_table)):
        out.append(f"<h2>{title}</h2>")
        for track in G.TRACKS:
            extra = f" ({_e(G.PRIMARY[track])})" if fn is _fx_bars else ""
            out.append(f"<h3>{_e(track)}{extra}</h3>{fn(state, track)}")
    p = g["params"]
    out.append(f'<p class="muted small">Gate: beat both baselines and the shuffle control in &ge;{p["min_regimes"]} of '
               f'{len(p["regimes"])} regimes incl. {_e(" or ".join(p["hard_regimes"]))}; positive after '
               f'{p["cost_bps"]:g} bps; control p &le; {p["max_p"]:g}; top {p["max_survivors"]} per track by '
               f'mean &minus; std.</p>')
    out.append("</main>")
    return "\n".join(out) + "\n"


# ================================================================== orchestration
def build_outputs(out_dir: Union[str, Path], *, summary: Optional[Dict[str, Any]] = None, report: bool = True,
                  dashboard: bool = True, live: Optional[bool] = None, cache: Optional[StateCache] = None,
                  strict_old_rows: bool = True) -> Dict[str, Any]:
    """Rebuild ``dashboard.html`` (and ``report.md`` when ``report``) of ``out_dir`` from its ``results.jsonl``, each
    written atomically. Reads only; runs nothing. ``live`` / ``cache`` / ``strict_old_rows``: see ``collect_state``.
    Returns the state (rows, gate, identity, summary)."""
    out = Path(out_dir)
    st = collect_state(out, summary, live, cache, strict_old_rows)
    if dashboard:
        write_atomic(out / DASHBOARD_NAME, render_dashboard(st))
    if report:
        write_atomic(out / REPORT_NAME, render_report(st))
    return st


def refresh(out_dir: Union[str, Path], summary: Dict[str, Any], final: bool, force: bool = False) -> bool:
    """The runner's hook: the dashboard after a batch of finished rows; at the end (``final``) also the report and the
    saved run summary (so ``--report-only`` can show what the run did). A live rebuild is throttled to at most one
    per max(60 s, 10 x the last build's duration) and parses only what was appended since the last one
    (``StateCache``); ``final`` and ``force`` (the run's start) always build. Returns whether it built."""
    cache = get_cache(out_dir)
    now = _now()
    if not (final or force) and not cache.due(now):
        return False
    cache.last_start = now
    summary = dict(summary)
    if "pool_rebuilds" in summary:
        summary["pool_rebuilds_total"] = int(summary["prior_pool_rebuilds"] if "prior_pool_rebuilds" in summary
                                             else 0) + int(summary["pool_rebuilds"])
    write_run_summary(out_dir, summary)
    build_outputs(out_dir, summary=summary, report=final, live=not final, cache=cache)
    cache.last_duration = _now() - now
    if final:
        drop_cache(out_dir)
    return True
