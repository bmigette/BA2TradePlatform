"""Autonomous driver for the FMPSenateTraderWeight optimization matrix.

Runs `ba2-test optimize --strategy <S?>` SEQUENTIALLY (one job at a time) over a STATIC,
disclosure-derived universe (tools/senate_universe.txt — built by
tools/build_senate_universe.py from real senate/house disclosure activity, NOT the 5min
screener cap-band mechanism: congressional trades are too sparse per symbol to split by
market-cap band, and the signal has no natural cap-band concept at all).

  strategies: S2, S3, S5, S6
    (S1 is the FMPRating live-ruleset replica and S7 is an FMPRating refinement around its
    archived winner -- neither applies to Senate; S4 anchors TP on expert_target_price,
    which Senate has no real analyst-target equivalent for.)

Each job is a SEPARATE `optimize` run that persists its own top-5 as tagged Backtests. Jobs
are named `sen-<strategy>[suffix]` and are IDEMPOTENT/RESUMABLE: a job whose
StrategyOptimization row is already `completed` is skipped, so the driver can be killed and
re-run to continue.

Prereqs (see docs/plans/2026-07-15-senate-weight-fast-optimization.md):
  ba2-test fetch-cache --symbols @tools/senate_universe.txt --timeframes 1d \
      --start 2022-10-01 --end <end> --provider fmp --workers 5
  ba2-test prewarm --symbols @tools/senate_universe.txt --experts FMPSenateTraderWeight \
      --end <end> --workers 5
Then verify hermeticity (zero FMPHistoryCacheMiss / FMP HTTP calls) with one single-symbol
smoke backtest before launching the full matrix.

Usage (test venv; FMP_API_KEY/DB_FILE in env):
    ba2-venvs/test/Scripts/python.exe tools/run_senate_matrix.py \
        [--strategies S2,S3,S5,S6] [--start 2023-01-01] [--end 2026-06-30] \
        [--population 60] [--generations 8] [--fitness calmar_ratio] \
        [--initial-capital 10000] [--dry-run]
"""
import argparse
import os
import subprocess
import sys

_UNIVERSE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "senate_universe.txt")
_EXPERT = "FMPSenateTraderWeight"
_DEFAULT_STRATEGIES = ["S2", "S3", "S5", "S6"]
_DEFAULT_CAPITAL = 10000.0


def _universe() -> str:
    with open(_UNIVERSE_FILE, encoding="utf-8") as f:
        syms = [s.strip() for s in f.read().split() if s.strip()]
    return ",".join(syms)


def _db_path() -> str:
    return os.getenv("DB_FILE", r"C:\Users\basti\Documents\ba2\test\dl_forecasting.db")


def _completed_names() -> set:
    import sqlite3
    try:
        c = sqlite3.connect(_db_path())
        rows = c.execute(
            "SELECT name FROM strategy_optimizations WHERE status='completed'").fetchall()
        c.close()
        return {r[0] for r in rows}
    except Exception:  # noqa: BLE001
        return set()


def _jobs(strategies, name_suffix=""):
    """Yield (name, strategy) in priority order."""
    for s in strategies:
        yield (f"sen-{s}{name_suffix}", s)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--strategies", default=",".join(_DEFAULT_STRATEGIES),
                    help="Comma list of strategy keys (default S2,S3,S5,S6 -- S1/S4/S7 need "
                         "a real analyst target Senate doesn't have).")
    ap.add_argument("--start", default="2023-01-01")
    ap.add_argument("--end", default="2026-06-30")
    ap.add_argument("--population", type=int, default=60,
                    help="Default 60: the expert has 15 optimizable params (7 legacy + 8 "
                         "skill/scalper) plus strategy/cond genes -- sized like FMPRating's "
                         "bumped jobs.")
    ap.add_argument("--generations", type=int, default=8)
    ap.add_argument("--mutation-prob", type=float, default=None,
                    help="Per-gene mutation probability passthrough (default: launcher's).")
    ap.add_argument("--interval", default="1d",
                    help="Daily is the right clock: disclosures lag execution by 30-45 days.")
    ap.add_argument("--fitness", default="calmar_ratio")
    ap.add_argument("--initial-capital", type=float, default=_DEFAULT_CAPITAL)
    ap.add_argument("--name-suffix", default="",
                    help="Suffix appended to every job name -- re-runs the whole matrix under "
                         "FRESH names without clobbering prior runs' tagged Backtests.")
    ap.add_argument("--workers", default=None,
                    help="Comma-separated remote worker NAMES to distribute GA trials to. "
                         "Workers must be registered + cache-synced first (fmp_history + "
                         "parquet ship automatically via push_cache).")
    ap.add_argument("--parallel", type=int, default=4)
    ap.add_argument("--profit-cap-pct", type=float, default=2000.0,
                    help="Cap each trade's gain at this %% of cost basis for the ADJUSTED "
                         "fitness. Default 2000. Pass 0 to disable.")
    ap.add_argument("--profit-share-cap-pct", type=float, default=25.0,
                    help="Cap each trade's share of the run's net profit for the ADJUSTED "
                         "fitness. Default 25. Pass 0 to disable.")
    ap.add_argument("--spread-bps", type=float, default=0.0,
                    help="Round-trip bid-ask spread in basis points (see BacktestAccount._slip/"
                         "_limit_trigger_price). Senate's universe spans all cap bands, so this "
                         "is a single blended assumption, not cap-band-specific. Default 0.0.")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    strategies = [s.strip() for s in args.strategies.split(",") if s.strip()]
    universe = _universe()
    exe = os.path.join(os.path.dirname(sys.executable), "ba2-test.exe")
    if not os.path.exists(exe):
        exe = os.path.join(os.path.dirname(sys.executable), "ba2-test")

    jobs = list(_jobs(strategies, args.name_suffix))
    done = _completed_names()
    print(f"senate matrix: {len(jobs)} jobs (strategies={strategies}, "
          f"universe={len(universe.split(','))} symbols); "
          f"{sum(1 for j in jobs if j[0] in done)} already completed.")
    if args.dry_run:
        for nm, s in jobs:
            print(f"  {'DONE' if nm in done else 'TODO'}  {nm}  ({_EXPERT} {s})")
        return 0

    for i, (name, strat) in enumerate(jobs, 1):
        if name in _completed_names():   # re-read each loop (resumable)
            print(f"[{i}/{len(jobs)}] SKIP {name} (already completed)", flush=True)
            continue
        cmd = [exe, "optimize", "--expert", _EXPERT, "--universe", universe,
               "--strategy", strat,
               "--start", args.start, "--end", args.end, "--fitness", args.fitness,
               "--interval", args.interval, "--population", str(args.population),
               "--generations", str(args.generations),
               "--initial-capital", str(args.initial_capital),
               "--run-schedule", "weekly", "--name", name, "--parallel", str(args.parallel)]
        if args.mutation_prob is not None:
            cmd += ["--mutation-prob", str(args.mutation_prob)]
        if args.profit_cap_pct and args.profit_cap_pct > 0:
            cmd += ["--profit-cap-pct", str(args.profit_cap_pct)]
        if args.profit_share_cap_pct and args.profit_share_cap_pct > 0:
            cmd += ["--profit-share-cap-pct", str(args.profit_share_cap_pct)]
        if args.workers:
            cmd += ["--workers", args.workers]
        if args.spread_bps and args.spread_bps > 0:
            cmd += ["--spread-bps", str(args.spread_bps)]
        cmd += ["--labels", strat]
        print(f"[{i}/{len(jobs)}] RUN  {name} ...", flush=True)
        rc = subprocess.run(cmd, env=os.environ.copy()).returncode
        print(f"[{i}/{len(jobs)}] {name} exit={rc}", flush=True)
    print("senate matrix driver: done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
