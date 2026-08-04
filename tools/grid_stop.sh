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
[ "${1:-}" = "--dry-run" ] && DRY=1

powershell -NoProfile -Command "
\$dry = $DRY

\$groups = @(
  @{ n='1. wrapper script'; p={ \$_.Name -eq 'bash.exe' -and \$_.CommandLine -like '* tools/grid_goal2020.sh' } },
  @{ n='2. matrix driver' ; p={ \$_.CommandLine -like '*run_screener_capband_matrix*' } },
  @{ n='3. optimize / pool workers'; p={ \$_.CommandLine -like '*BA2TradePlatform\.venv*' } }
)

foreach (\$g in \$groups) {
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
      \$_.CommandLine -like '*BA2TradePlatform\.venv*' })
  'remaining grid processes : {0}' -f \$left.Count
  'live platform (ba2-trade): {0}  <- must still be > 0' -f @(Get-CimInstance Win32_Process | Where-Object { \$_.CommandLine -like '*ba2-trade*' }).Count
}
" 2>/dev/null

if [ "$DRY" = "0" ]; then
  echo
  echo "Next: mark the interrupted row so its NAME is free and it can never be read as a result."
  echo "A half-finished GA has no usable winner, and a duplicate name is what broke the senate"
  echo "grid's resume (see reference-distributed-optimize-traps)."
  echo
  echo "  .venv/Scripts/python.exe tools/grid_abandon.py <reason-slug>"
fi
