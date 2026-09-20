"""Autonomous driver for the OPTIONS strategy optimization matrix.

Stage 1: --profile discovery selects 16 permitted singles x FMPRating/DeterministicScorer,
population 200, generations 60 and patience 8. It requires the gate store with blanket
--max-stock-price 0 so the strategy-specific caps apply. --dry-run prints the actual commands.
Discovery names include a configuration digest; change --name-suffix after code/cache-content
changes at unchanged paths. This preserves older results and checkpoints as separate experiments.
The default matrix profile retains the existing family-search interface.

OPERATOR CHECKLIST BEFORE LAUNCHING ANY OPTION GRID (2026-09-03, options-grid2 closeout):
  1. Window: retarget --start to 2020-01-01 on the ThetaData store (the TastyTrade parquet is a
     bull-only 2024-02+ window). Provider PARITY first: the parquet store layout, the per-worker
     chain cache (_WORKER_RAW_CACHE), the BS/greeks fallback and tools/probe_option_chain_depth.py
     must behave identically on ThetaData partitions -- pin it the way BT/live parity is pinned.
  2. Option-cache cost before a 2020 window: measured 2026-09-02 (GA probe, opt 429) ~3.6 s and
     ~22 MB per symbol cold on the 2024+ store, ~x3 at 2020 (~7 GB of cached chains per worker for
     105 symbols). Do the optimization first: one parquet per symbol (184 per-expiry files today)
     and a higher _MAX_TASKS_PER_CHILD for option jobs; size local slots from the measured MB/symbol.
  3. Results baselines are SPLIT (docs results-comparability note): pre-Task-3 numbers (BS mark
     fallback), pre-683c7379 stress restatement (return/total_return/calmar fitness), and the O_CC /
     O_WHEEL rulesets after cc_dte + wheel_stock_guard -- never compare across them.
  4. Never merge into the checkout a running grid imports from mid-job: long-lived masters lazily
     import new modules against old enums at their persist phase (AttributeError 2026-09-03). Merge
     at a job boundary (stop the wrapper shells only, let the master finish, merge + ONE
     TEST_APP_VERSION bump, relaunch from GIT BASH).
  5. Equity grids: BT_BAR_CACHE_TRIALS=0 re-preloads bars before EVERY individual (matrix3 paid
     ~8 h); evaluate a non-zero value for the next launch (memory: the union of per-individual
     symbol sets is retained until recycle).

Runs `ba2-test optimize --strategy <OS?/O_?>` SEQUENTIALLY (one job at a time) over the
option-strategy matrix on the top-100 large-cap universe covered by the offline options
cache (see tools/options_universe_top100.txt). The list does not prove time/contract coverage;
validate that independently on the machine serving the run:

  per expert, in order:
    OS1   grouped: directional DEBIT  (O_LC long call, O_LP long put, O_VERT bear put
          vertical, O_BF call butterfly) — one job, the GA toggles members on/off so the
          persisted top-5 can differ in STRUCTURE, not just parameters
    OS2   grouped: neutral CREDIT     (O_SSTG short strangle, O_SSTD short straddle,
          O_IC iron condor)
    OS3   grouped: skewed CREDIT      (O_JL jade lizard, O_RS put ratio spread)
    O_CC  covered call (equity entry + call overlay — different entry path, own job)
    O_STK plain equity baseline (control)

Matrix experts: FMPRating only by default. FMPEarningsDrift/FMPInsiderClusterBuy are EXCLUDED —
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
from datetime import date
import hashlib
import json
import math
import os
from pathlib import Path
import shlex
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

# Discovery keeps every structure in its own job. Family composition is a separate
# experiment; its larger genome cannot answer the per-structure knowledge question.
_DISCOVERY_STRATEGIES = [
    "O_LC", "O_LP", "O_VERT", "O_BULLCS", "O_BULLPS", "O_BEARCS", "O_BF",
    "O_IC", "O_JL", "O_RS", "O_CSP", "O_STRD", "O_STRG",
    "O_CC", "O_PP", "O_WHEEL",
]
# The 2026-08-31 risk decision supersedes the original 18-structure design.
# Both singles are refused by the launcher; this is not a performance survival gate.
_DISCOVERY_EXCLUDED = {"O_SSTG", "O_SSTD"}
_DISCOVERY_EXPERTS = ["FMPRating", "DeterministicScorer"]


def _universe(path=_UNIVERSE_FILE) -> str:
    with open(path, encoding="utf-8") as f:
        syms = list(dict.fromkeys(s.upper() for s in f.read().split()))
    if not syms:
        raise ValueError(f"Empty options universe: {path}")
    return ",".join(syms)


def _db_path() -> str:
    return os.getenv("DB_FILE", r"C:\Users\basti\Documents\ba2\test\dl_forecasting.db")


def _completed_names() -> set:
    import sqlite3
    path = Path(_db_path()).resolve()
    if not path.exists():
        return set()  # A new instance is allowed, but a dry-run must not create its DB.
    c = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    try:
        rows = c.execute(
            "SELECT name FROM strategy_optimizations WHERE status='completed'").fetchall()
    finally:
        c.close()
    return {r[0] for r in rows}


def _jobs(experts, strategies, name_suffix=""):
    """Yield (name, expert, strategy) in priority order (per expert: groups then equity)."""
    for expert in experts:
        for s in strategies:
            yield (f"optm-{expert}-{s}{name_suffix}", expert, s)


def _market_condition_passthrough(args) -> list:
    """Extra optimize CLI tokens for the market-condition profile ([] when it is ``none``).

    NOTE that a manifest without a profile never reaches here: dropping it silently is exactly
    the fault review 2026-09-16 (F2) found, so ``resolve_args`` refuses that combination first.

    Both tokens are EXPLICIT per job for the same reason the options store is (see build_cmd): a
    distributed trial carries {config, fitness_metric, cache_root, inmem_trades} and no
    environment, so a profile or a manifest chosen through the environment is a decision the
    master made that the worker cannot see.
    """
    profile = getattr(args, "market_condition_profile", None) or "none"
    if profile == "none":
        # A manifest here is refused in resolve_args, before any command or name is built (F2).
        return []
    out = ["--market-condition-profile", profile]
    if getattr(args, "market_condition_manifest", None):
        out += ["--market-condition-manifest", args.market_condition_manifest]
    return out


def _gate_passthrough(args) -> list:
    """Extra optimize CLI tokens for the gate-only screener entry gate ([] when unset)."""
    if not args.screener_gate_store:
        return []
    return ["--screener-gate-store", args.screener_gate_store,
            "--max-stock-price", str(args.max_stock_price)]


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--profile", choices=["matrix", "discovery"], default="matrix",
                    help="Discovery: 16 permitted singles x Rating/DS, pop 200, gen 60, patience 8. "
                         "Matrix retains the existing grouped defaults. Explicit knobs override.")
    ap.add_argument("--experts", default=None,
                    help="Comma list of experts (default FMPRating; EarningsDrift/Insider "
                         "excluded — no large-cap signal on this options universe).")
    ap.add_argument("--strategies", default=None,
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
    ap.add_argument("--start", default="2020-01-01",
                    help="Backtest start (default 2020-01-01, the goal2020 window; requires the "
                         "options cache -- and the serving vendor's history floor -- to reach "
                         "that far back, which is why the store below defaults to thetadata).")
    ap.add_argument("--options-store", default="thetadata",
                    help="Options store serving the run, forwarded to the launcher per job "
                         "(default thetadata -- floor 2018-09-14, the only vendor reaching a "
                         "2020 start; tastytrade floors at 2022-10-01, alpaca at 2024-01-18).")
    ap.add_argument("--end", default="2025-12-31",
                    help="Backtest end (default 2025-12-31: 2026 is the reserved "
                         "walk-forward holdout and the launcher refuses to search into it).")
    ap.add_argument("--population", type=int, default=None)
    ap.add_argument("--generations", type=int, default=None)
    ap.add_argument("--early-stop", type=int, default=None,
                    help="Generations without improvement before a job stops (spec stage 1: 8). "
                         "Omitted -> launcher default.")
    ap.add_argument("--mutation-prob", type=float, default=None,
                    help="Per-gene mutation probability passthrough (default: launcher's).")
    ap.add_argument("--seed", type=int, default=42,
                    help="Random seed, included in discovery job identity; vary for stability pilots.")
    ap.add_argument("--elitism-percent", type=float, default=10.0)
    ap.add_argument("--interval", default="1d",
                    help="Analysis/fill interval (default 1d — option cache bars are daily).")
    ap.add_argument("--fitness", default=None,
                    help="Fitness metric forced on EVERY job. Default: omitted, so each job "
                         "gets ba2test_launcher's per-strategy-kind auto-resolution "
                         "(option_consistent_annual_return for pure-option kinds OS1-4/O_* "
                         "AND the equity-entry overlays O_CC/O_PP, sharpe_ratio for O_STK -- "
                         "see _resolve_fitness/_OPTION_CAR_STRATEGIES) -- passing this flag "
                         "here overrides that auto-resolution uniformly for the whole matrix. "
                         "'option_car_over_risk' is the second option objective: ~50%%/yr WITH "
                         "a drawdown tolerance (full credit to 40%% dd), which is what to pass "
                         "when the auto-resolved option_consistent_annual_return's 16x "
                         "small-drawdown reward is producing low-return grinders. "
                         "'option_car_target' is the third: CAR > 35%%/yr AND CAR > drawdown, "
                         "as two soft ramps that stop paying at their targets, plus the same "
                         "(40/dd)^1.5 penalty past 40%% dd -- the only one of the three that "
                         "prices the CAR/DD ratio (option_car_over_risk divides by sqrt(dd), "
                         "so it scores a 40%%/40%% genome and a 20%%/10%% one identically). "
                         "'option_car_target_soft30' keeps those targets but replaces the "
                         "annual trade floor/ramp with min(completed structures / 30, 1) "
                         "over the whole backtest; a positive count is penalised, not discarded. "
                         "These objectives are not comparable -- a matrix run under one never "
                         "shares a table with a matrix run under another.")
    ap.add_argument("--initial-capital", type=float, default=_DEFAULT_CAPITAL,
                    help=f"Starting cash per trial (default {_DEFAULT_CAPITAL:.0f} — options "
                         "need more headroom than the equity grid's 10k).")
    ap.add_argument("--equity-cap", type=float, default=None,
                    help="Optional fixed sizing ceiling; omitted preserves compounding. "
                         "Recorded in discovery identity; different capital policies are separate runs.")
    ap.add_argument("--universe-file", default=_UNIVERSE_FILE,
                    help="Whitespace-separated offline-cache universe, preserving symbol order.")
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
    ap.add_argument("--robust-fitness", dest="robust_fitness", action="store_true", default=True,
                    help="Rank on the ROBUSTNESS-ADJUSTED fitness (concentration x monte-carlo x "
                         "spread). ON BY DEFAULT since 2026-09-17 -- this driver passes NO flag "
                         "and every job inherits `ba2-test optimize`'s default -- so the flag is "
                         "only an explicit restatement of it.")
    ap.add_argument("--no-robust-fitness", dest="robust_fitness", action="store_false",
                    help="Rank every job on the RAW metric instead (the pre-2026-09-17 default). "
                         "Forwarded to every `optimize` call AND folded into the discovery "
                         "identity digest, so a raw-ranked run gets its own job names and can "
                         "never resume a robustness-ranked checkpoint (the backend refuses that "
                         "outright). Scores are NOT comparable across this setting.")
    ap.add_argument("--launcher", default=None,
                    help="Path to the launcher executable (or ba2test_launcher.py). Default: "
                         "the ba2-test installed next to the Python interpreter. Point this at "
                         "a WORKTREE launcher to run code different from the editable install.")
    ap.add_argument("--market-condition-profile", default="none",
                    metavar="none|<profile>[,<profile>...]",
                    help="Forward --market-condition-profile to every job: append the registered "
                         "profile(s)' market-condition gates (mode + threshold genes) to each "
                         "structure's INITIAL-ENTRY tree. Default 'none' = today's rules and "
                         "genes. The flag folds into the discovery identity digest, so a gated "
                         "run gets its own job names and never resumes an ungated checkpoint.")
    ap.add_argument("--market-condition-manifest", default=None,
                    metavar="DIGEST[,DIGEST...]|<profile>=DIGEST,...",
                    help="The prepared snapshot digest every trial reads, ONE PER PROFILE "
                         "(required by the launcher whenever a profile is on; "
                         "tools/warm_market_conditions.py build --print-digest prints each). "
                         "Bare digests are matched to the profiles in order; profile=digest "
                         "pairs are explicit. Part of the identity digest too: a different "
                         "snapshot is a different experiment, not a resume.")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--screener-gate-store", default=None,
                    help="Attach this parquet metric store as a GATE-ONLY per-bar entry gate on "
                         "EVERY job (passes --screener-gate-store/--max-stock-price through to "
                         "ba2-test optimize). The store must cover the options universe.")
    ap.add_argument("--max-stock-price", type=float, default=100.0,
                    help="Max underlying price for the gate-only entry gate (default 100 — the "
                         "$20k-account cap). 0 disables the price filter.")
    return ap


def resolve_args(ap, argv=None):
    args = ap.parse_args(argv)
    discovery = args.profile == "discovery"
    if args.experts is None:
        args.experts = ",".join(_DISCOVERY_EXPERTS if discovery else _DEFAULT_EXPERTS)
    if args.strategies is None:
        args.strategies = ",".join(_DISCOVERY_STRATEGIES if discovery else _DEFAULT_STRATEGIES)
    if args.population is None:
        args.population = 200 if discovery else 40
    if args.generations is None:
        args.generations = 60 if discovery else 8
    if discovery:
        if args.early_stop is None:
            args.early_stop = 8
        if args.mutation_prob is None:
            args.mutation_prob = 0.3
        if args.fitness is None:
            args.fitness = "option_consistent_annual_return"
    for label in ("experts", "strategies"):
        values = [v.strip() for v in getattr(args, label).split(",") if v.strip()]
        if not values or len(values) != len(set(values)):
            ap.error(f"--{label} must be a non-empty list without duplicates")
        setattr(args, label, ",".join(values))
    if discovery:
        if set(args.strategies.split(",")) & _DISCOVERY_EXCLUDED:
            ap.error("O_SSTG/O_SSTD are excluded by the existing unbounded-risk policy; "
                     "discovery does not override that policy")
        if set(args.strategies.split(",")) - set(_DISCOVERY_STRATEGIES):
            ap.error("Discovery requires stage-1 single structures; groups/baselines belong in matrix mode")
        if set(args.experts.split(",")) - set(_DISCOVERY_EXPERTS):
            ap.error("Discovery is defined for FMPRating and DeterministicScorer")
        if not args.screener_gate_store or args.max_stock_price != 0:
            ap.error("Discovery requires --screener-gate-store and --max-stock-price 0 "
                     "to retain the launcher's per-structure affordability caps")
    # A MANIFEST WITHOUT A PROFILE IS A REFUSAL, NOT AN OMISSION (review 2026-09-16, F2).
    # _market_condition_passthrough used to return [] for it, so the launcher never saw the
    # manifest and its own manifest-without-profile refusal could not fire. The job then ran
    # UNGATED under a discovery name identical to the ordinary ungated job -- so it could also be
    # skipped against an existing ungated completion, and every listing afterwards would read as
    # though a snapshot had been pinned. Refused HERE, in resolve_args, so it holds for
    # build_cmd, discovery_name and --dry-run alike.
    manifest = (getattr(args, "market_condition_manifest", None) or "").strip()
    if manifest and (args.market_condition_profile or "none") == "none":
        ap.error("--market-condition-manifest was given without --market-condition-profile: "
                 "nothing would read that snapshot and the jobs would run UNGATED under the "
                 "ungated job names. Pass --market-condition-profile, or drop the manifest.")
    try:
        start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)
    except ValueError:
        ap.error("--start and --end must be ISO dates (YYYY-MM-DD)")
    if start > end:
        ap.error("--start must not be after --end")
    # Scope to this option-grid driver, including the equity-entry overlays/control.
    # Do not extend the backend's pure-option rail into unrelated equity optimizations.
    if end >= date(2026, 1, 1):
        ap.error("Option-grid search must end before the reserved 2026 holdout")
    for key in ("population", "generations", "parallel", "initial_capital",
                "early_stop", "equity_cap", "fitness_trade_scale_cap", "fitness_trade_scale_target"):
        value = getattr(args, key)
        if value is not None and (not math.isfinite(value) or value <= 0):
            ap.error(f"--{key.replace('_', '-')} must be finite and positive")
    for key in ("profit_cap_pct", "profit_share_cap_pct", "max_stock_price"):
        value = getattr(args, key)
        if not math.isfinite(value) or value < 0:
            ap.error(f"--{key.replace('_', '-')} must be finite and non-negative")
    if not math.isfinite(args.elitism_percent) or not 0 <= args.elitism_percent <= 100:
        ap.error("--elitism-percent must be in [0, 100]")
    if args.mutation_prob is not None and (not math.isfinite(args.mutation_prob)
                                          or not 0 <= args.mutation_prob <= 1):
        ap.error("--mutation-prob must be in [0, 1]")
    return args


def build_cmd(args, launcher, name, expert, strat, universe):
    cmd = ([sys.executable, launcher] if launcher.endswith(".py") else [launcher]) + [
        "optimize", "--expert", expert, "--universe", universe, "--strategy", strat,
        "--start", args.start, "--end", args.end,
        "--interval", args.interval, "--population", str(args.population),
        "--generations", str(args.generations), "--initial-capital", str(args.initial_capital),
        "--run-schedule", "daily", "--name", name, "--parallel", str(args.parallel),
        "--seed", str(args.seed), "--elitism-percent", str(args.elitism_percent),
        # EXPLICIT per job, never left to BACKTEST_OPTIONS_STORE. A distributed trial ships only
        # {config, fitness_metric, cache_root, inmem_trades} -- no environment goes with it -- so
        # a store chosen via env is a decision the master made that the worker cannot see, and
        # the worker would silently re-resolve to the sqlite default. That is how a whole grid
        # once scored against the wrong vendor's history while every log said otherwise.
        "--options-store", args.options_store]
    cmd += _gate_passthrough(args)
    cmd += _market_condition_passthrough(args)
    for field, flag in (("fitness", "--fitness"), ("early_stop", "--early-stop"),
                        ("mutation_prob", "--mutation-prob"), ("equity_cap", "--equity-cap")):
        value = getattr(args, field)
        if value is not None:
            cmd += [flag, str(value)]
    cmd += cap_passthrough(args)
    if args.fitness_trade_scale:
        cmd += ["--fitness-trade-scale", "--fitness-trade-scale-cap", str(args.fitness_trade_scale_cap),
                "--fitness-trade-scale-target", str(args.fitness_trade_scale_target)]
    if args.fitness_win_rate_factor:
        cmd += ["--fitness-win-rate-factor"]
    # Robustness is DEFAULT-ON in the launcher, so the ON case passes nothing (every existing
    # job name is unchanged) and only the opt-OUT is forwarded -- and it is a digest token, so a
    # raw-ranked run cannot share a name, and therefore a checkpoint, with a robust one.
    if not args.robust_fitness:
        cmd += ["--no-robust-fitness"]
    if args.workers:
        cmd += ["--workers", args.workers]
    return cmd


def discovery_name(args, launcher, name, expert, strat, universe):
    """Version the experiment, not the machine's consumer count or selected job subset.

    Name is also the backend checkpoint key. Changed window/seed/capital/GA knobs
    must neither skip an older completion nor resume its incompatible experiment.
    Cache contents/code changes at the same paths still require a fresh name suffix.
    """
    cmd = build_cmd(args, launcher, "", expert, strat, universe)
    tokens = cmd[cmd.index("optimize") + 1:]
    config = {}
    i = 0
    while i < len(tokens):
        flag = tokens[i]
        if flag in ("--fitness-trade-scale", "--fitness-win-rate-factor",
                    "--no-robust-fitness"):
            config[flag] = True
            i += 1
        else:
            if flag not in ("--name", "--parallel", "--workers"):
                config[flag] = tokens[i + 1]
            i += 2
    identity = {"schema": 1, "args": config, "launcher": os.path.abspath(launcher),
                "store": {key: os.environ.get(key) for key in (
                    "BA2_HOME", "BACKTEST_OPTIONS_STORE", "BACKTEST_OPTIONS_PARQUET_ROOT",
                    "BACKTEST_OPTIONS_RISK_FREE_RATE", "TASTYTRADE_OPTIONS_HISTORY_FLOOR")}}
    digest = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:12]
    return f"{name}-d{digest}"


def main(argv=None) -> int:
    ap = build_parser()
    args = resolve_args(ap, argv)

    experts = [e.strip() for e in args.experts.split(",") if e.strip()]
    strategies = [s.strip() for s in args.strategies.split(",") if s.strip()]
    universe = _universe(args.universe_file)

    launcher = args.launcher
    if not launcher:
        launcher = os.path.join(os.path.dirname(sys.executable), "ba2-test.exe")
        if not os.path.exists(launcher):
            launcher = os.path.join(os.path.dirname(sys.executable), "ba2-test")
    jobs = list(_jobs(experts, strategies, args.name_suffix))
    if args.profile == "discovery":
        jobs = [(discovery_name(args, launcher, nm, exp, strat, universe), exp, strat)
                for nm, exp, strat in jobs]
        print("DISCOVERY: permitted singles only; all remain eligible for composition. "
              "Stage-1 rankings provide seeds, not a survival gate.")
        print("Risk-policy exclusions: O_SSTG, O_SSTD (2026-08-31; not performance exclusions).")
        print(f"Search budget: population={args.population}, generations={args.generations}, "
              f"patience={args.early_stop}, seed={args.seed}; "
              f"capital={args.initial_capital}, equity_cap={args.equity_cap}")
        if args.population < 200:
            print("PILOT BUDGET: population below the documented 200; precision equivalence is unmeasured.")
        if args.start > "2020-01-01":
            print("LIMITED WINDOW: this does not satisfy the later goal2020 specification. "
                  "2020 requires ThetaData reader/vendor parity and verified cache coverage.")
    done = _completed_names()
    print(f"options matrix: {len(jobs)} jobs (experts={experts}, strategies={strategies}, "
          f"universe={len(universe.split(','))} symbols); "
          f"{sum(1 for j in jobs if j[0] in done)} already completed.")
    # The OBJECTIVE, in full and unconditionally. The robustness adjustment rescales the metric,
    # so a launch line naming only the metric states half of what the search is ranked on -- which
    # is how the first gated stage-1 run spent its whole life ranking raw with nothing saying so.
    print(f"Objective: fitness={args.fitness or 'per-job default'}, robust_fitness="
          f"{'ON (launcher default)' if args.robust_fitness else 'OFF (--no-robust-fitness)'}"
          " -- scores are NOT comparable across the robustness setting.")
    if args.dry_run:
        for nm, exp, s in jobs:
            print(f"  {'DONE' if nm in done else 'TODO'}  {nm}  ({exp} {s})")
            print("    " + shlex.join(build_cmd(args, launcher, nm, exp, s, universe)))
        print("Dry-run only: cache coverage and vendor compatibility have NOT been validated.")
        return 0

    if args.screener_gate_store and not Path(args.screener_gate_store).exists():
        ap.error(f"Screener gate store does not exist: {args.screener_gate_store}")

    for i, (name, expert, strat) in enumerate(jobs, 1):
        if name in _completed_names():   # re-read each loop (resumable)
            print(f"[{i}/{len(jobs)}] SKIP {name} (already completed)", flush=True)
            continue
        cmd = build_cmd(args, launcher, name, expert, strat, universe)
        print(f"[{i}/{len(jobs)}] RUN  {name} ...", flush=True)
        rc = subprocess.run(cmd, env=os.environ.copy()).returncode
        print(f"[{i}/{len(jobs)}] {name} exit={rc}", flush=True)
        if rc != 0:
            print(f"options matrix stopped: {name} failed; remaining jobs were not launched.", flush=True)
            return rc if rc > 0 else 1
    print("options matrix driver: done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
