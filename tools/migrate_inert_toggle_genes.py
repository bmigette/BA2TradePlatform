#!/usr/bin/env python
"""Write the inert toggles into stored genomes as the 0 they always effectively were.

    .venv/Scripts/python.exe tools/migrate_inert_toggle_genes.py            # dry run
    .venv/Scripts/python.exe tools/migrate_inert_toggle_genes.py --apply
    .venv/Scripts/python.exe tools/migrate_inert_toggle_genes.py --like %goal2020% --apply

WHY, WHEN A RUNTIME PIN ALREADY EXISTS. ``INERT_RM_TOGGLES`` forces ``use_atr_stop`` and
``regime_overlay_enabled`` off in ``_build_daily_trial_config``, which covers every re-run,
warm start and robustness variant. It does NOT cover the DEPLOY path:
``_derive_export_payload`` builds a payload's ``expert_params`` directly from the ``model:*``
keys of the stored row, with no trial config in between. A saved genome carrying
``model:use_atr_stop: 1`` therefore exports ``use_atr_stop: True`` -- and now that coerce_bool
reads it correctly, the live instance would ENABLE a feature that was off in every run that
selected it. Four of the six currently-deployed genomes carry that value.

A stored row should also simply say what it did. ``model:use_atr_stop: 1`` on a row whose
results were produced with the ATR stop-leg disabled is a lie the next reader has to know a
bug's history to see through.

The values written are not a guess: both settings were unreadable-as-true for the whole life of
every run on record (a bool gene arriving as int 1 was stored as the JSON string "1", which the
reader tested against 'true'), so 0 is what actually executed. ``_inert_toggle_pin`` records the
original pair on each row so the edit is reversible and auditable.

Rows are only touched where a gene is present and non-zero. Re-running the tool is a no-op.
"""
import argparse
import json
import os
import sqlite3
import sys
from datetime import date

DB = os.environ.get("BA2_TEST_DB",
                    os.path.expanduser(r"~\Documents\ba2\test\dl_forecasting.db"))

#: (table, json column) pairs that hold a genome.
TARGETS = (("backtests", "strategy_params"), ("strategy_optimizations", "best_params"))

GENES = ("model:use_atr_stop", "model:regime_overlay_enabled")
MARK = "_inert_toggle_pin"

WHY = ("both genes were unreadable-as-true for the life of every run on record (a bool gene "
       "arriving as int 1 was stored as the JSON string \"1\"), so 0 is what executed; written "
       "so the row is self-describing and a deploy cannot export them as ON")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write (default: dry run)")
    ap.add_argument("--like", help="only rows whose name matches this SQL LIKE pattern")
    ap.add_argument("--revert", action="store_true", help="restore the recorded originals")
    ns = ap.parse_args()

    con = sqlite3.connect(DB)
    total = 0
    try:
        for table, col in TARGETS:
            cols = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
            if col not in cols:
                continue
            sql = f"SELECT id, name, {col} FROM {table} WHERE {col} IS NOT NULL"
            params = ()
            if ns.like:
                sql += " AND name LIKE ?"
                params = (ns.like,)
            changed = 0
            for rid, name, raw in con.execute(sql, params).fetchall():
                try:
                    d = json.loads(raw) if isinstance(raw, str) else raw
                except (TypeError, ValueError):
                    continue
                if not isinstance(d, dict):
                    continue

                if ns.revert:
                    mark = d.get(MARK)
                    if not mark or "from" not in mark:
                        continue
                    for k, v in mark["from"].items():
                        d[k] = v
                    d.pop(MARK, None)
                else:
                    present = {g: d[g] for g in GENES if g in d}
                    # Only rows that actually claim ON. A row already at 0 is already honest,
                    # and marking it would add provenance for an edit that never happened.
                    if not any(v for v in present.values()):
                        continue
                    d[MARK] = {"from": present, "on": date.today().isoformat(), "why": WHY}
                    for g in present:
                        d[g] = 0

                changed += 1
                total += 1
                print(f"  {table}#{rid} {str(name)[:56]}")
                if ns.apply:
                    con.execute(f"UPDATE {table} SET {col}=? WHERE id=?",
                                (json.dumps(d), rid))
            print(f"{table}.{col}: {changed} row(s)")
        if ns.apply:
            con.commit()
    finally:
        con.close()

    verb = "reverted" if ns.revert else ("rewritten" if ns.apply else "would change")
    print(f"\n{total} row(s) {verb}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
