"""Autonomous driver for the 5min screener CAP-BAND optimization matrix.

Runs `ba2-test optimize --screener --screener-cap-band <band> --strategy <S?>` SEQUENTIALLY
(one job at a time — each 5min job preloads 500-1100 symbols, so parallel runs would blow memory)
over the planned matrix, in PRIORITY order (large band first = fastest, then mid, then small):

  per band, in order:
    FMPRating          x {S1,S2,S3,S4}
    FMPEarningsDrift   x {S1,S2,S3,S4}   (mid/small only — no large-cap earnings-drift signal)
    FMPInsiderClusterBuy x {S1,S2,S3,S4} (mid/small only — FMP has no large-cap insider data)
    FactorRanker       (once — bypass expert, no strategy variants)

Each job is a SEPARATE `optimize` run that persists its own top-5 as tagged Backtests. Jobs are
named `scr-<band>-<expert>[-<S?>]` and are IDEMPOTENT/RESUMABLE: a job whose StrategyOptimization
row is already `completed` is skipped, so the driver can be killed and re-run to continue.

Usage (test venv; FMP_API_KEY/DB_FILE in env):
    ba2-venvs/test/Scripts/python.exe test_files/run_screener_capband_matrix.py \
        [--bands large,mid,small] [--strategies S1,S2,S3,S4] \
        [--start 2023-01-01] [--end 2026-01-01] [--population 40] [--generations 8] \
        [--interval 5min] [--fitness calmar_ratio] [--include-no-data] [--dry-run]
"""
import argparse
import os
import subprocess
import sys

_STORE = r"C:\Users\basti\Documents\ba2\common\cache\screener\metric_store"
# A real --universe is required by the CLI but is OVERRIDDEN by the screened union when --screener
# is set; pass the NDQ30 as a harmless placeholder.
_PLACEHOLDER_UNIVERSE = ("AAPL,MSFT,NVDA,AMZN,META,GOOGL,AVGO,TSLA,COST,NFLX,AMD,PEP,ADBE,CSCO,TMUS,"
                         "INTC,QCOM,INTU,AMAT,TXN,AMGN,ISRG,BKNG,HON,VRTX,ADP,SBUX,GILD,MU,LRCX")
_CLASSIC = ["FMPRating", "FMPEarningsDrift", "FMPInsiderClusterBuy"]
_RANKER = "FactorRanker"
# Experts with no usable data in the large-cap band (skipped on `large` unless --include-no-data).
_NO_LARGE_CAP = {"FMPEarningsDrift", "FMPInsiderClusterBuy"}


def _db_path() -> str:
    return os.getenv("DB_FILE", r"C:\Users\basti\Documents\ba2\test\dl_forecasting.db")


def _completed_names() -> set:
    import sqlite3
    try:
        c = sqlite3.connect(_db_path())
        rows = c.execute("SELECT name FROM strategy_optimizations WHERE status='completed'").fetchall()
        c.close()
        return {r[0] for r in rows}
    except Exception:  # noqa: BLE001
        return set()


def _jobs(bands, strategies, include_no_data, skip_experts=frozenset()):
    """Yield (name, expert, strategy_or_None, band) in priority order.

    ``skip_experts`` (a set of expert class names) drops those experts entirely — used to defer
    an expert that is too slow for the matrix (e.g. FMPInsiderClusterBuy: ~1.5h/backtest) without
    editing the expert list."""
    for band in bands:
        for expert in _CLASSIC:
            if expert in skip_experts:
                continue
            if expert in _NO_LARGE_CAP and band == "large" and not include_no_data:
                continue
            for s in strategies:
                yield (f"scr-{band}-{expert}-{s}", expert, s, band)
        if _RANKER not in skip_experts:
            yield (f"scr-{band}-{_RANKER}", _RANKER, None, band)  # bypass: one job per band


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--bands", default="large,mid,small")
    ap.add_argument("--strategies", default="S1,S2,S3,S4")
    ap.add_argument("--start", default="2023-01-01")
    ap.add_argument("--end", default="2026-01-01")
    ap.add_argument("--population", type=int, default=40)
    ap.add_argument("--generations", type=int, default=8)
    ap.add_argument("--interval", default="5min")
    ap.add_argument("--fitness", default="calmar_ratio")
    ap.add_argument("--store", default=_STORE)
    ap.add_argument("--cadence-days", type=int, default=7)
    ap.add_argument("--include-no-data", action="store_true",
                    help="Also run EarningsDrift/Insider on the large band (default: skip — no data).")
    ap.add_argument("--skip-experts", default="",
                    help="Comma list of expert class names to EXCLUDE entirely (e.g. "
                         "'FMPInsiderClusterBuy' — too slow at ~1.5h/backtest; defer it).")
    ap.add_argument("--workers", default=None,
                    help="Comma-separated remote worker NAMES to distribute each job's GA trials to "
                         "(e.g. 'remote150'); trials spread across these + local. Workers must be "
                         "registered + cache-synced first.")
    ap.add_argument("--parallel", type=int, default=2,
                    help="Local trial consumers per job (ThreadPoolExecutor). Keep low when "
                         "distributing to remote workers — each local consumer holds the OHLCV "
                         "cache in RAM (~5GB at 5min), so 4 saturates a 64GB host. Default 2.")
    ap.add_argument("--profit-cap-pct", type=float, default=2000.0,
                    help="Cap each trade's gain at this %% of its cost basis for the ADJUSTED "
                         "fitness/return, so one lucky non-reproducible mega-winner (e.g. a sub-$1 "
                         "stock that 90x'd) can't dominate the GA. Default 2000. Pass 0 to disable.")
    ap.add_argument("--profit-share-cap-pct", type=float, default=25.0,
                    help="Cap each trade's gain at this %% of the run's NET profit for the ADJUSTED "
                         "fitness/return, so no single trade contributes more than this share of "
                         "total return (a trade can pass --profit-cap-pct yet still be 60%% of the "
                         "book). Default 25. Pass 0 to disable.")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    bands = [b.strip() for b in args.bands.split(",") if b.strip()]
    strategies = [s.strip() for s in args.strategies.split(",") if s.strip()]
    exe = os.path.join(os.path.dirname(sys.executable), "ba2-test.exe")
    if not os.path.exists(exe):
        exe = os.path.join(os.path.dirname(sys.executable), "ba2-test")

    skip_experts = frozenset(e.strip() for e in args.skip_experts.split(",") if e.strip())
    jobs = list(_jobs(bands, strategies, args.include_no_data, skip_experts))
    done = _completed_names()
    print(f"matrix: {len(jobs)} jobs (bands={bands}, strategies={strategies}); "
          f"{sum(1 for j in jobs if j[0] in done)} already completed.")
    if args.dry_run:
        for nm, exp, s, band in jobs:
            print(f"  {'DONE' if nm in done else 'TODO'}  {nm}  ({exp} {s or '(bypass)'} / {band})")
        return 0

    for i, (name, expert, strat, band) in enumerate(jobs, 1):
        if name in _completed_names():   # re-read each loop (resumable)
            print(f"[{i}/{len(jobs)}] SKIP {name} (already completed)", flush=True)
            continue
        cmd = [exe, "optimize", "--expert", expert, "--universe", _PLACEHOLDER_UNIVERSE,
               "--screener", "--screener-store", args.store, "--screener-cap-band", band,
               "--start", args.start, "--end", args.end, "--fitness", args.fitness,
               "--interval", args.interval, "--population", str(args.population),
               "--generations", str(args.generations), "--screener-cadence-days", str(args.cadence_days),
               "--run-schedule", "weekly", "--name", name, "--parallel", str(args.parallel)]
        if args.profit_cap_pct and args.profit_cap_pct > 0:
            cmd += ["--profit-cap-pct", str(args.profit_cap_pct)]
        if args.profit_share_cap_pct and args.profit_share_cap_pct > 0:
            cmd += ["--profit-share-cap-pct", str(args.profit_share_cap_pct)]
        if strat is not None:
            cmd += ["--strategy", strat]
        if args.workers:
            cmd += ["--workers", args.workers]   # distribute trials across remote workers + local
        print(f"[{i}/{len(jobs)}] RUN  {name} ...", flush=True)
        rc = subprocess.run(cmd, env=os.environ.copy()).returncode
        print(f"[{i}/{len(jobs)}] {name} exit={rc}", flush=True)
    print("matrix driver: done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
