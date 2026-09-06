#!/usr/bin/env python
"""Make genomes saved under the swapped ATR wiring reproduce on the FIXED code.

    .venv/Scripts/python.exe tools/migrate_atr_budget_swap.py            # dry run
    .venv/Scripts/python.exe tools/migrate_atr_budget_swap.py --apply

WHAT WAS WRONG. dd1f912e (2026-08-16, "ATR budget gene") added ``atr_risk_budget_pct`` to carry
the risk_atr SIZE budget so ``risk_per_trade_pct`` could keep owning stop distance -- its commit
message says exactly that -- but the edit landed in ``_ensure_safeguard_stop`` and never touched
``_risk_atr_quantity``. So from that commit until the fix, the two genes drove each other's jobs:

    pre-fix :  stop <- atr_risk_budget_pct        size <- risk_per_trade_pct
    post-fix:  stop <- risk_per_trade_pct         size <- atr_risk_budget_pct (fallback rptp)

WHY A PURE SWAP IS THE WHOLE MIGRATION. Read those two lines as equations. For a stored genome to
behave the same after the fix, the value that USED to reach the stop must now be in the gene the
stop reads, and likewise for size -- i.e. exactly swap the two stored numbers. Verified against
the real methods, not derived on paper: a genome (budget=1.0, rptp=8.0) produced stop $93.00 /
300 shares on the old code; on the fixed code unmigrated it gives $92.00 / 125 shares, and
migrated (budget=8.0, rptp=1.0) it gives $93.00 / 300 again.

WHAT IS NOT TOUCHED, and why each is already correct:

* Rows carrying only ``risk_per_trade_pct``. Pre-fix the budget fell back to it, so stop and size
  read the SAME number; post-fix the fallback runs the other way and they still do. Identical
  before and after -- nothing to migrate. This is every run before 2026-08-16.
* Rows where the two genes hold the SAME value: same argument, the swap is a no-op.
* Live expert settings. ``atr_risk_budget_pct`` was never set on any prod or dev ExpertInstance
  (checked 2026-09-06), so live always took the fallback and was never affected.
* Measured results -- equity curves, returns, drawdowns, fitness. Those are real observations of a
  real parameter combination and do not change. This migration only relabels WHICH gene holds
  WHICH number so the recipe reproduces the observation.

REVERSIBLE WITHOUT A 10.9 GB DB COPY. Each migrated JSON gains ``_atr_swap_migration`` recording
the original pair, so a row can be restored (and a re-run of this tool skips it).

CAVEAT WORTH KNOWING. After the swap a value may sit outside its gene's declared range
(``atr_risk_budget_pct`` is declared 0.25-3.0 but can receive up to 10.0; ``risk_per_trade_pct``
is declared 0.5-10.0 but can receive 0.25). That is correct for REPRODUCTION and harmless to read
back, but seeding a fresh optimization from a migrated genome would start outside the search box.
Re-optimize from the declared ranges, not from these rows.
"""
import argparse
import json
import sqlite3
import sys
from datetime import date
from pathlib import Path

DB = Path.home() / "Documents" / "ba2" / "test" / "dl_forecasting.db"
BUDGET = "model:atr_risk_budget_pct"
RISK = "model:risk_per_trade_pct"
MARK = "_atr_swap_migration"


def swapped(blob):
    """(new_json, before, after) when this row needs migrating, else None."""
    try:
        d = json.loads(blob)
    except (TypeError, ValueError):
        return None
    if not isinstance(d, dict) or MARK in d:
        return None
    b, r = d.get(BUDGET), d.get(RISK)
    if b is None or r is None:
        return None
    try:
        bf, rf = float(b), float(r)
    except (TypeError, ValueError):
        return None
    if bf == rf:
        return None                      # the swap is a no-op; leave the row untouched
    d[BUDGET], d[RISK] = r, b
    d[MARK] = {"from": {BUDGET: b, RISK: r}, "on": date.today().isoformat(),
               "why": "dd1f912e wired the two genes to each other's jobs; swap reproduces "
                      "the behaviour these results were measured under"}
    return json.dumps(d), (b, r), (r, b)


def revert(apply: bool) -> int:
    """Restore the pre-migration pair from each row's own ``_atr_swap_migration`` record."""
    conn = sqlite3.connect(DB)
    total = 0
    for table, col in (("strategy_optimizations", "best_params"),
                       ("backtests", "strategy_params")):
        out = []
        for i, blob in conn.execute(
                f"SELECT id, {col} FROM {table} WHERE {col} LIKE '%{MARK}%'").fetchall():
            d = json.loads(blob)
            was = d.pop(MARK)["from"]
            d[BUDGET], d[RISK] = was[BUDGET], was[RISK]
            out.append((json.dumps(d), i))
        print(f"{table}.{col}: {len(out)} migrated row(s) to restore")
        if apply and out:
            conn.executemany(f"UPDATE {table} SET {col}=? WHERE id=?", out)
        total += len(out)
    if apply:
        conn.commit()
        print(f"\nREVERTED {total} row(s) to their pre-migration values.")
    else:
        print(f"\nDRY RUN — {total} row(s) would be restored. Add --apply to write.")
    conn.close()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write (default is a dry run)")
    ap.add_argument("--revert", action="store_true",
                    help="undo a previous --apply, restoring the values recorded in "
                         "_atr_swap_migration. Run this if the code fix is NOT live yet: a "
                         "migrated genome only reproduces on the FIXED wiring, so while the "
                         "running platform still has the swap, the DB must stay unmigrated or "
                         "the two disagree.")
    ns = ap.parse_args()
    if ns.revert:
        return revert(ns.apply)
    if not DB.exists():
        print(f"FATAL: test DB not found at {DB}")
        return 1
    conn = sqlite3.connect(DB)
    total = 0
    for table, col in (("strategy_optimizations", "best_params"),
                       ("backtests", "strategy_params")):
        rows = conn.execute(
            f"SELECT id, {col} FROM {table} WHERE {col} LIKE '%atr_risk_budget_pct%'").fetchall()
        hits = [(i, s) for i, blob in rows if (s := swapped(blob)) is not None]
        print(f"{table}.{col}: {len(rows)} row(s) carry the gene, {len(hits)} need the swap")
        for i, (new, before, after) in hits[:4]:
            print(f"    id={i}  budget/risk {before} -> {after}")
        if len(hits) > 4:
            print(f"    ... and {len(hits) - 4} more")
        if ns.apply and hits:
            conn.executemany(f"UPDATE {table} SET {col}=? WHERE id=?",
                             [(new, i) for i, (new, _b, _a) in hits])
        total += len(hits)
    if ns.apply:
        conn.commit()
        print(f"\nAPPLIED to {total} row(s).")
    else:
        print(f"\nDRY RUN — {total} row(s) would change. Re-run with --apply to write.")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
