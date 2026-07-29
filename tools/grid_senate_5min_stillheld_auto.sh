#!/usr/bin/env bash
# Senate S1-S7 @ 5min, still-held + long-lookback grid — RESTART-SAFE variant of
# grid_senate_5min_stillheld.sh. Identical search; the only difference is that it decides per
# strategy whether to SKIP, WARM-START, or start fresh, so an interruption costs minutes instead
# of the whole strategy.
#
# WHY. Each `optimize` restarts at generation 0, so anything that kills the process discards that
# strategy's entire search. On 2026-07-29 four consecutive attempts at S1 died at 216 / 165 / 79 /
# 1 trials — two power cuts, one bug, one operator kill — and not one completed. `--warm-start-from`
# already existed but had to be wired by hand, which nobody does at 3am.
#
# Per strategy, tools/grid_warm_start_arg.py prints one of:
#     SKIP                     an already-COMPLETED run exists -> don't redo it
#     --warm-start-from <id>   a partial run exists -> seed the starting population from it
#     (empty)                  nothing usable -> fresh random population
#
# WARM START IS NOT A RESUME (the launcher's own wording): this job still runs its FULL
# --generations budget from generation 0 and --seed still applies. What carries over is the
# starting population — the source's evaluated individuals, most-recent (~ its final generation)
# first. So the search CONTINUES rather than restarting from random. Expect the fitness to pick up
# near where the dead run left off, not to skip the compute.
#
# ---------------------------------------------------------------------------------------------
# NOT_BEFORE IS A CORRECTNESS GUARD. Seeding from a run whose fitnesses came from DIFFERENT code
# imports that code's bias. opt 232 evaluated require_still_held=1 under the name-key bug that
# dropped ~21% of feed rows (fixed in 2026.07.997) — warm-starting from it would carry a distorted
# view of the exact gene this grid exists to test, which is the ATR failure mode laundered through
# a population. 233 is the first S1 run on fixed code.
#
# RAISE THIS whenever a change alters BACKTEST RESULTS (not merely speed or logging). If you are
# unsure whether a change is result-affecting, raise it — a fresh start costs compute, a poisoned
# population costs a wrong conclusion.
NOT_BEFORE=233
# ---------------------------------------------------------------------------------------------
#
# STOPPING THIS GRID: kill the BASH SCRIPT FIRST. Killing only the python master makes `optimize`
# return non-zero and this loop simply ADVANCES TO THE NEXT STRATEGY, spawning a fresh master —
# that is how two concurrent grids ended up on one SQLite DB on 2026-07-29. Order:
#   1) bash.exe matching this script   2) python.exe matching ba2test_launcher
#   3) any python.exe >1GB RSS (orphaned pool children outlive the master)
#
# DO NOT bump ba2_trade_platform/version.py while this runs: the master snapshots its version at
# job start, and a repo that moves ahead makes the worker pull the new version and then fail the
# job's version match forever (cost us opt 218).
set -u
cd "$(dirname "$0")/.."
UNI=$(cat "$HOME/Documents/ba2/senate_universe.csv")
PY=.venv/Scripts/python.exe

# Total generation budget for the grid. Passed BOTH to optimize (below) and to the warm-start
# helper, which uses it to compute how many generations a resumed run still owes.
GENERATIONS=8

for S in S1 S2 S3 S4 S5 S6 S7; do
  NAME="sen5min3-${S}"
  WS=$("$PY" tools/grid_warm_start_arg.py --name "$NAME" --not-before "$NOT_BEFORE" \
                                          --generations "$GENERATIONS" 2>/dev/null)

  if [ "$WS" = "SKIP" ]; then
    echo "=== Senate $S @ 5min   SKIPPED — a completed run already exists ($(date))"
    continue
  fi

  echo "=================================================================="
  echo "=== Senate $S @ 5min   start $(date)"
  if [ -n "$WS" ]; then
    echo "===   resuming: $WS  (population seeded; full generation budget still runs)"
  else
    echo "===   fresh start (no usable prior run at or after opt $NOT_BEFORE)"
  fi
  echo "=================================================================="

  # $WS is intentionally UNQUOTED: it must expand to argv entries
  # ("--warm-start-from" "233" ["--generations" "4"]) or to nothing at all.
  #
  # ORDER MATTERS: $WS sits AFTER --generations below, so when it carries a reduced
  # --generations that later value WINS (argparse keeps the last occurrence for a non-append
  # argument -- verified). Moving $WS above --generations would silently restore the full budget.
  # shellcheck disable=SC2086
  "$PY" testplatform/ba2test_launcher.py optimize \
    --expert FMPSenateTraderWeight \
    --strategy "$S" \
    --universe "$UNI" \
    --start 2023-01-01 --end 2026-06-30 \
    --interval 5min \
    --fitness consistent_annual_return \
    --population 60 --generations "$GENERATIONS" --early-stop 4 --mutation-prob 0.3 \
    --parallel 4 --seed 42 \
    --initial-capital 10000 --commission 1.0 --spread-bps 20 \
    --workers remote150 \
    $WS \
    --labels "sen5min3,stillheld,longlookback,${S},ForwardTestCandidate" \
    --name "$NAME"
  echo "=== Senate $S @ 5min   done rc=$? $(date)"
done
echo "=== GRID COMPLETE $(date) ==="
