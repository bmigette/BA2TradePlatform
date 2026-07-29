#!/usr/bin/env bash
# Full Senate strategy grid (S1-S7) on a 5-MINUTE execution clock, gap-aware fill engine.
#
# WHY 5min: both Senate ForwardTest rows ran execution_interval="1d". The ENTRY cadence is
# genuinely low-frequency (disclosures, Mondays), but the TP/SL EXITS are not -- on a daily
# clock a target is tested once per bar. The A/B (backtests 794/795) showed S5 falling
# 179% -> 81% at 5min because its take-profits fire the instant price TOUCHES the level
# intraday, halving its biggest winners (top-10 avg 14.64% -> 8.86%) on a strategy whose edge
# IS the right tail. Those exits were tuned against daily closes, so they are out-of-sample at
# 5min. Re-optimize on the clock live actually runs.
#
# WHY ALL SEVEN: the clock plausibly changes WHICH exit template wins. S5's right-tail design
# is exactly what intraday touches punish, so a bracket (S2) or target-anchored (S4) exit may
# suit 5min better. Running only the deployed pair (S3/S5) would never surface that.
#
# WHY SEQUENTIAL `optimize` AND NOT `optimize-batch` -- this is the load-bearing detail.
# optimize-batch SUBMITS to the main task queue, which is started with max_workers=4
# (app/main.py init_task_queue), so it runs up to FOUR optimizations at once. --parallel is
# PER JOB, so machine concurrency becomes jobs x parallel and is bounded by nothing real.
# Observed: 2 jobs x --parallel 4 = 8 local trial slots, 0.9GB free of 63.7GB and 28.1GB of
# pagefile -- worse than the parallel-6 swap that prompted dropping to 4. Each job also
# claimed its own 4 REMOTE slots (2 x 4 = 8 on remote150) and separately pushed the full
# 5.47GB cache. Running `optimize` in-process, one strategy at a time, keeps exactly
# --parallel local + the expert's 4 remote live at any moment, and needs no serve backend.
#
# SIZING: FMPSenateTraderWeight runs ~11-12GB RSS per trial (documented on its
# max_remote_worker_slots attribute; confirmed live at 12.46/10.75/10.22/9.04GB). 4 local
# slots ~= 46GB of the 63.7GB box. The remote cap stays at the expert's declared 4/worker --
# 6 would need ~69GB there, which is the OOM that attribute exists to prevent.
#
# remote150 is listed regardless: distribution engages only when a worker is ONLINE, so it is
# a no-op while powered off and is re-admitted mid-run by the background re-check.
#
# DO NOT bump ba2_trade_platform/version.py while this runs. The master version is snapshotted
# once at job start; if the repo moves ahead, the worker correctly pulls to the NEW version and
# then fails the job's version match forever (cost us opt 218: worker on 986 vs job's 985).
set -u
cd "$(dirname "$0")/.."
UNI=$(cat "$HOME/Documents/ba2/senate_universe.csv")
PY=.venv/Scripts/python.exe

for S in S1 S2 S3 S4 S5 S6 S7; do
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
