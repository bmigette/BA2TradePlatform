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

# Sibling helper in tools/ (shared by all three matrix drivers). The directory is put on the
# path explicitly so the import works however the script is reached (path, -m, or a test import).
_TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)
from matrix_flags import (  # noqa: E402
    cap_passthrough, decision_times_plan, decision_times_tokens, job_name_with_digest,
    with_decision_times_name)

_UNIVERSE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "senate_universe.txt")
_EXPERT = "FMPSenateTraderWeight"
_DEFAULT_STRATEGIES = ["S2", "S3", "S5", "S6"]
_DEFAULT_CAPITAL = 10000.0


def _universe(path: str = _UNIVERSE_FILE) -> str:
    """Comma-joined symbol list from ``path``. Lines starting with ``#`` are comments (e.g. the
    provenance header on ``docs/strategy_research/atr_grid/senate_universe_2020_2025.txt``, the
    goal2027atr Senate-lane universe -- see ``--universe-file``); blank lines are skipped too."""
    with open(path, encoding="utf-8") as f:
        syms = [s.strip() for s in f.read().splitlines()
                if s.strip() and not s.strip().startswith("#")]
    return ",".join(syms)


def market_condition_passthrough(args) -> list:
    """Extra ``optimize`` CLI tokens for the goal2027atr market-condition flags ([] when none of
    the five is given, so an ordinary invocation of this driver is untouched).

    Byte-for-byte the same five flags/logic as
    ``tools/run_screener_capband_matrix.py:market_condition_passthrough`` (the equity S1-S7
    lane) -- Senate needs the identical set (profile, manifest, exit kinds, mode, sl-loosen), so
    this mirrors that function rather than inventing a Senate-specific shape. Kept as a separate
    function (not moved into ``matrix_flags.py``) because the sibling drivers' market-condition
    flag sets already diverge in practice (``run_options_matrix.py``'s is a 2-flag, "none"-aware
    variant) -- one shared implementation would have to grow driver-specific branches instead of
    being a plain shared helper.
    """
    out: list = []
    if getattr(args, "market_condition_profile", None):
        out += ["--market-condition-profile", args.market_condition_profile]
    if getattr(args, "market_condition_manifest", None):
        out += ["--market-condition-manifest", args.market_condition_manifest]
    if getattr(args, "market_exit", None):
        out += ["--market-exit", args.market_exit]
    if getattr(args, "market_condition_mode", None) and args.market_condition_mode != "searched":
        out += ["--market-condition-mode", args.market_condition_mode]
    if getattr(args, "search_sl_loosen", False):
        out += ["--search-sl-loosen"]
    return out


def _job_name(name: str, cmd: list) -> str:
    """``name``, or ``name-d<digest>`` when ``market_condition_passthrough`` added anything.

    Thin wrapper over ``matrix_flags.job_name_with_digest``, exactly mirroring
    ``tools/run_screener_capband_matrix.py:_job_name`` (same digest math, shared implementation).
    """
    return job_name_with_digest(name, cmd)


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
                    help="Comma list of strategy keys (default S2,S3,S5,S6). S1 was long "
                         "believed inapplicable (it anchored on expert_target_price, which "
                         "Senate has none of) -- since S4's target-anchored TP was merged into "
                         "S1 as a GA-TOGGLEABLE, self-disabling entry_action, S1 runs fine for "
                         "Senate (goal2020 confirms it: backtests 1438-1440 etc.) and is part "
                         "of the goal2027atr Senate lane (see tools/grid_atr27.sh PHASE=senate, "
                         "--strategies S1,S3,S5,S6). S7 stays FMPRating-only (a refinement "
                         "around ITS archived winner).")
    ap.add_argument("--start", default="2023-01-01")
    ap.add_argument("--end", default="2026-06-30")
    ap.add_argument("--universe-file", default=_UNIVERSE_FILE,
                    help="Path to a symbol-per-line universe file (default "
                         "tools/senate_universe.txt, the static disclosure-derived universe). "
                         "goal2027atr's PHASE=senate points this at "
                         "docs/strategy_research/atr_grid/senate_universe_2020_2025.txt, a "
                         "FRESH derivation for the 2020-2025 window -- see that file's header. "
                         "'#'-prefixed and blank lines are skipped, so a provenance header is "
                         "safe to keep in the file.")
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
    ap.add_argument("--stress-spread-bps", type=float, default=0.0,
                    help="Rank every genome on the WORSE of its fitness at --spread-bps and at "
                         "--spread-bps plus this many bps. Default 0.0 (off). Like --spread-bps, "
                         "a single blended value -- Senate's universe has no cap-band dimension "
                         "to key a per-band value on (contrast the equity driver's "
                         "--stress-spread-bps, which is 'large=..,mid=..,small=..'). Forwarded "
                         "only when > 0, so an ordinary invocation is unchanged.")
    ap.add_argument("--early-stop", type=int, default=None,
                    help="GA early-stop patience passthrough (default: the launcher's own, 4). "
                         "Forwarded only when given, so existing commands are unchanged.")
    ap.add_argument("--early-stop-min-rel", type=float, default=None,
                    help="Minimum RELATIVE gain (a fraction, 0.01 = 1%%) that resets the "
                         "early-stop patience; passthrough, forwarded only when given.")
    ap.add_argument("--sizing-mode", choices=("notional", "risk_atr"), default=None,
                    help="Pin sizing_mode for every job, overriding the expert's default. "
                         "REQUIRES --name-suffix to contain the mode token ('riskatr'/"
                         "'notional'), or a same-named prior run of the other mode would be "
                         "skipped as already-completed -- same guard as the equity driver's "
                         "--sizing-mode.")
    ap.add_argument("--robust-fitness", dest="robust_fitness", action="store_true", default=True,
                    help="Rank on the ROBUSTNESS-ADJUSTED fitness (concentration + monte carlo + "
                         "spread). ON BY DEFAULT (the launcher's own default since 2026-09-17), "
                         "so this flag only restates it explicitly; nothing is forwarded.")
    ap.add_argument("--no-robust-fitness", dest="robust_fitness", action="store_false",
                    help="Rank on the RAW metric instead (the pre-2026-09-17 default) -- "
                         "forwarded to every job. Scores are NOT comparable across this "
                         "setting; use a fresh --name-suffix when flipping it on a re-run.")
    ap.add_argument("--rm-toggle-policy", default=None, choices=("pinned", "atr-searched"),
                    help="See `ba2-test optimize --rm-toggle-policy`; passthrough, forwarded "
                         "only when given, so every existing command is unchanged. "
                         "'atr-searched' requires --name (or --name-suffix, folded into the "
                         "built name) to contain '-atr27' -- the launcher refuses per-job "
                         "otherwise.")
    # -------------------------------------------------------------------------------------------
    # goal2027atr market-condition passthrough (Senate lane) -- see market_condition_passthrough's
    # docstring: each of the five is forwarded ONLY when given, and an invocation that passes
    # none of them builds a byte-identical job name/argv to before this block existed. When any
    # IS given, _job_name folds the job's full resolved argv (minus --name/--parallel/--workers)
    # into a digest suffix, so a flag change gets a new job identity and can never resume an old
    # (differently-configured) row under the same name.
    # -------------------------------------------------------------------------------------------
    ap.add_argument("--market-condition-profile", default=None,
                    metavar="none|<profile>[,<profile>...]",
                    help="Forward --market-condition-profile to every job (see `ba2-test "
                         "optimize --help`). Folds into the job's name digest.")
    ap.add_argument("--market-condition-manifest", default=None,
                    metavar="DIGEST[,DIGEST...]|<profile>=DIGEST,...",
                    help="Forward --market-condition-manifest to every job. Folds into the "
                         "job's name digest.")
    ap.add_argument("--market-exit", default=None, metavar="KIND[,KIND...]",
                    help="Forward --market-exit to every job (S1-S7 only; requires "
                         "--market-condition-profile). Folds into the job's name digest.")
    ap.add_argument("--market-condition-mode", default=None, choices=("searched", "all-off"),
                    help="Forward --market-condition-mode to every job ('searched' is the "
                         "launcher default and is NOT forwarded -- pass 'all-off' for the "
                         "matched control arm). Folds into the job's name digest when 'all-off'.")
    ap.add_argument("--search-sl-loosen", action="store_true", default=False,
                    help="Forward --search-sl-loosen to every job. Folds into the job's name "
                         "digest.")
    ap.add_argument("--decision-times", default=None, metavar="default|fixed|HH:MM,HH:MM,...",
                    help="The DECISION TIME gene (schedule:time). DEFAULT (flag absent) = ON with "
                         "the shared DEFAULT_DECISION_TIME_CHOICES on an intraday --interval, "
                         "off on --interval 1d; 'fixed' = no gene; or a comma list. Adds "
                         "'-timegene' to the job name and folds into the name digest.")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    # A sizing-mode matrix MUST be name-distinguished from its sibling -- see --sizing-mode help.
    if args.sizing_mode:
        token = "riskatr" if args.sizing_mode == "risk_atr" else "notional"
        if token not in args.name_suffix.replace("_", "").lower():
            ap.error(
                f"--sizing-mode {args.sizing_mode} requires --name-suffix to contain "
                f"'{token}' (got {args.name_suffix!r}). Otherwise a re-run under the other mode "
                f"is skipped as already-completed. Example: --name-suffix -goal2027atr-{token}")

    strategies = [s.strip() for s in args.strategies.split(",") if s.strip()]
    universe = _universe(args.universe_file)
    exe = os.path.join(os.path.dirname(sys.executable), "ba2-test.exe")
    if not os.path.exists(exe):
        exe = os.path.join(os.path.dirname(sys.executable), "ba2-test")

    dt_times, dt_header = decision_times_plan(args.decision_times, args.interval)
    print(dt_header, flush=True)
    jobs = list(_jobs(strategies, args.name_suffix))
    done = _completed_names()
    mc_tokens_preview = market_condition_passthrough(args)
    print(f"senate matrix: {len(jobs)} jobs (strategies={strategies}, "
          f"universe={len(universe.split(','))} symbols); "
          f"{sum(1 for j in jobs if j[0] in done)} already completed"
          f"{' (by base name; digest-suffixed names are checked per job)' if mc_tokens_preview else ''}.")

    for i, (name, strat) in enumerate(jobs, 1):
        cmd = [exe, "optimize", "--expert", _EXPERT, "--universe", universe,
               "--strategy", strat,
               "--start", args.start, "--end", args.end, "--fitness", args.fitness,
               "--interval", args.interval, "--population", str(args.population),
               "--generations", str(args.generations),
               "--initial-capital", str(args.initial_capital),
               "--run-schedule", "weekly", "--name", name, "--parallel", str(args.parallel)]
        if args.sizing_mode:
            cmd += ["--sizing-mode", args.sizing_mode]
        if args.mutation_prob is not None:
            cmd += ["--mutation-prob", str(args.mutation_prob)]
        if args.early_stop is not None:
            cmd += ["--early-stop", str(args.early_stop)]
        if args.early_stop_min_rel is not None:
            cmd += ["--early-stop-min-rel", str(args.early_stop_min_rel)]
        if args.rm_toggle_policy:
            cmd += ["--rm-toggle-policy", args.rm_toggle_policy]
        # "Pass 0 to disable" (see the --profit-cap-pct help): a 0 must be FORWARDED, because
        # omitting the flag lets ba2test_launcher re-apply its own 2000/25 default instead.
        cmd += cap_passthrough(args)
        if args.workers:
            cmd += ["--workers", args.workers]
        if args.spread_bps and args.spread_bps > 0:
            cmd += ["--spread-bps", str(args.spread_bps)]
        if args.stress_spread_bps and args.stress_spread_bps > 0:
            cmd += ["--stress-spread-bps", str(args.stress_spread_bps)]
        # Default-ON in the launcher: the ON case passes nothing (job names/argv unchanged) and
        # only the opt-OUT is forwarded.
        if not args.robust_fitness:
            cmd += ["--no-robust-fitness"]
        cmd += ["--labels", strat]
        # goal2027atr market-condition passthrough: [] with none of the five flags given, so an
        # ordinary invocation's cmd (and therefore its name/digest below) is byte-identical to
        # before this block existed. Appended LAST so it never displaces an existing token's
        # position for a job with no new flags.
        mc_tokens = market_condition_passthrough(args)
        cmd += mc_tokens
        # The decision-time gene (DEFAULT ON on an intraday --interval; see matrix_flags
        # .decision_times_plan): '-timegene' in the base name, tokens last, digest on its own.
        dt_tokens = decision_times_tokens(dt_times)
        if dt_tokens:
            name = with_decision_times_name(name, dt_times)
            cmd[cmd.index("--name") + 1] = name
        cmd += dt_tokens
        job_name = name
        if mc_tokens or dt_tokens:
            job_name = _job_name(name, cmd)
            cmd[cmd.index("--name") + 1] = job_name
        if args.dry_run:
            print(f"  {'DONE' if job_name in done else 'TODO'}  {job_name}  ({_EXPERT} {strat})")
            continue
        if job_name in _completed_names():   # re-read each loop (resumable)
            print(f"[{i}/{len(jobs)}] SKIP {job_name} (already completed)", flush=True)
            continue
        print(f"[{i}/{len(jobs)}] RUN  {job_name} ...", flush=True)
        rc = subprocess.run(cmd, env=os.environ.copy()).returncode
        print(f"[{i}/{len(jobs)}] {job_name} exit={rc}", flush=True)
    print("senate matrix driver: done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
