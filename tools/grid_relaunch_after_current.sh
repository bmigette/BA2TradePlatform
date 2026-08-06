#!/usr/bin/env bash
# One-shot watcher: wait for the CURRENTLY RUNNING optimization to finish, then relaunch the
# goal2020 grid so it picks up jobs that are not `completed`.
#
# Why this exists (2026-08-06): FactorRanker large (opt 251/252) is marked `failed` — it produced
# ZERO trades because one unpriceable symbol aborted every rebalance (fixed in 2026.08.1021). The
# running grid driver already passed the large band, so it will NOT revisit FactorRanker within
# this pass; only a relaunch re-runs it. Relaunching immediately would have discarded the
# in-flight mid-band job at gen 4/8, so this waits for that job to land first.
#
# The relaunched driver SKIPS every `completed` optimization, so nothing already done is redone.
#
#   run:    nohup bash tools/grid_relaunch_after_current.sh > grid_relaunch_watch.log 2>&1 &
#   cancel: kill the PID printed on startup (nothing else is affected)
set -u

REPO="C:/Users/basti/Documents/dev/BA2TradePlatform"
DB="C:/Users/basti/Documents/ba2/test/dl_forecasting.db"
PY="$REPO/.venv/Scripts/python.exe"
WATCH_OPT="${WATCH_OPT:-253}"          # the optimization to wait on
POLL_SECONDS="${POLL_SECONDS:-120}"
MAX_WAIT_HOURS="${MAX_WAIT_HOURS:-12}" # give up rather than hang forever if the job wedges

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }

log "watcher started (pid $$) — waiting for opt ${WATCH_OPT} to leave 'running'"
log "poll=${POLL_SECONDS}s  max_wait=${MAX_WAIT_HOURS}h"

deadline=$(( $(date +%s) + MAX_WAIT_HOURS * 3600 ))
while :; do
    status=$("$PY" -c "
import sqlite3
c = sqlite3.connect('file:$DB?mode=ro', uri=True)
r = c.execute('select status from strategy_optimizations where id=$WATCH_OPT').fetchone()
print(r[0] if r else 'missing')
" 2>/dev/null)

    if [ "$status" != "running" ]; then
        log "opt ${WATCH_OPT} is now '${status}' — proceeding to relaunch"
        break
    fi
    if [ "$(date +%s)" -ge "$deadline" ]; then
        log "ABORT: opt ${WATCH_OPT} still running after ${MAX_WAIT_HOURS}h; NOT relaunching."
        log "Investigate before restarting by hand (see docs/RUNBOOK-goal2020-grid.md)."
        exit 1
    fi
    sleep "$POLL_SECONDS"
done

# Stop the old lane completely, or two lanes race for the same jobs.
#
# MUST include run_screener_capband_matrix.py. The process tree is
#   grid_goal2020.sh -> run_screener_capband_matrix.py -> ba2-test optimize -> fork workers
# and the MATRIX RUNNER is what loops over jobs. Killing only the bash driver + the current
# optimize (the first version of this script) left the matrix runner alive; it immediately
# spawned a replacement optimize and kept marching through the band. That produced exactly the
# duplicate-lane incident this script was meant to avoid (2026-08-06 12:06: opt 254/256 running
# alongside the relaunched 255, both lanes claiming all 6 remote150 slots).
#
# Re-scan in a loop: a single snapshot is a TOCTOU race — the old driver can spawn the next job
# microseconds after Get-CimInstance and before the kill lands, so that child survives.
log "stopping the old grid lane (driver + matrix runner + optimize + pool children)"
for attempt in 1 2 3 4 5; do
    remaining=$(powershell.exe -NoProfile -Command "
\$p = Get-CimInstance Win32_Process |
  Where-Object { \$_.CommandLine -match 'grid_goal2020|run_screener_capband_matrix|ba2-test.exe optimize|multiprocessing-fork' }
foreach (\$q in \$p) { try { Stop-Process -Id \$q.ProcessId -Force -ErrorAction Stop } catch {} }
(\$p | Measure-Object).Count
" 2>/dev/null | tr -d '\r' | tail -1)
    log "  sweep ${attempt}: matched ${remaining:-0} process(es)"
    [ "${remaining:-0}" = "0" ] && break
    sleep 3
done
sleep 5

# Any optimization still flagged 'running' now is dead (we just killed it) — mark it so the
# relaunch does not treat it as in-flight and so the row is not left dangling forever.
"$PY" -c "
import sqlite3
c = sqlite3.connect(r'$DB')
n = c.execute(\"update strategy_optimizations set status='failed', \"
              \"error_message='killed by grid_relaunch_after_current.sh watcher' \"
              \"where status='running' and name like '%goal2020%'\").rowcount
c.commit()
print(f'  marked {n} dangling running row(s) failed')
"

cd "$REPO" || exit 1
ts=$(date '+%Y%m%d-%H%M%S')
[ -f grid_goal2020.log ] && mv -f grid_goal2020.log "grid_goal2020.log.${ts}-pre-relaunch"
log "relaunching the grid"
nohup bash tools/grid_goal2020.sh > grid_goal2020.log 2>&1 &
log "grid relaunched (pid $!) — watcher done"
