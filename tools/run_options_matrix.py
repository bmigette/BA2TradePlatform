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
        [--start 2023-01-01] [--end 2025-12-31] [--population 40] [--generations 8] \
        [--fitness <override>] [--initial-capital 20000] [--dry-run]
"""
import argparse
import os
import subprocess
import sys

# Sibling helper in tools/ (shared by all three matrix drivers). The directory is put on the
# path explicitly so the import works however the script is reached (path, -m, or a test import).
_TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)
from matrix_flags import cap_passthrough  # noqa: E402

_UNIVERSE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "options_universe_top100.txt")
# Grouped families first (the interesting structure search), then the equity-entry pair.
# OS1-4 collectively cover all 15 pure-option structure types (see _OPTION_GROUPS in
# ba2test_launcher.py); O_CC/O_PP are equity + option-overlay hybrids; O_STK is the plain
# equity control.
_DEFAULT_STRATEGIES = ["OS1", "OS2", "OS3", "OS4", "O_CC", "O_PP", "O_STK"]
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


def _gate_passthrough(args) -> list:
    """Extra optimize CLI tokens for the gate-only screener entry gate ([] when unset)."""
    if not args.screener_gate_store:
        return []
    return ["--screener-gate-store", args.screener_gate_store,
            "--max-stock-price", str(args.max_stock_price)]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--experts", default=",".join(_DEFAULT_EXPERTS),
                    help="Comma list of experts (default FMPRating; EarningsDrift/Insider "
                         "excluded — no large-cap signal on this options universe).")
    ap.add_argument("--strategies", default=",".join(_DEFAULT_STRATEGIES),
                    help="Comma list of option strategy keys: grouped OS1-4 and/or singles "
                         "(O_LC,O_LP,O_VERT,O_BULLCS,O_BF,O_SSTG,O_SSTD,O_IC,O_CSP,O_JL,O_RS,"
                         "O_BEARCS,O_STRD,O_STRG,O_CC,O_PP,O_STK). OS1=directional debit, "
                         "OS2=neutral credit, OS3=skewed credit, OS4=volatility debit "
                         "(non-directional).")
    # THE GRID WINDOW: 2023-01-01 .. 2025-12-31 (set 2026-08-26).
    #
    # 2026 is the RESERVED walk-forward holdout -- a separate exercise, worth nothing if the
    # search has already seen the data. The previous default ended 2026-06-30, i.e. six months
    # INSIDE it. ba2test_launcher._assert_option_window_excludes_holdout is the rail that stops
    # a pure-option job reaching past the boundary whatever is passed here.
    #
    # NOTE ON THE START: daily_backtest_handler.validate_options_window enforces the
    # options-history floor of the VENDOR SERVING THE RUN'S STORE (see
    # ba2_providers.options.options_history_floor -- Alpaca 2024-01-18 measured, TastyTrade
    # 2022-10-01). The store the backtest reads DEFAULTS to the Alpaca-built OptionsHistoryCache
    # sqlite, so an option job starting 2023-01-01 raises "Options backtests served by 'alpaca'
    # require start >= 2024-01-18" before it runs unless the run selects the TastyTrade parquet
    # store (options_store="parquet" / BACKTEST_OPTIONS_STORE=parquet -- see
    # backtest/options_store.py, added 2026-08-28). Selecting it moves the floor to 2022-10-01
    # HONESTLY, because it also moves which store is read; do NOT instead lower the Alpaca
    # number, which would admit a window the sqlite is empty for.
    ap.add_argument("--start", default="2023-01-01",
                    help="Backtest start (default 2023-01-01; requires the options cache -- "
                         "and the serving vendor's history floor -- to reach that far back).")
    ap.add_argument("--end", default="2025-12-31",
                    help="Backtest end (default 2025-12-31: 2026 is the reserved "
                         "walk-forward holdout and the launcher refuses to search into it).")
    ap.add_argument("--population", type=int, default=40)
    ap.add_argument("--generations", type=int, default=8)
    ap.add_argument("--early-stop", type=int, default=None,
                    help="Generations without improvement before a job stops (spec stage 1: 8). "
                         "Omitted -> launcher default.")
    ap.add_argument("--mutation-prob", type=float, default=None,
                    help="Per-gene mutation probability passthrough (default: launcher's).")
    ap.add_argument("--interval", default="1d",
                    help="Analysis/fill interval (default 1d — option cache bars are daily).")
    ap.add_argument("--fitness", default=None,
                    help="Fitness metric forced on EVERY job. Default: omitted, so each job "
                         "gets ba2test_launcher's per-strategy-kind auto-resolution "
                         "(consistent_annual_return for pure-option kinds OS1-4/O_*, "
                         "sharpe_ratio for O_CC/O_PP/O_STK) -- passing this flag here "
                         "overrides that auto-resolution uniformly for the whole matrix.")
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
                    help="Cap each BET's gain at this %% of the capital deployed in it for the "
                         "ADJUSTED fitness (options tails are fat: one 40x long call must not "
                         "own the GA). A multi-leg structure counts ONCE -- net P&L against net "
                         "debit -- and a net-CREDIT structure has no basis, so only the share "
                         "cap bounds it. Default 2000. Pass 0 to disable.")
    ap.add_argument("--profit-share-cap-pct", type=float, default=25.0,
                    help="Cap each BET's share of the run's net profit for the ADJUSTED "
                         "fitness (same per-structure unit as --profit-cap-pct). Default 25. "
                         "Pass 0 to disable.")
    ap.add_argument("--fitness-trade-scale", action="store_true",
                    help="Down-weight thin-trade-count configs: multiplies a positive fitness "
                         "by min(avg_trades_per_year, cap)/100. Options entries are naturally "
                         "sparse (far fewer signals than the equity screener grid), so a "
                         "handful-of-trades config with a near-zero max_drawdown can otherwise "
                         "post an artificially huge calmar_ratio (return/~0 blows up) despite a "
                         "modest real dollar result. Passed through to `ba2-test optimize`.")
    ap.add_argument("--fitness-trade-scale-cap", type=float, default=100.0,
                    help="Cap (trades/year) for --fitness-trade-scale. Default 100.")
    ap.add_argument("--fitness-trade-scale-target", type=float, default=100.0,
                    help="Trades/year that earns FULL credit (factor 1.0) for --fitness-trade-scale. "
                         "Default 100 (an equities-scale cadence); options strategies trade far less "
                         "often, so lower this (e.g. 50) to avoid crushing a healthy options config "
                         "just for not hitting an equities-scale trade count.")
    ap.add_argument("--fitness-win-rate-factor", action="store_true",
                    help="Multiply a positive fitness by 2 x win_rate_fraction. Passed through "
                         "to `ba2-test optimize`.")
    ap.add_argument("--launcher", default=None,
                    help="Path to the launcher executable (or ba2test_launcher.py). Default: "
                         "the ba2-test installed next to the Python interpreter. Point this at "
                         "a WORKTREE launcher to run code different from the editable install.")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--screener-gate-store", default=None,
                    help="Attach this parquet metric store as a GATE-ONLY per-bar entry gate on "
                         "EVERY job (passes --screener-gate-store/--max-stock-price through to "
                         "ba2-test optimize). The store must cover the options universe.")
    ap.add_argument("--max-stock-price", type=float, default=100.0,
                    help="Max underlying price for the gate-only entry gate (default 100 — the "
                         "$20k-account cap). 0 disables the price filter.")
    args = ap.parse_args()

    experts = [e.strip() for e in args.experts.split(",") if e.strip()]
    strategies = [s.strip() for s in args.strategies.split(",") if s.strip()]
    universe = _universe()

    launcher = args.launcher
    if not launcher:
        launcher = os.path.join(os.path.dirname(sys.executable), "ba2-test.exe")
        if not os.path.exists(launcher):
            launcher = os.path.join(os.path.dirname(sys.executable), "ba2-test")
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
        cmd = ([sys.executable, launcher] if launcher.endswith(".py") else [launcher]) + ["optimize", "--expert", expert, "--universe", universe,
               "--strategy", strat,
               "--start", args.start, "--end", args.end,
               "--interval", args.interval, "--population", str(args.population),
               "--generations", str(args.generations),
               "--initial-capital", str(args.initial_capital),
               # Daily cadence: option entries want the day's signal, not a weekly scan —
               # mirrors scripts/run_options_grid.sh.
               "--run-schedule", "daily", "--name", name, "--parallel", str(args.parallel)]
        cmd += _gate_passthrough(args)
        if args.fitness:
            # Explicit override forces this metric uniformly; omitted (default) lets
            # ba2test_launcher's _resolve_fitness() pick per-strategy-kind (pure-option ->
            # option_consistent_annual_return, O_CC/O_PP/O_STK -> sharpe_ratio).
            cmd += ["--fitness", args.fitness]
        if args.early_stop is not None:
            cmd += ["--early-stop", str(args.early_stop)]
        if args.mutation_prob is not None:
            cmd += ["--mutation-prob", str(args.mutation_prob)]
        # "Pass 0 to disable" (see the --profit-cap-pct help): a 0 must be FORWARDED, because
        # omitting the flag lets ba2test_launcher re-apply its own 2000/25 default instead.
        cmd += cap_passthrough(args)
        if args.fitness_trade_scale:
            cmd += ["--fitness-trade-scale", "--fitness-trade-scale-cap", str(args.fitness_trade_scale_cap),
                    "--fitness-trade-scale-target", str(args.fitness_trade_scale_target)]
        if args.fitness_win_rate_factor:
            cmd += ["--fitness-win-rate-factor"]
        if args.workers:
            cmd += ["--workers", args.workers]
        print(f"[{i}/{len(jobs)}] RUN  {name} ...", flush=True)
        rc = subprocess.run(cmd, env=os.environ.copy()).returncode
        print(f"[{i}/{len(jobs)}] {name} exit={rc}", flush=True)
    print("options matrix driver: done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
