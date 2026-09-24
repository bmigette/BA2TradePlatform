#!/usr/bin/env python
"""Gene counts and wall-clock estimate for the 2027 risk-ATR grid (no DB access).

Inputs are measured goal2020 figures (goal2020_extract.json) plus the RUNBOOK anchor.
Every assumption is a named constant below so the arithmetic can be re-run with other values.
"""
import json
import os
import statistics as st
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
D = json.load(open(os.path.join(HERE, "goal2020_extract.json"), encoding="utf-8"))

# ---- gene blocks (counts from goal2020 parameter_ranges, see s1_s7_relevance.md §5) ----------
STRATEGY_GENES = {"S1": 35, "S2": 20, "S3": 16, "S5": 21, "S6": 11, "S7": 15}
EXPERT_GENES = {"FMPRating": 6, "FMPEarningsDrift": 6, "FMPInsiderClusterBuy": 4,
                "DeterministicScorer": 9, "FMPSenateTraderWeight": 19}
SCREENER_GENES = {"FMPSenateTraderWeight": 0}          # others: 6
RM_GENES = 7        # risk_per_trade_pct, atr_risk_budget_pct, atr_multiplier, atr_period,
                    # min_stop_loss_pct, use_atr_stop (NOW SEARCHED), max_virtual_equity
REGIME_SCALE_GENES = 0   # overlay stays pinned off -> its 3 scale genes are dropped (decision D3)
SL_LOOSEN_GENE = 1       # allow_ruleset_sl_loosen searched (decision D6)
SCHEDULE_GENES = 5       # weekdays only (decision D8); goal2020 carried 7
MKT_ENTRY_GENES = 15     # ohlcv-v1 (6) + ta-structure-v1 (9), ONE shared set per strategy (D5)
MKT_EXIT_GENES = 9       # market exit / stop / TP rules, ~3 genes each (pullback plan B5)

# ---- A4 budget rule (operator-approved 2026-09-24) -------------------------------------------
def budget(genes):
    pop = max(24, min(120, 4 * genes))
    gens = 30 if genes > 20 else 25
    return pop, gens, 8

NEW_FRACTION = 0.71      # measured: recorded trials / (pop x gens) in goal2020 (memo dedupe)
GENS_TYPICAL = 20        # assumption: early stop (patience 8) fires around gen 20
GENS_MAX = 30

# ---- per-evaluation cost -----------------------------------------------------------------
# RUNBOOK anchor: FMPRating/S1/large (2022-2025), population 140, 31.8 min/generation on
# 4 local + 6 remote = 10 slots. With ~71% of each generation new: 0.71*140 = 99.4 evals/gen.
ANCHOR_SLOT_MIN = 31.8 * 10 / (NEW_FRACTION * 140)

# Relative per-trial cost by expert/band, from goal2020 measured wall-min per recorded trial
# (jobs with >=150 recorded trials), normalised to FMPRating = 1. Caveat: the fleets differed
# (DeterministicScorer = matrix3 on local+remote150; the rest on remote227), so DS weights are
# an UPPER bound if remote227 carried more slots than matrix3's 10.
def _p(s):
    for f in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(s, f)
        except Exception:
            pass

per = {}
for o in D["completed"]:
    p = o["parsed"]
    if o["n_trials"] < 150:
        continue
    h = (_p(o["completed_at"]) - _p(o["started_at"] or o["created_at"])).total_seconds() / 60
    per.setdefault((p["expert"], p["band"]), []).append(h / o["n_trials"])
wall_min_per_trial = {k: st.median(v) for k, v in per.items()}
fmpr = st.median([v for (e, b), v in wall_min_per_trial.items() if e == "FMPRating"])
weight = {k: v / fmpr for k, v in wall_min_per_trial.items()}

WINDOW_FACTOR = {"FMPRating": 5 / 4}   # 2022-2026 vs 2022-2025; everyone else 7/6
OVERHEAD = 1.06          # market-condition gates (+0.4% measured) + market exits (assumed +5%)

# ---- fleet ------------------------------------------------------------------------------
SLOTS = {"large": 32, "mid": 32, "small": 17, "all": 16}
# large/mid: remote227 at 28 + 4 local. small: peak child 7.4-13 GB -> ~15 on remote227 + 2 local.
# Senate: ~12.3 GB/child -> 16 on remote227, none local.

CELLS = [("FMPRating", "large"), ("FMPRating", "mid"), ("FMPRating", "small"),
         ("FMPEarningsDrift", "mid"), ("FMPEarningsDrift", "small"),
         ("FMPInsiderClusterBuy", "mid"), ("FMPInsiderClusterBuy", "small"),
         ("DeterministicScorer", "large"), ("DeterministicScorer", "mid"),
         ("DeterministicScorer", "small"), ("FMPSenateTraderWeight", "all")]
KEEP = {"S1", "S2", "S5", "S6"}
SKIP = {("FMPInsiderClusterBuy", "S2")}     # S2 last or 5th in 4/4 ICB cells


def genes_for(expert, strat, market=True):
    g = (STRATEGY_GENES[strat] + EXPERT_GENES[expert] + SCREENER_GENES.get(expert, 6)
         + RM_GENES + REGIME_SCALE_GENES + SL_LOOSEN_GENE + SCHEDULE_GENES)
    return g + (MKT_ENTRY_GENES + MKT_EXIT_GENES if market else 0)


def job_hours(expert, band, strat, market, gens_run):
    g = genes_for(expert, strat, market)
    pop, gmax, _ = budget(g)
    trials = NEW_FRACTION * pop * min(gens_run, gmax)
    w = weight.get((expert, band), 1.0)
    slot_min = ANCHOR_SLOT_MIN * w * WINDOW_FACTOR.get(expert, 7 / 6) * (OVERHEAD if market else 1.0)
    return g, pop, gmax, trials, trials * slot_min / SLOTS[band] / 60


def main(strats=KEEP, gens_run=GENS_TYPICAL, control=True, verbose=True):
    total = 0.0
    rows = []
    for (e, b) in CELLS:
        for s in sorted(strats):
            if (e, s) in SKIP:
                continue
            arms = [True, False] if control else [True]
            for m in arms:
                g, pop, gmax, tr, h = job_hours(e, b, s, m, gens_run)
                total += h
                rows.append((e, b, s, "mkt" if m else "all-off", g, pop, gmax, round(tr), round(h, 1)))
    if verbose:
        print(f"anchor slot-min/eval = 31.8*10/(0.71*140) = {ANCHOR_SLOT_MIN:.2f}")
        print("relative weights:", {f"{k[0][:6]}/{k[1]}": round(v, 2) for k, v in sorted(weight.items())})
        for r in rows:
            print(r)
    return total, len(rows)


if __name__ == "__main__":
    for label, kw in [
        ("KEEP S1/S2/S5/S6, typical 20 gens, + all-off control", dict()),
        ("KEEP S1/S2/S5/S6, full 30 gens, + all-off control", dict(gens_run=GENS_MAX)),
        ("KEEP S1/S2/S5/S6, typical 20 gens, NO control", dict(control=False)),
        ("ALL six strategies, typical 20 gens, + control", dict(strats={"S1", "S2", "S3", "S5", "S6", "S7"})),
        ("KEEP S1/S6 only, typical 20 gens, + control", dict(strats={"S1", "S6"})),
    ]:
        t, n = main(verbose=(label.startswith("KEEP S1/S2/S5/S6, typical 20 gens, + all")), **kw)
        print(f"== {label}: {n} jobs, {t:.0f} h = {t/24:.1f} days sequential")
