#!/usr/bin/env bash
# Resume the Senate 5min grid at S5, after the scoring-cache sharding fix.
#
# WHY A SEPARATE SCRIPT: optimize_senate_5min.sh runs S1..S7. S1-S4 completed on 2026-07-26/28
# (best_fitness 17.58 / 13.49 / 17.29 / 18.20) and must NOT be re-run -- re-running would
# overwrite nothing but would waste ~20h and produce duplicate Backtest rows.
#
# WHY THE GRID WAS STOPPED MID-S5: six remote trials timed out during S4 because each pool
# child held its own 5.17GB copy of the congress scoring caches (4 children = 20.7GB), taking
# remote150 to 99.2% memory; trials swapped and blew past the master's fixed 1800s budget.
# Sharding those caches by settings suffix (commit ffbf9f5 + tools/shard_scoring_caches.py)
# cut the worst-case per-trial footprint to 0.72GB, MEASURED:
#
#     skill shard        567,796 entries -> 0.22GB      (was 2.22GB whole)
#     confidence shard   655,127 entries -> 0.50GB      (was 2.95GB whole)
#     worst-case trial                      0.72GB      (was 5.17GB)
#     x4 pool children                      2.87GB      (was 20.68GB)
#
# S5 had reached only gen 1/8 when it was stopped, so ~16 min was discarded -- cheap against
# running all three remaining strategies on the old, swapping code.
#
# COMPARABILITY: S1-S4 ran pre-sharding, S5-S7 run post. This is safe because sharding is a
# pure STORAGE change -- the same key returns the same value, it is just split across files.
# Fitness is unaffected. (The columnar alternative was rejected for being 20x slower per hit,
# not for changing values; see commit ecfe14c.)
#
# remote150 will self-update to 2026.07.987 on first contact (version mismatch -> git pull),
# which is how it picks up the sharding code. Do NOT bump version.py again while this runs.
set -u
cd "$(dirname "$0")/.."
UNI=$(cat "$HOME/Documents/ba2/senate_universe.csv")
PY=.venv/Scripts/python.exe

for S in S5 S6 S7; do
  echo "=================================================================="
  echo "=== Senate $S @ 5min   start $(date)"
  echo "=================================================================="
  "$PY" testplatform/ba2test_launcher.py optimize \
    --expert FMPSenateTraderWeight \
    --strategy "$S" \
    --universe "$UNI" \
    --start 2023-01-01 --end 2026-06-30 \
    --interval 5min \
    --fitness consistent_annual_return \
    --population 60 --generations 8 --early-stop 4 --mutation-prob 0.3 \
    --parallel 4 --seed 42 \
    --initial-capital 10000 --commission 1.0 --spread-bps 20 \
    --workers remote150 \
    --labels "sen5min,${S},ForwardTestCandidate" \
    --name "sen5min-${S}"
  echo "=== Senate $S @ 5min   done rc=$? $(date)"
done
echo "=== GRID COMPLETE $(date) ==="
