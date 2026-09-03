#!/usr/bin/env python
"""Autonomous driver for OPTIONS GRID 2 -- the multi-structure option matrix.

OPERATOR CHECKLIST BEFORE LAUNCHING (2026-09-03, options-grid2 closeout) -- the full list lives
at the top of tools/run_options_matrix.py; the items that gate THIS launcher:
  1. Retarget the window to 2020-01-01 on the ThetaData store, after provider-parity pins
     (store layout, per-worker chain cache, BS/greeks fallback, the depth probe).
  2. Option-cache optimization first: ~3.6 s / ~22 MB per symbol cold on the 2024+ store, ~x3 at
     2020 -> one parquet per symbol + a higher _MAX_TASKS_PER_CHILD for option jobs; size local
     slots from the measured MB/symbol.
  3. Baselines are split (BS mark fallback; the 683c7379 stress restatement; O_CC/O_WHEEL after
     cc_dte + wheel_stock_guard) -- never compare across them.
  4. Merge only at a grid job boundary (see run_options_matrix.py item 4).

Design: docs/superpowers/specs/2026-08-31-leaps-grid-design.md (Sections 2, 5, 7).
Plan:   docs/superpowers/plans/2026-08-31-options-grid2-convex-earnings-impl.md (Task 10).

Runs `ba2-test optimize --strategy <O_?>` SEQUENTIALLY (one job at a time) over grid 2's
PHASE-1 strategy keys -- one job per (strategy x expert), so every result is attributable to
ONE structure (design Section 7):

    O_LEAP    LEAPS long -- ONE signal-driven key: a bullish buy_call arm (O_LEAPC) and a
              bearish buy_put arm (O_LEAPP), delta 0.70-0.90, entry DTE 365-550, sharing one
              exit ruleset. Operator decision 2026-09-02, superseding the two separate keys:
              the two arms are the same structure pointed either way, and the group shape gives
              the GA a per-arm on/off gene so it can drop a direction in a one-sided regime.
    O_PMCC    poor man's covered call -- a LEAPS call carrying a rolling short-call
              overlay sold above its strike. TWO EXPIRIES, one structure; the overlay is
              bought back and re-sold as it nears its own expiry.
    O_ERN     earnings long vol -- straddle | strangle before the print (event-driven)
    O_CBS     call backspread   -- 1x2, convexity financed by the short
    O_PBS     put backspread    -- the crash-hedge arm

`O_CAL` (the ATM calendar) is still PHASE-GATED -- design Section 2 holds it behind PMCC
proving the two-expiry lifecycle in a real run -- so it is not in the default list. Naming
it refuses loudly at the launcher, so this driver does not need to police it. `O_PMCC`
JOINED phase 1 on 2026-09-02 (plan Task 6) once that lifecycle landed.

THIS IS A SEPARATE DRIVER FROM ``run_options_matrix.py``, NOT A FLAG ON IT. That one searches
the grid-1 families (OS1-OS4 + the equity-entry overlays) and its defaults are that grid's;
grid 2's keys have different DTE ranges, a different universe requirement per key, a different
expert on one arm, and a different trade floor. Two drivers with honest defaults beat one with
a mode switch.

PREFLIGHT IS THE POINT OF THIS DRIVER (design Section 5). Every key states the chain DEPTH it
needs, and `tools/probe_option_chain_depth.py` measures whether each universe symbol's parquet
tree actually reaches it over the run window. Without it a LEAPS job over the full stage-1
universe spends most of its compute on names that never listed a 1-year expiry, and reports
the resulting nothing as a result. Per-strategy thresholds:

    O_LEAP  / O_PMCC    DTE >= 365     (January-cycle LEAPS: both O_LEAP arms, and the
                                       PMCC's long leg)
    O_CBS   / O_PBS     DTE >= 180
    O_ERN               DTE >= 7       (nearly the whole universe)

The probe runs ONCE PER DISTINCT THRESHOLD (three, not five) and each job is launched with the
KEPT list for its own threshold. A threshold that keeps NOTHING fails the whole run rather
than launching a job with an empty universe.

Jobs are named `opt2-<expert>-<strategy>[suffix]` and are IDEMPOTENT/RESUMABLE: a job whose
StrategyOptimization row is already `completed` is skipped, so the driver can be killed and
re-run to continue.

Prereqs: the TastyTrade options PARQUET tree must cover the window (`--options-store parquet`
is forced below -- the sqlite/Alpaca store's history floor is 2024-01-18 and cannot serve a
2023 start), plus the usual OHLCV/FMP prewarm for the expert signals.

Usage (test venv; FMP_API_KEY/DB_FILE in env):
    ba2-venvs/test/Scripts/python.exe tools/run_options2_matrix.py \
        [--strategies O_LEAP,O_PMCC,O_ERN,O_CBS,O_PBS] \
        [--experts FMPRating] [--earnings-expert FMPEarningsEvent] \
        [--start 2023-01-01] [--end 2025-12-31] \
        [--population 40] [--generations 6] [--dry-run]

Environment overrides (so a longer/shorter search does not need a code edit):
    BA2_GRID2_POPULATION, BA2_GRID2_GENERATIONS
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

# PHASE 1 (design Section 7). Order matters only for how a partial run reads: the key with the
# most data support first, the event arm next, the two thinnest last.
_DEFAULT_STRATEGIES = ["O_LEAP", "O_PMCC", "O_ERN", "O_CBS", "O_PBS"]

# The CHAIN-DEPTH each key needs, in DTE (design Section 5). Keyed by strategy so a new key
# cannot be added to the list above without stating what it needs -- an unlisted key is a hard
# error below rather than a job with an unfiltered universe.
_MIN_DTE = {
    # Keyed by the LAUNCHABLE key: O_LEAPC/O_LEAPP are the two arms of O_LEAP and are not
    # launchable on their own, so a threshold for them would never be read.
    "O_LEAP": 365,
    # The PMCC's LONG leg is a LEAPS, so it needs the same January-cycle depth O_LEAP
    # does; its 30-45-DTE overlay is listed on every name and is never the binding
    # constraint.
    "O_PMCC": 365,
    "O_ERN": 7,
    "O_CBS": 180,
    "O_PBS": 180,
}

# Experts driving the SCREENER-style keys. FMPRating is the grid-1 default and the only expert
# with a measured large-cap signal on this options universe.
_DEFAULT_EXPERTS = ["FMPRating"]

# O_ERN CHAINS BEHIND ITS OWN EXPERT, AND ONLY O_ERN DOES (design Section 9). The expert ranks
# upcoming earnings EVENTS and stamps the event date onto its recommendations; the strategy's
# rec_days_to_earnings / days_after_event gates read that stamp. Running O_ERN under FMPRating
# would leave both gates unable to fire (no stamp -> never fires, by design), i.e. a job that
# trades nothing; running a LEAPS key under FMPEarningsEvent would gate a 400-day position on
# a 10-day event window. So the pairing is per-strategy, not a global --experts list.
_EARNINGS_STRATEGIES = {"O_ERN"}
_DEFAULT_EARNINGS_EXPERT = "FMPEarningsEvent"

# Options need ~2x the equity balance headroom -- $20k, the same figure grid 1 runs at, so the
# two grids' results are read against the same account size.
_DEFAULT_CAPITAL = 20000.0

# POP 40 / GEN 6, modest BY DESIGN (design Section 7): the sample supports "does any region
# work", not fine-tuning. Env-overridable so a follow-up deeper search needs no code edit.
_DEFAULT_POPULATION = int(os.getenv("BA2_GRID2_POPULATION", "40"))
_DEFAULT_GENERATIONS = int(os.getenv("BA2_GRID2_GENERATIONS", "6"))

# Fitness: option_car for EVERY key in this grid (design Section 7). Passed explicitly rather
# than left to the launcher's per-kind auto-resolution so the run config records the choice --
# the convex grid (tools/run_convex_matrix.py, plan Task 13) uses a DIFFERENT fitness over
# overlapping structures, and "which metric scored this job" must never be inferred.
_FITNESS = "option_car"


def _universe():
    with open(_UNIVERSE_FILE, encoding="utf-8") as f:
        return [s.strip() for s in f.read().split() if s.strip()]


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


def _expert_for(strategy: str, experts, earnings_expert: str):
    """The expert(s) a strategy runs under: the earnings expert for O_ERN, the general list
    for everything else. Yielded rather than returned so a future many-experts arm is one
    edit, and so the job list is built in ONE place."""
    if strategy in _EARNINGS_STRATEGIES:
        return [earnings_expert]
    return list(experts)


def _jobs(strategies, experts, earnings_expert, name_suffix=""):
    """Yield (name, expert, strategy) in list order -- strategy-major, so a partial run
    finishes whole structures rather than half of each."""
    for s in strategies:
        for expert in _expert_for(s, experts, earnings_expert):
            yield (f"opt2-{expert}-{s}{name_suffix}", expert, s)


def _thresholds(strategies) -> dict:
    """{min_dte: [strategy, ...]} for the requested strategies. Raises on an unlisted key."""
    unknown = [s for s in strategies if s not in _MIN_DTE]
    if unknown:
        raise SystemExit(
            f"run_options2_matrix: no chain-depth threshold declared for {unknown}. Every "
            f"grid-2 key must state the DTE depth its universe needs (design Section 5) -- "
            f"add it to _MIN_DTE rather than running the job on an unfiltered universe.")
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
        out_file = os.path.join(out_dir, f"grid2_universe_dte{min_dte}.txt")
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
                f"run_options2_matrix: chain-depth preflight FAILED (exit {rc}) at "
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
        # THE PARQUET STORE, ALWAYS. daily_backtest_handler.validate_options_window enforces
        # the serving vendor's history floor, and the sqlite/Alpaca store's is 2024-01-18 --
        # so the design's 2023-01 start RAISES before the job runs on the default store.
        "--options-store", args.options_store,
        "--fitness", args.fitness,
        # Daily cadence: option entries want the day's signal, and O_ERN's entry window is
        # 1-5 days wide, which a weekly scan would miss outright.
        "--run-schedule", "daily", "--name", name, "--parallel", str(args.parallel),
    ]
    if args.early_stop is not None:
        cmd += ["--early-stop", str(args.early_stop)]
    if args.mutation_prob is not None:
        cmd += ["--mutation-prob", str(args.mutation_prob)]
    # "Pass 0 to disable": a 0 must be FORWARDED, or the launcher re-applies its own default.
    cmd += cap_passthrough(args)
    if args.screener_gate_store:
        cmd += ["--screener-gate-store", args.screener_gate_store,
                "--max-stock-price", str(args.max_stock_price)]
    if args.workers:
        cmd += ["--workers", args.workers]
    return cmd


def build_parser() -> argparse.ArgumentParser:
    """The driver's CLI, split out of ``main`` so a test can parse the REAL defaults rather
    than a hand-written stand-in that can drift from them."""
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--strategies", default=",".join(_DEFAULT_STRATEGIES),
                    help="Comma list of grid-2 phase-1 keys (O_LEAP,O_PMCC,O_ERN,"
                         "O_CBS,O_PBS). O_CAL is still phase-gated and refuses at the "
                         "launcher.")
    ap.add_argument("--experts", default=",".join(_DEFAULT_EXPERTS),
                    help="Comma list of experts for the SCREENER-driven keys (default "
                         "FMPRating). O_ERN ignores this and uses --earnings-expert.")
    ap.add_argument("--earnings-expert", default=_DEFAULT_EARNINGS_EXPERT,
                    help=f"Expert driving O_ERN (default {_DEFAULT_EARNINGS_EXPERT}). It "
                         f"stamps the event date the strategy's timing gates read; no other "
                         f"expert can make those gates fire.")
    ap.add_argument("--start", default="2023-01-01",
                    help="Backtest start (design Section 7 window; needs the parquet store).")
    ap.add_argument("--end", default="2025-12-31",
                    help="Backtest end (2026 is the reserved walk-forward holdout and the "
                         "launcher refuses to search into it).")
    ap.add_argument("--population", type=int, default=_DEFAULT_POPULATION,
                    help=f"GA population (default {_DEFAULT_POPULATION}; "
                         f"$BA2_GRID2_POPULATION overrides).")
    ap.add_argument("--generations", type=int, default=_DEFAULT_GENERATIONS,
                    help=f"GA generations (default {_DEFAULT_GENERATIONS}; "
                         f"$BA2_GRID2_GENERATIONS overrides).")
    ap.add_argument("--early-stop", type=int, default=None,
                    help="Generations without improvement before a job stops. Omitted -> "
                         "launcher default.")
    ap.add_argument("--mutation-prob", type=float, default=None,
                    help="Per-gene mutation probability passthrough (default: launcher's).")
    ap.add_argument("--interval", default="1d",
                    help="Analysis/fill interval (default 1d -- option cache bars are daily).")
    ap.add_argument("--fitness", default=_FITNESS,
                    help=f"Fitness metric for every job (default {_FITNESS} -- design "
                         f"Section 7: option_car for every key in THIS grid; the convex grid "
                         f"uses option_convex and must not be crossed with it).")
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
                         "the ADJUSTED fitness. Pass 0 to disable.")
    ap.add_argument("--profit-share-cap-pct", type=float, default=25.0,
                    help="Cap each BET's share of the run's net profit. Pass 0 to disable.")
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

    experts = [e.strip() for e in args.experts.split(",") if e.strip()]
    strategies = [s.strip() for s in args.strategies.split(",") if s.strip()]
    with open(args.universe_file, encoding="utf-8") as f:
        symbols = [s.strip() for s in f.read().split() if s.strip()]

    launcher = args.launcher
    if not launcher:
        launcher = os.path.join(os.path.dirname(sys.executable), "ba2-test.exe")
        if not os.path.exists(launcher):
            launcher = os.path.join(os.path.dirname(sys.executable), "ba2-test")

    jobs = list(_jobs(strategies, experts, args.earnings_expert, args.name_suffix))
    done = _completed_names()
    print(f"options grid 2: {len(jobs)} jobs (strategies={strategies}, experts={experts}, "
          f"earnings_expert={args.earnings_expert}, universe={len(symbols)} symbols); "
          f"{sum(1 for j in jobs if j[0] in done)} already completed.")

    if args.skip_preflight:
        print("options grid 2: PREFLIGHT SKIPPED -- every job runs the full universe. "
              "Names that cannot reach the key's DTE depth will trade nothing and score the "
              "zero-trade sentinel; read the results accordingly.")
        kept_by_dte = {dte: symbols for dte in _thresholds(strategies)}
    else:
        kept_by_dte = _preflight(strategies, symbols, args.start, args.end,
                                 args.probe_out_dir, dry_run=args.dry_run)

    if args.dry_run:
        for nm, exp, s in jobs:
            uni = kept_by_dte[_MIN_DTE[s]]
            print(f"  {'DONE' if nm in done else 'TODO'}  {nm}  ({exp} {s}, "
                  f"DTE>={_MIN_DTE[s]}, {len(uni)} symbols)")
        return 0

    for i, (name, expert, strat) in enumerate(jobs, 1):
        if name in _completed_names():   # re-read each loop (resumable)
            print(f"[{i}/{len(jobs)}] SKIP {name} (already completed)", flush=True)
            continue
        cmd = build_cmd(args, launcher, name, expert, strat, kept_by_dte[_MIN_DTE[strat]])
        print(f"[{i}/{len(jobs)}] RUN  {name} ...", flush=True)
        rc = subprocess.run(cmd, env=os.environ.copy()).returncode
        print(f"[{i}/{len(jobs)}] {name} exit={rc}", flush=True)
    print("options grid 2 driver: done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
