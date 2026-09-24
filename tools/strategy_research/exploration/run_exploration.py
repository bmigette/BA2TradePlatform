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

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.strategy_research.exploration.market_conditions import refuse_inert_market_exit
from tools.strategy_research.exploration.profiles import (
    ALL_FAMILIES, EXTENSION_FAMILIES, FAMILIES, build_manifest, fingerprint)
from tools.strategy_research.exploration.runtime import (
    check_database, database_url, execute_ready, job_lock, preflight, refuse_unrunnable,
    resolve_universe, write_json)


def parser():
    ap = argparse.ArgumentParser(description=__doc__)
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Offline manifest only (the default).")
    mode.add_argument("--preflight", action="store_true", help="Read existing caches; do not create backtest jobs.")
    mode.add_argument("--run", action="store_true", help="Execute jobs and save results in the test database.")
    mode.add_argument("--export-universe", type=Path,
                      help="Write the full selected symbol union for warmup; no price fetch or job creation.")
    ap.add_argument("--families", nargs="+", choices=ALL_FAMILIES, default=list(FAMILIES),
                    help="Default: the ten campaign families. Opt-in (never in the default): "
                         + ", ".join(EXTENSION_FAMILIES) + ".")
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
    ap.add_argument("--market-condition-profile", default="none",
                    help="none, ohlcv-v1, ta-structure-v1, or their comma-separated combination.")
    ap.add_argument("--market-condition-manifest",
                    help="Pinned digest, or profile=digest pairs for multiple profiles.")
    ap.add_argument("--market-condition-mode", choices=("search", "all-off"), default="search",
                    help="Search entry genes, or retain original rules with pinned condition diagnostics.")
    ap.add_argument("--market-exit", default="", metavar="exit,stop,tp",
                    help="Comma list: append market exit/stop/TP rules, each off by default behind a "
                         "searched toggle. Needs a profile and --search genetic; single-direction jobs only.")
    ap.add_argument("--allow-sl-loosen", action="store_true",
                    help="Set allow_ruleset_sl_loosen on every job's experts: ruleset stops may loosen "
                         "down to the trade's max-loss stop (default: tighten only).")
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
    # Bind the engine to the same central cache checked below, before backend imports.
    os.environ["CACHE_FOLDER"] = str(args.cache_dir.resolve().parent)
    path = args.job_file.resolve()
    job = json.loads(path.read_text(encoding="utf-8"))
    verify_job(job)
    database = check_database(args.db_file)
    # Bind the backend to --db-file before ANYTHING can import app.models.database (its engine
    # is created at import); preflight must not, but this does not depend on it.
    os.environ["DATABASE_URL"] = database_url(database)
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
            save_top=args.save_top, store=args.store, spread_bps=args.spread_bps, etf_symbols=args.etf_symbols,
            market_condition_profile=args.market_condition_profile,
            market_condition_manifest=args.market_condition_manifest,
            market_condition_mode=args.market_condition_mode,
            market_exit=tuple(s.strip() for s in args.market_exit.split(",") if s.strip()),
            allow_sl_loosen=args.allow_sl_loosen)
        if args.variants is not None:
            available = {j["variant"] for j in manifest["jobs"]}
            unknown = set(args.variants) - available
            if unknown:
                raise ValueError(f"Variants unavailable for selected families: {sorted(unknown)}")
            manifest["jobs"] = [j for j in manifest["jobs"] if j["variant"] in args.variants]
            refuse_inert_market_exit(manifest["jobs"])
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
            if "market_condition" in bt:
                mc = bt["market_condition"]
                print(f"    conditions={','.join(mc['profiles'])} mode={mc['mode']} "
                      f"added_genes={mc['gene_count']}; budget={args.population}x{args.generations}")
            if "market_exit" in bt:
                mx = bt["market_exit"]
                print(f"    market_exit={','.join(mx['kinds'])} direction={mx['direction']} "
                      f"rules={','.join(mx['rules'])} at_exit_index={mx['insert_index']} (off by default) "
                      f"added_genes={mx['gene_count']}")
                for kind, reason in mx["omitted"].items():
                    print(f"    market_exit {kind} OMITTED: {reason}")
        market_exit = manifest["jobs"][0]["optimization_config"]["backtest"].get("market_exit") if manifest["jobs"] else None
        print(f"Market exits: {','.join(market_exit['kinds']) if market_exit else 'none'}; "
              f"ruleset SL loosen: {'on' if args.allow_sl_loosen else 'off'}")
        print(f"{len(manifest['jobs'])} jobs. Each is an independent account.", flush=True)
        if args.export_universe is not None:
            path = args.export_universe.resolve()
            if path.parent == ROOT:
                raise ValueError("Put the warmup universe in a research subdirectory, not the repository root")
            symbols = sorted({s for job in manifest["jobs"]
                              for s in resolve_universe(job["optimization_config"]["backtest"])[0]})
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("\n".join(symbols) + "\n", encoding="utf-8")
            print(f"Warmup universe: {path} ({len(symbols)} symbols); no optimization started.")
            return 0
        if args.run or args.preflight:
            # The whole selection, before its first job: a short job must not fail hours in.
            refuse_unrunnable(manifest["jobs"])
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
