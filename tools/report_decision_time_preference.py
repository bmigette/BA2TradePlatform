#!/usr/bin/env python
"""Which decision time did a ``schedule:time`` GA job prefer?  READ-ONLY.

    python tools/report_decision_time_preference.py <optimization_id> [--db PATH] [--top 5,25,50]

Reads ONLY the optimization's ``all_results`` column (opened ``mode=ro``) and prints, per time
value of the gene:

  * how many trials evaluated it (the search effort, generation 0 is stratified),
  * its median / mean / best fitness over ALL measured trials (the fair comparison: every value
    was sampled, so a high median means the time is good for MANY genomes, not for one lucky one),
  * its share of the top N trials (N from --top) and of the top decile.

READ IT AS A DISTRIBUTION, NOT A WINNER: the winner is one genome. A time the optimizer prefers
shows up as (a) a clearly higher median fitness, (b) a share of the top N well above 1/k, and (c) a
share that GROWS from the top 50 to the top 5. The GA does not persist its last generation (the
checkpoint is cleared on completion), so "the final population" is approximated by the top decile
of the cumulative trials, which selection pressure has concentrated on the late generations.
"""
import argparse
import sqlite3
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

GENE = "schedule:time"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("optimization_id", type=int)
    ap.add_argument("--db", default=str(Path.home() / "Documents/ba2/test/dl_forecasting.db"))
    ap.add_argument("--top", default="5,25,50")
    ns = ap.parse_args()

    import json
    con = sqlite3.connect(f"file:{Path(ns.db).as_posix()}?mode=ro", uri=True)
    row = con.execute("SELECT name, all_results FROM strategy_optimizations WHERE id=?",
                      (ns.optimization_id,)).fetchone()
    con.close()
    if row is None:
        print(f"optimization {ns.optimization_id} not found")
        return 1
    name, blob = row
    results = json.loads(blob) if isinstance(blob, str) else (blob or [])
    measured = [r for r in results
                if isinstance(r.get("params"), dict) and GENE in r["params"]
                and isinstance(r.get("fitness"), (int, float)) and r.get("status") != "stalled"]
    if not measured:
        print(f"{name}: no measured trial carries the {GENE} gene")
        return 1
    ranked = sorted(measured, key=lambda r: r["fitness"], reverse=True)
    values = sorted({r["params"][GENE] for r in measured})
    by_time = defaultdict(list)
    for r in measured:
        by_time[r["params"][GENE]].append(r["fitness"])

    print(f"{name} (#{ns.optimization_id}): {len(measured)} measured trials, {len(values)} times")
    print(f"{'time':<7}{'trials':>8}{'median':>10}{'mean':>10}{'best':>10}")
    for t in values:
        f = by_time[t]
        print(f"{t:<7}{len(f):>8}{statistics.median(f):>10.3f}{statistics.fmean(f):>10.3f}{max(f):>10.3f}")
    tops = [int(x) for x in ns.top.split(",") if x.strip()] + [max(1, len(ranked) // 10)]
    print(f"\nshare of the top-N trials (uniform would be {1 / len(values):.0%}):")
    print(f"{'N':<12}" + "".join(f"{t:>8}" for t in values))
    for n in tops:
        c = Counter(r["params"][GENE] for r in ranked[:n])
        label = f"top {n}" + (" (decile)" if n == tops[-1] else "")
        print(f"{label:<12}" + "".join(f"{c[t] / min(n, len(ranked)):>8.0%}" for t in values))
    print(f"\nwinner: {ranked[0]['params'][GENE]} (fitness {ranked[0]['fitness']:.3f})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
