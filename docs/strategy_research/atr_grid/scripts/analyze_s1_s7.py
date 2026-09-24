#!/usr/bin/env python
"""Rank S1..S7 per goal2020 cell from goal2020_extract.json (no DB access).

Outputs (next to this script):
  s1_s7_cell_ranks.csv     one row per (expert, band, mode, strategy)
  s1_s7_pooled.csv         reading (b): pooled top-5 per expert and per expert x band
  s1_s7_strategy_summary.csv
  analysis_tables.md       markdown tables pasted into s1_s7_relevance.md
"""
from __future__ import annotations

import csv
import json
import math
import os
import statistics as st
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
D = json.load(open(os.path.join(HERE, "goal2020_extract.json"), encoding="utf-8"))
STRATS = ["S1", "S2", "S3", "S5", "S6", "S7"]
EXP_SHORT = {"FMPRating": "FMPR", "FMPEarningsDrift": "ED", "FMPInsiderClusterBuy": "ICB",
             "DeterministicScorer": "DS", "FMPSenateTraderWeight": "SEN", "FactorRanker": "FR"}

opts = {o["id"]: o for o in D["completed"]}
bts_by_opt = defaultdict(list)
for b in D["backtests"]:
    if b.get("status") == "completed" and b.get("rank", 0) > 0:
        bts_by_opt[b["opt_id"]].append(b)


def fnum(x, fmt="{:.2f}"):
    return "" if x is None else fmt.format(x)


def matched(b):
    """Persisted row reproduces the GA's own record of the same genome."""
    m = b.get("ga_match")
    if not m:
        return None
    if m.get("trades") != b.get("total_trades"):
        return False
    if m.get("total_return") is not None and b.get("total_return") is not None:
        if abs(m["total_return"] - b["total_return"]) > 0.05:
            return False
    return True


def year_dependence(yearly):
    """Share of cumulative log-growth from the single best year, and from 2020 / 2022."""
    if not yearly:
        return None, None, None, None
    logs = {y: math.log1p(r / 100.0) for y, r in yearly.items() if r is not None and r > -99.9}
    tot = sum(logs.values())
    if tot <= 0:
        return None, None, None, sum(1 for v in yearly.values() if v is not None and v < 0)
    best = max(logs.values())
    return (100 * best / tot, 100 * logs.get("2020", 0) / tot, 100 * logs.get("2022", 0) / tot,
            sum(1 for v in yearly.values() if v is not None and v < 0))


def rowsum(b):
    ybest, y20, y22, nneg = year_dependence(b.get("yearly"))
    c = b.get("conc") or {}
    cap = b.get("capital") or {}
    profit = (b["final_equity"] - b["initial_capital"]) if b.get("final_equity") is not None else None
    return {"bt_id": b["id"], "rank": b["rank"], "ga_fitness": b.get("ga_fitness"),
            "profit": profit, "total_return": b.get("total_return"), "car": b.get("car"),
            "max_dd": b.get("max_drawdown"), "calmar": b.get("calmar"),
            "trades": b.get("total_trades"), "win_rate": b.get("win_rate"),
            "top1_pct": c.get("top1_pct"), "top5_pct": c.get("top5_pct"),
            "cap_avg_pct": cap.get("avg_pct"), "cap_max_pct": cap.get("max_pct"),
            "best_year_share": ybest, "y2020_share": y20, "y2022_share": y22,
            "neg_years": nneg, "yearly": b.get("yearly"), "ga_matched": matched(b),
            "pins": b.get("pins"), "labels": b.get("labels")}


# ---------------------------------------------------------------- cells
cells = defaultdict(dict)   # (expert, band, mode) -> strat -> record
for oid, o in opts.items():
    p = o["parsed"]
    if not p or not p.get("strat"):
        continue
    rows = sorted(bts_by_opt.get(oid, []), key=lambda b: b["rank"])
    rs = [rowsum(b) for b in rows]
    top = rs[0] if rs else None
    best_car_row = max((r for r in rs if r["car"] is not None), key=lambda r: r["car"], default=None)
    cells[(p["expert"], p["band"], p["mode"])][p["strat"]] = {
        "opt_id": oid, "name": o["name"], "best_fitness": o.get("best_fitness"),
        "genes": o.get("genes"), "gene_groups": o.get("gene_groups"),
        "population": o.get("population"), "generations": o.get("generations"),
        "n_trials": o.get("n_trials"), "n_ok_trials": o.get("n_ok_trials"),
        "robust_fitness": o.get("robust_fitness"), "start": o.get("start_date"),
        "started_at": o.get("started_at"), "completed_at": o.get("completed_at"),
        "persisted": [r["rank"] for r in rs], "rows": rs, "top": top, "best_car_row": best_car_row,
        "n_matched": sum(1 for r in rs if r["ga_matched"]),
        "n_mismatch": sum(1 for r in rs if r["ga_matched"] is False),
        "n_unmatched": sum(1 for r in rs if r["ga_matched"] is None),
        "ga_best_trial": o.get("ga_best_trial"),
    }


def rank_of(values: dict, reverse=True):
    """strategy -> 1-based rank (None values rank last)."""
    order = sorted(values, key=lambda s: (values[s] is None, -(values[s] or 0) if reverse
                                          else (values[s] or 0)))
    return {s: i + 1 for i, s in enumerate(order)}


cell_rows = []
for key in sorted(cells):
    cs = cells[key]
    fit = {s: cs[s]["best_fitness"] for s in cs}
    car_top = {s: (cs[s]["top"] or {}).get("car") for s in cs}
    car_best = {s: (cs[s]["best_car_row"] or {}).get("car") for s in cs}
    calmar_top = {s: (cs[s]["top"] or {}).get("calmar") for s in cs}
    rf, rc, rcb, rcal = rank_of(fit), rank_of(car_top), rank_of(car_best), rank_of(calmar_top)
    n = len(cs)
    for s in STRATS:
        if s not in cs:
            cell_rows.append({"expert": key[0], "band": key[1], "mode": key[2], "strategy": s,
                              "status": "MISSING"})
            continue
        r = cs[s]
        t = r["top"] or {}
        cell_rows.append({
            "expert": key[0], "band": key[1], "mode": key[2], "strategy": s, "status": "ok",
            "n_strats_in_cell": n, "opt_id": r["opt_id"], "genes": r["genes"],
            "population": r["population"], "generations": r["generations"],
            "robust_fitness": r["robust_fitness"],
            "best_fitness": r["best_fitness"], "rank_fitness": rf[s],
            "top_rank_persisted": t.get("rank"), "top_bt_id": t.get("bt_id"),
            "top_ga_matched": t.get("ga_matched"),
            "top_profit": t.get("profit"), "top_car": t.get("car"), "top_max_dd": t.get("max_dd"),
            "top_calmar": t.get("calmar"), "top_trades": t.get("trades"),
            "top_win_rate": t.get("win_rate"), "top_cap_avg_pct": t.get("cap_avg_pct"),
            "top_top1_pct": t.get("top1_pct"), "top_top5_pct": t.get("top5_pct"),
            "top_best_year_share": t.get("best_year_share"), "top_y2020_share": t.get("y2020_share"),
            "top_y2022_share": t.get("y2022_share"), "top_neg_years": t.get("neg_years"),
            "rank_car_top": rc[s], "rank_calmar_top": rcal[s],
            "best_car_any_topn": car_best[s], "rank_car_best_topn": rcb[s],
            "in_top5_fitness": rf[s] <= 5, "in_top5_car": rc[s] <= 5,
            "in_top3_fitness": rf[s] <= 3, "in_top3_car": rc[s] <= 3,
            "persisted_ranks": ",".join(map(str, r["persisted"])),
            "n_rows_matched": r["n_matched"], "n_rows_mismatch": r["n_mismatch"],
            "n_rows_unmatched": r["n_unmatched"],
        })

fields = list({k: None for r in cell_rows for k in r}.keys())
with open(os.path.join(HERE, "s1_s7_cell_ranks.csv"), "w", newline="", encoding="utf-8") as f:
    w = csv.DictWriter(f, fieldnames=fields)
    w.writeheader()
    for r in cell_rows:
        w.writerow({k: (round(v, 4) if isinstance(v, float) else v) for k, v in r.items()})

# ---------------------------------------------------------------- pooled (reading b)
pool_rows = []
all_topn = []
for key, cs in cells.items():
    for s, r in cs.items():
        for row in r["rows"]:
            all_topn.append({"expert": key[0], "band": key[1], "mode": key[2], "strategy": s,
                             "robust": r["robust_fitness"], **row})


def pooled(group_key, label):
    groups = defaultdict(list)
    for x in all_topn:
        groups[group_key(x)].append(x)
    out = {}
    for g, xs in sorted(groups.items()):
        byfit = sorted([x for x in xs if x["ga_fitness"] is not None],
                       key=lambda x: -x["ga_fitness"])[:5]
        bycar = sorted([x for x in xs if x["car"] is not None], key=lambda x: -x["car"])[:5]
        bycal = sorted([x for x in xs if x["calmar"] is not None], key=lambda x: -x["calmar"])[:5]
        robust_mix = sorted({str(x["robust"]) for x in xs})
        out[g] = {"n_rows": len(xs), "fit": byfit, "car": bycar, "calmar": bycal,
                  "robust_settings": robust_mix}
        for metric, lst in (("fitness", byfit), ("car", bycar), ("calmar", bycal)):
            for i, x in enumerate(lst):
                pool_rows.append({"pool": label, "group": "|".join(g) if isinstance(g, tuple) else g,
                                  "metric": metric, "pos": i + 1, "strategy": x["strategy"],
                                  "band": x["band"], "mode": x["mode"], "bt_id": x["bt_id"],
                                  "rank_in_job": x["rank"], "ga_fitness": x["ga_fitness"],
                                  "car": x["car"], "max_dd": x["max_dd"], "calmar": x["calmar"],
                                  "trades": x["trades"], "top1_pct": x["top1_pct"],
                                  "top5_pct": x["top5_pct"], "ga_matched": x["ga_matched"]})
    return out


P_exp = pooled(lambda x: x["expert"], "per_expert")
P_exp_band = pooled(lambda x: (x["expert"], x["band"]), "per_expert_band")
P_exp_mode = pooled(lambda x: (x["expert"], x["mode"]), "per_expert_mode")
with open(os.path.join(HERE, "s1_s7_pooled.csv"), "w", newline="", encoding="utf-8") as f:
    w = csv.DictWriter(f, fieldnames=list(pool_rows[0].keys()))
    w.writeheader()
    for r in pool_rows:
        w.writerow({k: (round(v, 4) if isinstance(v, float) else v) for k, v in r.items()})

# ---------------------------------------------------------------- strategy summary
summ = []
for s in STRATS:
    cr = [r for r in cell_rows if r["strategy"] == s and r["status"] == "ok"]
    tops = [x for x in all_topn if x["strategy"] == s]
    top1s = [cells[(r["expert"], r["band"], r["mode"])][s]["top"] for r in cr]
    top1s = [t for t in top1s if t]
    best = max((t for t in tops if t["car"] is not None), key=lambda t: t["car"], default=None)
    bestcal = max((t for t in tops if t["calmar"] is not None), key=lambda t: t["calmar"], default=None)

    def pool_hits(P, metric):
        return sum(1 for g, v in P.items() if any(x["strategy"] == s for x in v[metric]))

    def med(xs):
        xs = [x for x in xs if x is not None]
        return st.median(xs) if xs else None
    summ.append({
        "strategy": s, "cells": len(cr),
        "top5_fit_cells": sum(r["in_top5_fitness"] for r in cr),
        "top5_car_cells": sum(r["in_top5_car"] for r in cr),
        "top3_fit_cells": sum(r["in_top3_fitness"] for r in cr),
        "top3_car_cells": sum(r["in_top3_car"] for r in cr),
        "first_fit_cells": sum(r["rank_fitness"] == 1 for r in cr),
        "first_car_cells": sum(r["rank_car_top"] == 1 for r in cr),
        "last_fit_cells": sum(r["rank_fitness"] == r["n_strats_in_cell"] for r in cr),
        "last_car_cells": sum(r["rank_car_top"] == r["n_strats_in_cell"] for r in cr),
        "mean_rank_fit": st.mean(r["rank_fitness"] for r in cr),
        "mean_rank_car": st.mean(r["rank_car_top"] for r in cr),
        "pool_expert_fit_hits": pool_hits(P_exp, "fit"), "pool_expert_car_hits": pool_hits(P_exp, "car"),
        "pool_expert_calmar_hits": pool_hits(P_exp, "calmar"),
        "pool_expband_fit_hits": pool_hits(P_exp_band, "fit"),
        "pool_expband_car_hits": pool_hits(P_exp_band, "car"),
        "pool_expband_calmar_hits": pool_hits(P_exp_band, "calmar"),
        "n_expert_pools": len(P_exp), "n_expband_pools": len(P_exp_band),
        "best_car": best and best["car"], "best_car_dd": best and best["max_dd"],
        "best_car_bt": best and best["bt_id"],
        "best_car_cell": best and f'{best["expert"]}/{best["band"]}/{best["mode"]}',
        "best_car_top1": best and best["top1_pct"], "best_car_top5": best and best["top5_pct"],
        "best_calmar": bestcal and bestcal["calmar"], "best_calmar_bt": bestcal and bestcal["bt_id"],
        "best_calmar_car": bestcal and bestcal["car"], "best_calmar_dd": bestcal and bestcal["max_dd"],
        "med_top1_car": med([t["car"] for t in top1s]), "med_top1_dd": med([t["max_dd"] for t in top1s]),
        "med_top1_trades": med([t["trades"] for t in top1s]),
        "med_top1_top1pct": med([t["top1_pct"] for t in top1s]),
        "med_top1_top5pct": med([t["top5_pct"] for t in top1s]),
        "med_top1_cap_avg": med([t["cap_avg_pct"] for t in top1s]),
        "top1_rows_top5pct_over_50": sum(1 for t in top1s if (t["top5_pct"] or 0) > 50),
        "top1_rows_best_year_over_50": sum(1 for t in top1s if (t["best_year_share"] or 0) > 50),
        "top1_rows_2020_over_50": sum(1 for t in top1s if (t["y2020_share"] or 0) > 50),
        "top1_rows_2022_over_50": sum(1 for t in top1s if (t["y2022_share"] or 0) > 50),
        "n_top1_rows": len(top1s),
    })
with open(os.path.join(HERE, "s1_s7_strategy_summary.csv"), "w", newline="", encoding="utf-8") as f:
    w = csv.DictWriter(f, fieldnames=list(summ[0].keys()))
    w.writeheader()
    for r in summ:
        w.writerow({k: (round(v, 3) if isinstance(v, float) else v) for k, v in r.items()})

# ---------------------------------------------------------------- markdown tables
md = []
md.append("### Per-cell ranks (fitness rank / CAR rank of the job's top persisted row)\n")
md.append("| expert | band | mode | " + " | ".join(STRATS) + " |")
md.append("|---|---|---|" + "---|" * len(STRATS))
for key in sorted(cells):
    cs = cells[key]
    cellr = {r["strategy"]: r for r in cell_rows if (r["expert"], r["band"], r["mode"]) == key}
    parts = []
    for s in STRATS:
        r = cellr.get(s)
        if not r or r["status"] != "ok":
            parts.append("—")
        else:
            parts.append(f'{r["rank_fitness"]}/{r["rank_car_top"]}')
    md.append(f"| {EXP_SHORT[key[0]]} | {key[1]} | {key[2]} | " + " | ".join(parts) + " |")

md.append("\n### Per-cell economics of the job's top persisted row (CAR% / maxDD% / trades)\n")
md.append("| expert | band | mode | " + " | ".join(STRATS) + " |")
md.append("|---|---|---|" + "---|" * len(STRATS))
for key in sorted(cells):
    cellr = {r["strategy"]: r for r in cell_rows if (r["expert"], r["band"], r["mode"]) == key}
    parts = []
    for s in STRATS:
        r = cellr.get(s)
        if not r or r["status"] != "ok":
            parts.append("—")
        else:
            parts.append(f'{fnum(r["top_car"], "{:.1f}")} / {fnum(r["top_max_dd"], "{:.1f}")} / '
                         f'{r["top_trades"]}')
    md.append(f"| {EXP_SHORT[key[0]]} | {key[1]} | {key[2]} | " + " | ".join(parts) + " |")

md.append("\n### Per-cell best GA fitness\n")
md.append("| expert | band | mode | " + " | ".join(STRATS) + " |")
md.append("|---|---|---|" + "---|" * len(STRATS))
for key in sorted(cells):
    cs = cells[key]
    md.append(f"| {EXP_SHORT[key[0]]} | {key[1]} | {key[2]} | " + " | ".join(
        fnum(cs[s]["best_fitness"], "{:.2f}") if s in cs else "—" for s in STRATS) + " |")

md.append("\n### Strategy summary\n")
md.append("| strat | cells | top-5 fit | top-5 CAR | top-3 fit | top-3 CAR | 1st fit | 1st CAR | last fit | last CAR | mean rank fit | mean rank CAR |")
md.append("|---|---|---|---|---|---|---|---|---|---|---|---|")
for r in summ:
    md.append(f'| {r["strategy"]} | {r["cells"]} | {r["top5_fit_cells"]} | {r["top5_car_cells"]} | '
              f'{r["top3_fit_cells"]} | {r["top3_car_cells"]} | {r["first_fit_cells"]} | '
              f'{r["first_car_cells"]} | {r["last_fit_cells"]} | {r["last_car_cells"]} | '
              f'{r["mean_rank_fit"]:.2f} | {r["mean_rank_car"]:.2f} |')

md.append("\n### Pooled top-5 hits (reading b)\n")
md.append("| strat | expert pools hit (fit / CAR / calmar) of N | expert×band pools hit (fit / CAR / calmar) of N |")
md.append("|---|---|---|")
for r in summ:
    md.append(f'| {r["strategy"]} | {r["pool_expert_fit_hits"]} / {r["pool_expert_car_hits"]} / '
              f'{r["pool_expert_calmar_hits"]} of {r["n_expert_pools"]} | '
              f'{r["pool_expband_fit_hits"]} / {r["pool_expband_car_hits"]} / '
              f'{r["pool_expband_calmar_hits"]} of {r["n_expband_pools"]} |')

md.append("\n### Pooled top-5 per expert (by fitness | by CAR)\n")
for g, v in P_exp.items():
    f5 = ", ".join(f'{x["strategy"]}({x["band"][0]}/{x["mode"][:1]}) {x["ga_fitness"]:.2f}' for x in v["fit"])
    c5 = ", ".join(f'{x["strategy"]}({x["band"][0]}/{x["mode"][:1]}) {x["car"]:.1f}%' for x in v["car"])
    md.append(f'- **{EXP_SHORT[g]}** ({v["n_rows"]} rows, robust={v["robust_settings"]}): fitness: {f5}; CAR: {c5}')

md.append("\n### Strategy economics and risk flags\n")
md.append("| strat | best CAR (bt, cell) | its DD | its top1/top5 % | best calmar (bt) CAR/DD | median TOP row CAR / DD / trades | median top1% / top5% | median capital in use % | TOP rows top5>50% | best year >50% of growth | 2020 >50% | 2022 >50% |")
md.append("|---|---|---|---|---|---|---|---|---|---|---|---|")
for r in summ:
    md.append(
        f'| {r["strategy"]} | {fnum(r["best_car"], "{:.1f}")}% (#{r["best_car_bt"]}, {r["best_car_cell"]}) | '
        f'{fnum(r["best_car_dd"], "{:.1f}")} | {fnum(r["best_car_top1"], "{:.0f}")}/{fnum(r["best_car_top5"], "{:.0f}")} | '
        f'{fnum(r["best_calmar"])} (#{r["best_calmar_bt"]}) {fnum(r["best_calmar_car"], "{:.1f}")}/{fnum(r["best_calmar_dd"], "{:.1f}")} | '
        f'{fnum(r["med_top1_car"], "{:.1f}")} / {fnum(r["med_top1_dd"], "{:.1f}")} / {fnum(r["med_top1_trades"], "{:.0f}")} | '
        f'{fnum(r["med_top1_top1pct"], "{:.0f}")} / {fnum(r["med_top1_top5pct"], "{:.0f}")} | '
        f'{fnum(r["med_top1_cap_avg"], "{:.0f}")} | {r["top1_rows_top5pct_over_50"]}/{r["n_top1_rows"]} | '
        f'{r["top1_rows_best_year_over_50"]}/{r["n_top1_rows"]} | {r["top1_rows_2020_over_50"]}/{r["n_top1_rows"]} | '
        f'{r["top1_rows_2022_over_50"]}/{r["n_top1_rows"]} |')

open(os.path.join(HERE, "analysis_tables.md"), "w", encoding="utf-8").write("\n".join(md) + "\n")
print("\n".join(md))
