#!/usr/bin/env python
"""Retire the goal2020 rows of an interrupted grid run.

    .venv/Scripts/python.exe tools/grid_abandon.py nocosts
    .venv/Scripts/python.exe tools/grid_abandon.py --list

WHY RENAME AND NOT JUST CANCEL. Two rows sharing one name is what broke the senate grid's resume
on 2026-07-30: the NOT_BEFORE guard is id-based, so a stale row sat above it and PASS 2
warm-started from a pre-fix population. Renaming frees the name so the relaunch owns it
unambiguously, and the suffix records WHY the run was discarded -- which you will want months
later when a stray row turns up in a report.

Only ``running``/``pending`` rows are touched. A ``completed`` row is a real result: it is left
alone, and the grid driver will correctly SKIP that job on relaunch.
"""
import sqlite3
import sys
from pathlib import Path

DB = Path.home() / "Documents" / "ba2" / "test" / "dl_forecasting.db"
PATTERN = "%goal2020%"


def rows(conn, where=""):
    return list(conn.execute(
        "select id, name, status, round(coalesce(best_fitness,0),3) "
        f"from strategy_optimizations where name like ? {where} order by id", (PATTERN,)))


def main() -> int:
    if not DB.exists():
        print(f"FATAL: test DB not found at {DB}")
        return 1
    conn = sqlite3.connect(DB)

    if len(sys.argv) < 2 or sys.argv[1] in ("--list", "-l"):
        for r in rows(conn):
            print(f"  {r[0]:<5} {r[2]:<10} fit={r[3]:<9} {r[1]}")
        if len(sys.argv) < 2:
            print("\nusage: grid_abandon.py <reason-slug>   (e.g. nocosts, localonly, outage)")
        return 0

    slug = sys.argv[1].strip().strip("-")
    if not slug:
        print("FATAL: give a non-empty reason slug")
        return 1

    live = rows(conn, "and status in ('running','pending') and name not like '%abandoned%'")
    if not live:
        print("nothing to abandon (no running/pending goal2020 rows)")
        return 0

    print(f"abandoning {len(live)} row(s) with suffix -abandoned-{slug}:")
    for r in live:
        print(f"  {r[0]:<5} {r[2]:<10} {r[1]}")
    conn.execute(
        "update strategy_optimizations "
        f"set name = name || '-abandoned-{slug}', status = 'cancelled' "
        "where name like ? and status in ('running','pending') and name not like '%abandoned%'",
        (PATTERN,))
    conn.commit()
    print("\ndone. `completed` rows were left alone — the driver will skip those jobs on relaunch.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
