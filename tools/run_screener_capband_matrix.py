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
    ba2-venvs/test/Scripts/python.exe tools/run_screener_capband_matrix.py \
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
# DeterministicScorer added 2026-08-09. It is a CLASSIC expert (bypasses_classic_rm = False), so
# it runs the full S1/S2/S3 strategy set across every band like the others, not one job per band.
#
# NOT floored to 2022 like FMPRating, deliberately. FMPRating keys ENTIRELY off FMP price targets,
# which do not serve rows before ~2021-04, so a 2020 run gave it ~20 dead months. DeterministicScorer
# blends FIVE sections (analyst / earnings / fundamental / macro / technical), and the analyst
# section itself splits into grades (which reach back to 2012) and targets. Only the target sub-leg
# is thin before 2021-04, and both its weights (aw_grades / aw_targets, w_analyst) are GA genes that
# can turn it down, so the expert still has real signal across the whole window.
#
# CAVEAT worth knowing when reading its results: over 2020-01 .. 2021-04 the target sub-leg is
# degraded, so the GA sees a window where aw_targets pays less than it would on clean data and may
# settle lower than the truth. That biases one gene, not the run.
_CLASSIC = ["FMPRating", "FMPEarningsDrift", "FMPInsiderClusterBuy", "DeterministicScorer"]
_RANKER = "FactorRanker"
# Experts with no usable data in the large-cap band (skipped on `large` unless --include-no-data).
_NO_LARGE_CAP = {"FMPEarningsDrift", "FMPInsiderClusterBuy"}
# Strategies restricted to experts producing a REAL, updating analyst price target. S4 (which
# anchored TP on expert_target_price) is now MERGED into S1, and S1's target-anchored TP is a
# GA-TOGGLEABLE entry_action that self-disables for experts without a real analyst target — so no
# strategy needs the restriction anymore. Kept (empty) for the _jobs() gating call site.
# S4 anchors its TP-follow on expert_target_price / percent_to_new_target — only
# meaningful for experts with a REAL analyst target (FMPRating).
_TARGET_PRICE_STRATEGIES: set = {"S4"}
_TARGET_PRICE_EXPERTS = {"FMPRating"}

# PER-EXPERT EARLIEST USABLE START, floored against the driver's --start.
#
# An expert can only be evaluated as far back as the DATA ITS SIGNAL DEPENDS ON, which is not the
# same for all of them and is not something the OHLCV/metric_store backfill can fix.
#
# FMPRating keys off FMP's analyst PRICE TARGETS, and that endpoint simply does not serve rows
# before ~2021-04 (probed 2026-08-04 against the live API: AAPL oldest 2021-06-11, MSFT
# 2021-04-22, JPM 2021-04-27 -- while the analyst GRADE endpoint on the same symbols goes back to
# 2012). A 2020-01-01 run therefore produced its FIRST TRADE on 2021-08-16: ~20 months of the
# window were silently dead, the fitness bucketed an empty 2020 calendar year, and the result was
# not comparable with experts that really did cover 2020. Those runs (opt 248/249/250) were
# discarded.
#
# 2022-01-01 for FMPRating is deliberately LATER than the ~2021-04 data floor: it starts clear of
# the sparse ramp-up period rather than hugging the edge, and it re-optimizes this expert on the
# post-fix engine (condition store-blindness, ATR tz, market_cap chunking, warmup) rather than
# inheriting anything from the old runs.
#
# Everything else is left to the driver's --start: FactorRanker is pure OHLCV (backfilled to
# 2018), insider Form-4 history reaches 2003, and the earnings endpoint serves full history.
_EXPERT_MIN_START = {
    "FMPRating": "2022-01-01",
}


def _start_for(expert: str, default_start: str) -> str:
    """The later of the driver's --start and the expert's own data floor."""
    floor = _EXPERT_MIN_START.get(expert)
    return max(default_start, floor) if floor else default_start


def _window_tag(expert: str) -> str:
    """Name suffix marking an expert whose window is floored (e.g. '-from2022'), else ''.

    Put in the NAME because the name is the only thing carried into the results table and the
    resume check: without it a 2022-start FMPRating job and a 2020-start one are indistinguishable
    downstream, and `--name-suffix goal2020` would label a run that never saw 2020-2021.
    """
    floor = _EXPERT_MIN_START.get(expert)
    return f"-from{floor[:4]}" if floor else ""
# Per-strategy population/generations override. S7 is a NARROW refinement around a known-good point
# (the archived 186% S2-large winner) so it converges with far fewer individuals/generations. S1 is
# the RICHEST strategy (live "high conviction" conditions + entry TP/SL bracket + target-anchored TP
# + exit rules => largest gene space), so it gets a bit MORE population. Unlisted strategies keep the
# driver's --population/--generations args unchanged.
_STRATEGY_BUDGET_OVERRIDE = {
    # S1 absorbed S4's entry TP/SL genes (entry:s1_tp_target:*, entry:s1_sl_entry:*) on top of
    # its existing ~90+ condition/exit/model genes, so it now searches a LARGER space than a
    # plain FMPRating job (base --population + --fmp-population-bonus = 90) -- bump above that
    # baseline rather than below it.
    "S1": {"population": 140},
    # S7 rebuilt as a FAITHFUL replica of the archived 186% winner (correct first-match exit
    # order, winner's gates restored as toggleable, step-1/2 ranges): the search space is a
    # real neighborhood now (thousands of distinct genomes, not 21) — budget accordingly.
    "S7": {"population": 60, "generations": 8},
}


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


def _jobs(bands, strategies, include_no_data, skip_experts=frozenset(), name_suffix=""):
    """Yield (name, expert, strategy_or_None, band) in priority order.

    ``skip_experts`` (a set of expert class names) drops those experts entirely — used to defer
    an expert that is too slow for the matrix (e.g. FMPInsiderClusterBuy: ~1.5h/backtest) without
    editing the expert list.

    ``name_suffix`` is appended to every job NAME (e.g. ``-pd``) — used to re-run the whole matrix
    under fresh names WITHOUT clobbering the prior runs: the new names aren't in the completed set,
    so all jobs re-run, and the existing tagged Backtests (old names) are left untouched.

    ORDER: FMPRating runs across ALL bands FIRST (large -> mid -> small), so its full optimization
    completes before any other expert starts; then the remaining classic experts + FactorRanker run
    band-by-band. (FMPRating is the most general rating expert, so prioritising it surfaces its
    results first.)"""
    def _eligible(band, expert):
        if expert in skip_experts:
            return False
        if expert in _NO_LARGE_CAP and band == "large" and not include_no_data:
            return False
        return True

    # 1) FMPRating across every band first.
    if "FMPRating" not in skip_experts:
        for band in bands:
            if not _eligible(band, "FMPRating"):
                continue
            for s in strategies:
                # ``_window_tag`` marks a data-floored expert in its own NAME, so a 2022-start
                # FMPRating row can never be read as, or resumed as, a full-window "goal2020" run.
                yield (f"scr-{band}-FMPRating-{s}{name_suffix}{_window_tag('FMPRating')}",
                       "FMPRating", s, band)
    # 2) then the remaining classic experts + FactorRanker, band by band.
    for band in bands:
        for expert in _CLASSIC:
            if expert == "FMPRating" or not _eligible(band, expert):
                continue
            for s in strategies:
                if s in _TARGET_PRICE_STRATEGIES and expert not in _TARGET_PRICE_EXPERTS:
                    continue  # S4 needs a real analyst target; these experts have none
                yield (f"scr-{band}-{expert}-{s}{name_suffix}", expert, s, band)
        if _RANKER not in skip_experts:
            yield (f"scr-{band}-{_RANKER}{name_suffix}", _RANKER, None, band)  # bypass: one job per band



# Realised bid-ask spreads differ by an ORDER OF MAGNITUDE across cap bands -- a few bps on a
# mega-cap, tens of bps on a small-cap. One stress level for the whole grid is therefore
# meaningless: it is either far too harsh on large or far too soft on small, and the bands stop
# being rankable against each other. Accepts a scalar (same everywhere, for a single-band run)
# or explicit per-band values.
_STRESS_BAND_DEFAULTS = {"large": 10.0, "mid": 25.0, "small": 50.0}


def _parse_stress_spread(spec: str) -> dict:
    """'' | '0' -> off. '40' -> 40 for every band. 'large=10,small=50' -> per band.

    An unknown band name is an ERROR, not a silent skip: a typo like 'smal=50' would otherwise
    leave the small band running unstressed while the operator believed it was covered.
    """
    spec = (spec or "").strip()
    if not spec:
        return {}
    if "=" not in spec:
        try:
            v = float(spec)
        except ValueError:
            raise SystemExit(f"--stress-spread-bps: expected a number or band=value pairs, got {spec!r}")
        return {} if v <= 0 else {b: v for b in _STRESS_BAND_DEFAULTS}
    out = {}
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        band, _, val = part.partition("=")
        band = band.strip().lower()
        if band not in _STRESS_BAND_DEFAULTS:
            raise SystemExit(f"--stress-spread-bps: unknown band {band!r}; "
                             f"expected one of {sorted(_STRESS_BAND_DEFAULTS)}")
        try:
            out[band] = float(val)
        except ValueError:
            raise SystemExit(f"--stress-spread-bps: {band}={val!r} is not a number")
    return {b: v for b, v in out.items() if v > 0}

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--bands", default="large,mid,small")
    ap.add_argument("--strategies", default="S1,S2,S3")  # S4 merged into S1
    ap.add_argument("--start", default="2023-01-01")
    ap.add_argument("--end", default="2026-01-01")
    ap.add_argument("--population", type=int, default=40)
    ap.add_argument("--generations", type=int, default=8)
    ap.add_argument("--mutation-prob", type=float, default=None,
                    help="Per-gene mutation probability passthrough (default: launcher's 0.3).")
    ap.add_argument("--interval", default="5min")
    ap.add_argument("--spread-bps", type=float, default=0.0,
                    help="Round-trip bid-ask spread in basis points, modeled at the fill-engine "
                         "level (widens LIMIT/TP trigger thresholds + degrades MARKET/STOP fill "
                         "prices) -- see BacktestAccount._slip/_limit_trigger_price. Applied to "
                         "EVERY job this invocation runs (one value per driver call -- run large/"
                         "mid/small as separate invocations with different values, since real "
                         "spread varies sharply by cap band). Default 0.0 (off).")
    ap.add_argument("--fitness", default="calmar_ratio")
    ap.add_argument("--store", default=_STORE)
    ap.add_argument("--cadence-days", type=int, default=7)
    ap.add_argument("--include-no-data", action="store_true",
                    help="Also run EarningsDrift/Insider on the large band (default: skip — no data).")
    ap.add_argument("--skip-experts", default="",
                    help="Comma list of expert class names to EXCLUDE entirely (e.g. "
                         "'FMPInsiderClusterBuy' — too slow at ~1.5h/backtest; defer it).")
    ap.add_argument("--name-suffix", default="",
                    help="Suffix appended to every job name (e.g. '-pd'). Re-runs the whole matrix "
                         "under FRESH names so prior runs' tagged Backtests are kept untouched — "
                         "used after a screener fix (e.g. the price-drop rebuild) to re-explore the "
                         "now-meaningful dimension without overwriting the old results.")
    ap.add_argument("--sizing-mode", choices=("notional", "risk_atr"), default=None,
                    help="Pin sizing_mode for every job in this matrix, overriding the expert "
                         "spec's default (risk_atr for the classic experts). Exists so the two "
                         "modes are compared as TWO SEPARATE MATRICES rather than one GA gene: "
                         "under notional the five ATR genes are inert and drift random, so a "
                         "crossover flipping the mode would judge it with unselected parameters "
                         "and bias the result toward whichever mode dominates the population. "
                         "REQUIRES --name-suffix to contain the mode token (enforced below), or "
                         "the second matrix would be skipped as already-completed. No effect on "
                         "FactorRanker (bypass expert — never reads sizing_mode).")
    ap.add_argument("--workers", default=None,
                    help="Comma-separated remote worker NAMES to distribute each job's GA trials to "
                         "(e.g. 'remote150'); trials spread across these + local. Workers must be "
                         "registered + cache-synced first.")
    ap.add_argument("--parallel", type=int, default=4,
                    help="Local trial consumers per job (ThreadPoolExecutor). Keep low when "
                         "distributing to remote workers — each local consumer holds the OHLCV "
                         "cache in RAM (~5GB at 5min), so 4 saturates a 64GB host. Default 4.")
    ap.add_argument("--profit-cap-pct", type=float, default=2000.0,
                    help="Cap each trade's gain at this %% of its cost basis for the ADJUSTED "
                         "fitness/return, so one lucky non-reproducible mega-winner (e.g. a sub-$1 "
                         "stock that 90x'd) can't dominate the GA. Default 2000. Pass 0 to disable.")
    ap.add_argument("--profit-share-cap-pct", type=float, default=25.0,
                    help="Cap each trade's gain at this %% of the run's NET profit for the ADJUSTED "
                         "fitness/return, so no single trade contributes more than this share of "
                         "total return (a trade can pass --profit-cap-pct yet still be 60%% of the "
                         "book). Default 25. Pass 0 to disable.")
    ap.add_argument("--robust-fitness", action="store_true",
                    help="Rank every job on the ROBUSTNESS-ADJUSTED fitness (concentration + "
                         "monte carlo + spread) instead of the raw metric, so a genome whose "
                         "headline number rests on one unrepeatable winner is not selected. Both "
                         "values are stored per trial. Scores are NOT comparable with a grid run "
                         "without it.")
    ap.add_argument("--stress-spread-bps", default="",
                    help="Rank every genome on the WORSE of its fitness at the modelled "
                         "spread and at spread + this many bps. Selects against configs "
                         "whose edge only survives the assumed cost. Empty/0 = off "
                         "(default). PER BAND: 'large=10,mid=25,small=50' -- realised "
                         "spreads differ ~10x across bands, so one level for the whole grid "
                         "is either far too harsh on large or far too soft on small. A bare "
                         "number applies to every band (fine for a single-band run). "
                         "Suggested: 'large=10,mid=25,small=50'. NOTE: a non-zero value "
                         "RESCALES fitness -- do not mix levels within one grid.")
    ap.add_argument("--fitness-trade-scale", action="store_true",
                    help="Scale each trial's fitness by min(avg_trades_per_year, cap)/100 so "
                         "statistically thin (few-trade) configs are down-weighted (stops a 16-trade "
                         "lottery winner from topping the search). Default: OFF.")
    ap.add_argument("--fitness-trade-scale-cap", type=float, default=100.0,
                    help="Cap (trades/year) for --fitness-trade-scale so the GA is not rewarded for "
                         "over-trading (scalping). Default 100 = factor maxes at 1.0.")
    ap.add_argument("--fitness-win-rate-factor", action="store_true",
                    help="Scale each trial's fitness by 2 * win_rate_fraction (50%% win = break-even "
                         "1.0x, 100%% win = 2x, 0%% win = 0x). Applies even to consistent_annual_return. "
                         "Default: OFF.")
    ap.add_argument("--fmp-population-bonus", type=int, default=10,
                    help="Extra GA population for FMPRating jobs ONLY (its search space grew with the "
                         "price-target + analyst-recency genes). Added to --population for FMPRating. "
                         "Default 10.")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    # A sizing-mode matrix MUST be name-distinguished from its sibling. Jobs are skipped by NAME
    # when already `completed`, so running notional and risk_atr under the same --name-suffix
    # would silently skip the entire second matrix and leave you comparing one mode against
    # itself. Fail loudly rather than let that happen hours later.
    if args.sizing_mode:
        token = "riskatr" if args.sizing_mode == "risk_atr" else "notional"
        if token not in args.name_suffix.replace("_", "").lower():
            ap.error(
                f"--sizing-mode {args.sizing_mode} requires --name-suffix to contain "
                f"'{token}' (got {args.name_suffix!r}). Otherwise the second matrix is skipped "
                f"as already-completed. Example: --name-suffix goal2020-{token}")

    bands = [b.strip() for b in args.bands.split(",") if b.strip()]
    stress_by_band = _parse_stress_spread(args.stress_spread_bps)
    if stress_by_band:
        print(f"spread stress (bps, per band): "
              f"{ {b: stress_by_band.get(b, 0.0) for b in bands} }")
        _un = [b for b in bands if b not in stress_by_band]
        if _un:
            # Loud: an unstressed band inside a stressed grid is scored on a DIFFERENT
            # fitness scale, so its jobs cannot be ranked against the rest.
            print(f"WARNING: bands {_un} have NO stress -- their fitness is on a "
                  f"different scale from the stressed bands and is not comparable.")
    strategies = [s.strip() for s in args.strategies.split(",") if s.strip()]
    exe = os.path.join(os.path.dirname(sys.executable), "ba2-test.exe")
    if not os.path.exists(exe):
        exe = os.path.join(os.path.dirname(sys.executable), "ba2-test")

    skip_experts = frozenset(e.strip() for e in args.skip_experts.split(",") if e.strip())
    jobs = list(_jobs(bands, strategies, args.include_no_data, skip_experts, args.name_suffix))
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
        # Data-floored start (see _EXPERT_MIN_START). Announced per job so a shorter window is
        # visible in the log instead of being inferred later from a suspiciously late first trade.
        job_start = _start_for(expert, args.start)
        if job_start != args.start:
            print(f"[{i}/{len(jobs)}] NOTE {expert}: start floored {args.start} -> {job_start} "
                  f"(signal data does not reach {args.start})", flush=True)
        # FMPRating's search space grew (price-target + analyst-recency genes), so give it extra
        # population to explore the larger space; other experts use the base --population.
        population = args.population + (args.fmp_population_bonus if expert == "FMPRating" else 0)
        generations = args.generations
        budget = _STRATEGY_BUDGET_OVERRIDE.get(strat)
        if budget:
            # A refinement strategy (e.g. S7) ignores the FMPRating bonus too -- it's a narrow
            # neighborhood search regardless of expert, not exploring the full space. An override
            # may specify only ONE of population/generations (e.g. S1's population-only bump) --
            # fall back to the driver's own value (population still gets the FMP bonus above) for
            # whichever key is absent.
            population = budget.get("population", population)
            generations = budget.get("generations", generations)
        cmd = [exe, "optimize", "--expert", expert, "--universe", _PLACEHOLDER_UNIVERSE,
               "--screener", "--screener-store", args.store, "--screener-cap-band", band,
               # Per-expert floor, not the raw --start: see _EXPERT_MIN_START. An expert whose
               # signal data does not reach --start would otherwise burn the early years as dead
               # window and bucket empty calendar years into the consistency fitness.
               "--start", job_start, "--end", args.end, "--fitness", args.fitness,
               "--interval", args.interval, "--population", str(population),
               "--generations", str(generations), "--screener-cadence-days", str(args.cadence_days),
               # --run-schedule-day now only seeds the STATIC fallback (used as a base for the
               # scan time-of-day, and for any bypass expert that skips the per-day genes
               # entirely) -- every non-bypass strategy (S1-S7) searches WHICH day(s) itself via
               # the schedule:<day> genes (see _SCHEDULE_DAY_OPT in ba2test_launcher.py), so a
               # fast-decaying signal like FMPEarningsDrift discovers its own best cadence instead
               # of a hand-picked "monday,thursday" pin.
               "--run-schedule", "weekly", "--name", name, "--parallel", str(args.parallel)]
        if args.sizing_mode:
            # Pinned, not searched — see the --sizing-mode help. Harmless for FactorRanker
            # (bypass expert: it never reads sizing_mode), so no need to special-case it here.
            cmd += ["--sizing-mode", args.sizing_mode]
        if args.mutation_prob is not None:
            cmd += ["--mutation-prob", str(args.mutation_prob)]
        if args.profit_cap_pct and args.profit_cap_pct > 0:
            cmd += ["--profit-cap-pct", str(args.profit_cap_pct)]
        if args.profit_share_cap_pct and args.profit_share_cap_pct > 0:
            cmd += ["--profit-share-cap-pct", str(args.profit_share_cap_pct)]
        _stress = stress_by_band.get(band, 0.0)
        if _stress > 0:
            cmd += ["--stress-spread-bps", str(_stress)]
        if args.fitness_trade_scale:
            cmd += ["--fitness-trade-scale",
                    "--fitness-trade-scale-cap", str(args.fitness_trade_scale_cap)]
        if args.fitness_win_rate_factor:
            cmd += ["--fitness-win-rate-factor"]
        if args.robust_fitness:
            cmd += ["--robust-fitness"]
        if args.spread_bps and args.spread_bps > 0:
            cmd += ["--spread-bps", str(args.spread_bps)]
        # Auto-labels for easy filtering (GET /api/backtests?label=...): one tag for the grid/
        # batch id (--name-suffix, e.g. "goal5"), one for the strategy (or expert, for the
        # bypass FactorRanker job which has no strategy). Every top-N Backtest this job persists
        # carries both, independent of cap-band/optimization_id.
        job_labels = []
        if args.name_suffix.strip():
            job_labels.append(args.name_suffix.strip().lstrip("-"))
        job_labels.append(strat or expert)
        cmd += ["--labels", ",".join(job_labels)]
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
