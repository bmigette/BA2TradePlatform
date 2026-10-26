"""Autonomous driver for the 5min screener CAP-BAND optimization matrix.

OPERATOR CHECKLIST FOR THE NEXT GRID LAUNCH (2026-09-03, learned on matrix3):
  1. BT_BAR_CACHE_TRIALS=0 (default) flushes the bar cache before EVERY individual, so each
     individual re-preloads its whole working set (matrix3: 778 preloads, mean 39 s, ~8.35 h by
     2026-09-02). Evaluate a non-zero value: it retains the UNION of per-individual symbol sets
     until the worker recycle, so budget memory for the full band, not one individual's set.
  2. Never merge into this checkout while a grid runs from it: a long-lived master lazily imports
     new modules against the enums it loaded at start and dies at its persist phase
     (AttributeError N_LOSS_PCT_OF_MAX_LOSS, 2026-09-03). Merge at a job boundary: stop ONLY the
     wrapper bash shells (verified by cmdline), let the master finish, merge + ONE
     TEST_APP_VERSION bump, mark dangling 'running' rows failed, relaunch from GIT BASH
     (PowerShell Start-Process nohup resolves to WSL bash and fails). Lost TOP-N ranks:
     tools/recover_missing_topn.py <worker> <opt>:<ranks> (retry after the worker's version
     self-update, which forgets in-flight jobs).
  3. The default --fitness here is calmar_ratio; every grid wrapper passes
     consistent_annual_return explicitly. A bare run of this driver started before 683c7379 is
     on the old (inert-stress) calmar scale.
  4. Option grids have their own checklist at the top of tools/run_options_matrix.py.

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

WALK-FORWARD (goal2027, design docs/plans/2026-10-25-goal2027-grid-design.md 1.2 / 2.5): with
`--walk-forward` every cell is NOT one optimize job but a chain, built from the cell's own flags:

    <cell>-wf1   train 2020-01-01..2023-12-31  (cold start, --wf-first-generations)   test 2024
    <cell>-wf2   train ..2024-12-31            (seeded from wf1's final population)    test 2025
    <cell>-wf3   train ..2025-12-31            (seeded from wf2's final population)    test 2026
    <cell>-refit train ..2026-12-31            (seeded from wf3; PASSING cells only)

`<cell>` is the name the cell has without --walk-forward. --wf-next-generations budgets wf2, wf3 and
the refit; --generations and the per-strategy budget overrides are not used. FMPRating's data floor
(_EXPERT_MIN_START) moves its train start (names keep -from2022). Resume is by name: a completed fold
is skipped and its stored report (<--wf-report-dir>/<cell>/fold<k>.json) reused. Pass rule:
--pass-min-oos-return / --pass-max-dd-mult / --pass-min-folds (no defaults) and --pass-require-fold
<test-year> (repeatable). --wf-data-end clamps the last test window and the refit end (rehearsal on
data that stops short). Out-of-sample re-runs are local, ~14 GB each. Reports: per cell
<--wf-report-dir>/<cell>/{cell,report}.json + report.md, grid-wide <--wf-report-dir>/summary.{json,md}.
"""
import argparse
import json
import os
import subprocess
import sys
from datetime import date
from types import SimpleNamespace

# Sibling helper in tools/ (shared by all three matrix drivers). The directory is put on the
# path explicitly so the import works however the script is reached (path, -m, or a test import).
_TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)
from matrix_flags import (  # noqa: E402
    finish_matrix, note_job_exit,
    add_history_source_flag, cap_passthrough, decision_times_plan, decision_times_split, decision_times_tokens,
    history_source_tokens,
    job_name_with_digest, screener_dry_run_universe_note, with_decision_times_name, with_history_source_name,
    with_universe_rule_name)

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


def market_condition_passthrough(args) -> list:
    """Extra `optimize` CLI tokens for the goal2027atr market-condition flags ([] when none of
    the five is given, so an ordinary invocation of this driver is untouched)."""
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


def sizing_gene_tokens(args, strat) -> list:
    """`--search-sizing-mode` for a CLASSIC job when the matrix searches the sizing mode (goal2027 item 2.1),
    else []. The FactorRanker job (``strat is None``, a bypass expert) never reads sizing_mode and the
    launcher refuses the gene there, so it is not forwarded (the caller prints a NOTE)."""
    if getattr(args, "search_sizing_mode", False) and strat is not None:
        return ["--search-sizing-mode"]
    return []


def market_profile_gene_tokens(args, strat) -> list:
    """`--search-market-profile` for a CLASSIC job when the matrix searches the market profile (goal2027 item 2.2),
    else []. The FactorRanker job (``strat is None``, a bypass expert) carries no market genes and the launcher
    refuses the gene there, so it is not forwarded (the caller prints a NOTE)."""
    if getattr(args, "search_market_profile", False) and strat is not None:
        return ["--search-market-profile"]
    return []


def ds_factor_gene_tokens(args, expert) -> list:
    """`--search-ds-factor-weights` for a DeterministicScorer job when the matrix searches the DS factor genes
    (goal2027 item 2.8), else []. Every other expert never reads those settings and the launcher refuses the flag
    for them, so it is not forwarded (the caller prints a NOTE)."""
    if getattr(args, "search_ds_factor_weights", False) and expert == "DeterministicScorer":
        return ["--search-ds-factor-weights"]
    return []


#: Must equal ``ba2test_launcher._EXPLORATION_RANGE_PAIRS`` (pinned by test): the drivers cannot import the launcher.
EXPLORATION_RANGE_PAIRS = {
    "FMPRating": {"S1", "S2"},
    "FMPEarningsDrift": {"S1", "S2", "S3", "S5"},
    "FMPInsiderClusterBuy": {"S1"},
    "DeterministicScorer": {"S1", "S2", "S6", "SQM"},
}


def exploration_range_tokens(args, expert, strat) -> list:
    """`--search-exploration-ranges` for a job whose (expert, strategy) pair the flag is defined for (goal2027 item
    2.9), else []. The launcher refuses the flag for any other pair, so it is not forwarded (the caller prints a
    NOTE)."""
    if getattr(args, "search_exploration_ranges", False) and strat in EXPLORATION_RANGE_PAIRS.get(expert, ()):
        return ["--search-exploration-ranges"]
    return []


def exclude_symbols_passthrough(args) -> list:
    """Extra `optimize` CLI tokens for --exclude-symbols ([] when not given, so an ordinary
    invocation of this driver is untouched). Kept separate from
    ``market_condition_passthrough`` so it also triggers the job-name digest ON ITS OWN (see the
    call site) -- an exclusion change must get a new job identity even in a run that passes none
    of the five market-condition flags."""
    out: list = []
    if getattr(args, "exclude_symbols", None):
        out += ["--exclude-symbols", args.exclude_symbols]
    return out


def fr_top_n_below_pool_passthrough(args) -> list:
    """Extra `optimize` CLI tokens for --no-fr-top-n-below-pool ([] when not given, so an
    ordinary invocation of this driver is untouched).

    --fr-top-n-below-pool is DEFAULT ON in the launcher as of 2026-09-29 (operator decision):
    every NEW bypass-expert (FactorRanker) job this driver launches gets the repair automatically,
    with NO passthrough needed for the default. Only the OPT-OUT (--no-fr-top-n-below-pool) is
    ever forwarded, so an ordinary invocation of this driver stays byte-identical (same argv, same
    job names) -- but note the LAUNCHED FactorRanker job now behaves differently under the same
    name/row than it did before the launcher's default flipped (the repair itself is new
    behaviour, not just a new flag). Use a fresh --name-suffix for a grid that must distinguish
    pre-default from post-default FactorRanker results (goal2027atr's re-run uses -atr27-fr4, a
    NEW suffix, precisely for this reason).

    Kept separate from ``market_condition_passthrough``/``exclude_symbols_passthrough`` so it
    also triggers the job-name digest ON ITS OWN (see the call site) -- an opt-out change must
    get a new job identity even in a run that passes none of the other six sources."""
    out: list = []
    if getattr(args, "fr_top_n_below_pool", True) is False:
        out += ["--no-fr-top-n-below-pool"]
    return out


def decision_times_passthrough(args) -> list:
    """Extra `optimize` CLI tokens for the decision-time gene (``args.decision_times_resolved``,
    set once in ``main`` from the DEFAULT-ON ``--decision-times`` flag).

    Like the other passthroughs it triggers the job-name digest ON ITS OWN (see the call site),
    and the job's base name gets the ``-timegene`` token: the time list changes what the job
    scores, so two jobs differing only by it must not share a name, a skip-completed check or a
    checkpoint."""
    return decision_times_tokens(getattr(args, "decision_times_resolved", None),
                                 split=decision_times_split(getattr(args, "decision_times", None)))


def _job_name(name: str, cmd: list) -> str:
    """``name``, or ``name-d<digest>`` when ``market_condition_passthrough``,
    ``exclude_symbols_passthrough`` or ``fr_top_n_below_pool_passthrough`` added anything.

    Thin wrapper over ``matrix_flags.job_name_with_digest`` (the digest math itself is shared
    with ``tools/run_senate_matrix.py``'s Senate lane -- see that function's docstring). Mirrors
    ``tools/run_options_matrix.py:discovery_name``. Unlike ``discovery_name`` this digest is
    added ONLY when at least one new market-condition/sl-loosen flag, ``--exclude-symbols`` or
    ``--fr-top-n-below-pool`` was actually forwarded (``market_condition_passthrough``,
    ``exclude_symbols_passthrough`` or ``fr_top_n_below_pool_passthrough`` returned something):
    with none of those seven sources, every job name -- and therefore the skip-completed-by-name
    check and the DB row it resumes -- stays byte-identical to before this change existed.
    """
    return job_name_with_digest(name, cmd)


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


def _jobs(bands, strategies, include_no_data, skip_experts=frozenset(), name_suffix="",
          strategy_plan=None):
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
    def _strategies_for(expert):
        # ``strategy_plan`` (expert class -> strategy list, see --strategy-plan) REPLACES the
        # global list per expert; without a plan every expert runs ``strategies``.
        return strategies if strategy_plan is None else strategy_plan[expert]

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
            for s in _strategies_for("FMPRating"):
                # ``_window_tag`` marks a data-floored expert in its own NAME, so a 2022-start
                # FMPRating row can never be read as, or resumed as, a full-window "goal2020" run.
                yield (f"scr-{band}-FMPRating-{s}{name_suffix}{_window_tag('FMPRating')}",
                       "FMPRating", s, band)
    # 2) then the remaining classic experts + FactorRanker, band by band.
    for band in bands:
        for expert in _CLASSIC:
            if expert == "FMPRating" or not _eligible(band, expert):
                continue
            for s in _strategies_for(expert):
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
# Stress WIDENS the spread the backtest already charged -- apply_spread_cost deducts this ON TOP
# of the baseline, it is not an absolute level. So to stress a genome to the MEASURED p90 the
# widening must be (p90 - median), per band (Alpaca SIP quotes, 2026-08-17):
#            median   p90    widening
#   large      2.68   7.05      4
#   mid        9.01  22.16     13
#   small     16.78  45.31     28
# A genome must therefore survive the worst decile of REAL quoted spreads. Was {10, 25, 50} (a
# round guess), and briefly {7, 22, 45} -- which was the p90 read as if it were absolute and so
# actually stressed to median+p90.
_STRESS_BAND_DEFAULTS = {"large": 4.0, "mid": 13.0, "small": 28.0}


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

_KNOWN_STRATEGIES = {"S1", "S2", "S3", "S4", "S5", "S6", "S7", "SQM"}


def load_strategy_plan(path, skip_experts=frozenset()) -> dict:
    """Read a --strategy-plan file: ``{"experts": {expert: [strategy, ...]}}``.

    Refuses, rather than guesses: a classic expert that is not skipped and has no entry (it would
    otherwise run nothing, or everything), an empty list, an unknown strategy key, a duplicate,
    and an expert the driver does not know.
    """
    with open(path, encoding="utf-8") as f:
        doc = json.load(f)
    plan = doc["experts"]
    unknown_experts = sorted(set(plan) - set(_CLASSIC) - {"FMPSenateTraderWeight"})
    if unknown_experts:
        raise SystemExit(f"--strategy-plan {path}: unknown expert(s) {unknown_experts}")
    missing = [e for e in _CLASSIC if e not in skip_experts and e not in plan]
    if missing:
        raise SystemExit(f"--strategy-plan {path}: no strategy list for {missing}; add them or "
                         f"--skip-experts them")
    out = {}
    for expert, strats in plan.items():
        if not strats:
            raise SystemExit(f"--strategy-plan {path}: {expert} has an empty strategy list; "
                             f"--skip-experts it instead")
        bad = sorted(set(strats) - _KNOWN_STRATEGIES)
        if bad or len(set(strats)) != len(strats):
            raise SystemExit(f"--strategy-plan {path}: {expert} has unknown or repeated "
                             f"strategies {strats}")
        out[expert] = list(strats)
    return out


_FAILED_JOBS: list = []   # (job name, exit code, reason) of every job that exited non-zero


_WF_STRIP = ("--start", "--end", "--name", "--generations")


def wf_optimize_args(cmd: list) -> list:
    """A cell's base ``optimize`` args for the walk-forward chain: the command the plain driver
    would run, minus the executable, ``optimize`` and the four flags the chain sets per job
    (``--start --end --name --generations``). Everything else (cap band, screener, strategy,
    spread, stress, time genes, early stop, ...) is the cell's own and rides on every job."""
    out, skip = [], False
    for tok in cmd[2:]:
        if skip:
            skip = False
            continue
        if tok in _WF_STRIP:
            skip = True
            continue
        out.append(tok)
    return out


def _wf_selection_defaults() -> dict:
    import run_walk_forward as RWF
    return RWF.selection_defaults()


def _make_wf_runner(args):
    """The real runner (ba2-test subprocess + test DB), shared by every cell."""
    import run_walk_forward as RWF
    ns = SimpleNamespace(**_wf_selection_defaults(), min_free_gb=args.wf_min_free_gb,
                         force_memory=args.wf_force_memory)
    return RWF.RealRunner(ns)


def _wf_import():
    """The walk-forward modules live in the test backend; import them lazily so an ordinary run of
    this driver never touches the backend."""
    backend = os.path.join(os.path.dirname(_TOOLS_DIR), "testplatform", "backend")
    if backend not in sys.path:
        sys.path.insert(0, backend)
    from app.services import walk_forward as WF
    from app.services import wf_chain as C
    return WF, C


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--bands", default="large,mid,small")
    ap.add_argument("--strategies", default="S1,S2,S3")  # S4 merged into S1
    ap.add_argument("--start", default="2023-01-01")
    ap.add_argument("--end", default="2026-01-01")
    # 40 -> 60 (2026-08-18). At 40, only ~25 individuals per generation are INVALID (i.e.
    # actually changed by crossover/mutation and therefore needing evaluation) -- against a
    # 16-slot fleet. The extra trials 60 brings land in slots that were already idle, so the
    # wall-clock cost is well under the +50% the trial count suggests. It also narrows an
    # existing imbalance: S1 is pinned at 140 by _STRATEGY_BUDGET_OVERRIDE, so comparing S1
    # against S2/S3 within a band was comparing a 3.5x search budget against a 1x one.
    ap.add_argument("--population", type=int, default=60)
    ap.add_argument("--generations", type=int, default=8)
    ap.add_argument("--mutation-prob", type=float, default=None,
                    help="Per-gene mutation probability passthrough (default: launcher's 0.3).")
    ap.add_argument("--strategy-plan", default=None,
                    help="JSON file mapping each classic expert to its strategy list "
                         "({\"experts\": {\"FMPRating\": [\"S1\", ...], ...}}, as written by "
                         "tools/strategy_research/atr_grid/select_strategies.py). Replaces "
                         "--strategies per expert; every classic expert that is not skipped must "
                         "have an entry. FactorRanker is unaffected.")
    ap.add_argument("--no-budget-overrides", action="store_true",
                    help="Use --population/--generations exactly for every job: no FMPRating "
                         "population bonus and no per-strategy override (S1 140, S7 60x8). For "
                         "grids whose budget is set by a rule of their own (goal2027atr).")
    ap.add_argument("--early-stop", type=int, default=None,
                    help="GA early-stop patience passthrough (default: the launcher's own, 4). "
                         "Forwarded only when given, so existing grid commands are unchanged.")
    ap.add_argument("--early-stop-min-rel", type=float, default=None,
                    help="Minimum RELATIVE gain (a fraction, 0.01 = 1%%) that resets the "
                         "early-stop patience; passthrough, forwarded only when given.")
    ap.add_argument("--rm-toggle-policy", default=None, choices=("pinned", "atr-searched"),
                    help="See ba2-test optimize --rm-toggle-policy; passthrough, forwarded only "
                         "when given, so every existing matrix command stays unchanged. "
                         "'atr-searched' requires every job name this driver builds to contain "
                         "'-atr27' -- the launcher refuses per-job otherwise.")
    # -------------------------------------------------------------------------------------------
    # goal2027atr market-condition passthrough (equity S1-S7, deferral lifted 2026-09-29). Each is
    # forwarded to `ba2-test optimize` ONLY when given -- see _market_condition_passthrough -- so
    # an invocation that passes none of these five flags builds byte-identical job names/argv to
    # before this block existed. When any IS given, _job_name below folds the job's full resolved
    # argv (minus --name/--parallel/--workers) into a digest suffix, so a flag change gets a new
    # job identity and the skip-completed-by-name check above can never reuse an old row under it.
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
                    help="Forward --market-exit to every job (equity S1-S7 only; requires "
                         "--market-condition-profile). Folds into the job's name digest.")
    ap.add_argument("--market-condition-mode", default=None, choices=("searched", "all-off"),
                    help="Forward --market-condition-mode to every job ('searched' is the "
                         "launcher default and is NOT forwarded, so it never folds into the "
                         "digest on its own -- pass 'all-off' for the matched control arm). "
                         "Folds into the job's name digest when 'all-off'.")
    ap.add_argument("--search-sl-loosen", action="store_true", default=False,
                    help="Forward --search-sl-loosen to every job. Folds into the job's name "
                         "digest.")
    ap.add_argument("--exclude-symbols", default=None, metavar="SYM,SYM,...|@file",
                    help="Forward --exclude-symbols to every job (see `ba2-test optimize "
                         "--help`), e.g. "
                         "@docs/strategy_research/atr_grid/excluded_symbols.txt to drop IAC "
                         "(FMP spin-off split-basis drift; see that file's header) from every "
                         "-atr27 S1-S7 job, treatment and all-off control alike. "
                         "Folds into the job's name digest (on its own, not only alongside a "
                         "market-condition flag) so an exclusion change can never reuse an old "
                         "job's completed row under the same name.")
    ap.add_argument("--no-fr-top-n-below-pool", dest="fr_top_n_below_pool",
                    action="store_false", default=True,
                    help="Opt OUT of --fr-top-n-below-pool (see `ba2-test optimize --help`), "
                         "which is DEFAULT ON in the launcher as of 2026-09-29 for every "
                         "bypass-expert (FactorRanker) job -- no passthrough is needed for the "
                         "default, only for this opt-out. Only a FactorRanker job is ever "
                         "affected, whatever this flag says. Folds into the job's name digest "
                         "(on its own) so an opted-out run can never resume/be confused with a "
                         "repaired run's completed row under the same name.")
    ap.add_argument("--decision-times", default=None, metavar="default|split|fixed|HH:MM,HH:MM,...",
                    help="The DECISION TIME gene (schedule:time) searched by every classic job. "
                         "DEFAULT (flag absent) = ON with the shared DEFAULT_DECISION_TIME_CHOICES "
                         "(09:35,09:40,09:45,10:00,12:00,15:30,15:45); 'fixed' = no gene, the "
                         "legacy single DEFAULT_DECISION_TIME; or a comma list on the --interval "
                         "grid. Adds '-timegene' to the job name and folds into the name digest "
                         "(new names, so nothing is skipped as already completed). Not applied "
                         "to the FactorRanker job (bypass: no schedule genes). Daily-clock "
                         "(--interval 1d) runs cannot carry it. NOTE: 'fixed' keeps the LEGACY job name, so "
                         "a job whose legacy row is already completed is SKIPPED (the skip line "
                         "says so).")
    ap.add_argument("--interval", default="5min")
    add_history_source_flag(ap)
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
    ap.add_argument("--search-sizing-mode", action="store_true", default=False,
                    help="Search sizing_mode (risk_atr | notional) as a model:sizing_mode GENE in every "
                         "classic (S1-S7) job instead of pinning it (goal2027 item 2.1). Mutually exclusive "
                         "with --sizing-mode. REQUIRES --name-suffix to contain 'sizegene'. Not forwarded "
                         "to the FactorRanker job (bypass expert); folds into the job's name digest.")
    ap.add_argument("--search-market-profile", action="store_true", default=False,
                    help="Search the market profile (off | ohlcv-v1 | ta-structure-v1 | both) as a market:profile "
                         "GENE in every classic (S1-S7) job, replacing the on/off market:enabled gene (goal2027 "
                         "item 2.2). REQUIRES --market-condition-profile ohlcv-v1,ta-structure-v1 with a manifest "
                         "for BOTH, and --name-suffix to contain 'mktprof'. Not forwarded to the FactorRanker job "
                         "(bypass expert); folds into the job's name digest.")
    ap.add_argument("--search-ds-factor-weights", action="store_true", default=False,
                    help="Search the DeterministicScorer factor genes (tw_*/fw_* sub-weights, mom_lookback_days, "
                         "the analyst target window and minimum; w_analyst widened to 0.8) in every "
                         "DeterministicScorer job (goal2027 item 2.8). REQUIRES --name-suffix to contain 'dsfw'. "
                         "Not forwarded to any other expert's job; folds into the job's name digest.")
    ap.add_argument("--search-exploration-ranges", action="store_true", default=False,
                    help="Widen the time-exit lattices and add the FMPEarningsDrift S5 time exit so every "
                         "exploration phase-1 timeout and its control is searchable (goal2027 item 2.9). REQUIRES "
                         "--name-suffix to contain 'explr'. Forwarded only to the expert/strategy pairs it applies "
                         "to (see EXPLORATION_RANGE_PAIRS); folds into the job's name digest.")
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
    ap.add_argument("--robust-fitness", dest="robust_fitness", action="store_true", default=True,
                    help="Rank every job on the ROBUSTNESS-ADJUSTED fitness (concentration + "
                         "monte carlo + spread) instead of the raw metric, so a genome whose "
                         "headline number rests on one unrepeatable winner is not selected. Both "
                         "values are stored per trial. ON BY DEFAULT since 2026-09-17 (the "
                         "launcher's own default), so this flag is now only an explicit "
                         "restatement of it.")
    ap.add_argument("--no-robust-fitness", dest="robust_fitness", action="store_false",
                    help="Rank on the RAW metric instead (the pre-2026-09-17 default) -- forwarded "
                         "to every job. Scores are NOT comparable across this setting, and the "
                         "backend REFUSES to resume a checkpoint scored under the other one, so a "
                         "changed setting needs its own --name-suffix.")
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
    ap.add_argument("--exclude-uncached", action="store_true",
                    help="Forward --screener-exclude-uncached to every job: a static-universe symbol with "
                         "no cached OHLCV (execution interval or daily) is EXCLUDED from the screen and "
                         "recorded on the run, instead of REFUSING the launch (the default). Needed for "
                         "the small band (BID, GRSD, HCAC, OGG, SHOT, TRIB, VII) and the large band "
                         "(FITB-PA) of the 2026-10-07 store until their bars are fetched.")
    ap.add_argument("--dry-run", action="store_true")
    # ----------------------------------------------------------------------------------------
    # goal2027 walk-forward (docs/plans/2026-10-25-goal2027-walk-forward-plan.md). Every flag below
    # except --walk-forward itself is REFUSED without --walk-forward, and the pass thresholds, the
    # report dir and the top-N have NO defaults: a verdict is only as honest as its thresholds.
    # ----------------------------------------------------------------------------------------
    ap.add_argument("--walk-forward", action="store_true",
                    help="Run each cell as a walk-forward chain <cell>-wf1/-wf2/-wf3 (+ -refit for "
                         "a passing cell) instead of one optimize job. See the module docstring.")
    ap.add_argument("--wf-train-start", default=None, metavar="YYYY-MM-DD",
                    help="First day every fold trains from (default 2020-01-01; an expert's data "
                         "floor, e.g. FMPRating 2022-01-01, moves it later).")
    ap.add_argument("--wf-test-years", default=None, metavar="Y,Y,..",
                    help="Test years, one fold each (default 2024,2025,2026).")
    ap.add_argument("--wf-data-end", default=None, metavar="YYYY-MM-DD",
                    help="Last day of data: clamps the LAST test window and the refit end "
                         "(rehearsal). Refused when it leaves that window empty.")
    ap.add_argument("--wf-first-generations", type=int, default=None,
                    help="Generation budget of fold 1 (cold start; default 30).")
    ap.add_argument("--wf-next-generations", type=int, default=None,
                    help="Generation budget of folds 2, 3 and the refit (default 15).")
    ap.add_argument("--wf-top-n", type=int, default=None,
                    help="Distinct top genomes per fold re-run out of sample (REQUIRED).")
    ap.add_argument("--wf-report-dir", default=None,
                    help="Directory for the per-cell reports, the stored fold reports and the grid "
                         "summary (REQUIRED).")
    ap.add_argument("--wf-test-parallel", type=int, default=None,
                    help="Out-of-sample re-runs at a time (default 1; ~14 GB RAM each).")
    ap.add_argument("--wf-min-free-gb", type=float, default=None,
                    help="Free-memory floor for the out-of-sample re-runs (default 20).")
    ap.add_argument("--wf-force-memory", action="store_true",
                    help="Ignore --wf-min-free-gb.")
    ap.add_argument("--pass-min-oos-return", type=float, default=None,
                    help="Percent: a fold's TOTAL out-of-sample return must reach this (REQUIRED).")
    ap.add_argument("--pass-min-oos-return-exclusive", action="store_true",
                    help="Strict return rule: a fold passes only if its OOS return is ABOVE "
                         "--pass-min-oos-return (default: at or above).")
    ap.add_argument("--pass-max-dd-mult", type=float, default=None,
                    help="OOS |maxDD| must be <= this x IS |maxDD| (REQUIRED).")
    ap.add_argument("--pass-min-folds", type=int, default=None,
                    help="Fold winners that must pass for the cell to pass (REQUIRED).")
    ap.add_argument("--pass-require-fold", type=int, action="append", default=[], metavar="YEAR",
                    help="A test year whose fold winner MUST pass (repeatable; the cell fails "
                         "otherwise even when --pass-min-folds is met).")
    args = ap.parse_args()

    # Walk-forward flags: required with --walk-forward, refused without it.
    _wf_opt = ("wf_train_start", "wf_test_years", "wf_data_end", "wf_first_generations",
               "wf_next_generations", "wf_top_n", "wf_report_dir", "wf_test_parallel",
               "wf_min_free_gb", "pass_min_oos_return", "pass_max_dd_mult", "pass_min_folds")
    if not args.walk_forward:
        _stray = [("--" + k.replace("_", "-")) for k in _wf_opt if getattr(args, k) is not None]
        _stray += ["--wf-force-memory"] if args.wf_force_memory else []
        _stray += ["--pass-min-oos-return-exclusive"] if args.pass_min_oos_return_exclusive else []
        _stray += ["--pass-require-fold"] if args.pass_require_fold else []
        if _stray:
            ap.error(f"{', '.join(_stray)} need --walk-forward")
    else:
        _need = [("--" + k.replace("_", "-")) for k in
                 ("wf_top_n", "wf_report_dir", "pass_min_oos_return", "pass_max_dd_mult",
                  "pass_min_folds") if getattr(args, k) is None]
        if _need:
            ap.error(f"--walk-forward requires {', '.join(_need)} (no defaults: the verdict is "
                     f"only as honest as its thresholds)")
        if args.wf_top_n < 1:
            ap.error("--wf-top-n must be >= 1")
        args.wf_train_start = args.wf_train_start or "2020-01-01"
        args.wf_test_years = args.wf_test_years or "2024,2025,2026"
        args.wf_first_generations = args.wf_first_generations or 30
        args.wf_next_generations = args.wf_next_generations or 15
        args.wf_test_parallel = args.wf_test_parallel or 1
        args.wf_min_free_gb = 20.0 if args.wf_min_free_gb is None else args.wf_min_free_gb
        try:
            args.wf_train_start_d = date.fromisoformat(args.wf_train_start)
            args.wf_data_end_d = date.fromisoformat(args.wf_data_end) if args.wf_data_end else None
            args.wf_years_l = [int(y) for y in args.wf_test_years.split(",") if y.strip()]
        except ValueError as e:
            ap.error(f"--wf-train-start/--wf-data-end/--wf-test-years: {e}")

    # A sizing-mode matrix MUST be name-distinguished from its sibling. Jobs are skipped by NAME
    # when already `completed`, so running notional and risk_atr under the same --name-suffix
    # would silently skip the entire second matrix and leave you comparing one mode against
    # itself. Fail loudly rather than let that happen hours later.
    if args.search_sizing_mode:
        if args.sizing_mode:
            ap.error(f"--search-sizing-mode and --sizing-mode {args.sizing_mode} are mutually exclusive "
                     f"(search the mode, or pin it; not both)")
        if "sizegene" not in args.name_suffix.replace("_", "").lower():
            ap.error(f"--search-sizing-mode requires --name-suffix to contain 'sizegene' "
                     f"(got {args.name_suffix!r}): its gene space differs from a pinned run's, so it "
                     f"must not share a job name. Example: --name-suffix goal2027-sizegene")
    if args.search_market_profile:
        _need = ("ohlcv-v1", "ta-structure-v1")
        _profiles = [t.strip() for t in (args.market_condition_profile or "").split(",") if t.strip()]
        if sorted(_profiles) != sorted(_need):
            ap.error(f"--search-market-profile requires --market-condition-profile {','.join(_need)} "
                     f"(both profiles; got {args.market_condition_profile!r})")
        _pinned = {}
        for _i, _tok in enumerate(t.strip() for t in (args.market_condition_manifest or "").split(",") if t.strip()):
            if "=" in _tok:
                _pinned[_tok.split("=", 1)[0].strip()] = _tok.split("=", 1)[1].strip()
            elif _i < len(_profiles):
                _pinned[_profiles[_i]] = _tok
        _missing = [p for p in _need if not _pinned.get(p)]
        if _missing:
            ap.error(f"--search-market-profile requires a --market-condition-manifest for BOTH profiles "
                     f"(none pinned for {_missing})")
        if args.market_condition_mode == "all-off":
            ap.error("--search-market-profile searches the market genes; --market-condition-mode all-off is the "
                     "no-impact control and cannot carry it")
        if "mktprof" not in args.name_suffix.replace("_", "").lower():
            ap.error(f"--search-market-profile requires --name-suffix to contain 'mktprof' "
                     f"(got {args.name_suffix!r}): its gene space differs from a market:enabled run's, so it "
                     f"must not share a job name. Example: --name-suffix goal2027-mktprof")
    if args.search_exploration_ranges:
        if "explr" not in args.name_suffix.replace("_", "").lower():
            ap.error(f"--search-exploration-ranges requires --name-suffix to contain 'explr' "
                     f"(got {args.name_suffix!r}): its gene space differs from a plain run's, so it "
                     f"must not share a job name. Example: --name-suffix goal2027-explr")
    if args.search_ds_factor_weights:
        if "dsfw" not in args.name_suffix.replace("_", "").lower():
            ap.error(f"--search-ds-factor-weights requires --name-suffix to contain 'dsfw' "
                     f"(got {args.name_suffix!r}): its gene space differs from a fixed-weight run's, so it "
                     f"must not share a job name. Example: --name-suffix goal2027-dsfw")
    if args.sizing_mode:
        token = "riskatr" if args.sizing_mode == "risk_atr" else "notional"
        if token not in args.name_suffix.replace("_", "").lower():
            ap.error(
                f"--sizing-mode {args.sizing_mode} requires --name-suffix to contain "
                f"'{token}' (got {args.name_suffix!r}). Otherwise the second matrix is skipped "
                f"as already-completed. Example: --name-suffix goal2020-{token}")

    # The decision-time gene is DEFAULT ON for this driver (shared DEFAULT_DECISION_TIME_CHOICES);
    # `--decision-times fixed` restores the legacy single-time job. Resolved ONCE and announced.
    args.decision_times_resolved, _dt_header = decision_times_plan(args.decision_times, args.interval)
    print(_dt_header, flush=True)

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
    strategy_plan = None
    if args.strategy_plan:
        strategy_plan = load_strategy_plan(args.strategy_plan, skip_experts)
        print(f"strategy plan {args.strategy_plan}: "
              + "; ".join(f"{e} {','.join(s)}" for e, s in sorted(strategy_plan.items())))
    jobs = list(_jobs(bands, strategies, args.include_no_data, skip_experts, args.name_suffix,
                      strategy_plan))
    done = _completed_names()
    print(f"matrix: {len(jobs)} jobs (bands={bands}, strategies="
          f"{'per --strategy-plan' if strategy_plan else strategies}); "
          f"{sum(1 for j in jobs if with_universe_rule_name(j[0], simulated=j[2] is not None) in done)} already completed"
          f" (by base name + universe-rule token; digest/time-gene-suffixed names are checked per job).")
    # --dry-run walks the SAME loop below and stops short of launching: a job's final name can
    # carry a digest of its resolved argv (market-condition flags), which only exists once the
    # command is built, so listing the pre-digest names here would show (and check "DONE"
    # against) names no job will ever have.
    wf_specs: list = []
    if args.walk_forward:
        WF, WFC = _wf_import()
    for i, (name, expert, strat, band) in enumerate(jobs, 1):
        # The decision-time gene reaches every classic job. The FactorRanker (bypass) job has no
        # schedule genes: it keeps its plain name and the fixed default time, announced here.
        job_dt_times = None if strat is None else args.decision_times_resolved
        if strat is None and args.decision_times_resolved:
            print(f"[{i}/{len(jobs)}] NOTE {name}: bypass expert, decision time stays fixed "
                  f"(no schedule genes)", flush=True)
        name = with_decision_times_name(name, job_dt_times,
                                        split=decision_times_split(args.decision_times))
        # The static-universe RULE is part of the job's identity (every job here is a --screener job):
        # a name without the token was launched under the old cap-ranked top-50 list and must never be
        # skipped as "completed" or resumed as this job.
        name = with_universe_rule_name(name, simulated=strat is not None)   # FactorRanker (bypass): no gate
        # The OHLCV history is part of the job's identity too (an alpaca job never resumes an fmp one).
        name = with_history_source_name(name, args.ohlcv_history_source)
        # Data-floored start (see _EXPERT_MIN_START). Announced per job so a shorter window is
        # visible in the log instead of being inferred later from a suspiciously late first trade.
        job_start = _start_for(expert, args.start)
        job_end = args.end
        if args.walk_forward:
            # The chain's window comes from the fold spec, not --start/--end. The cell's own base
            # command (and therefore its name digest) carries the chain's overall span.
            WF, WFC = _wf_import()
            _floor = _EXPERT_MIN_START.get(expert)
            try:
                wf_folds = WFC.build_cell_folds(args.wf_train_start_d, args.wf_years_l,
                                                args.wf_data_end_d,
                                                date.fromisoformat(_floor) if _floor else None)
            except WF.WalkForwardError as e:
                raise SystemExit(f"--walk-forward: {e}")
            job_start = wf_folds[0].train_start.isoformat()
            job_end = WFC.refit_window(wf_folds)[1].isoformat()
        _ref_start = args.wf_train_start if args.walk_forward else args.start
        if job_start != _ref_start:
            print(f"[{i}/{len(jobs)}] NOTE {expert}: start floored {_ref_start} -> {job_start} "
                  f"(signal data does not reach {_ref_start})", flush=True)
        # FMPRating's search space grew (price-target + analyst-recency genes), so give it extra
        # population to explore the larger space; other experts use the base --population.
        population = args.population + (args.fmp_population_bonus if expert == "FMPRating" else 0)
        generations = args.generations
        budget = _STRATEGY_BUDGET_OVERRIDE.get(strat)
        if args.no_budget_overrides:
            population, budget = args.population, None
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
               "--start", job_start, "--end", job_end, "--fitness", args.fitness,
               "--interval", args.interval, *history_source_tokens(args.ohlcv_history_source),
               "--population", str(population),
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
        if args.early_stop is not None:
            cmd += ["--early-stop", str(args.early_stop)]
        if args.early_stop_min_rel is not None:
            cmd += ["--early-stop-min-rel", str(args.early_stop_min_rel)]
        if getattr(args, "rm_toggle_policy", None):
            cmd += ["--rm-toggle-policy", args.rm_toggle_policy]
        # "Pass 0 to disable" (see the --profit-cap-pct help): a 0 must be FORWARDED, because
        # omitting the flag lets ba2test_launcher re-apply its own 2000/25 default instead.
        cmd += cap_passthrough(args)
        _stress = stress_by_band.get(band, 0.0)
        if _stress > 0:
            cmd += ["--stress-spread-bps", str(_stress)]
        if args.fitness_trade_scale:
            cmd += ["--fitness-trade-scale",
                    "--fitness-trade-scale-cap", str(args.fitness_trade_scale_cap)]
        if args.fitness_win_rate_factor:
            cmd += ["--fitness-win-rate-factor"]
        # Default-ON in the launcher: the ON case passes nothing (job names and command lines
        # unchanged) and only the opt-OUT is forwarded.
        if not args.robust_fitness:
            cmd += ["--no-robust-fitness"]
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
        # goal2027atr market-condition passthrough: [] with none of the five flags given, so an
        # ordinary invocation's cmd (and therefore its name/digest below) is byte-identical to
        # before this block existed. Appended LAST so it never displaces an existing token's
        # position for a job with no new flags.
        mc_tokens = market_condition_passthrough(args)
        cmd += mc_tokens
        # --exclude-symbols: appended after the market-condition tokens (same "never displaces an
        # existing token's position" reasoning). It ALSO triggers the digest on its own (see
        # exclude_symbols_passthrough's docstring) -- an ordinary invocation of neither this nor
        # the five market-condition flags stays byte-identical.
        excl_tokens = exclude_symbols_passthrough(args)
        cmd += excl_tokens
        # --no-fr-top-n-below-pool (the opt-out; the default needs no token): appended last (same
        # "never displaces an existing token's position" reasoning) and, like --exclude-symbols,
        # triggers the digest on its own.
        fr_tokens = fr_top_n_below_pool_passthrough(args)
        cmd += fr_tokens
        # --decision-times: appended last (same reasoning); digest on its own. (A bypass job was
        # skipped above: it cannot carry the gene.)
        dt_tokens = decision_times_tokens(job_dt_times, split=decision_times_split(args.decision_times))
        cmd += dt_tokens
        unc_tokens = ["--screener-exclude-uncached"] if args.exclude_uncached else []
        cmd += unc_tokens
        # --search-sizing-mode: appended last (same reasoning); digest on its own. A bypass job is
        # not given the gene (the launcher would refuse it).
        sz_tokens = sizing_gene_tokens(args, strat)
        cmd += sz_tokens
        if args.search_sizing_mode and strat is None:
            print(f"[{i}/{len(jobs)}] NOTE {name}: bypass expert, sizing mode gene not applied "
                  f"(never reads sizing_mode)", flush=True)
        # --search-market-profile: appended last (same reasoning); digest on its own. A bypass job is not given
        # the gene (the launcher would refuse it).
        mp_tokens = market_profile_gene_tokens(args, strat)
        cmd += mp_tokens
        if args.search_market_profile and strat is None:
            print(f"[{i}/{len(jobs)}] NOTE {name}: bypass expert, market profile gene not applied "
                  f"(carries no market genes)", flush=True)
        # --search-ds-factor-weights: appended last (same reasoning); digest on its own. Only DS jobs get it.
        ds_tokens = ds_factor_gene_tokens(args, expert)
        cmd += ds_tokens
        if args.search_ds_factor_weights and not ds_tokens:
            print(f"[{i}/{len(jobs)}] NOTE {name}: {expert} is not DeterministicScorer, DS factor genes not applied",
                  flush=True)
        # --search-exploration-ranges: appended last (same reasoning); digest on its own.
        er_tokens = exploration_range_tokens(args, expert, strat)
        cmd += er_tokens
        job_name = name
        # The walk-forward budgets and fold spec are part of the cell identity: a changed budget,
        # window, top-N or pass rule must never resume an older cell's -wf/-refit names.
        wf_tokens = ([] if not args.walk_forward else [
            "--wf-spec", args.wf_train_start, args.wf_test_years, str(args.wf_data_end),
            str(args.wf_first_generations), str(args.wf_next_generations), str(args.wf_top_n),
            str(args.pass_min_oos_return), str(args.pass_max_dd_mult), str(args.pass_min_folds),
            ",".join(map(str, sorted(set(args.pass_require_fold)))) or "-",
            "exclusive" if args.pass_min_oos_return_exclusive else "inclusive"])
        if mc_tokens or excl_tokens or fr_tokens or dt_tokens or unc_tokens or sz_tokens or mp_tokens or ds_tokens or er_tokens or wf_tokens:
            job_name = _job_name(name, cmd + wf_tokens)
            cmd[cmd.index("--name") + 1] = job_name
        if args.walk_forward:
            wf_specs.append(WFC.CellSpec(
                name=job_name, optimize_args=wf_optimize_args(cmd), launcher=exe,
                train_start=args.wf_train_start_d, test_years=args.wf_years_l,
                data_end=args.wf_data_end_d, first_generations=args.wf_first_generations,
                next_generations=args.wf_next_generations, top_n=args.wf_top_n,
                thresholds=WF.Thresholds(args.pass_min_oos_return, args.pass_max_dd_mult,
                                         args.pass_min_folds),
                require_test_years=tuple(args.pass_require_fold),
                exclusive_return=args.pass_min_oos_return_exclusive,
                test_parallel=args.wf_test_parallel, selection=_wf_selection_defaults(),
                expert=expert, strategy=strat, band=band,
                floor=date.fromisoformat(_EXPERT_MIN_START[expert])
                if expert in _EXPERT_MIN_START else None))
            try:
                WFC.make_cell_plan(wf_specs[-1])      # refuse a bad spec before anything runs
            except WF.WalkForwardError as e:
                raise SystemExit(f"--walk-forward {job_name}: {e}")
            continue
        if args.dry_run:
            print(f"  {'DONE' if job_name in done else 'TODO'}  {job_name}  "
                  f"({expert} {strat or '(bypass)'} / {band})  "
                  f"[{screener_dry_run_universe_note(args.store, band, job_start, args.end, args.interval)}]")
            continue
        if job_name in _completed_names():   # re-read each loop (resumable)
            _fixed_note = ("; NOTE --decision-times fixed keeps the legacy job name, so this is the "
                           "completed LEGACY run, not a time-gene run" if not job_dt_times else "")
            print(f"[{i}/{len(jobs)}] SKIP {job_name} (already completed{_fixed_note})", flush=True)
            continue
        print(f"[{i}/{len(jobs)}] RUN  {job_name} ...", flush=True)
        rc = subprocess.run(cmd, env=os.environ.copy()).returncode
        print(f"[{i}/{len(jobs)}] {job_name} exit={rc}", flush=True)
        note_job_exit(_FAILED_JOBS, job_name, rc)
    if args.walk_forward:
        return _run_walk_forward_cells(args, wf_specs, WF, WFC)
    return finish_matrix(_FAILED_JOBS, "matrix driver")


def _run_walk_forward_cells(args, specs, WF, WFC) -> int:
    """Dry-run: print each cell's chain. Otherwise run every cell's chain in order with ONE shared
    runner, write the per-cell and grid reports, and exit non-zero when a cell ERRORed."""
    print(f"walk-forward: {len(specs)} cell(s); reports under {args.wf_report_dir}", flush=True)
    if args.dry_run:
        for spec in specs:
            plan, folds = WFC.make_cell_plan(spec)
            print(f"\n=== cell {spec.name} ({spec.expert} {spec.strategy or '(bypass)'} / "
                  f"{spec.band}) ===")
            print(WF.render_dry_run(plan))
            print("  refit (only if the cell passes): " + " ".join(WFC.refit_command(spec, folds)))
        return 0
    runner = _make_wf_runner(args)
    cells = WFC.run_cells(specs, lambda _spec: runner, args.wf_report_dir)
    for c in cells:
        print(f"  {c['verdict']:5s} {c['cell']}" + (f"  refit={c['refit_job']}" if c["refit_job"] else "")
              + (f"  ({c['error']})" if c.get("error") else ""), flush=True)
        if c["verdict"] == "ERROR":
            note_job_exit(_FAILED_JOBS, c["cell"], 1, c["error"])
    return finish_matrix(_FAILED_JOBS, "matrix driver (walk-forward)")


if __name__ == "__main__":
    raise SystemExit(main())
