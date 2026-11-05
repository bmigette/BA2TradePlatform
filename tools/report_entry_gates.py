"""Per-gate blocked-entry report for ONE optimization job (read-only).

Sums the per-trial ``eg`` counts (``testplatform/backend/app/services/backtest/entry_gate_bt.py``)
over ``strategy_optimizations.all_results`` and prints, per entry rule, evaluated / met and per leaf in
evaluation order: reached, blocked (first false leaf), pass rate. Zero-trade trials are shown apart.

Usage:  python tools/report_entry_gates.py --opt-id N [--db PATH]
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from typing import Any, Dict, List

DEFAULT_DB = os.path.expanduser(os.path.join("~", "Documents", "ba2", "test", "dl_forecasting.db"))


def sum_eg(trials: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Sum the ``eg`` blobs of ``trials``: {rule: {"n", "met", "b", "order"}}."""
    out: Dict[str, Dict[str, Any]] = {}
    for t in trials:
        for rule, v in ((t.get("eg") or {}) if isinstance(t, dict) else {}).items():
            o = out.setdefault(rule, {"n": 0, "met": 0, "b": {}, "order": []})
            o["n"] += int(v["n"])
            o["met"] += int(v["met"])
            for leaf, c in v["b"].items():
                o["b"][leaf] = o["b"].get(leaf, 0) + int(c)
            for leaf in v["order"]:
                if leaf not in o["order"]:
                    o["order"].append(leaf)
    return out


def format_table(title: str, summed: Dict[str, Dict[str, Any]], n_trials: int) -> List[str]:
    lines = [f"== {title}: {n_trials} trial(s) =="]
    if not summed:
        return lines + ["  (no entry-gate data)"]
    for rule, v in sorted(summed.items()):
        lines.append(f"rule {rule}: evaluated={v['n']} met={v['met']}")
        reached = v["n"]
        for leaf in v["order"]:
            blocked = v["b"].get(leaf, 0)
            rate = f"{100.0 * (reached - blocked) / reached:5.1f}%" if reached else "  n/a"
            lines.append(f"    {leaf:<40} reached={reached:<9} blocked={blocked:<9} pass={rate}")
            reached -= blocked
        for leaf, c in v["b"].items():
            if leaf not in v["order"]:
                lines.append(f"    {leaf:<40} (not in order) blocked={c}")
    return lines


def report(all_results: List[Dict[str, Any]]) -> str:
    trials = [t for t in all_results if isinstance(t, dict)]
    zero = [t for t in trials if not t.get("trades")]
    trading = [t for t in trials if t.get("trades")]
    return "\n".join(format_table("zero-trade trials", sum_eg(zero), len(zero)) + [""]
                     + format_table("trading trials", sum_eg(trading), len(trading)))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--opt-id", type=int, required=True)
    ap.add_argument("--db", default=DEFAULT_DB)
    a = ap.parse_args(argv)
    con = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True)
    try:
        row = con.execute("SELECT all_results FROM strategy_optimizations WHERE id = ?",
                          (a.opt_id,)).fetchone()
    finally:
        con.close()
    if row is None:
        print(f"optimization {a.opt_id} not found in {a.db}", file=sys.stderr)
        return 1
    print(report(json.loads(row[0]) if row[0] else []))
    return 0


if __name__ == "__main__":
    sys.exit(main())
