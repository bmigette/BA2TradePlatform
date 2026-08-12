#!/usr/bin/env bash
# Is the goal2020 grid alive, and where is it?  Read-only — safe to run any time.
#
#   bash tools/grid_status.sh            # summary
#   bash tools/grid_status.sh -v         # + the last 15 log lines
#
# Answers, in the order you actually want them:
#   1. is a run in progress at all
#   2. is it DISTRIBUTED or silently local-only  (the failure that cost 4h40m on 2026-08-04)
#   3. which job, which generation
#   4. what is already banked
set -u
cd "$(dirname "$0")/.."

LOG=grid_goal2020.log
TESTDB="$HOME/Documents/ba2/test/dl_forecasting.db"
PY=.venv/Scripts/python.exe

echo "=================== goal2020 grid status  $(date) ==================="

# --- 1. processes -----------------------------------------------------------------------------
# Match the SCRIPT's own bash (…/bash.exe tools/grid_goal2020.sh …), not an interactive shell
# that merely mentions the name — otherwise your own grep/editor sessions count as "running".
#
# The pattern is '* tools/grid_goal2020.sh*': a LEADING SPACE plus a trailing wildcard.
#   - the leading space is what excludes a grep/editor, whose command line puts the path AFTER
#     the pattern or filename (e.g. `grep -n foo tools/grid_goal2020.sh` still matches ' tools/…'
#     — but its Name is not bash.exe, which is the real discriminator, so the space is belt and
#     braces for a `bash -c "... tools/grid_goal2020.sh"` one-liner);
#   - the TRAILING wildcard is required because the runbook documents passthrough arguments
#     (`bash tools/grid_goal2020.sh --population 60`, `--skip-experts …`). The old pattern
#     anchored on the path being LAST, so any documented passthrough launch reported
#     "PARTIAL — wrapper GONE" on a perfectly healthy grid. That false alarm invites a restart
#     of a multi-day run, which is far more expensive than the noise it was guarding against.
read -r N_SH N_DRV <<<"$(powershell -NoProfile -Command "
  \$sh  = @(Get-CimInstance Win32_Process | Where-Object { \$_.Name -eq 'bash.exe' -and \$_.CommandLine -like '* tools/grid_goal2020.sh*' })
  \$drv = @(Get-CimInstance Win32_Process | Where-Object { \$_.CommandLine -like '*run_screener_capband_matrix*' })
  '{0} {1}' -f \$sh.Count, \$drv.Count" 2>/dev/null)"

if [ "${N_SH:-0}" -gt 0 ]; then
  echo "RUN     script alive (${N_SH} bash, ${N_DRV} driver)"
elif [ "${N_DRV:-0}" -gt 0 ]; then
  echo "PARTIAL driver alive but the wrapper script is GONE — it will NOT advance to the next"
  echo "        band/matrix when this job ends. Stop it and relaunch."
else
  echo "STOPPED no grid process running"
fi

# --- 1b. orphaned spawn-pool children ----------------------------------------------------------
# Survivors of an earlier kill, holding GBs and reporting to nobody. Worth its own line because
# the symptom is indirect: on 2026-08-05 eight of them held 10.3 GB, took the box to 96.8% memory
# and STALLED the running grid mid-generation for ~40 min. Nothing in the grid's own log says
# "you are out of memory" except the RSS figure on the gen lines.
read -r N_ORPH MB_ORPH <<<"$(powershell -NoProfile -Command "
  \$o = @(Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object {
      \$_.CommandLine -like '*--multiprocessing-fork*' -and
      \$null -eq (Get-Process -Id \$_.ParentProcessId -ErrorAction SilentlyContinue) })
  '{0} {1}' -f \$o.Count, [math]::Round((\$o | Measure-Object WorkingSetSize -Sum).Sum/1MB)" 2>/dev/null)"
if [ "${N_ORPH:-0}" -gt 0 ]; then
  echo "!! ORPHANS ${N_ORPH} spawn-pool process(es) holding ${MB_ORPH} MB with a DEAD parent."
  echo "   They will never finish anything. Free them:  bash tools/grid_stop.sh --orphans-only"
fi

# --- 1c. host memory ---------------------------------------------------------------------------
powershell -NoProfile -Command "
  \$os = Get-CimInstance Win32_OperatingSystem
  \$free = [math]::Round(\$os.FreePhysicalMemory/1KB)
  \$pct  = [math]::Round(100-100*\$os.FreePhysicalMemory/\$os.TotalVisibleMemorySize,1)
  \$flag = if (\$free -lt 5000) { '  <- LOW: the grid stalls, it does not crash' } else { '' }
  'MEM     {0} MB free ({1}% used){2}' -f \$free, \$pct, \$flag" 2>/dev/null

# --- 2. distributed or local-only? -------------------------------------------------------------
if [ -f "$LOG" ]; then
  if grep -q "DISTRIBUTED across" "$LOG"; then
    echo "DIST    $(grep -h 'distributed evaluator' "$LOG" | tail -1 | sed 's/.*distributed evaluator/distributed evaluator/')"
  elif grep -qE "gen [0-9]+/" "$LOG"; then
    echo "!! LOCAL-ONLY: trials are running but no 'DISTRIBUTED across' line was ever logged."
    echo "   The remote worker is NOT helping (~2.5x slower). Stop, check WORKERS, relaunch."
  else
    echo "DIST    (not yet — still in setup/worker sync)"
  fi
  # [^)]* so the optional ", stress +N" suffix is captured too, not treated as a non-match:
  # the wrapper gained STRESS_SPREAD_MULT on 2026-08-12 and the old pattern anchored on
  # "round-trip)" being the end, so this line silently went blank on every stressed run.
  echo "SPREAD  $(grep -hoE '\(spread [0-9.]+ bps round-trip[^)]*\)' "$LOG" | tail -1)"
fi

# --- 3. current position ------------------------------------------------------------------------
if [ -f "$LOG" ]; then
  echo "JOB     $(grep -hE '^\[[0-9]+/[0-9]+\] RUN' "$LOG" | tail -1)"
  echo "GEN     $(grep -hoE 'gen [0-9]+/[0-9]+ ind [0-9]+/[0-9]+' "$LOG" | tail -1)"
  echo "LOG     $LOG  ($(date -r "$LOG" '+%H:%M:%S') last write)"
fi

# --- 4. what is banked --------------------------------------------------------------------------
"$PY" - "$TESTDB" <<'EOF'
import sqlite3, sys
c = sqlite3.connect(sys.argv[1])
rows = list(c.execute(
    "select id,name,status,round(coalesce(best_fitness,0),3) from strategy_optimizations "
    "where name like '%goal2020%' and name not like '%abandoned%' order by id"))
done = sum(1 for r in rows if r[2] == 'completed')
print(f"\nOPTS    {len(rows)} row(s), {done} completed   (45 jobs total: 24 risk_atr + 21 notional)")
for r in rows[-12:]:
    print(f"  {r[0]:<5} {r[2]:<10} fit={r[3]:<9} {r[1]}")
EOF

if [ "${1:-}" = "-v" ] && [ -f "$LOG" ]; then
  echo; echo "--- last 15 log lines ---"; tail -15 "$LOG"
fi
