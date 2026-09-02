#!/usr/bin/env python
"""Autonomous driver for the CONVEX-HARVEST GRID -- the separate, single-key matrix that scores
`O_CONVEX` under `option_convex` and NEVER under grid 2's `option_car`.

Design: docs/superpowers/specs/2026-08-31-convex-harvest-grid-design.md (Sections 2, 4, 5, 6, 8).
Plan:   docs/superpowers/plans/2026-08-31-options-grid2-convex-earnings-impl.md (Task 13).

Runs `ba2-test optimize --strategy O_CONVEX` SEQUENTIALLY (one job at a time), one job per
expert (design Section 5: "Jobs: O_CONVEX x stage-1's experts, singles."):

    O_CONVEX   cheap far-OTM long-dated calls (+ a put toggle) -- convexity harvesting

THIS IS A SEPARATE DRIVER FROM ``run_options2_matrix.py``, NOT A FLAG ON IT, and deliberately
so (design Section 8): the convex-harvest grid scores an OVERLAPPING structure family under a
DIFFERENT fitness (`option_convex` vs `option_car`) and a DIFFERENT universe threshold (DTE
>= 270 vs 365/180/7). Sharing a driver -- or a matrix row -- would invite comparing fitness
numbers across metrics, which is exactly the trap the 2026-08-04 CAR-scale change taught us
never to allow. The launcher's own `_refuse_convex_fitness_mismatch` (plan Task 13) is the
second, independent line of defence: even a hand-typed `ba2-test optimize` command that mixes
`--strategy O_CONVEX` with any other `--fitness` (or vice versa) is refused at launch, not
just discouraged here.

PREFLIGHT IS THE POINT OF THIS DRIVER (design Section 4/5), same discipline as grid 2's:
`tools/probe_option_chain_depth.py` measures whether each universe symbol's parquet tree
carries an expiry with bars at DTE >= 270 over the run window -- broader than grid 2's LEAPS
threshold (365) because O_CONVEX's entry band is 180-540 DTE, not January-cycle-only. A
threshold that keeps NOTHING fails the whole run rather than launching a job with an empty
universe.

Jobs are named `convex-<expert>-O_CONVEX[suffix]` and are IDEMPOTENT/RESUMABLE: a job whose
StrategyOptimization row is already `completed` is skipped, so the driver can be killed and
re-run to continue.

Prereqs: the TastyTrade options PARQUET tree must cover the window (`--options-store parquet`
is forced below, same reasoning as grid 2's driver), plus the usual OHLCV/FMP prewarm for the
expert signals.

Usage (test venv; FMP_API_KEY/DB_FILE in env):
    ba2-venvs/test/Scripts/python.exe tools/run_convex_matrix.py \
        [--experts FMPRating,DeterministicScorer] \
        [--start 2023-01-01] [--end 2025-12-31] \
        [--population 40] [--generations 6] [--dry-run]

Environment overrides (so a longer/shorter search does not need a code edit):
    BA2_CONVEX_POPULATION, BA2_CONVEX_GENERATIONS
"""
import argparse
import os
import subprocess
import sys

# Sibling helper in tools/ (shared by all matrix drivers). The directory is put on the path
# explicitly so the import works however the script is reached (path, -m, or a test import).
_TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)
from matrix_flags import cap_passthrough  # noqa: E402

_UNIVERSE_FILE = os.path.join(_TOOLS_DIR, "options_universe_top100.txt")
_PROBE = os.path.join(_TOOLS_DIR, "probe_option_chain_depth.py")

# The convex-harvest grid's key set (design Section 2/5) -- exactly one key. Kept as a LIST
# (not the launcher's ``_CONVEX_OPTION_STRATEGIES`` set directly) for the same reason grid 2's
# driver keeps its own ``_DEFAULT_STRATEGIES``: a driver-level constant that a test can pin
# independently of the launcher's internal set shape, while still being checked AGAINST it
# (see test_convex_matrix_script.py).
_DEFAULT_STRATEGIES = ["O_CONVEX"]

# The CHAIN-DEPTH O_CONVEX needs, in DTE (design Section 4: "keep a stage-1-universe symbol
# iff the cache carries expiries with bars at DTE >= 270 in-window -- regular monthlies
# qualify; January-only names too"). Broader than grid 2's LEAPS threshold (365) because
# O_CONVEX's entry band starts at 180 DTE, not a January-cycle-only 365+. Keyed by strategy,
# mirroring run_options2_matrix.py's ``_MIN_DTE``, so a second convex-grid key added later
# cannot run unfiltered by omission.
_MIN_DTE = {
    "O_CONVEX": 270,
}

# STAGE-1'S EXPERTS (design Section 5: "O_CONVEX x stage-1's experts, singles. 2-3 jobs.").
# FMPRating is the grid-1/grid-2 default and the only expert with a measured large-cap signal
# on this options universe -- the same precedent run_options2_matrix.py's own
# ``_DEFAULT_EXPERTS`` set for the screener-style grid-2 keys. DeterministicScorer joins it
# (review addition, 2026-09-02): it has MEASURED large-cap results in the matrix3 grid, so it
# clears the same "measured, not just registered" bar FMPRating does, and TWO experts x ONE
# strategy key lands the driver's default at 2 jobs -- inside the design's own 2-3 range.
# ``--experts`` is a real override either way.
_DEFAULT_EXPERTS = ["FMPRating", "DeterministicScorer"]

# Options need ~2x the equity balance headroom -- $20k, the same figure grid 1 and grid 2 run
# at, so results across all three grids are read against the same account size.
_DEFAULT_CAPITAL = 20000.0

# POP 40 / GEN 6, modest BY DESIGN (design Section 5): the sample supports "does any region
# work", not fine-tuning. Env-overridable so a follow-up deeper search needs no code edit.
_DEFAULT_POPULATION = int(os.getenv("BA2_CONVEX_POPULATION", "40"))
_DEFAULT_GENERATIONS = int(os.getenv("BA2_CONVEX_GENERATIONS", "6"))

# Fitness: option_convex, and ONLY option_convex, for every job this driver launches (design
# Section 3/8). Passed explicitly rather than left to the launcher's auto-resolution -- the
# launcher never defaults ANY kind to option_convex (``_resolve_fitness``'s own comment) -- and
# the launcher's ``_refuse_convex_fitness_mismatch`` (plan Task 13) refuses the job outright if
# this is ever changed to anything else while --strategy stays O_CONVEX, or vice versa.
_FITNESS = "option_convex"


def _universe():
    with open(_UNIVERSE_FILE, encoding="utf-8") as f:
        return [s.strip() for s in f.read().split() if s.strip()]


def _db_path() -> str:
    return os.getenv("DB_FILE", r"C:\Users\basti\Documents\ba2\test\dl_forecasting.db")


def _completed_names() -> set:
    """Names already ``completed`` in ``strategy_optimizations`` -- read fresh on every call
    (called once per loop iteration in ``main``) so the driver is resumable if killed and
    re-run mid-matrix.

    Catches ONLY the sqlite exceptions a missing/locked/pre-migration db can genuinely raise
    (``OperationalError`` -- locked db, no such table on a fresh db, disk I/O; ``DatabaseError``
    -- their common base, corrupt file) and LOGS them at WARNING rather than swallowing them
    silently (review finding, 2026-09-02: a bare ``except Exception: return set()`` makes a
    LOCKED db -- a real, occasionally-hit condition when another process holds the sqlite file
    -- silently read as "nothing completed", so ``main`` re-launches a job that already
    finished, burning a full GA search for a duplicate result nothing ever reports as wrong).
    Anything else (a programming error, an unexpected exception type) is NOT this function's
    business to hide and re-raises.
    """
    import logging
    import sqlite3

    try:
        c = sqlite3.connect(_db_path())
        try:
            rows = c.execute(
                "SELECT name FROM strategy_optimizations WHERE status='completed'").fetchall()
        finally:
            c.close()
        return {r[0] for r in rows}
    except (sqlite3.OperationalError, sqlite3.DatabaseError) as e:
        logging.getLogger(__name__).warning(
            "run_convex_matrix: could not read completed job names from %s (%s: %s) -- "
            "treating as NONE completed. If this is a LOCKED db (another process holding it), "
            "a job that already finished may be RE-RUN.", _db_path(), type(e).__name__, e)
        return set()


def _jobs(strategies, experts, name_suffix=""):
    """Yield (name, expert, strategy) in list order -- strategy-major, mirroring
    run_options2_matrix.py's ``_jobs`` shape (kept even though today's grid has one strategy,
    so a second convex-grid key needs no reshaping)."""
    for s in strategies:
        for expert in experts:
            yield (f"convex-{expert}-{s}{name_suffix}", expert, s)


def _thresholds(strategies) -> dict:
    """{min_dte: [strategy, ...]} for the requested strategies. Raises on an unlisted key."""
    unknown = [s for s in strategies if s not in _MIN_DTE]
    if unknown:
        raise SystemExit(
            f"run_convex_matrix: no chain-depth threshold declared for {unknown}. Every "
            f"convex-grid key must state the DTE depth its universe needs (design Section 4) "
            f"-- add it to _MIN_DTE rather than running the job on an unfiltered universe.")
    out: dict = {}
    for s in strategies:
        out.setdefault(_MIN_DTE[s], []).append(s)
    return out


def _preflight(strategies, symbols, start, end, out_dir, python=None, dry_run=False) -> dict:
    """Run the chain-depth probe once per DISTINCT threshold; return {min_dte: [kept, ...]}.

    Exits non-zero when a threshold keeps NOTHING: a job launched on an empty universe does
    not fail, it completes with a zero-trade sentinel for every trial, and that is
    indistinguishable in the results table from a structure that genuinely does not work.
    """
    python = python or sys.executable
    kept_by_dte: dict = {}
    for min_dte, keys in sorted(_thresholds(strategies).items()):
        out_file = os.path.join(out_dir, f"convex_universe_dte{min_dte}.txt")
        cmd = [python, _PROBE, "--symbols", ",".join(symbols), "--min-dte", str(min_dte),
               "--start", start, "--end", end, "--out", out_file]
        print(f"preflight: DTE>={min_dte} for {','.join(keys)} -> {out_file}", flush=True)
        if dry_run:
            print("           " + " ".join(cmd[:3] + ["<symbols>"] + cmd[4:]))
            kept_by_dte[min_dte] = list(symbols)
            continue
        rc = subprocess.run(cmd, env=os.environ.copy()).returncode
        if rc != 0:
            raise SystemExit(
                f"run_convex_matrix: chain-depth preflight FAILED (exit {rc}) at "
                f"DTE>={min_dte} for {','.join(keys)}. Refusing to launch a job whose "
                f"universe cannot carry the structure -- a no-contract trial scores the "
                f"zero-trade sentinel and reads exactly like a bad strategy.")
        with open(out_file, encoding="utf-8") as f:
            kept_by_dte[min_dte] = [s.strip() for s in f if s.strip()]
        print(f"preflight: DTE>={min_dte} kept {len(kept_by_dte[min_dte])}/{len(symbols)}",
              flush=True)
    return kept_by_dte


def build_cmd(args, launcher, name, expert, strategy, universe) -> list:
    """The full `ba2-test optimize` argv for one job. Split out of the loop so the job-list
    test can assert on it without launching anything."""
    cmd = ([sys.executable, launcher] if launcher.endswith(".py") else [launcher]) + [
        "optimize", "--expert", expert, "--universe", ",".join(universe),
        "--strategy", strategy,
        "--start", args.start, "--end", args.end,
        "--interval", args.interval,
        "--population", str(args.population),
        "--generations", str(args.generations),
        "--initial-capital", str(args.initial_capital),
        # THE PARQUET STORE, ALWAYS -- same reasoning as grid 2's driver: the sqlite/Alpaca
        # store's history floor is 2024-01-18 and cannot serve the design's 2023-01 start.
        "--options-store", args.options_store,
        "--fitness", args.fitness,
        # Daily cadence, matching grid 2's driver: option entries want the day's signal.
        "--run-schedule", "daily", "--name", name, "--parallel", str(args.parallel),
    ]
    if args.early_stop is not None:
        cmd += ["--early-stop", str(args.early_stop)]
    if args.mutation_prob is not None:
        cmd += ["--mutation-prob", str(args.mutation_prob)]
    # "Pass 0 to disable": a 0 must be FORWARDED, or the launcher re-applies its own default.
    cmd += cap_passthrough(args)
    # STRESS-SPREAD-BPS IS DELIBERATELY NOT FORWARDED. main() refuses a non-zero value before
    # this function is ever reached (see _refuse_nonzero_stress below) -- a zero value needs
    # no flag at all (the launcher's own --stress-spread-bps default is 0.0), so there is
    # nothing here to pass on the only value this driver ever allows through.
    if args.screener_gate_store:
        cmd += ["--screener-gate-store", args.screener_gate_store,
                "--max-stock-price", str(args.max_stock_price)]
    if args.workers:
        cmd += ["--workers", args.workers]
    return cmd


def _refuse_nonzero_stress(stress_spread_bps: float) -> None:
    """Design §6 item 4 / strategy_fitness.py's own TASK 14 CARRY note: ``option_convex``
    ranks on RAW ``total_return`` (design §3's "end-of-window total return"), and
    ``stressed_results`` restates ``annualized_return``/``max_drawdown`` but NOT
    ``total_return`` -- so ``_min_with_stressed`` is SKIPPED ENTIRELY for ``option_convex``
    (see strategy_fitness.compute_fitness's ``_CONVEX_ALIASES`` branch). A non-zero
    --stress-spread-bps on a convex job would therefore be accepted, forwarded, and silently
    INERT: the run would look stress-tested and would not be. Refusing here is cheaper than
    debugging a "looks-applied" stress six months from now, and correct until Task 14 fixes
    ``stressed_results`` to restate ``total_return`` (that TASK 14 CARRY note is the same one
    strategy_fitness.py's compute_fitness names).
    """
    if stress_spread_bps:
        raise SystemExit(
            f"run_convex_matrix: --stress-spread-bps {stress_spread_bps} is REFUSED for "
            f"option_convex jobs. option_convex ranks on RAW total_return, and "
            f"stressed_results does not yet restate total_return under stress (only "
            f"annualized_return/max_drawdown) -- see strategy_fitness.py's TASK 14 CARRY note "
            f"on the _CONVEX_ALIASES branch. Passing a non-zero stress here would be silently "
            f"INERT for the return term while looking applied. Drop the flag (or pass 0) "
            f"until Task 14 restates total_return in stressed_results.")


def build_parser() -> argparse.ArgumentParser:
    """The driver's CLI, split out of ``main`` so a test can parse the REAL defaults rather
    than a hand-written stand-in that can drift from them."""
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--strategies", default=",".join(_DEFAULT_STRATEGIES),
                    help="Comma list of convex-grid keys (default O_CONVEX -- the only key "
                         "today).")
    ap.add_argument("--experts", default=",".join(_DEFAULT_EXPERTS),
                    help="Comma list of stage-1 experts (default FMPRating,DeterministicScorer "
                         "-- both have measured large-cap results on this options universe).")
    ap.add_argument("--start", default="2023-01-01",
                    help="Backtest start (design Section 5 window; needs the parquet store).")
    ap.add_argument("--end", default="2025-12-31",
                    help="Backtest end (2026 is the reserved walk-forward holdout and the "
                         "launcher refuses to search into it).")
    ap.add_argument("--population", type=int, default=_DEFAULT_POPULATION,
                    help=f"GA population (default {_DEFAULT_POPULATION}; "
                         f"$BA2_CONVEX_POPULATION overrides).")
    ap.add_argument("--generations", type=int, default=_DEFAULT_GENERATIONS,
                    help=f"GA generations (default {_DEFAULT_GENERATIONS}; "
                         f"$BA2_CONVEX_GENERATIONS overrides).")
    ap.add_argument("--early-stop", type=int, default=None,
                    help="Generations without improvement before a job stops. Omitted -> "
                         "launcher default.")
    ap.add_argument("--mutation-prob", type=float, default=None,
                    help="Per-gene mutation probability passthrough (default: launcher's).")
    ap.add_argument("--interval", default="1d",
                    help="Analysis/fill interval (default 1d -- option cache bars are daily).")
    ap.add_argument("--fitness", default=_FITNESS,
                    help=f"Fitness metric for every job (default {_FITNESS} -- design "
                         f"Section 3/8: the ONLY fitness the convex grid may use; the launcher "
                         f"refuses any other metric for --strategy O_CONVEX).")
    ap.add_argument("--initial-capital", type=float, default=_DEFAULT_CAPITAL,
                    help=f"Starting cash per trial (default {_DEFAULT_CAPITAL:.0f}).")
    ap.add_argument("--options-store", default="parquet",
                    help="Options store serving the run (default parquet -- the only vendor "
                         "whose history floor reaches a 2023 start).")
    ap.add_argument("--universe-file", default=_UNIVERSE_FILE,
                    help="Symbol list probed and then passed to each job.")
    ap.add_argument("--probe-out-dir", default=_TOOLS_DIR,
                    help="Where the preflight writes its per-threshold KEPT lists.")
    ap.add_argument("--skip-preflight", action="store_true",
                    help="Run the FULL universe on every job without probing chain depth. "
                         "Off by design: see this file's PREFLIGHT note.")
    ap.add_argument("--name-suffix", default="",
                    help="Suffix appended to every job name -- re-runs the matrix under "
                         "FRESH names without clobbering prior runs' tagged Backtests.")
    ap.add_argument("--workers", default=None,
                    help="Comma-separated remote worker NAMES to distribute GA trials to.")
    ap.add_argument("--parallel", type=int, default=4,
                    help="Local trial consumers per job (default 4).")
    ap.add_argument("--profit-cap-pct", type=float, default=2000.0,
                    help="Cap each BET's gain at this %% of the capital deployed in it for "
                         "the ADJUSTED fitness. Pass 0 to disable. NOTE: option_convex ranks "
                         "on RAW total_return and does not read the adjusted variant -- this "
                         "cap is recorded on the results and reported, never scored, for a "
                         "convex job (results.py keeps raw total_return untouched); left at "
                         "its grid-1/grid-2 default so the recorded number is comparable.")
    ap.add_argument("--profit-share-cap-pct", type=float, default=25.0,
                    help="Cap each BET's share of the run's net profit. Pass 0 to disable. "
                         "Same non-scoring note as --profit-cap-pct above.")
    ap.add_argument("--stress-spread-bps", type=float, default=0.0,
                    help="REFUSED if non-zero (design Section 6 item 4): option_convex ranks "
                         "on raw total_return, which stressed_results does not yet restate "
                         "under stress, so a non-zero value would be silently inert. Task 14 "
                         "carry; see strategy_fitness.py's TASK 14 CARRY note.")
    ap.add_argument("--screener-gate-store", default=None,
                    help="Attach this parquet metric store as a GATE-ONLY per-bar entry gate.")
    ap.add_argument("--max-stock-price", type=float, default=100.0,
                    help="Max underlying price for the gate-only entry gate. 0 disables it.")
    ap.add_argument("--launcher", default=None,
                    help="Path to the launcher executable (or ba2test_launcher.py). Point "
                         "this at a WORKTREE launcher to run code different from the "
                         "editable install.")
    ap.add_argument("--dry-run", action="store_true")
    return ap


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    _refuse_nonzero_stress(args.stress_spread_bps)

    experts = [e.strip() for e in args.experts.split(",") if e.strip()]
    strategies = [s.strip() for s in args.strategies.split(",") if s.strip()]
    with open(args.universe_file, encoding="utf-8") as f:
        symbols = [s.strip() for s in f.read().split() if s.strip()]

    launcher = args.launcher
    if not launcher:
        launcher = os.path.join(os.path.dirname(sys.executable), "ba2-test.exe")
        if not os.path.exists(launcher):
            launcher = os.path.join(os.path.dirname(sys.executable), "ba2-test")

    jobs = list(_jobs(strategies, experts, args.name_suffix))
    done = _completed_names()
    print(f"convex-harvest grid: {len(jobs)} jobs (strategies={strategies}, experts={experts}, "
          f"fitness={args.fitness}, universe={len(symbols)} symbols); "
          f"{sum(1 for j in jobs if j[0] in done)} already completed.")

    if args.skip_preflight:
        print("convex-harvest grid: PREFLIGHT SKIPPED -- every job runs the full universe. "
              "Names that cannot reach the key's DTE depth will trade nothing and score the "
              "zero-trade sentinel; read the results accordingly.")
        kept_by_dte = {dte: symbols for dte in _thresholds(strategies)}
    else:
        kept_by_dte = _preflight(strategies, symbols, args.start, args.end,
                                 args.probe_out_dir, dry_run=args.dry_run)

    if args.dry_run:
        # UNFILTERED, LABELLED (review finding, 2026-09-02): --dry-run makes _preflight
        # (and --skip-preflight) short-circuit to the RAW universe -- neither the real probe
        # subprocess nor a file read ever runs -- so the symbol count below is the input
        # list's size, not a DTE>=270-filtered one. Printing it unlabelled reads as "this many
        # symbols passed the chain-depth probe", which is false for every --dry-run job line.
        for nm, exp, s in jobs:
            uni = kept_by_dte[_MIN_DTE[s]]
            print(f"  {'DONE' if nm in done else 'TODO'}  {nm}  ({exp} {s}, "
                  f"DTE>={_MIN_DTE[s]}, {len(uni)} symbols (unfiltered, dry-run))")
        return 0

    for i, (name, expert, strat) in enumerate(jobs, 1):
        if name in _completed_names():   # re-read each loop (resumable)
            print(f"[{i}/{len(jobs)}] SKIP {name} (already completed)", flush=True)
            continue
        cmd = build_cmd(args, launcher, name, expert, strat, kept_by_dte[_MIN_DTE[strat]])
        print(f"[{i}/{len(jobs)}] RUN  {name} ...", flush=True)
        rc = subprocess.run(cmd, env=os.environ.copy()).returncode
        print(f"[{i}/{len(jobs)}] {name} exit={rc}", flush=True)
    print("convex-harvest grid driver: done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
