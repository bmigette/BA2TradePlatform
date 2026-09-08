#!/usr/bin/env python
"""Preview or run ten strategy families, with $10,000 equity by default.

Default: write an offline manifest. --preflight checks existing caches. --run
executes sequentially, one child process per job, in the backtest database.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.strategy_research.profiles import FAMILIES, build_manifest, fingerprint
from tools.strategy_research.runtime import (
    check_database, execute_ready, job_lock, preflight, write_json)


def parser():
    ap = argparse.ArgumentParser(description=__doc__)
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Offline manifest only (the default).")
    mode.add_argument("--preflight", action="store_true", help="Read existing caches; do not create backtest jobs.")
    mode.add_argument("--run", action="store_true", help="Execute jobs and save results in the test database.")
    ap.add_argument("--families", nargs="+", choices=FAMILIES, default=list(FAMILIES))
    ap.add_argument("--variants", nargs="+", help="Optional subset, e.g. control timeout.")
    ap.add_argument("--equity", type=float, default=10000.0, help="Starting account equity per independent job.")
    ap.add_argument("--equity-cap", type=float, default=10000.0, help="Sizing cap; 0 enables uncapped compounding.")
    ap.add_argument("--start", default="2020-01-01")
    ap.add_argument("--end", default="2025-12-31")
    ap.add_argument("--search", choices=("grid", "genetic"), default="grid")
    ap.add_argument("--population", type=int, default=24)
    ap.add_argument("--generations", type=int, default=4)
    ap.add_argument("--parallel", type=int, default=1, help="Local GA individuals; grid and saved reruns are serial.")
    ap.add_argument("--workers", default="", help="Comma-separated configured worker names (genetic search only).")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--save-top", type=int, default=5)
    ap.add_argument("--spread-bps", type=float, help="Override every family's spread; 0 is preserved.")
    ap.add_argument("--store", help="Override the snapshot's metric-store directory.")
    ap.add_argument("--etf-symbols", nargs="+", help="Fixed ETF research universe; default SPY IEF TLT GLD.")
    data_home = Path(os.environ["BA2_HOME"]).expanduser() if "BA2_HOME" in os.environ else Path.home() / "Documents/ba2"
    ap.add_argument("--db-file", type=Path, default=data_home / "test/dl_forecasting.db")
    ap.add_argument("--cache-dir", type=Path, default=data_home / "common/cache/FMPOHLCVProvider")
    ap.add_argument("--output-dir", type=Path, help="Default: reports/strategy_research/<campaign hash>/.")
    ap.add_argument("--resume", action="store_true", help="Resume an interrupted job after its previous runner has stopped.")
    ap.add_argument("--job-file", type=Path, help=argparse.SUPPRESS)
    return ap


def verify_job(job):
    unsigned = {k: v for k, v in job.items() if k != "fingerprint"}
    if fingerprint(unsigned) != job["fingerprint"]:
        raise ValueError("Job manifest was modified: regenerate it with the driver")


def run_child(args):
    path = args.job_file.resolve()
    job = json.loads(path.read_text(encoding="utf-8"))
    verify_job(job)
    database = check_database(args.db_file)
    # Lock scope is the destination DB, so different output directories cannot
    # accidentally start the same job twice.
    lock = database.parent / "research10-locks" / (job["name"] + ".lock")
    with job_lock(lock):
        ready = preflight(job, args.cache_dir)
        write_json(path.with_suffix(".prepared.json"), ready)
        result = execute_ready(ready, database, resume=args.resume)
        write_json(path.with_suffix(".result.json"), result)
    return 0


def run_jobs(jobs, output, args):
    """Fail fast, preserve the failed log, and never claim a partial campaign passed."""
    database = check_database(args.db_file)
    for index, job in enumerate(jobs, 1):
        job_path = output / (job["name"] + ".json")
        write_json(job_path, job)
        log_path = output / (job["name"] + ".log")
        command = [sys.executable, str(Path(__file__).resolve()), "--job-file", str(job_path),
                   "--db-file", str(database), "--cache-dir", str(args.cache_dir.resolve())]
        if args.resume:
            command.append("--resume")
        print(f"[{index}/{len(jobs)}] {job['name']}\n  Log: {log_path}", flush=True)
        with log_path.open("a", encoding="utf-8") as log:
            result = subprocess.run(command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, check=False)
        if result.returncode:
            print(f"FAILED ({result.returncode}). Remaining jobs were not started. See {log_path}", file=sys.stderr)
            return result.returncode if result.returncode > 0 else 1
    return 0


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.job_file is not None:
            return run_child(args)
        manifest = build_manifest(
            families=args.families, equity=args.equity,
            equity_cap=None if args.equity_cap == 0 else args.equity_cap,
            start=args.start, end=args.end, search=args.search, population=args.population,
            generations=args.generations, parallel=args.parallel, seed=args.seed,
            workers=[s.strip() for s in args.workers.split(",") if s.strip()],
            save_top=args.save_top, store=args.store, spread_bps=args.spread_bps, etf_symbols=args.etf_symbols)
        if args.variants is not None:
            available = {j["variant"] for j in manifest["jobs"]}
            unknown = set(args.variants) - available
            if unknown:
                raise ValueError(f"Variants unavailable for selected families: {sorted(unknown)}")
            manifest["jobs"] = [j for j in manifest["jobs"] if j["variant"] in args.variants]
        output = args.output_dir.resolve() if args.output_dir else ROOT / "reports/strategy_research" / fingerprint(manifest)[:12]
        if output == ROOT:
            raise ValueError("Use an output subdirectory, not the repository root")
        write_json(output / "manifest.json", manifest)
        print(f"Manifest: {output / 'manifest.json'}")
        for job in manifest["jobs"]:
            bt = job["optimization_config"]["backtest"]
            cap = bt["account_settings"]["equity_cap"]
            print(f"  {job['family']:16} {job['variant']:26} {bt['start_date']}..{bt['end_date']} "
                  f"equity={bt['initial_capital']:g} cap={cap} {job['optimization_type']}")
        print(f"{len(manifest['jobs'])} jobs. Each is an independent account.", flush=True)
        if args.run:
            return run_jobs(manifest["jobs"], output, args)
        if args.preflight:
            for job in manifest["jobs"]:
                ready = preflight(job, args.cache_dir)
                write_json(output / (job["name"] + ".prepared.json"), ready)
                print(f"PASS: {job['name']}: {ready['preflight']['symbols']} symbols", flush=True)
        else:
            print("Preview only: no database opened and no optimization started.")
        return 0
    except (ValueError, RuntimeError, OSError, KeyError) as exc:
        print(f"research10: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
