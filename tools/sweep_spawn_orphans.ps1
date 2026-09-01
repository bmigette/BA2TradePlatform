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
# SAFETY -- READ THIS BEFORE WIDENING THE FILTER.
#
# On 2026-08-16 the live trade platform (BOTH the 8080 dev and 8081 prod instances) was killed
# during orphan cleanup. The kill used a hardcoded "preserve PIDs 19656,19660" list. Those PIDs
# were STALE -- the platform had restarted earlier in the session -- so the exclusion protected
# nothing and a blanket "kill all python except those" took the platform down. The health check
# in use ("live 2/2") had been reporting on dead PIDs for a while and looked fine throughout.
#
# Two rules follow, and they are why this script is shaped the way it is:
#   1. NEVER identify the thing to protect by PID. PIDs go stale silently. Identify it by what it
#      IS -- the ba2-venvs\trade executable, or whatever is listening on 8080/8081.
#   2. NEVER widen to "kill all python". Targeted cleanup failing is a reason to fix the filter,
#      not to increase the blast radius on a box that runs live trading.
#
# This script matches ONLY `multiprocessing.spawn` children AND explicitly excludes anything
# running from the trade venv, belt and braces.
#
#   3. An ORPHAN is a spawn child whose stated parent is GONE. Every spawn child carries its
#      parent in its own command line (`spawn_main(parent_pid=N, ...)`); if that PID is still
#      alive the child is a legitimate worker of a RUNNING pool and must be left alone. Measured
#      2026-09-01: grid_goal2020.sh runs this sweep at every (re)launch, and two relaunches at
#      20:25 and 20:28 each killed matrix3's four live local trial children ("killing 4 spawn
#      orphan(s) holding 20945 MB") -- the master logged `local pool broken`, rebuilt the pool
#      and requeued the trials, but up to an hour of in-flight work per slot was thrown away
#      each time. Same rule as worker_server._sweep_orphaned_spawn_children on the workers.

$TRADE_VENV = 'ba2-venvs\\trade'

$orphans = Get-CimInstance Win32_Process -Filter "Name like 'python%'" |
    Where-Object {
        $_.CommandLine -and
        $_.CommandLine -match 'multiprocessing\.spawn' -and
        $_.CommandLine -notmatch [regex]::Escape($TRADE_VENV)
    } |
    Where-Object {
        # parent_pid alive -> a running pool's worker, NOT an orphan. No parent_pid in the
        # cmdline -> cannot prove it is orphaned -> leave it (fail closed).
        $m = [regex]::Match($_.CommandLine, 'parent_pid=(\d+)')
        if (-not $m.Success) { return $false }
        $parentAlive = [bool](Get-Process -Id ([int]$m.Groups[1].Value) -ErrorAction SilentlyContinue)
        return (-not $parentAlive)
    }

# Refuse to run if the live platform is not answering -- if it is already down, cleaning up
# orphans is not the urgent problem and a sweep would only muddy the picture.
$liveUp = @(8080, 8081) | ForEach-Object {
    [bool](Get-NetTCPConnection -LocalPort $_ -State Listen -ErrorAction SilentlyContinue)
}
if ($liveUp -contains $false) {
    Write-Output "sweep: WARNING live trade platform not listening on 8080 and/or 8081 -- start it (Desktop\ba2.bat) before sweeping"
}

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
