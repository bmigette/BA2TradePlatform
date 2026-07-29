"""Decide how a grid driver should (re)start ONE strategy: fresh, warm-started, or skipped.

Prints exactly one line to stdout, for a shell to capture:

    SKIP                      -- an already-COMPLETED run exists; don't redo it
    --warm-start-from <id>    -- a partial run exists; seed this job from its population
    (empty)                   -- nothing usable; start fresh

WHY THIS EXISTS. The grid drivers are a `for S in S1..S7` loop and each `optimize` restarts at
generation 0. A power cut therefore discards the whole strategy's search: four attempts at S1 on
2026-07-29 died at 216, 165, 79 and 1 trials without one completing. `--warm-start-from` already
existed but had to be wired by hand, so nobody used it under pressure.

WARM START != RESUME (the launcher's own wording): the new job still runs its full
``--generations`` budget from generation 0, and ``--seed`` still applies. What carries over is the
STARTING POPULATION — the source job's evaluated individuals, most-recent (~ its final generation)
first. So an interrupted run continues evolving instead of re-searching from random.

**--not-before IS REQUIRED, AND IT IS A CORRECTNESS GUARD, NOT A CONVENIENCE.** Seeding from a run
whose fitnesses were produced by DIFFERENT code imports that code's bias into the new population.
Concretely: opt 232 evaluated `require_still_held=1` under a name-key bug that dropped ~21% of
feed rows (fixed in 2026.07.997), so warm-starting from it would carry a systematically distorted
view of the very gene the grid exists to test — the ATR failure mode, laundered through a
population. Pass the FIRST optimization id whose results are trustworthy for this grid.
"""
import argparse
import json
import os
import sqlite3
import sys

_DEFAULT_DB = os.path.expanduser("~/Documents/ba2/test/dl_forecasting.db")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", required=True,
                    help="exact optimization name, e.g. sen5min3-S1")
    ap.add_argument("--not-before", type=int, required=True,
                    help="ignore optimizations with id < this. REQUIRED: warm-starting from a run "
                         "produced by older/buggy code imports its bias (see module docstring).")
    ap.add_argument("--min-trials", type=int, default=10,
                    help="a source needs at least this many evaluated individuals to be worth "
                         "seeding from; below it a fresh random population is no worse (default 10)")
    ap.add_argument("--db", default=_DEFAULT_DB)
    a = ap.parse_args()

    if not os.path.exists(a.db):
        print("", flush=True)               # no DB -> fresh start, never fail the grid
        return 0

    try:
        con = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True)
        rows = con.execute(
            "SELECT id, status, all_results FROM strategy_optimizations "
            "WHERE name = ? AND id >= ? ORDER BY id DESC",
            (a.name, a.not_before),
        ).fetchall()
    except sqlite3.Error as e:
        # A read problem must NEVER stop the grid — degrade to a fresh start and say so on stderr.
        print(f"grid_warm_start_arg: DB read failed ({e}); starting fresh", file=sys.stderr)
        print("", flush=True)
        return 0

    best_id, best_n = None, 0
    for oid, status, all_results in rows:
        if status == "completed":
            # Already done under trustworthy code. Re-running would overwrite a finished result
            # with a worse one if this attempt is interrupted early.
            print("SKIP", flush=True)
            return 0
        try:
            n = len(json.loads(all_results)) if all_results else 0
        except (ValueError, TypeError):
            n = 0
        if n > best_n:
            best_id, best_n = oid, n

    if best_id is not None and best_n >= a.min_trials:
        print(f"--warm-start-from {best_id}", flush=True)
    else:
        print("", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
