#!/usr/bin/env bash
# Full Senate strategy grid on a 5-MINUTE execution clock, gap-aware fill engine.
#
# WHY 5min: both Senate ForwardTest rows ran execution_interval="1d". The entry cadence is
# genuinely low-frequency (disclosures, Mondays), but the TP/SL EXITS are not -- on a daily
# clock a target is tested once per bar. The A/B (backtests 794/795) showed S5 falling
# 179% -> 81% at 5min because its take-profits fire the instant price TOUCHES the level
# intraday, halving its biggest winners (top-10 avg 14.64% -> 8.86%) on a strategy whose edge
# IS the right tail. The 1d-tuned exits are out-of-sample at 5min, so re-optimize on the clock
# live actually runs.
#
# WHY ALL SEVEN and not just S3/S5: the clock plausibly changes WHICH exit template wins. S5's
# right-tail style is exactly what intraday touches punish, while a bracket (S2) or
# target-anchored (S4) design may suit 5min better. Testing only the deployed pair would miss
# that.
#
# PARALLELISM -- measured, not guessed. FMPSenateTraderWeight runs ~11-12GB RSS per trial (its
# own class comment documents the investigation; confirmed live: slots at 12.46/10.75/10.22/
# 9.04 GB). At --parallel 6 the 63.7GB box hit 0.3GB free with 16.7GB of pagefile in use and
# throughput collapsed to 18.3 min per 1% -- a swapping artifact, not the real cost. 4 slots
# (~46GB) is the ceiling that still fits.
#
# The remote cap stays at the expert's declared max_remote_worker_slots=4 for the same reason:
# 6 remote slots would need ~69GB on that box. remote150 is listed regardless -- distribution
# engages only when a worker is ONLINE, so it is a no-op while powered off and absorbs slots
# as soon as it is up.
set -u
cd "$(dirname "$0")/.."
UNI=$(cat "$HOME/Documents/ba2/senate_universe.csv")

echo "=== Senate FULL grid @ 5min, gap-aware fills  ($(date)) ==="
.venv/Scripts/python.exe testplatform/ba2test_launcher.py optimize-batch \
  --experts FMPSenateTraderWeight \
  --strategies S1,S2,S3,S4,S5,S6,S7 \
  --universe "$UNI" \
  --start 2023-01-01 --end 2026-06-30 \
  --interval 5min \
  --fitness consistent_annual_return \
  --population 60 --generations 8 --early-stop 4 --mutation-prob 0.3 \
  --parallel 4 --seed 42 \
  --initial-capital 10000 --commission 1.0 --spread-bps 20 \
  --workers remote150 \
  --name-prefix sen5min
echo "=== FINISHED rc=$? ($(date)) ==="
