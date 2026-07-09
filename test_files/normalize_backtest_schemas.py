"""One-off schema normalization for saved Backtest rows (strategy_params), so every saved
backtest/optimization TOP-N exports cleanly under the CURRENT canonical schema.

What it fixes (survey of 2026-07-08, 213 rows):
  1. Removes the DEAD legacy bracket keys `initialTpPercent`/`initialSlPercent` (152 rows, all
     holding exactly the never-applied defaults 5.0/2.0 — see migration 026's rationale: those
     dedicated fields were rejected as leftover/dead; the values never influenced a run).
     Converting them into entryActions would FABRICATE a bracket that never executed.
  2. Adds `entryActions: []` where the key is missing entirely (honest: those runs had no
     entry-time bracket) so the ruleset export shape is uniform across all saved rows.

FactorRanker rows with no buy tree are left alone on purpose — that expert bypasses rulesets.

Dry-run by default; pass --apply to write.

Usage:
    python test_files/normalize_backtest_schemas.py [--apply]
"""
import json
import sqlite3
import sys

DB = r"C:\Users\basti\Documents\ba2\test\dl_forecasting.db"
DEAD_KEYS = ("initialTpPercent", "initialSlPercent", "initial_tp_percent",
             "initial_sl_percent", "initial_tp_reference", "initialTpReference")


def main() -> int:
    apply = "--apply" in sys.argv
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT id, name, strategy_params FROM backtests ORDER BY id").fetchall()

    changed = 0
    for row in rows:
        if not row["strategy_params"]:
            continue
        sp = json.loads(row["strategy_params"])
        if not isinstance(sp, dict):
            continue
        removed = [k for k in DEAD_KEYS if k in sp]
        for k in removed:
            del sp[k]
        added_ea = False
        if "entryActions" not in sp and "entry_actions" not in sp:
            sp["entryActions"] = []
            added_ea = True
        if not removed and not added_ea:
            continue
        changed += 1
        detail = []
        if removed:
            detail.append(f"removed {removed}")
        if added_ea:
            detail.append("added entryActions=[]")
        print(f"  bt {row['id']} ({row['name'][:50]}): {', '.join(detail)}")
        if apply:
            conn.execute("UPDATE backtests SET strategy_params=? WHERE id=?",
                         (json.dumps(sp), row["id"]))

    if apply:
        conn.commit()
    print(f"{'APPLIED' if apply else 'DRY-RUN (pass --apply to write)'}: {changed} row(s) changed")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
