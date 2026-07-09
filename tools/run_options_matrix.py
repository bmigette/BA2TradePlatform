"""Autonomous driver for the OPTIONS strategy optimization matrix.

Runs `ba2-test optimize --strategy <OS?/O_?>` SEQUENTIALLY (one job at a time) over the
option-strategy matrix on the top-100 large-cap universe covered by the offline options
cache (built via `ba2-test fetch-options`; see tools/options_universe_top100.txt — the
DISTINCT underlyings actually present in the cache, so no OptionsCacheMiss mid-run):

  per expert, in order:
    OS1   grouped: directional DEBIT  (O_LC long call, O_LP long put, O_VERT bear put
          vertical, O_BF call butterfly) — one job, the GA toggles members on/off so the
          persisted top-5 can differ in STRUCTURE, not just parameters
    OS2   grouped: neutral CREDIT     (O_SSTG short strangle, O_SSTD short straddle,
          O_IC iron condor)
    OS3   grouped: skewed CREDIT      (O_JL jade lizard, O_RS put ratio spread)
    O_CC  covered call (equity entry + call overlay — different entry path, own job)
    O_STK plain equity baseline (control)

Experts: FMPRating only by default. FMPEarningsDrift/FMPInsiderClusterBuy are EXCLUDED —
they have no large-cap signal/data and the options cache is large-cap only. FactorRanker
is a bypass expert (no strategy rules), so it cannot drive option entries.

Jobs are named `optm-<expert>-<strategy>[suffix]` and are IDEMPOTENT/RESUMABLE: a job whose
StrategyOptimization row is already `completed` is skipped, so the driver can be killed and
re-run to continue.

Prereqs: the options cache must cover [start-60d, end] for the universe (fetch-options with
--live if the configured Alpaca key is a live-only account's), plus the usual OHLCV/FMP
prewarm for the expert signals.

Usage (test venv; FMP_API_KEY/DB_FILE in env):
    ba2-venvs/test/Scripts/python.exe tools/run_options_matrix.py \
        [--strategies OS1,OS2,OS3,O_CC,O_STK] [--experts FMPRating] \
        [--start 2024-04-01] [--end 2026-06-30] [--population 40] [--generations 8] \
        [--fitness calmar_ratio] [--initial-capital 20000] [--dry-run]
"""
import argparse
import os
import subprocess
import sys

_UNIVERSE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "options_universe_top100.txt")
# Grouped families first (the interesting structure search), then the equity-entry pair.
_DEFAULT_STRATEGIES = ["OS1", "OS2", "OS3", "O_CC", "O_STK"]
_DEFAULT_EXPERTS = ["FMPRating"]
# Options need ~2x the equity balance headroom (100-share multipliers, CSP/strangle margin
# reservations) — $20k keeps mid-priced large-cap structures affordable without letting one
# contract dominate the book.
_DEFAULT_CAPITAL = 20000.0


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


def _jobs(experts, strategies, name_suffix=""):
    """Yield (name, expert, strategy) in priority order (per expert: groups then equity)."""
    for expert in experts:
        for s in strategies:
            yield (f"optm-{expert}-{s}{name_suffix}", expert, s)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--experts", default=",".join(_DEFAULT_EXPERTS),
                    help="Comma list of experts (default FMPRating; EarningsDrift/Insider "
                         "excluded — no large-cap signal on this options universe).")
    ap.add_argument("--strategies", default=",".join(_DEFAULT_STRATEGIES),
                    help="Comma list of option strategy keys: grouped OS1/OS2/OS3 and/or "
                         "singles (O_LC,O_LP,O_VERT,O_BF,O_SSTG,O_SSTD,O_IC,O_JL,O_RS,"
                         "O_CC,O_STK).")
    ap.add_argument("--start", default="2024-04-01",
                    help="Backtest start (options cache floor 2024-02-01 + expert warmup; "
                         "default 2024-04-01).")
    ap.add_argument("--end", default="2026-06-30")
    ap.add_argument("--population", type=int, default=40)
    ap.add_argument("--generations", type=int, default=8)
    ap.add_argument("--mutation-prob", type=float, default=None,
                    help="Per-gene mutation probability passthrough (default: launcher's).")
    ap.add_argument("--interval", default="1d",
                    help="Analysis/fill interval (default 1d — option cache bars are daily).")
    ap.add_argument("--fitness", default="calmar_ratio")
    ap.add_argument("--initial-capital", type=float, default=_DEFAULT_CAPITAL,
                    help=f"Starting cash per trial (default {_DEFAULT_CAPITAL:.0f} — options "
                         "need more headroom than the equity grid's 10k).")
    ap.add_argument("--name-suffix", default="",
                    help="Suffix appended to every job name — re-runs the whole matrix under "
                         "FRESH names without clobbering prior runs' tagged Backtests.")
    ap.add_argument("--workers", default=None,
                    help="Comma-separated remote worker NAMES to distribute GA trials to. "
                         "Workers must be registered + cache-synced first (the options "
                         "sqlite is part of the cache sync).")
    ap.add_argument("--parallel", type=int, default=4,
                    help="Local trial consumers per job (default 4; daily-interval trials are "
                         "far lighter than the 5min screener grid).")
    ap.add_argument("--profit-cap-pct", type=float, default=2000.0,
                    help="Cap each trade's gain at this %% of cost basis for the ADJUSTED "
                         "fitness (options tails are fat: one 40x long call must not own the "
                         "GA). Default 2000. Pass 0 to disable.")
    ap.add_argument("--profit-share-cap-pct", type=float, default=25.0,
                    help="Cap each trade's share of the run's net profit for the ADJUSTED "
                         "fitness. Default 25. Pass 0 to disable.")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    experts = [e.strip() for e in args.experts.split(",") if e.strip()]
    strategies = [s.strip() for s in args.strategies.split(",") if s.strip()]
    universe = _universe()
    exe = os.path.join(os.path.dirname(sys.executable), "ba2-test.exe")
    if not os.path.exists(exe):
        exe = os.path.join(os.path.dirname(sys.executable), "ba2-test")

    jobs = list(_jobs(experts, strategies, args.name_suffix))
    done = _completed_names()
    print(f"options matrix: {len(jobs)} jobs (experts={experts}, strategies={strategies}, "
          f"universe={len(universe.split(','))} symbols); "
          f"{sum(1 for j in jobs if j[0] in done)} already completed.")
    if args.dry_run:
        for nm, exp, s in jobs:
            print(f"  {'DONE' if nm in done else 'TODO'}  {nm}  ({exp} {s})")
        return 0

    for i, (name, expert, strat) in enumerate(jobs, 1):
        if name in _completed_names():   # re-read each loop (resumable)
            print(f"[{i}/{len(jobs)}] SKIP {name} (already completed)", flush=True)
            continue
        cmd = [exe, "optimize", "--expert", expert, "--universe", universe,
               "--strategy", strat,
               "--start", args.start, "--end", args.end, "--fitness", args.fitness,
               "--interval", args.interval, "--population", str(args.population),
               "--generations", str(args.generations),
               "--initial-capital", str(args.initial_capital),
               # Daily cadence: option entries want the day's signal, not a weekly scan —
               # mirrors scripts/run_options_grid.sh.
               "--run-schedule", "daily", "--name", name, "--parallel", str(args.parallel)]
        if args.mutation_prob is not None:
            cmd += ["--mutation-prob", str(args.mutation_prob)]
        if args.profit_cap_pct and args.profit_cap_pct > 0:
            cmd += ["--profit-cap-pct", str(args.profit_cap_pct)]
        if args.profit_share_cap_pct and args.profit_share_cap_pct > 0:
            cmd += ["--profit-share-cap-pct", str(args.profit_share_cap_pct)]
        if args.workers:
            cmd += ["--workers", args.workers]
        print(f"[{i}/{len(jobs)}] RUN  {name} ...", flush=True)
        rc = subprocess.run(cmd, env=os.environ.copy()).returncode
        print(f"[{i}/{len(jobs)}] {name} exit={rc}", flush=True)
    print("options matrix driver: done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
