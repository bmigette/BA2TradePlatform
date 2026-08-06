#!/usr/bin/env python
"""Label each optimization's persisted top-N backtests `robust` / `fragile` as the jobs land.

WHY THIS EXISTS
---------------
A grid ranks genomes on ONE path through history. That single number cannot distinguish an edge
from a lucky ordering, and the fitness deliberately does not try to: skew is a legitimate return
profile, so `consistent_annual_return` is concentration-agnostic on purpose (see
docs, and the 2026-08-06 decision not to add a concentration penalty). The judgement about
whether a given result is TRUSTWORTHY therefore belongs here, after the fact, not in the GA.

Measured 2026-08-06 on the first four goal2020 jobs: 15 of 18 persisted backtests had their top-5
trades carrying >= 60% of net P&L, and several of those out-ranked the diversified ones. Deciding
that by hand per job does not scale to 90 jobs.

WHAT IT DOES
------------
For every `completed` optimization matching --name-like, take its persisted Backtest rows and run
the EXISTING Monte Carlo suite (app.services.backtest.monte_carlo.run_monte_carlo) over each one's
stored trade list. Nothing is re-simulated: MC is a pure function of the trades, so this is cheap
and touches no market data and no FMP API.

Three independent questions, each a separate verdict:

  survives_drop_k  drop the K best trades -- does it still make money?   (concentration)
  mc_downside      p5 of the bootstrap/shuffle annualised return          (ordering luck)
  dd_risk          probability a path breaches the drawdown limit         (tail risk)

A backtest is `robust` only if ALL of them pass; otherwise `fragile`. The failing reasons are
written alongside so a `fragile` label is actionable rather than just a verdict.

Labels are written into Backtest.labels (the same JSON list that already carries "goal2020",
"ForwardTest", ...), so they show up wherever labels already do.

USAGE
-----
    # one pass over everything not yet labelled
    python tools/robustness_label_topn.py --name-like %goal2020%

    # keep running as jobs land (safe to leave in a terminal; polls, does not busy-spin)
    python tools/robustness_label_topn.py --name-like %goal2020% --watch --interval 600

    # see what it WOULD do
    python tools/robustness_label_topn.py --name-like %goal2020% --dry-run

Idempotent: a backtest already carrying `robust` or `fragile` is skipped unless --relabel.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

BACKEND = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "testplatform", "backend")
sys.path.insert(0, os.path.abspath(BACKEND))

ROBUST, FRAGILE = "robust", "fragile"

# --- verdict thresholds ----------------------------------------------------------------------
# Deliberately LENIENT: the point is to flag results that fall apart without a handful of trades,
# not to demand a great strategy. A `fragile` label should mean "do not trust this number", not
# "this is a bad strategy".
DROP_K = 3                    # drop the 3 best trades
MIN_DROP_K_ANN = 0.0          # ...and still be profitable (annualised >= 0)
# RETENTION is the discriminator, not the absolute level. "Still profitable after dropping 3" is
# almost free for a run with 100+ trades: calibrating on the first 18 goal2020 backtests, that
# test passed 18/18 and told us nothing. What separates them is how much of the HEADLINE survives:
#   bt 852 (top5 = 32.6% of P&L)  11.88 / 14.65 = 81%
#   bt 860 (top5 = 94.3%)          2.77 / 12.10 = 23%
#   bt 862 (top5 = 99.6%)          3.62 / 16.50 = 22%
# i.e. retention tracks concentration directly, and is scale-free across strategies.
MIN_DROP_K_RETENTION = 0.50   # >= 50% of the headline annualised return survives dropping K
MIN_MC_P5_ANN = 0.0           # 5th-percentile resampled annual return >= 0 (95% of orderings profit)
MAX_DD_BREACH_PROB = 0.35     # < 35% of paths may breach the drawdown limit
DD_LIMIT_PCT = 25.0           # what counts as a breach
MC_PATHS = 500
MC_SEED = 20260806            # fixed: a verdict must be reproducible, not resampled per run


def _mc_cfg() -> Dict[str, Any]:
    return {
        "methods": ["bootstrap", "shuffle"],
        "n_paths": MC_PATHS,
        "seed": MC_SEED,
        "drop_k": [1, 2, DROP_K],
        "jitter_bp": 0.0,
        "dd_limit": DD_LIMIT_PCT,
        "target_annual": 10.0,
        "spread_sweep_bps": [],
    }


def _years(bt) -> float:
    try:
        d0, d1 = bt.start_date, bt.end_date
        return max((d1 - d0).days / 365.25, 0.25)
    except Exception:  # noqa: BLE001
        return 1.0


def assess(mc: Dict[str, Any], headline_ann: float) -> Tuple[str, List[str], Dict[str, Any]]:
    """Return (label, reasons, metrics). `reasons` is non-empty exactly when FRAGILE."""
    reasons: List[str] = []
    metrics: Dict[str, Any] = {}

    # 1. concentration — drop the K best trades: still profitable, and how much SURVIVES?
    row = next((r for r in (mc.get("drop_k") or []) if int(r.get("k", -1)) == DROP_K), None)
    if row is None:
        reasons.append(f"no drop_{DROP_K} row")
    else:
        ann = float(row.get("annualized_return") or 0.0)
        metrics[f"drop{DROP_K}_ann"] = round(ann, 2)
        if ann < MIN_DROP_K_ANN:
            reasons.append(f"drop-{DROP_K} annualised {ann:.1f}% < {MIN_DROP_K_ANN}%")
        if headline_ann > 0:
            retention = ann / headline_ann
            metrics["retention"] = round(retention, 2)
            if retention < MIN_DROP_K_RETENTION:
                reasons.append(
                    f"only {retention:.0%} of the headline {headline_ann:.1f}% survives "
                    f"dropping the {DROP_K} best trades (need {MIN_DROP_K_RETENTION:.0%})")

    # 2. ordering luck — worst-case band across resampled paths, taken over ALL methods
    p5s = []
    for name, summary in (mc.get("methods") or {}).items():
        band = (summary or {}).get("annualized_return") or {}
        if "p5" in band:
            p5s.append((name, float(band["p5"])))
    if not p5s:
        reasons.append("no MC paths")
    else:
        worst_name, worst = min(p5s, key=lambda kv: kv[1])
        metrics["mc_p5_ann"] = round(worst, 2)
        metrics["mc_p5_method"] = worst_name
        if worst < MIN_MC_P5_ANN:
            reasons.append(f"{worst_name} p5 annualised {worst:.1f}% < {MIN_MC_P5_ANN}%")

    # 3. tail risk — how often does a resampled path breach the drawdown limit?
    breaches = [float((s or {}).get("prob_dd_breach") or 0.0) for s in (mc.get("methods") or {}).values()]
    if breaches:
        worst_breach = max(breaches)
        metrics["prob_dd_breach"] = round(worst_breach, 3)
        if worst_breach > MAX_DD_BREACH_PROB:
            reasons.append(f"P(dd < -{DD_LIMIT_PCT:.0f}%) = {worst_breach:.0%} > {MAX_DD_BREACH_PROB:.0%}")

    return (FRAGILE if reasons else ROBUST), reasons, metrics


def _json_list(value) -> List[Any]:
    """Backtest.labels / Backtest.trades come back ALREADY DESERIALIZED as lists from SQLAlchemy
    (the columns are JSON-typed), but the same data read over raw sqlite3 is a str. Accept both.

    Getting this wrong is silent: json.loads(<list>) raises TypeError, and a bare `except: return
    []` turns every backtest into "0 trades" and skips the whole run while reporting success.
    That happened on the first pass here — hence no blanket except.
    """
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, (str, bytes)):
        try:
            parsed = json.loads(value)
        except (ValueError, TypeError):
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def _labels_of(bt) -> List[str]:
    return [str(x) for x in _json_list(bt.labels)]


def process(name_like: str, dry_run: bool, relabel: bool, limit: Optional[int]) -> int:
    """One pass. Returns how many backtests were labelled."""
    from app.models import SessionLocal, Backtest, StrategyOptimization
    from app.services.backtest.monte_carlo import run_monte_carlo

    db = SessionLocal()
    labelled = 0
    try:
        opts = (db.query(StrategyOptimization)
                  .filter(StrategyOptimization.status == "completed",
                          StrategyOptimization.name.like(name_like))
                  .order_by(StrategyOptimization.id).all())
        for opt in opts:
            bts = (db.query(Backtest)
                     .filter(Backtest.optimization_id == opt.id)
                     .order_by(Backtest.id).all())
            for bt in bts:
                labels = _labels_of(bt)
                if not relabel and (ROBUST in labels or FRAGILE in labels):
                    continue
                trades = _json_list(bt.trades)
                if len(trades) < 10:
                    print(f"  bt {bt.id:<5} SKIP  only {len(trades)} trades — MC is meaningless")
                    continue

                initial = float(getattr(bt, "initial_capital", 0) or 10000.0)
                mc = run_monte_carlo(trades, initial, _years(bt), _mc_cfg())
                label, reasons, metrics = assess(mc, float(bt.annualized_return or 0.0))

                detail = " ".join(f"{k}={v}" for k, v in metrics.items())
                why = ("; ".join(reasons)) if reasons else "all checks passed"
                print(f"  bt {bt.id:<5} opt {opt.id:<5} {label:<8} {detail}")
                print(f"        -> {why}")

                if not dry_run:
                    labels = [x for x in labels if x not in (ROBUST, FRAGILE)] + [label]
                    bt.labels = json.dumps(labels)
                    db.add(bt)
                    db.commit()
                labelled += 1
                if limit and labelled >= limit:
                    return labelled
    finally:
        db.close()
    return labelled


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--name-like", default="%goal2020%",
                    help="SQL LIKE over optimization name (default: %%goal2020%%)")
    ap.add_argument("--watch", action="store_true", help="keep polling for newly completed jobs")
    ap.add_argument("--interval", type=int, default=600, help="seconds between polls in --watch")
    ap.add_argument("--dry-run", action="store_true", help="print verdicts, write nothing")
    ap.add_argument("--relabel", action="store_true", help="re-assess already-labelled backtests")
    ap.add_argument("--limit", type=int, default=None, help="stop after N backtests")
    args = ap.parse_args()

    print(f"robustness labeller: name_like={args.name_like} "
          f"drop_k={DROP_K} mc_paths={MC_PATHS} seed={MC_SEED} "
          f"{'DRY RUN' if args.dry_run else ''}")
    while True:
        n = process(args.name_like, args.dry_run, args.relabel, args.limit)
        print(f"[{time.strftime('%H:%M:%S')}] labelled {n} backtest(s)")
        if not args.watch:
            return 0
        time.sleep(max(30, args.interval))


if __name__ == "__main__":
    raise SystemExit(main())
