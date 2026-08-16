# Kill orphaned multiprocessing.spawn pool workers left behind by a killed grid/optimize/pytest run.
#
# WHY THIS EXISTS. On Windows a ProcessPoolExecutor's children are DETACHED: killing the driver
# (or the launcher, or pytest) leaves them running and holding their full working set. They are
# invisible to a `pkill -f grid_goal2020.sh` and to any filter matching the parent's command line,
# because their own command line is just `python -c "from multiprocessing.spawn import ..."`.
#
# Measured 2026-08-15: 40 such orphans holding 41 GB on a 65 GB box, accumulated across a handful
# of grid kill/relaunch cycles. They poisoned every memory measurement taken that evening -- two
# "the fix did not hold" conclusions were drawn against a box that was mostly full of leftovers
# from PREVIOUS runs, and a 2.24 MiB numpy allocation failed with MemoryError as a result.
#
# Run this BEFORE any memory measurement and BEFORE relaunching a grid, or the numbers are fiction.
#
# SAFETY: matches ONLY `multiprocessing.spawn` children. The live trade platform runs as a normal
# module (ba2-venvs\trade\Scripts\...), never via spawn, so it cannot match. Anything currently
# running a legitimate pool WILL be killed -- that is the point; do not run it mid-grid.

$orphans = Get-CimInstance Win32_Process -Filter "Name like 'python%'" |
    Where-Object { $_.CommandLine -match 'multiprocessing\.spawn' }

if (-not $orphans -or $orphans.Count -eq 0) {
    Write-Output "sweep: no spawn orphans"
    exit 0
}

$mb = [int]((($orphans | Measure-Object WorkingSetSize -Sum).Sum) / 1MB)
Write-Output ("sweep: killing {0} spawn orphan(s) holding {1} MB" -f $orphans.Count, $mb)
$orphans | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep -Seconds 8

$os = Get-CimInstance Win32_OperatingSystem
Write-Output ("sweep: free {0} MB of {1} MB" -f
    [int]($os.FreePhysicalMemory / 1KB), [int]($os.TotalVisibleMemorySize / 1KB))
