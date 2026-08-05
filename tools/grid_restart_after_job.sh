#!/usr/bin/env bash
# Wait for a specific optimization to finish, THEN cycle the grid onto the current code.
#
#   bash tools/grid_restart_after_job.sh <opt_id> [reason-slug]
#
# WHY WAIT. A running job holds the code it was launched with; picking up new code needs a
# restart, and restarting mid-job throws away everything since its last checkpointed generation.
# Waiting for a clean job boundary costs nothing: the driver skips `completed` jobs on relaunch,
# so the finished work is banked and the next job simply starts on the new code.
#
# The job the driver starts IMMEDIATELY after the watched one is unavoidable collateral -- it will
# have run for at most one poll interval, and (since 2026.08.1013) resumes from its last completed
# generation anyway.
set -u
cd "$(dirname "$0")/.."

OPT_ID="${1:?usage: grid_restart_after_job.sh <opt_id> [reason-slug]}"
REASON="${2:-restart}"
DB='C:\Users\basti\Documents\ba2\test\dl_forecasting.db'
PY=.venv/Scripts/python.exe
POLL=60

status_of() {
  "$PY" -c "
import sqlite3, sys
r = sqlite3.connect(sys.argv[1]).execute(
    'select status from strategy_optimizations where id=?', (int(sys.argv[2]),)).fetchone()
print(r[0] if r else 'missing')" "$DB" "$OPT_ID" 2>/dev/null
}

echo "[$(date +%H:%M:%S)] waiting for opt $OPT_ID to leave 'running' (poll ${POLL}s)"
while :; do
  st="$(status_of)"
  case "$st" in
    running|"") sleep "$POLL" ;;
    *) echo "[$(date +%H:%M:%S)] opt $OPT_ID -> $st"; break ;;
  esac
done

echo "[$(date +%H:%M:%S)] stopping the grid"
bash tools/grid_stop.sh

echo "[$(date +%H:%M:%S)] retiring whatever job had just started"
"$PY" tools/grid_abandon.py "$REASON"

echo "[$(date +%H:%M:%S)] relaunching on $(grep -oE '[0-9]{4}\.[0-9]{2}\.[0-9]+' ba2_trade_platform/version.py)"
mv grid_goal2020.log "grid_goal2020.log.$(date +%Y%m%d-%H%M)" 2>/dev/null
nohup bash tools/grid_goal2020.sh > grid_goal2020.log 2>&1 &
sleep 20
echo "[$(date +%H:%M:%S)] relaunched:"
head -3 grid_goal2020.log
