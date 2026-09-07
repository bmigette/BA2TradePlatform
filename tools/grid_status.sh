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

# The grid may run under a per-matrix wrapper (e.g. grid_goal2020_matrix3.sh) writing to its OWN
# log (grid_goal2020_matrix3.log). The plain grid_goal2020.log then stays frozen while the live
# run logs elsewhere — hardcoding it made every line below it stale fiction (seen 2026-08-18:
# reported job/gen/dist from a run that had ended 2 days earlier, and flagged the alive wrapper
# as GONE). Use the most recently written grid_goal2020*.log as the live signal.
LOG=$(ls -1t grid_goal2020*.log 2>/dev/null | head -1)
LOG="${LOG:-grid_goal2020.log}"
TESTDB="$HOME/Documents/ba2/test/dl_forecasting.db"
PY=.venv/Scripts/python.exe

echo "=================== goal2020 grid status  $(date) ==================="

# --- 1. processes -----------------------------------------------------------------------------
# Match the SCRIPT's own bash (…/bash.exe tools/grid_goal2020.sh …), not an interactive shell
# that merely mentions the name — otherwise your own grep/editor sessions count as "running".
#
# THIS CHECK HAS NOW CRIED WOLF FOUR TIMES, so read the shape before touching the pattern. Every
# false "PARTIAL" has been the same mistake: pinning on the exact SPELLING of a launch that is
# documented to vary, when `Name -eq 'bash.exe'` was always the real discriminator. And the
# alarm's own advice — "stop it and relaunch" — would destroy days of a healthy multi-day run,
# so a false positive here is far more expensive than a missed detection.
#
#   1. anchoring on the path being LAST broke every documented passthrough launch
#      (`bash tools/grid_goal2020.sh --population 60`).
#   2. the literal `grid_goal2020.sh` missed the per-matrix wrappers (grid_goal2020_matrix3.sh).
#   3. a LEADING SPACE before `tools/` missed `bash ./tools/grid_goal2020.sh` — the form the
#      nohup launcher in the runbook actually uses. Seen 2026-09-07: both wrappers alive and
#      advancing jobs while this printed "wrapper is GONE".
#
# So: match the SCRIPT NAME anywhere in the command line of a real bash.exe, and exclude the one
# thing that genuinely is not the wrapper — a `bash -c "…"` shell that merely MENTIONS the
# script, which is what a nohup launcher or a tool-call shell looks like. That launcher exits in
# seconds while the wrapper it spawned runs for days, so counting it is the one way this check
# can report RUN when nothing is running.
read -r N_SH N_DRV <<<"$(powershell -NoProfile -Command "
  \$sh  = @(Get-CimInstance Win32_Process | Where-Object { \$_.Name -eq 'bash.exe' -and \$_.CommandLine -match 'grid_goal2020[^ ]*\.sh' -and \$_.CommandLine -notmatch '\s-c\s' })
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
    SUMMARY_LINE_NO=$(grep -n 'distributed evaluator (opt' "$LOG" | tail -1 | cut -d: -f1)
    echo "DIST    $(grep -h 'distributed evaluator' "$LOG" | tail -1 | sed 's/.*distributed evaluator/distributed evaluator/')"
    # The line above is a ONE-TIME summary printed at job START; it is NEVER updated by a
    # later re-admission/exclusion for the SAME job, so it goes stale the moment a worker
    # recovers or drops out mid-run. Seen for real (2026-08-26): opt 358 read "0 local + 0
    # remote slot(s) across 0 worker(s)" -- the only configured worker had just been excluded
    # at pre-flight (mid self-update-restart) -- while minutes later that worker was fully
    # busy on all 24 slots, with nothing here to say so. Rather than try to silently
    # reconstruct a live count (fragile: the summary line never names which worker(s) its Y/Z
    # cover, so a full per-worker state machine would have to guess), just surface whatever
    # happened AFTER the summary so this line is never read alone as current truth.
    if [ -n "$SUMMARY_LINE_NO" ]; then
      SINCE=$(tail -n "+$((SUMMARY_LINE_NO + 1))" "$LOG" \
              | grep -E "worker \S+ (recovered; re-admitted|pre-flight failed|pool resized)" | tail -3)
      if [ -n "$SINCE" ]; then
        echo "        since that summary:"
        echo "$SINCE" | sed 's/^.*WARNING:[^:]*:/          /'
      fi
    fi
  elif grep -qE "gen [0-9]+/" "$LOG"; then
    echo "!! LOCAL-ONLY: trials are running but no 'DISTRIBUTED across' line was ever logged."
    echo "   The remote worker is NOT helping (~2.5x slower). Stop, check WORKERS, relaunch."
  else
    echo "DIST    (not yet — still in setup/worker sync)"
  fi
  # [^)]* so the optional ", stress +N" suffix is captured too, not treated as a non-match:
  # the wrapper gained STRESS_SPREAD_MULT on 2026-08-12 and the old pattern anchored on
  # "round-trip)" being the end, so this line silently went blank on every stressed run.
  # 2026-08-18: matrix wrappers log it as `spread N bps, stress +X` (no parens, no
  # "round-trip") — try the parenthesized form first, then fall back to that, so this
  # line does not silently blank out just because the log moved format.
  SP=$(grep -hoE '\(spread [0-9.]+ bps round-trip[^)]*\)' "$LOG" | tail -1)
  [ -z "$SP" ] && SP=$(grep -hoE 'spread [0-9.]+ bps, stress \+[0-9.]+' "$LOG" | tail -1)
  echo "SPREAD  ${SP:-(no spread line in $LOG)}"
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

# FOUR MATRICES, NOT TWO. grid_goal2020.sh gained STRATEGIES_EXTRA=S5,S6,S7 (matrices 3 and 4,
# a measured +45 jobs), and grid_goal2020_matrix3.sh runs DeterministicScorer and the Senate on
# its own list. The old footer asserted a flat "45 jobs total" against a count that had long
# grown past it -- "88 completed (45 jobs total)" is not a progress line, it is noise. Split it
# the way the wrappers actually divide the work, keyed on the STRATEGY IN THE NAME, which is
# what the driver builds the name from.
def bucket(name):
    if "DeterministicScorer" in name:  return "ds"      # matrix3 wrapper, its own list
    if name.startswith("sen-"):        return "senate"  # matrix3 wrapper, 3 jobs
    if any(f"-{s}-" in name for s in ("S5", "S6", "S7")): return "ext"
    return "base"

banked = {}
for _id, _name, _status, _f in rows:
    if _status == "completed":
        banked.setdefault(bucket(_name), set()).add(_name)
n = lambda k: len(banked.get(k, ()))
print()
print(f"OPTS    {len(rows)} row(s), {done} completed")
print(f"        base S1-S3   {n('base'):>3}/45   (matrix 1+2: 24 risk_atr + 21 notional)")
# 42, not 45: FactorRanker carries no strategy in its name (it bypasses classic RM, so S1-S7
# never enter), which means matrix 3's 3 FactorRanker slots are satisfied by the rows matrix 1
# already banked and the driver SKIPs them. Counting them as work left to do overstates the
# remaining wall time by three multi-hour jobs.
print(f"        ext  S5-S7   {n('ext'):>3}/42   (matrix 3+4, less the 3 shared FactorRanker)")
print(f"        DeterministicScorer {n('ds')}   Senate {n('senate')}/3   (matrix3 wrapper)")
for r in rows[-12:]:
    print(f"  {r[0]:<5} {r[2]:<10} fit={r[3]:<9} {r[1]}")
EOF

if [ "${1:-}" = "-v" ] && [ -f "$LOG" ]; then
  echo; echo "--- last 15 log lines ---"; tail -15 "$LOG"
fi
