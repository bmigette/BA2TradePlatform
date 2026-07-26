#!/usr/bin/env bash
# Re-optimize the two Senate ForwardTest strategies on a 5-MINUTE execution clock.
#
# WHY: the A/B (backtests 794/795) showed the 1d-optimized genomes degrade badly when the fill
# clock moves to 5min -- S5 fell 179% -> 81% because its take-profits fire the moment price
# TOUCHES the level intraday, halving its biggest winners (top-10 avg 14.64% -> 8.86%) on a
# strategy whose whole edge is the right tail (35% win rate, PF 8.32). The TP/SL genes were
# calibrated against daily closes, so at 5min they are out-of-sample. Re-optimizing lets the GA
# pick exit distances that suit the clock live actually runs on.
#
# Everything else mirrors optimizations 201/202 exactly: same universe, window, capital, costs,
# fitness and GA shape -- only --interval changes (plus --parallel, sized to local RAM).
#
# remote150 is included deliberately: distribution engages only when a remote worker is ONLINE,
# so listing it while it is powered off is a no-op, and the job picks up the extra slots as soon
# as it comes up.
set -u
cd "$(dirname "$0")/.."
UNI=$(cat "$HOME/Documents/ba2/senate_universe.csv")
PY=.venv/Scripts/python.exe

for S in S5 S3; do
  echo "=================================================================="
  echo "=== Senate $S @ 5min  ($(date))"
  echo "=================================================================="
  "$PY" testplatform/ba2test_launcher.py optimize \
    --expert FMPSenateTraderWeight \
    --strategy "$S" \
    --universe "$UNI" \
    --start 2023-01-01 --end 2026-06-30 \
    --interval 5min \
    --fitness consistent_annual_return \
    --population 60 --generations 8 --early-stop 4 --mutation-prob 0.3 \
    --parallel 6 --seed 42 \
    --initial-capital 10000 --commission 1.0 --spread-bps 20 \
    --workers remote150 \
    --labels "sen5min,${S},ForwardTestCandidate" \
    --name "sen-${S}-5min"
  echo "=== Senate $S @ 5min FINISHED rc=$? ($(date))"
done
