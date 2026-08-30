#!/usr/bin/env python
"""Read-only per-generation fitness/profit/drawdown reporter for the stage-1 grid.

Reads (read-only, safe while jobs run):
  strategy_optimizations -> job rows (name, status, progress, best_fitness, best_params, all_results)
  task_queue.checkpoint_data -> GA checkpoint JSON with 'history': per-generation
      [{generation, best_fitness, best_params, stats:{avg,max,min}}]

For each generation it matches best_params against all_results to recover the best genome's
trade count, total return (%) and max drawdown (%). Jobs completed before the metrics patch
show '-' for return/DD.

Usage:
  python tools/stage1_gen_report.py [--db /home/debian/ba2-grid/home/test/dl_forecasting.db]
      [--job NAME] [--md OUT.md] [--last N]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from typing import Any, Dict, List, Optional


def _jloads(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, (dict, list)):
        return v
    try:
        return json.loads(v)
    except (TypeError, ValueError):
        return None


def _load(db: str) -> tuple:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    jobs = [
        {"name": r[0], "status": r[1], "progress": r[2], "best_fitness": r[3],
         "best_params": _jloads(r[4]), "all_results": _jloads(r[5])}
        for r in con.execute(
            "SELECT name, status, progress, best_fitness, best_params, all_results "
            "FROM strategy_optimizations ORDER BY id")
    ]
    ckpts = {}
    for task_id, data in con.execute("SELECT task_id, checkpoint_data FROM task_queue"):
        d = _jloads(data)
        if isinstance(d, dict):
            ckpts[task_id] = d
    con.close()
    return jobs, ckpts


def _hash_task_id(name: str) -> str:
    return "ckpt-" + hashlib.sha1(name.encode("utf-8")).hexdigest()[:24]


def _metrics_for(all_results: Optional[List[Dict]], best_params: Optional[Dict]) -> Dict[str, Any]:
    """Match the best genome against all_results -> trades / total_return / max_drawdown."""
    none = {"trades": None, "total_return": None, "max_drawdown": None}
    if not all_results or not isinstance(best_params, dict) or not best_params:
        return none
    for r in all_results:
        if isinstance(r, dict) and r.get("params") == best_params:
            return {"trades": r.get("trades"),
                    "total_return": r.get("total_return"),
                    "max_drawdown": r.get("max_drawdown")}
    return none


def _fmt(v: Optional[float], pct: bool = True) -> str:
    if v is None:
        return "-"
    return f"{v:.2f}%" if pct else f"{v:.2f}"


def report(db: str, job_filter: Optional[str], last: Optional[int]) -> str:
    jobs, ckpts = _load(db)
    out: List[str] = []
    out.append(f"Stage 1 per-generation report — {len(jobs)} job(s) — {db}\n")
    for j in jobs:
        if job_filter and job_filter not in j["name"]:
            continue
        ck = ckpts.get(_hash_task_id(j["name"])) or {}
        hist: List[Dict] = ck.get("history") or []
        shown = hist[-last:] if last else hist
        out.append(f"## {j['name']}  [{j['status']}] progress={j['progress'] or 0:.1f}%  "
                   f"db best_fitness={j['best_fitness']}")
        if not hist:
            out.append("   (no completed generation yet)\n")
            continue
        if last and len(hist) > last:
            out.append(f"   ({len(hist) - last} earlier generation(s) omitted)")
        out.append("   gen | best_fitness |    avg |    max | trades |   return | maxDD")
        for h in shown:
            st = h.get("stats") or {}
            m = _metrics_for(j["all_results"], h.get("best_params"))
            out.append(
                f"   {h.get('generation', '?'):>3} | {h.get('best_fitness', 0):>12.4f} | "
                f"{st.get('avg', 0):>7.3f} | {st.get('max', 0):>6.3f} | "
                f"{m['trades'] if m['trades'] is not None else '-':>6} | "
                f"{_fmt(m['total_return']):>8} | {_fmt(m['max_drawdown']):>6}")
        out.append("")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default="/home/debian/ba2-grid/home/test/dl_forecasting.db")
    ap.add_argument("--job", default=None, help="Substring filter on job name.")
    ap.add_argument("--md", default=None, help="Also write the report to this path.")
    ap.add_argument("--last", type=int, default=None, help="Only the last N generations.")
    args = ap.parse_args()
    text = report(args.db, args.job, args.last)
    print(text)
    if args.md:
        with open(args.md, "w", encoding="utf-8") as f:
            f.write(text + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
