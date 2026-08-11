"""Backfill ``backtests.ga_fitness`` for top-N rows persisted before migration 030.

RECONSTRUCTION, NOT ESTIMATION. The launcher's ranking is deterministic and its inputs are
still on the optimization row: sort ``all_results`` by fitness descending, drop duplicates on
``round(fitness, 6)`` (a converged GA emits many genomes differing only in inert genes, which
score identically and would otherwise persist behaviourally-identical rows), take the first N.
Rank i in that list is the row named ``TOP<i>-<opt.name>``. Replaying exactly that recovers the
score the GA actually ranked each row on -- no matching on metrics, no inference.

SAFETY. Only ever fills rows where ``ga_fitness IS NULL``; never overwrites a value written by
the current code path. Read-mostly: one UPDATE per matched row. ``--dry-run`` reports what it
would do and writes nothing.

The rank->row match is by NAME, and mismatches are reported rather than guessed at: if a row
named TOP3 cannot be found, or the optimization has no all_results, that optimization is
skipped and counted, because a wrong fitness is worse than a missing one.

Usage:
    python tools/backfill_ga_fitness.py --dry-run
    python tools/backfill_ga_fitness.py
"""
import argparse
import json
import os
import re
import sqlite3
import sys

DB_PATH = os.path.expanduser("~/Documents/ba2/test/dl_forecasting.db")


def _load_json(v):
    if v is None:
        return None
    if isinstance(v, (list, dict)):
        return v
    try:
        return json.loads(v)
    except Exception:  # noqa: BLE001
        return None


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=DB_PATH)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit-opts", type=int, default=None)
    args = ap.parse_args()

    if not os.path.exists(args.db):
        raise SystemExit(f"DB not found: {args.db}")
    con = sqlite3.connect(args.db, timeout=60.0)
    cur = con.cursor()

    cols = {r[1] for r in cur.execute("PRAGMA table_info(backtests)")}
    if "ga_fitness" not in cols:
        raise SystemExit("backtests.ga_fitness missing -- run migration 030 first")

    opts = cur.execute(
        "SELECT id, name, all_results, best_params, best_fitness FROM strategy_optimizations"
        " ORDER BY id").fetchall()
    if args.limit_opts:
        opts = opts[-args.limit_opts:]

    filled = skipped_no_results = skipped_no_rows = unmatched = already = 0
    for opt_id, opt_name, all_results_raw, best_params_raw, best_fitness in opts:
        rows = cur.execute(
            "SELECT id, name, ga_fitness FROM backtests"
            " WHERE optimization_id=? AND name LIKE 'TOP%'", (opt_id,)).fetchall()
        if not rows:
            skipped_no_rows += 1
            continue

        all_results = _load_json(all_results_raw) or []
        if not all_results:
            # Fall back to best_fitness for a lone TOP1 -- best_fitness IS that genome's score.
            if len(rows) == 1 and rows[0][1].startswith("TOP1-") and best_fitness is not None:
                if rows[0][2] is None:
                    if not args.dry_run:
                        cur.execute("UPDATE backtests SET ga_fitness=? WHERE id=?",
                                    (float(best_fitness), rows[0][0]))
                    filled += 1
                else:
                    already += 1
                continue
            skipped_no_results += 1
            continue

        # Replay the launcher's dedup-by-fitness ranking exactly.
        seen, ranked = set(), []
        for r in sorted(all_results,
                        key=lambda r: (r.get("fitness") if r.get("fitness") is not None else -1e9),
                        reverse=True):
            fit = r.get("fitness")
            key = (round(fit, 6) if isinstance(fit, (int, float))
                   else json.dumps(r.get("params"), sort_keys=True, default=str))
            if key in seen:
                continue
            seen.add(key)
            ranked.append(fit)

        for bt_id, bt_name, existing in rows:
            if existing is not None:
                already += 1
                continue
            m = re.match(r"^TOP(\d+)-", bt_name or "")
            if not m:
                unmatched += 1
                continue
            rank = int(m.group(1))
            if rank > len(ranked) or ranked[rank - 1] is None:
                unmatched += 1
                continue
            if not args.dry_run:
                cur.execute("UPDATE backtests SET ga_fitness=? WHERE id=?",
                            (float(ranked[rank - 1]), bt_id))
            filled += 1

    if not args.dry_run:
        con.commit()
    con.close()

    print(f"optimizations scanned : {len(opts):,}")
    print(f"rows filled           : {filled:,}{'  (dry run, nothing written)' if args.dry_run else ''}")
    print(f"already had a value   : {already:,}")
    print(f"unmatched rank/name   : {unmatched:,}")
    print(f"opts w/o all_results  : {skipped_no_results:,}")
    print(f"opts w/o TOP rows     : {skipped_no_rows:,}")


if __name__ == "__main__":
    main()
