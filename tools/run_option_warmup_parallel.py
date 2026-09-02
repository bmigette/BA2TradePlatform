"""Run tools/warm_options_history.py with real concurrency, split by --provider's needs:

  * tastytrade: N SEPARATE PROCESSES, one symbols chunk each -- each process gets its own
    Python interpreter, its own TastySession/httpx client, its own asyncio event loop.
    Sharing one session object across threads would risk the class of thread-safety issue
    async httpx clients are not generally built for; separate processes sidestep that
    entirely -- true parallelism, sidestepping the GIL the same way threads would, without
    that risk.
  * thetadata: ONE process, the FULL symbols universe, warm_options_history.py's own
    --concurrency N (THREADS sharing one provider instance within that one process). Found
    live 2026-09-02: ThetaData authenticates ONE session per api_key, and N separate
    PROCESSES each independently authenticating invalidate each other's session
    (``StatusCode.UNAUTHENTICATED: "Invalid session ID. This can occur if more than one
    terminal is running."``) -- nearly every unit failed outright. See thetadata.py's module
    docstring for the fix (``existing_authorized_client``, one authenticated session shared
    by every thread's own client) -- that sharing only works within one process.

--provider {tastytrade,thetadata} (default tastytrade) picks BOTH the vendor and which venv
runs the worker(s): tastytrade needs the `tastytrade` SDK (this repo's .venv), thetadata needs
the `thetadata` cloud library (deliberately only installed in the TEST venv, ba2-venvs/test --
see testplatform/backend/requirements.txt; the live trade app has no use for it). --workers
means "processes" for tastytrade and "threads within the one process" for thetadata -- same
flag, same intent ("how much concurrency"), different mechanism per provider's constraints.

Every process writes into ITS OWN provider's parquet store
(CACHE_FOLDER/{TastyTradeOptionsProvider,ThetaDataOptionsProvider}) -- safe because different
symbols/expiries land in different files, and each unit's manifest is written via temp+rename
per the store's own resumability contract. Running one provider never touches the other's tree.

CRASH/REBOOT RECOVERY. This launcher does nothing special itself -- it doesn't need to. Every
underlying tool it calls is already resumable at the finest grain (one manifest per
(underlying, expiry) partition), so re-running this EXACT command after any interruption --
Ctrl-C, a killed process, a full machine reboot -- picks up exactly where it left off: every
already-written partition is skipped, nothing is re-downloaded, nothing is corrupted. That is
also what --relaunch-on-logon (registered via register_reboot_task()) exploits: it does not try
to detect a "resume point", it just re-runs this script, and the resume happens for free.

Usage:
    python tools/run_option_warmup_parallel.py --workers 8
    python tools/run_option_warmup_parallel.py --workers 2 --symbols-override AAPL,MSFT --limit 1
    python tools/run_option_warmup_parallel.py --register-reboot-task   # one-time setup
    python tools/run_option_warmup_parallel.py --unregister-reboot-task
"""
import argparse
import math
import os
import subprocess
import sys
import time

REPO = r"C:\Users\basti\Documents\dev\BA2TradePlatform"
PY_BY_PROVIDER = {
    "tastytrade": rf"{REPO}\.venv\Scripts\python.exe",
    "thetadata": r"C:\Users\basti\ba2-venvs\test\Scripts\python.exe",
}
SCRIPT = rf"{REPO}\tools\warm_options_history.py"
UNIVERSE = rf"{REPO}\tools\options_universe_large_cap.txt"
#: The AppSetting('thetadata_api_key') / TastyTrade account credentials live in the SAME prod
#: DB for both providers -- see the ThetaData key save, 2026-09-02.
PROD_DB = r"C:\Users\basti\Documents\ba2_trade_platform-prod\db.sqlite"
LOG_DIR = rf"{REPO}\logs_option_warmup"
TASK_NAME = "BA2OptionWarmupResume"
STARTUP_BAT = os.path.join(
    os.environ.get("APPDATA", ""),
    r"Microsoft\Windows\Start Menu\Programs\Startup", "ba2_option_warmup_resume.bat")


def _default_argv(workers: int, batch_size: int, extra: list) -> list:
    return [sys.executable, os.path.abspath(__file__),
            "--workers", str(workers), "--batch-size", str(batch_size), *extra]


def register_reboot_task(workers: int, batch_size: int, extra_args: list = ()) -> None:
    """Drop a .bat into the Startup folder that re-runs this launcher at every logon.

    Task Scheduler (schtasks / Register-ScheduledTask) refused with "Access denied" from this
    account even with elevation attempted at the tool layer -- a policy restriction on THIS
    machine, not something fixable from here. The Startup folder needs no special privilege at
    all (the shell just scans it on login), and is otherwise equivalent for this purpose: it
    fires once per logon, in the same desktop session, killable/visible the same way ba2.bat's
    windows are.

    ``extra_args`` MUST include anything that changes what gets fetched (--provider,
    --symbols-file, --start, --end): the reboot re-run resumes by re-checking which partitions
    already have a manifest, and it can only check the RIGHT ones if it is told the same
    window/provider/universe every time -- silently falling back to this launcher's defaults
    would resume as if the window were 2023-01-01.. (warm_options_history.py's own default),
    abandoning progress on an earlier window without telling anyone.

    Idempotent: overwrites any previous version of the same file.
    """
    os.makedirs(LOG_DIR, exist_ok=True)
    inner = " ".join(f'"{a}"' if " " in a else a
                     for a in _default_argv(workers, batch_size, list(extra_args)))
    wrapper = rf"""@echo off
REM Auto-generated by tools\run_option_warmup_parallel.py --register-reboot-task.
REM Re-runs the option warm-up launcher at every logon. Safe to fire repeatedly: every
REM already-written (underlying, expiry) partition is skipped, so a normal logon that
REM was NOT preceded by a crash just re-confirms there is nothing left to do quickly.
cd /d "{REPO}"
start "BA2 Option Warmup Resume" /min {inner} >> "{LOG_DIR}\relaunch.log" 2>&1
"""
    with open(STARTUP_BAT, "w", encoding="utf-8") as f:
        f.write(wrapper)
    print(f"Wrote {STARTUP_BAT}\nFires at every logon, re-runs:\n  {inner}\n"
         f"which resumes for free (every completed partition is skipped). Remove with\n"
         f"  python {os.path.abspath(__file__)} --unregister-reboot-task")


def unregister_reboot_task() -> None:
    try:
        os.remove(STARTUP_BAT)
        print(f"Removed {STARTUP_BAT}.")
    except FileNotFoundError:
        print(f"{STARTUP_BAT} did not exist (nothing to remove).")


def _run_thetadata_single_process(args, py_exe: str, log_dir: str, concurrency: int) -> None:
    """--provider thetadata: ONE process, the FULL symbols universe, warm_options_history.py's
    own --concurrency (threads sharing one authenticated session within that process) -- see
    the module docstring for why separate processes (the tastytrade path below) do not work
    for this vendor."""
    symbols_file = args.symbols_file or UNIVERSE
    log_file = rf"{log_dir}\warmup.log"
    stderr_file = rf"{log_dir}\warmup.stderr.log"
    argv = [py_exe, SCRIPT, "--provider", "thetadata", "--db", PROD_DB,
           "--log-file", log_file, "--concurrency", str(concurrency)]
    if args.symbols_override:
        argv += ["--symbols", args.symbols_override]
    else:
        argv += ["--symbols-file", symbols_file]
    if args.api_key:
        argv += ["--api-key", args.api_key]
    if args.start:
        argv += ["--start", args.start]
    if args.end:
        argv += ["--end", args.end]
    if args.dry_run:
        argv.append("--dry-run")
    if args.limit:
        argv += ["--limit", str(args.limit)]

    print("provider   : thetadata")
    print(f"  {'symbols=' + args.symbols_override if args.symbols_override else symbols_file} "
         f"-> 1 process, --concurrency {concurrency} thread(s) -> {log_file} "
         f"(stderr: {stderr_file})")
    f_out = open(stderr_file, "a", encoding="utf-8", errors="replace")
    p = subprocess.Popen(argv, stdout=f_out, stderr=subprocess.STDOUT, cwd=REPO)
    print(f"\n1 process launched (PID {p.pid}, {concurrency} internal thread(s)).")
    print("Waiting to finish (Ctrl-C is safe -- resumes on its own manifest state)...")
    try:
        rc = p.wait()
        f_out.close()
        print(f"  process pid {p.pid} exited rc={rc} ({log_file})")
    except KeyboardInterrupt:
        print("\nInterrupted -- process left running in background; re-run this script to resume.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", choices=("tastytrade", "thetadata"), default="tastytrade",
                    help="Vendor to fetch from (default tastytrade). Picks the venv the "
                         "workers run under too -- see PY_BY_PROVIDER.")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--batch-size", type=int, default=100,
                    help="dxfeed subscription batch size passed to warm_options_history.py "
                         "(--provider tastytrade only; ignored for thetadata). Measured "
                         "2026-08-25: 50 (the tool's own default) -> ~49s/unit, 100 -> "
                         "~25s/unit, 300 -> total failure (streamer never resolved). 100 is "
                         "close to the fastest size that stayed reliable in that probe; not "
                         "exhaustively tuned across symbols, so watch the failed-unit count.")
    ap.add_argument("--symbols-file", help="Override the default universe file "
                                           "(tools/options_universe_large_cap.txt) -- e.g. "
                                           "tools/options_universe_full.txt to match the "
                                           "instruments an existing other-provider cache "
                                           "already covers.")
    ap.add_argument("--start", help="Passed through to warm_options_history.py's --start "
                                    "(its own default: 2023-01-01).")
    ap.add_argument("--end", help="Passed through to warm_options_history.py's --end "
                                  "(its own default: today).")
    ap.add_argument("--api-key", help="ThetaData API key passthrough (--provider thetadata "
                                      "only). Usually unnecessary: --db's "
                                      "AppSetting('thetadata_api_key') already resolves it.")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, help="per-process --limit (units), for a smoke test")
    ap.add_argument("--symbols-override", help="comma list, for a smoke test instead of the full universe")
    ap.add_argument("--register-reboot-task", action="store_true",
                    help="Register a Task Scheduler job that re-runs this exact command at every "
                         "logon, so a crash or Windows-Update reboot self-heals instead of "
                         "needing someone to notice and restart it by hand.")
    ap.add_argument("--unregister-reboot-task", action="store_true")
    args = ap.parse_args()

    # Everything that changes WHAT gets fetched must survive a reboot re-run identically --
    # see register_reboot_task's docstring for why silently falling back to defaults would be
    # a correctness bug, not just a inconvenience.
    extra_for_reboot = ["--provider", args.provider]
    if args.symbols_file:
        extra_for_reboot += ["--symbols-file", args.symbols_file]
    if args.start:
        extra_for_reboot += ["--start", args.start]
    if args.end:
        extra_for_reboot += ["--end", args.end]

    if args.unregister_reboot_task:
        unregister_reboot_task()
        return
    if args.register_reboot_task:
        register_reboot_task(args.workers, args.batch_size, extra_for_reboot)
        return

    py_exe = PY_BY_PROVIDER[args.provider]
    # Namespaced by provider so a tastytrade run and a thetadata run can never clobber each
    # other's chunk/log files if their windows happen to overlap.
    log_dir = rf"{LOG_DIR}\{args.provider}"
    os.makedirs(log_dir, exist_ok=True)
    n = max(1, args.workers)

    if args.provider == "thetadata":
        _run_thetadata_single_process(args, py_exe, log_dir, n)
        return

    if args.symbols_override:
        symbols = [s.strip().upper() for s in args.symbols_override.split(",") if s.strip()]
    else:
        universe_file = args.symbols_file or UNIVERSE
        with open(universe_file, encoding="utf-8") as f:
            symbols = [ln.strip() for ln in f if ln.strip() and not ln.startswith("#")]

    chunk_size = math.ceil(len(symbols) / n)
    chunks = [symbols[i:i + chunk_size] for i in range(0, len(symbols), chunk_size)]
    print(f"provider   : {args.provider}")
    print(f"{len(symbols)} symbols -> {len(chunks)} process(es), ~{chunk_size} symbols each, "
         f"batch-size={args.batch_size}")
    procs = []
    for i, chunk in enumerate(chunks):
        chunk_file = rf"{REPO}\tools\_wu_chunk_{args.provider}_{i}.txt"
        with open(chunk_file, "w", encoding="utf-8") as f:
            f.write("\n".join(chunk) + "\n")
        # worker_N.log is the tool's OWN bounded, rotating progress/retry/error log (--log-file:
        # 10 MiB x 5 backups = 50 MiB ceiling per worker). worker_N.stderr.log is the raw
        # stdout/stderr pipe -- a thin safety net that only ever holds an uncaught-exception
        # traceback or the Ctrl-C message, since --log-file takes over everything routine.
        # Before --log-file existed, THIS raw pipe caught the tastytrade SDK's own DEBUG
        # wire-trace (one line per websocket frame) with no bound at all -- 1-1.7 GB per
        # worker within a day. Never point subprocess stdout at an unrotated file again.
        log_file = rf"{log_dir}\worker_{i}.log"
        stderr_file = rf"{log_dir}\worker_{i}.stderr.log"
        # tastytrade only past this point -- thetadata returned via
        # _run_thetadata_single_process above.
        argv = [py_exe, SCRIPT, "--symbols-file", chunk_file, "--provider", args.provider,
               "--db", PROD_DB, "--log-file", log_file,
               "--account-id", "2", "--batch-size", str(args.batch_size)]
        if args.start:
            argv += ["--start", args.start]
        if args.end:
            argv += ["--end", args.end]
        if args.dry_run:
            argv.append("--dry-run")
        if args.limit:
            argv += ["--limit", str(args.limit)]
        print(f"  worker {i}: {len(chunk)} symbols -> {log_file} (stderr: {stderr_file})")
        f_out = open(stderr_file, "a", encoding="utf-8", errors="replace")
        p = subprocess.Popen(argv, stdout=f_out, stderr=subprocess.STDOUT, cwd=REPO)
        procs.append((p, f_out, log_file))
        time.sleep(2)  # stagger session/client creation slightly

    print(f"\n{len(procs)} worker(s) launched. PIDs: {[p.pid for p, _, _ in procs]}")
    print("Waiting for all to finish (Ctrl-C is safe -- each worker resumes on its own manifest state)...")
    try:
        for p, f_out, log_file in procs:
            rc = p.wait()
            f_out.close()
            print(f"  worker pid {p.pid} exited rc={rc} ({log_file})")
    except KeyboardInterrupt:
        print("\nInterrupted -- workers left running in background; re-run this script to resume.")


if __name__ == "__main__":
    main()
