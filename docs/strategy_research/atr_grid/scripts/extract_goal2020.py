#!/usr/bin/env python
"""READ-ONLY extraction of the goal2020 grid (strategy_optimizations + persisted TOP-N backtests).

Opens the test-platform DB with mode=ro. Never selects post-blob columns of `backtests` except
through the covering index (ix_backtests_summary) or by explicit single-row blob reads
(results / trades / equity_curve) for the TOP-N rows, one row at a time.

Output: goal2020_extract.json next to this script.

Usage:
    .venv/Scripts/python.exe extract_goal2020.py
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import time
from datetime import date, datetime

DB = r"C:\Users\basti\Documents\ba2\test\dl_forecasting.db"
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "goal2020_extract.json")

NAME_RE = re.compile(
    r"^scr-(?P<band>large|mid|small)-(?P<expert>[A-Za-z]+)(?:-(?P<strat>S\d))?-goal2020-"
    r"(?P<mode>riskatr|risk_atr|notional)(?P<tail>.*)$")
SEN_RE = re.compile(r"^sen-(?P<strat>S\d)-goal2020-(?P<mode>risk_atr|notional)(?P<tail>.*)$")


def parse_name(name: str):
    m = NAME_RE.match(name)
    if m:
        d = m.groupdict()
    else:
        m = SEN_RE.match(name)
        if not m:
            return None
        d = m.groupdict()
        d["band"] = "all"
        d["expert"] = "FMPSenateTraderWeight"
    d["mode"] = "risk_atr" if d["mode"] in ("riskatr", "risk_atr") else "notional"
    return d


def gene_count(parameter_ranges: dict) -> int:
    n = 0
    for _k, spec in (parameter_ranges or {}).items():
        if not isinstance(spec, dict):
            n += 1
            continue
        if spec.get("type") == "choice":
            if len(spec.get("choices") or []) > 1:
                n += 1
            continue
        if spec.get("min") is not None and spec.get("max") is not None and spec["min"] == spec["max"]:
            continue
        n += 1
    return n


def gene_groups(parameter_ranges: dict) -> dict:
    g = {"model_rm": 0, "model_expert": 0, "strategy": 0, "schedule": 0, "screener": 0, "other": 0}
    rm = {"risk_per_trade_pct", "atr_risk_budget_pct", "atr_multiplier", "atr_period",
          "min_stop_loss_pct", "use_atr_stop", "max_virtual_equity_per_instrument_percent",
          "regime_overlay_enabled", "regime_risk_scale", "regime_stop_scale", "regime_tp_scale"}
    for k in (parameter_ranges or {}):
        if k.startswith("model:"):
            g["model_rm" if k[6:] in rm else "model_expert"] += 1
        elif k.startswith(("cond:", "exit:", "entry:")):
            g["strategy"] += 1
        elif k.startswith("schedule:"):
            g["schedule"] += 1
        elif k.startswith("screener:"):
            g["screener"] += 1
        else:
            g["other"] += 1
    return g


def _day(s):
    return s[:10] if isinstance(s, str) and len(s) >= 10 else None


def capital_usage(trades, equity_curve):
    """Port of testplatform/frontend/src/lib/capitalUsage.ts (capitalUsageSeries+summariseUsage)."""
    eq_by_day = {}
    for p in equity_curve or []:
        d = _day(p.get("date"))
        if d:
            eq_by_day[d] = float(p.get("equity") or 0.0)
    if not eq_by_day:
        return None
    groups, lone = {}, []
    for t in trades or []:
        key = t.get("transaction_id")
        key = None if key in (None, "") else str(key).strip() or None
        if key:
            groups.setdefault(key, []).append(t)
        else:
            lone.append(t)
    positions = []

    def build(legs, is_struct):
        entries = sorted(d for d in (_day(l.get("entry_time")) for l in legs) if d)
        if not entries:
            return
        exits = sorted(d for d in (_day(l.get("exit_time")) for l in legs) if d)
        exit_day = exits[-1] if exits else None
        if not is_struct:
            l0 = legs[0]
            notional = abs(float(l0.get("entry_price") or 0) * float(l0.get("size") or 0)
                           * (float(l0.get("multiplier") or 0) or 1))
            positions.append((entries[0], exit_day, notional))
            return
        net = 0.0
        for l in legs:
            mult = float(l.get("multiplier") or 0) or 1
            sign = -1 if str(l.get("direction", "")).lower() == "short" else 1
            net += sign * abs(float(l.get("size") or 0)) * float(l.get("entry_price") or 0) * mult
        positions.append((entries[0], exit_day, abs(net)))

    for legs in groups.values():
        build(legs, len(legs) > 1)
    for t in lone:
        build([t], False)
    opens = sorted((p[0], p[2]) for p in positions)
    closes = sorted((p[1], p[2]) for p in positions if p[1])
    days = sorted(eq_by_day)
    o = c = 0
    open_n = 0.0
    pts = []
    for d in days:
        while o < len(opens) and opens[o][0] <= d:
            open_n += opens[o][1]
            o += 1
        while c < len(closes) and closes[c][0] < d:
            open_n -= closes[c][1]
            c += 1
        if open_n < 0:
            open_n = 0.0
        eq = eq_by_day[d]
        pts.append((open_n / eq * 100.0) if eq > 0 else 0.0)
    if not pts:
        return None
    return {"avg_pct": sum(pts) / len(pts), "max_pct": max(pts),
            "idle_days_pct": 100.0 * sum(1 for p in pts if p < 10) / len(pts),
            "heavy_days_pct": 100.0 * sum(1 for p in pts if p > 50) / len(pts)}


def yearly_returns(equity_curve, initial):
    last_by_year = {}
    for p in equity_curve or []:
        d = _day(p.get("date"))
        if d:
            last_by_year[int(d[:4])] = float(p.get("equity") or 0.0)
    out = {}
    prev = float(initial)
    for y in sorted(last_by_year):
        e = last_by_year[y]
        out[str(y)] = (e / prev - 1.0) * 100.0 if prev > 0 else None
        prev = e
    return out


def concentration(trades):
    pnls = [float(t.get("pnl") or 0.0) for t in trades or []]
    net = sum(pnls)
    if not pnls:
        return {"net_pnl": 0.0, "top1_pct": None, "top5_pct": None, "open_at_end_pnl_pct": None,
                "n_trades": 0}
    s = sorted(pnls, reverse=True)
    oae = sum(float(t.get("pnl") or 0.0) for t in trades
              if str(t.get("exit_reason", "")).lower() in ("open_at_end", "end_of_backtest", "eob"))
    return {
        "net_pnl": net,
        "top1_pct": (100.0 * s[0] / net) if net > 0 else None,
        "top5_pct": (100.0 * sum(s[:5]) / net) if net > 0 else None,
        "open_at_end_pnl_pct": (100.0 * oae / net) if net > 0 else None,
        "n_trades": len(pnls),
        "exit_reasons": _count([str(t.get("exit_reason")) for t in trades]),
    }


def _count(xs):
    d = {}
    for x in xs:
        d[x] = d.get(x, 0) + 1
    return d


def main():
    t0 = time.time()
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    acts = con.execute(
        "select id, name, status, created_at, started_at, completed_at "
        "from strategy_optimizations indexed by ix_strategy_optimizations_activity "
        "where name like '%goal2020%'").fetchall()
    opts = []
    for (oid, name, status, created, started, completed) in acts:
        p = parse_name(name)
        rec = {"id": oid, "name": name, "status": status, "created_at": created,
               "started_at": started, "completed_at": completed, "parsed": p}
        opts.append(rec)
    print(f"{len(opts)} goal2020 optimization rows")

    completed = [o for o in opts if o["status"] == "completed" and o["parsed"]
                 and not o["parsed"]["tail"].strip("-").replace("from2022", "").replace("ds", "")]
    for o in completed:
        row = con.execute(
            "select best_fitness, parameter_ranges, optimization_config, best_params "
            "from strategy_optimizations where id=?", (o["id"],)).fetchone()
        bf, pr, oc, bp = row
        pr = json.loads(pr) if pr else {}
        oc = json.loads(oc) if oc else {}
        bt = oc.get("backtest") or {}
        o.update({
            "best_fitness": bf,
            "genes": gene_count(pr),
            "gene_groups": gene_groups(pr),
            "population": oc.get("populationSize"),
            "generations": oc.get("generations"),
            "early_stop": oc.get("earlyStoppingGenerations"),
            "robust_fitness": bt.get("robust_fitness"),
            "stress_spread_bps": bt.get("stress_spread_bps"),
            "start_date": bt.get("start_date"), "end_date": bt.get("end_date"),
            "best_params": json.loads(bp) if bp else {},
        })
        ar = con.execute("select all_results from strategy_optimizations where id=?",
                         (o["id"],)).fetchone()[0]
        trials = json.loads(ar) if ar else []
        ok = [t for t in trials if (t.get("fitness") or -1e9) > -1e8]
        o["n_trials"] = len(trials)
        o["n_ok_trials"] = len(ok)
        # best GA trial economics (raw GA record, independent of what got persisted)
        o["ga_best_trial"] = None
        if ok:
            b = max(ok, key=lambda t: t["fitness"])
            o["ga_best_trial"] = {k: b.get(k) for k in ("fitness", "fitness_raw", "trades",
                                                        "total_return", "max_drawdown")}
        # keep a compact trial index for genome matching: key params json -> (fit, trades, tr, dd)
        o["_trials"] = [(json.dumps(t.get("params"), sort_keys=True), t.get("fitness"),
                         t.get("trades"), t.get("total_return"), t.get("max_drawdown"))
                        for t in ok]
        # GA-trial pool for secondary (all-trials) ranking
        o["ga_trials_econ"] = [{"fitness": t.get("fitness"), "trades": t.get("trades"),
                                "total_return": t.get("total_return"),
                                "max_drawdown": t.get("max_drawdown")} for t in ok]

    ids = [o["id"] for o in completed]
    qmarks = ",".join("?" * len(ids))
    bts = con.execute(
        f"select id, name, optimization_id, status, total_return, max_drawdown, total_trades, "
        f"win_rate, final_equity, initial_capital, ga_fitness, start_date, end_date, labels, "
        f"expert_name, created_at from backtests indexed by ix_backtests_summary "
        f"where optimization_id in ({qmarks})", ids).fetchall()
    print(f"{len(bts)} persisted backtests for {len(ids)} completed optimizations "
          f"({time.time()-t0:.1f}s)")
    by_opt = {o["id"]: o for o in completed}
    rows = []
    for i, b in enumerate(bts):
        (bid, name, oid, status, tr, dd, ntr, wr, fe, ic, gaf, sd, ed, labels, en, cat) = b
        m = re.match(r"^TOP(\d+)-", name or "")
        rank = int(m.group(1)) if m else 0
        rec = {"id": bid, "name": name, "opt_id": oid, "status": status, "rank": rank,
               "total_return": tr, "max_drawdown": dd, "total_trades": ntr, "win_rate": wr,
               "final_equity": fe, "initial_capital": ic, "ga_fitness": gaf,
               "start_date": sd, "end_date": ed, "labels": labels, "expert_name": en}
        if status != "completed":
            rows.append(rec)
            continue
        # cheap pre-blob column: the genome as executed
        sp = con.execute("select strategy_params from backtests where id=?", (bid,)).fetchone()[0]
        sp = json.loads(sp) if sp else {}
        genome = {k: v for k, v in sp.items() if ":" in k}
        rec["pins"] = [k for k in sp if k.startswith("_")]
        rec["regime_overlay_gene"] = sp.get("model:regime_overlay_enabled")
        rec["use_atr_stop_gene"] = sp.get("model:use_atr_stop")
        # UN-PIN: the _atr_swap_migration / _inert_toggle_pin blocks rewrote genes on the row
        # after the GA scored them; restore each block's `from` values to recover the genome
        # exactly as the GA recorded it (the row's EXECUTED values stay what the row says).
        pin_from = {}
        for pk in rec["pins"]:
            blk = sp.get(pk) or {}
            if isinstance(blk, dict) and isinstance(blk.get("from"), dict):
                pin_from.update(blk["from"])
        rec["pin_from"] = pin_from
        rec["regime_overlay_gene_as_searched"] = pin_from.get(
            "model:regime_overlay_enabled", sp.get("model:regime_overlay_enabled"))
        rec["use_atr_stop_gene_as_searched"] = pin_from.get(
            "model:use_atr_stop", sp.get("model:use_atr_stop"))
        genome = {**genome, **pin_from}
        # match to the GA trial with identical genome
        o = by_opt[oid]
        gkey = json.dumps(genome, sort_keys=True)
        match = None
        for (k, f, t, trr, ddd) in o["_trials"]:
            if k == gkey:
                match = {"fitness": f, "trades": t, "total_return": trr, "max_drawdown": ddd}
                break
        if match is None:
            # tolerate extra non-gene keys: compare on the trial's own keys
            for (k, f, t, trr, ddd) in o["_trials"]:
                tp = json.loads(k)
                if tp and all(genome.get(kk) == vv for kk, vv in tp.items()):
                    match = {"fitness": f, "trades": t, "total_return": trr, "max_drawdown": ddd}
                    break
        rec["ga_match"] = match
        # blobs, one row at a time: results (small), trades, equity_curve
        res = con.execute("select results from backtests where id=?", (bid,)).fetchone()[0]
        res = json.loads(res) if res else {}
        rec["car"] = res.get("annualized_return")
        rec["calmar"] = res.get("calmar_ratio")
        rec["fitness_raw"] = res.get("fitness_raw")
        rec["fitness_robust"] = res.get("fitness_robust")
        rec["robustness"] = res.get("robustness")
        rec["exposure_time"] = res.get("exposure_time")
        rec["avg_trades_per_year"] = res.get("avg_trades_per_year")
        trades = con.execute("select trades from backtests where id=?", (bid,)).fetchone()[0]
        trades = json.loads(trades) if trades else []
        rec["conc"] = concentration(trades)
        ec = con.execute("select equity_curve from backtests where id=?", (bid,)).fetchone()[0]
        ec = json.loads(ec) if ec else []
        rec["yearly"] = yearly_returns(ec, ic or 10000.0)
        rec["capital"] = capital_usage(trades, ec)
        del trades, ec
        rows.append(rec)
        if i % 25 == 0:
            print(f"  {i+1}/{len(bts)} ({time.time()-t0:.0f}s)", flush=True)
    for o in completed:
        o.pop("_trials", None)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump({"extracted_at": datetime.now().isoformat(), "optimizations": opts,
                   "completed": completed, "backtests": rows}, f, default=str)
    print(f"wrote {OUT} ({time.time()-t0:.0f}s)")


if __name__ == "__main__":
    main()
