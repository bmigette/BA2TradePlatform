#!/usr/bin/env bash
# Stop the goal2020 grid cleanly.
#
#   bash tools/grid_stop.sh --dry-run     # show what would be killed (safe)
#   bash tools/grid_stop.sh               # actually stop it
#
# ORDER IS THE WHOLE POINT. The wrapper script runs the driver as a child, one call per
# (mode, band). Kill the DRIVER first and the wrapper simply moves on and starts the NEXT band --
# on 2026-08-04 that silently launched matrix 2 while the operator thought the grid was stopped.
# So: wrapper first, then driver, then any orphaned optimize/pool workers.
#
# The live trading platform (ba2-trade) is never touched: it runs from a DIFFERENT venv
# (~/ba2-venvs/trade), and every pattern here is scoped to the repo venv or the grid scripts.
#
# Stopping mid-job loses that job's progress up to its last COMPLETED generation. From
# 2026.08.1013 the GA checkpoints per generation, so a relaunch resumes the same job at its last
# completed generation, provided the job NAME and gene space are unchanged.
set -u
cd "$(dirname "$0")/.."

DRY=0
ONLY_ORPHANS=0
case "${1:-}" in
  --dry-run)      DRY=1 ;;
  --orphans-only) ONLY_ORPHANS=1 ;;   # free leaked workers WITHOUT stopping a healthy run
esac

powershell -NoProfile -Command "
\$dry = $DRY
\$onlyOrphans = $ONLY_ORPHANS

\$groups = @(
  # NOTE the trailing * : this pattern used to be '* tools/grid_goal2020.sh', anchored at the END
  # of the command line, so it only matched a wrapper launched with NO arguments. Launching with
  # any flag (e.g. --parallel 3) made the wrapper invisible to this tier -- grid_stop killed the
  # driver and optimize beneath it, the wrapper survived, and its "for band in ..." loop simply
  # started the NEXT job. Measured 2026-08-09: a stop-then-relaunch left TWO complete lanes
  # running, each engaging 5 remote slots, so the worker reported busy=10 against capacity 6 and
  # its RSS climbed 24 -> 37 -> 53 GB (93.6% used) in 90 seconds.
  # ...and the -notlike '* -c *' is what keeps that trailing * safe. The REAL wrapper is invoked as
  # bash.exe tools/grid_goal2020.sh [args]; any shell that merely MENTIONS the script (the terminal
  # that launched it, a tooling shell running nohup bash tools/grid_goal2020.sh ..., even the shell
  # executing grid_stop itself) is invoked as bash.exe -c "...". Without this the widened pattern
  # matched 2 unrelated shells in a dry run. Same self-match hazard the tier-3 venv pattern already
  # documents, reached from a different direction.
  # (NB: no backticks anywhere in this block -- it is embedded in a bash double-quoted string, so a
  # backtick opens command substitution and breaks the whole script at RUNTIME while bash -n still
  # passes, because -n only parses the string, never its contents.)
  @{ n='1. wrapper script'; p={ \$_.Name -eq 'bash.exe' -and \$_.CommandLine -like '*tools/grid_goal2020.sh*' -and \$_.CommandLine -notlike '* -c *' } },
  @{ n='2. matrix driver' ; p={ \$_.CommandLine -like '*run_screener_capband_matrix*' } },
  @{ n='3. optimize / pool workers'; p={ \$_.CommandLine -like '*BA2TradePlatform\.venv*' } },
  # 4. SPAWN-POOL ORPHANS. These do NOT match tier 3: a multiprocessing spawn child runs from the
  # BASE interpreter (AppData\Local\Programs\Python) with a bare "--multiprocessing-fork" command
  # line, so the repo-venv path never appears in it. Missing them is not cosmetic -- on 2026-08-05
  # eight survivors of earlier kills held 10.3 GB, drove the box to 96.8% memory, and STALLED the
  # running grid for ~40 minutes mid-generation. Only ones whose PARENT IS GONE are touched: a
  # live pool's children belong to a running job and must never be killed from here.
  @{ n='4. orphaned spawn-pool children'; p={
        \$_.CommandLine -like '*--multiprocessing-fork*' -and
        \$null -eq (Get-Process -Id \$_.ParentProcessId -ErrorAction SilentlyContinue) } }
)

foreach (\$g in \$groups) {
  # --orphans-only: skip tiers 1-3 so a HEALTHY run keeps going and only the leaked children die.
  if (\$onlyOrphans -and \$g.n -notlike '4.*') { continue }
  # Never match a PowerShell process: the grid never runs under one, but THIS query does -- and
  # group 3's venv pattern happily matches the very shell executing it.
  \$procs = @(Get-CimInstance Win32_Process | Where-Object \$g.p |
              Where-Object { \$_.Name -notin @('powershell.exe','pwsh.exe') })
  if (\$procs.Count -eq 0) { '{0}: none' -f \$g.n; continue }
  '{0}: {1} process(es)' -f \$g.n, \$procs.Count
  foreach (\$p in \$procs) {
    \$c = \$p.CommandLine; if (\$c.Length -gt 78) { \$c = \$c.Substring(0,78) }
    '    [{0}] {1}' -f \$p.ProcessId, \$c
    if (-not \$dry) { Stop-Process -Id \$p.ProcessId -Force -ErrorAction SilentlyContinue }
  }
  # Let the parent notice its child died before killing the next tier, so it cannot
  # race ahead and spawn a replacement.
  if (-not \$dry) { Start-Sleep -Seconds 3 }
}

''
if (\$dry) { 'DRY RUN — nothing was killed.' } else {
  Start-Sleep -Seconds 2
  \$left = @(Get-CimInstance Win32_Process | Where-Object {
      \$_.CommandLine -like '*grid_goal2020.sh' -or
      \$_.CommandLine -like '*run_screener_capband_matrix*' -or
      \$_.CommandLine -like '*BA2TradePlatform\.venv*' -or
      (\$_.CommandLine -like '*--multiprocessing-fork*' -and
       \$null -eq (Get-Process -Id \$_.ParentProcessId -ErrorAction SilentlyContinue)) })
  'remaining grid processes : {0}' -f \$left.Count
  'live platform (ba2-trade): {0}  <- must still be > 0' -f @(Get-CimInstance Win32_Process | Where-Object { \$_.CommandLine -like '*ba2-trade*' }).Count
}
" 2>/dev/null

if [ "$DRY" = "0" ] && [ "$ONLY_ORPHANS" = "0" ]; then
  echo
  echo "Next: mark the interrupted row so its NAME is free and it can never be read as a result."
  echo "A half-finished GA has no usable winner, and a duplicate name is what broke the senate"
  echo "grid's resume (see reference-distributed-optimize-traps)."
  echo
  echo "  .venv/Scripts/python.exe tools/grid_abandon.py <reason-slug>"
fi
