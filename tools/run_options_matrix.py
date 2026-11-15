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
  6. Expert settings fixed for OPTION jobs only (a launcher spec's ``option_fixed_settings``,
     e.g. DeterministicScorer macro_short_side=mirror since 2026-09-25) are folded into the
     discovery digest, so such a job gets a NEW name and never SKIPs on / resumes a job run
     without them. The digest reads them from the launcher the job runs (--launcher when it is a
     ba2test_launcher.py, else this checkout's). --profile matrix names carry no digest: give a
     matrix relaunch a fresh --name-suffix after changing option_fixed_settings.

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

WALK-FORWARD (--walk-forward; docs/plans/2026-11-02-goal2027-two-phase-walk-forward.md, section
"Option walk-forward"). Every (expert, structure[, neutral arm]) job becomes a CELL run as an
anchored, expanding chain by the SAME engine the stock grid uses (app.services.wf_chain +
tools/run_walk_forward.py's RealRunner; nothing is forked):

    <cell>-wf1   train 2020-01-01..2023-12-31  cold start (--wf-first-generations, default 30)  test 2024
    <cell>-wf2   train 2020-01-01..2024-12-31  seeded from wf1 (--wf-next-generations, 15)        test 2025
    <cell>-wf3   train 2020-01-01..2025-12-31  seeded from wf2                                    test 2026
    <cell>-refit train 2020-01-01..2026-12-31  seeded from wf3, PASSING cells only

Defaults mirror the stock grid's phase A (--wf-train-start 2020-01-01, --wf-test-years
2024,2025,2026); every other knob of the plain driver (profile, store, gates, fitness, ...) rides
on every job. --start/--end/--generations are REFUSED (the chain sets them per job). A cell is named
optwf-<expert>-<structure><suffix>[-<arm>-rules1]-d<digest>; the digest covers its optimize args
and the walk-forward spec, so a changed spec never resumes an older cell. Out-of-sample tests re-run
the fold's top-N behaviour-distinct genomes through ba2test_launcher._persist_top_backtests(window=)
(the GA's own decode + trial-config path), locally, one at a time by default.
THE 2026 HOLDOUT RAIL (ba2test_launcher._OPTION_HOLDOUT_START) is respected, not bypassed: a plan
whose fold TRAINS into it, or whose refit would, is refused before anything runs. Today that means:
    phase A (now):  --wf-defer-last-test --wf-data-end 2025-12-31   (wf1 and wf2 tested, wf3 trained,
                    cells PENDING)
    phase B (2027): the same command without --wf-data-end, AFTER the rail has been moved as a
                    reviewed change (the 2026 test is allowed by the rail; the 2020-2026 refit is not)
No default for the verdict: --wf-top-n, --wf-report-dir, --pass-min-oos-return, --pass-max-dd-mult
and --pass-min-folds are required. --dry-run prints every cell's chain and runs nothing.
"""
import argparse
from datetime import date, timedelta
import hashlib
import json
import math
import os
from pathlib import Path
import shlex
import subprocess
import sys
from typing import Optional

# Sibling helper in tools/ (shared by all three matrix drivers). The directory is put on the
# path explicitly so the import works however the script is reached (path, -m, or a test import).
_TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)
from matrix_flags import (  # noqa: E402
    finish_matrix, is_analysis_failure_reason, note_job_exit,
    add_refused_decision_times_flag, cap_passthrough, refuse_decision_times_and_announce,
    add_estimates_source_flag, estimates_source_of, estimates_source_tokens, with_estimates_source_name,
    add_option_history_source_flag, option_store_tokens, with_option_history_source_name,
    wf_optimize_args)
from ba2_common.core.option_spread_model import (  # noqa: E402
    LEGACY_PCT_MODEL, SPREAD_MODEL_VERSION, SPREAD_MODELS)

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
_NEUTRAL_STRUCTURES = {"O_IC", "O_STRD", "O_STRG"}
# Option keys whose builders emit NO SPY regime leaves (launcher: outside _PURE_OPTION_STRATEGIES).
_NO_SPY_GATE = {"O_CC", "O_PP", "O_STK"}


def _universe(path=_UNIVERSE_FILE) -> str:
    with open(path, encoding="utf-8") as f:
        syms = list(dict.fromkeys(s.upper() for s in f.read().split()))
    if not syms:
        raise ValueError(f"Empty options universe: {path}")
    return ",".join(syms)


def _db_path() -> str:
    return os.getenv("DB_FILE", r"C:\Users\basti\Documents\ba2\test\dl_forecasting.db")


#: Mirrors ``NO_MEASUREMENT_MARKER`` in
#: testplatform/backend/app/services/strategy_optimization_handler.py -- keep the two literals
#: identical (test_no_measurement_policy pins them together). A job that ends with ZERO MEASURED
#: trials (every trial crashed or was abandoned as stalled) is recorded as `failed` with this prefix
#: in error_message, and the campaign CONTINUES: killing the whole matrix over one job costs far more
#: than the job is worth, and a stall-only search has no result worth protecting.
NO_MEASUREMENT_MARKER = "no measured trials"


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


def _failure_reason(name: str, created_after: Optional[int] = None) -> str:
    """error_message of the newest row for ``name`` created AFTER ``created_after`` ('' if none).

    ``created_after`` is the optimization id that existed BEFORE a job was launched: only a row
    created later can speak for the attempt that just exited (2026-09-21 recheck, H4). A marker
    left by an earlier run of the same name is not evidence about this one, and treating it as
    such hid a fresh launch failure (bad args, preflight, import, startup -- all of which write no
    row at all).

    Read-only and best effort: the driver must never crash on a DB hiccup, and an unreadable reason
    simply means "treat it as an ordinary failure" (stop the campaign), which is the safe default.
    """
    import sqlite3
    path = Path(_db_path()).resolve()
    if not path.exists():
        return ""
    try:
        c = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    except Exception:  # noqa: BLE001
        return ""
    try:
        sql = "SELECT error_message FROM strategy_optimizations WHERE name=?"
        params: list = [name]
        if created_after is not None:
            # ONLY a row created after the launch that just failed can speak for it (2026-09-21
            # recheck, H4). Without this the newest row with the same NAME was used, so a marker
            # left by an earlier run hid a fresh launch failure.
            sql += " AND id > ?"
            params.append(int(created_after))
        row = c.execute(sql + " ORDER BY id DESC LIMIT 1", tuple(params)).fetchone()
    except Exception:  # noqa: BLE001
        return ""
    finally:
        c.close()
    return (row[0] or "") if row else ""


def _classify_failure(name: str, prior_max_id: Optional[int]) -> str:
    """``'skip'`` or ``'stop'`` for a job that just exited non-zero.

    ONLY a no-measurement marker on a row created AFTER the launch may skip the job (2026-09-21
    recheck, H4). Anything else stops the campaign: a marker left by an earlier run of the same
    name is not evidence about this attempt, and an unreadable table means the marker cannot be
    attributed at all -- skipping on either would hide a real launch failure (bad args, preflight,
    import, startup), none of which write a row.
    """
    if prior_max_id is None:
        return "stop"
    reason = _failure_reason(name, created_after=prior_max_id)
    return "skip" if reason.startswith(NO_MEASUREMENT_MARKER) else "stop"


def _newest_optimization_id() -> Optional[int]:
    """The highest ``strategy_optimizations.id`` right now, or ``None`` if it cannot be read.

    Captured BEFORE a job is launched so the post-mortem can require a row created after it
    (2026-09-21 recheck, H4). Read-only and best effort, like ``_failure_reason``; ``None`` means
    the driver cannot tell whether a marker belongs to the attempt that failed, and it must then
    STOP rather than skip. An empty table yields 0, which lets any new row qualify.
    """
    import sqlite3
    path = Path(_db_path()).resolve()
    if not path.exists():
        return None
    try:
        c = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    except Exception:  # noqa: BLE001
        return None
    try:
        row = c.execute("SELECT MAX(id) FROM strategy_optimizations").fetchone()
    except Exception:  # noqa: BLE001
        return None
    finally:
        c.close()
    return int(row[0]) if row and row[0] is not None else 0


def _spread_name_tag(model: str) -> str:
    """The job-NAME token for an option spread model: '' for legacy-pct, else e.g. '-spow0922'.

    Matrix-mode job names carry no identity digest and ARE the completion/checkpoint key, so
    without this a relaunch under the calibrated model would skip (or resume) every job a
    legacy-priced campaign already completed under the same name. legacy-pct keeps today's
    names exactly, so existing legacy completions still match themselves.
    """
    if model == LEGACY_PCT_MODEL:
        return ""
    import re
    m = re.fullmatch(r"([a-z]+)-\d{4}-(\d{2})-(\d{2})", model)
    return "-s" + (m.group(1) + m.group(2) + m.group(3) if m else re.sub(r"[^a-z0-9]", "", model))


def _jobs(experts, strategies, name_suffix="", prefix="optm"):
    """Yield (name, expert, strategy) in priority order (per expert: groups then equity).
    ``prefix`` is ``optm`` for plain jobs and ``optwf`` for walk-forward cells."""
    for expert in experts:
        for s in strategies:
            yield (f"{prefix}-{expert}-{s}{name_suffix}", expert, s)


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
    # iv-v1 chain-quality overrides: forwarded ONLY when stated, so every existing argv is unchanged.
    for flag, attr in (("--iv-quality-max-run", "iv_quality_max_run"), ("--iv-quality-max-rate", "iv_quality_max_rate"),
                       ("--iv-quality-accept", "iv_quality_accept")):
        if getattr(args, attr, None) is not None:
            out += [flag, str(getattr(args, attr))]
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
    ap.add_argument("--neutral-entry-modes", default="legacy",
                    help="Discovery only: joint evolves HOLD vs low-confidence in one job; "
                         "hold,low_confidence runs two separate jobs for each "
                         "neutral structure. Other structures retain their existing identities.")
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
                         "that far back, which is why the store below defaults to thetadata). "
                         "Refused with --walk-forward (use --wf-train-start).")
    ap.add_argument("--options-store", default="thetadata",
                    help="Options store serving the run, forwarded to the launcher per job "
                         "(default thetadata -- floor 2018-09-14, the only vendor reaching a "
                         "2020 start; tastytrade floors at 2022-10-01, alpaca at 2024-01-18).")
    ap.add_argument("--ohlcv-history-source", choices=("alpaca", "fmp"), default=None,
                    help="OHLCV history of the underlyings, forwarded to the launcher per job only "
                         "when given (absent: the launcher's own default, fmp for option jobs, and "
                         "every existing job name/digest is unchanged). Stage 1 ran without it "
                         "(fmp); its re-runs on the Alpaca cache pass alpaca.")
    ap.add_argument("--option-spread-model", default=SPREAD_MODEL_VERSION, choices=SPREAD_MODELS,
                    help="Option fill spread model, forwarded to the launcher per job (default "
                         f"{SPREAD_MODEL_VERSION}: the decision bar's real quote, else the "
                         "calibrated fallback). ALWAYS passed, so it is part of the discovery "
                         "identity digest: a job priced by a different spread model can neither "
                         "skip against nor resume from one priced by this one. Matrix-mode job "
                         "names carry no digest, so the model is folded into the NAME instead "
                         "(e.g. -spow0922; nothing for legacy-pct).")
    ap.add_argument("--option-size-within-fill-volume", action="store_true", default=False,
                    help="Forward --option-size-within-fill-volume to every job (plan "
                         "2026-09-24 Task 11): opening option orders are sized at order time "
                         "to what the fill engine's volume cap can fill. Off by default so "
                         "existing job names/commands are unchanged; a digest token when on, so "
                         "a sized run never shares a name (checkpoint) with an unsized one. "
                         "tools/stage1_run.sh passes it.")
    ap.add_argument("--benchmark-regime-gates", action="store_true", default=False,
                    help="Forward --benchmark-regime-gates to every job: append the SPY trend / "
                         "vol regime entry gates (two categorical mode genes per member) to each "
                         "option structure. Option structures only (O_CC/O_PP/O_STK and equity "
                         "keys are refused). Off by default so existing job names/commands are "
                         "unchanged; a digest token when on, so a SPY-gated run never shares a "
                         "name (checkpoint) with an ungated one.")
    ap.add_argument("--end", default="2025-12-31",
                    help="Backtest end (default 2025-12-31: 2026 is the reserved "
                         "walk-forward holdout and the launcher refuses to search into it). "
                         "Refused with --walk-forward (use --wf-test-years / --wf-data-end).")
    ap.add_argument("--population", type=int, default=None)
    ap.add_argument("--generations", type=int, default=None)
    ap.add_argument("--early-stop", type=int, default=None,
                    help="Generations without improvement before a job stops (spec stage 1: 8). "
                         "Omitted -> launcher default.")
    ap.add_argument("--early-stop-min-rel", type=float, default=None,
                    help="Minimum RELATIVE improvement (a fraction in (0, 1): 0.01 = 1%%) that "
                         "resets the --early-stop patience (launcher --early-stop-min-rel). "
                         "Omitted -> nothing is forwarded and the legacy rule applies (any strict "
                         "improvement resets), so every existing job name is unchanged. When "
                         "given it is a discovery-digest token: a job under a different stopping "
                         "rule gets its own name, and so never skips on or resumes the other's.")
    ap.add_argument("--mutation-prob", type=float, default=None,
                    help="Per-gene mutation probability passthrough (default: launcher's).")
    ap.add_argument("--seed", type=int, default=42,
                    help="Random seed, included in discovery job identity; vary for stability pilots.")
    ap.add_argument("--labels", default="",
                    help="Comma-separated labels stamped on every persisted Top-N backtest "
                         "of every job (forwarded to the launcher's --labels). NOT part of "
                         "the discovery identity digest, so labelling a campaign does not "
                         "rename its jobs or orphan their checkpoints.")
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
    ap.add_argument("--iv-quality-max-run", type=int, default=None,
                    help="iv-v1: forwarded to every job (launcher --iv-quality-max-run); default the launcher's")
    ap.add_argument("--iv-quality-max-rate", type=float, default=None,
                    help="iv-v1: forwarded to every job (launcher --iv-quality-max-rate)")
    ap.add_argument("--iv-quality-accept", default=None, metavar="SYM[,SYM...]",
                    help="iv-v1: symbols whose chain-quality breach is accepted (launcher --iv-quality-accept)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--screener-gate-store", default=None,
                    help="Attach this parquet metric store as a GATE-ONLY per-bar entry gate on "
                         "EVERY job (passes --screener-gate-store/--max-stock-price through to "
                         "ba2-test optimize). The store must cover the options universe.")
    ap.add_argument("--max-stock-price", type=float, default=100.0,
                    help="Max underlying price for the gate-only entry gate (default 100 — the "
                         "$20k-account cap). 0 disables the price filter.")
    add_walk_forward_flags(ap)
    return ap


# ------------------------------------------------------------------------------------------------
# Walk-forward mode (see the module docstring). The flag names and their meaning are the stock
# grid's (tools/run_screener_capband_matrix.py), so one runbook covers both; every flag below except
# --walk-forward itself is REFUSED without --walk-forward, and the verdict's inputs have NO default.
# ------------------------------------------------------------------------------------------------

#: Valued walk-forward flags (dest names): None = not given.
_WF_VALUED = ("wf_train_start", "wf_test_years", "wf_data_end", "wf_first_generations",
              "wf_next_generations", "wf_top_n", "wf_report_dir", "wf_test_parallel",
              "wf_min_free_gb", "wf_min_trade_rows", "pass_min_oos_return", "pass_max_dd_mult",
              "pass_max_oos_dd", "wf_name_pass_max_dd_mult", "pass_min_folds")
_WF_SWITCHES = ("wf_defer_last_test", "wf_run_all_folds", "wf_force_memory",
                "pass_min_oos_return_exclusive")
_WF_REQUIRED = ("wf_top_n", "wf_report_dir", "pass_min_oos_return", "pass_max_dd_mult",
                "pass_min_folds")
#: The stock grid's phase-A fold layout (docs/plans/2026-11-02-goal2027-two-phase-walk-forward.md).
#: ThetaData and Alpha Vantage both reach 2020-01-01, so the option data window does not force
#: another layout; a vendor with a later floor is refused by the launcher at fold 1's launch.
WF_DEFAULT_TRAIN_START = "2020-01-01"
WF_DEFAULT_TEST_YEARS = "2024,2025,2026"
WF_DEFAULT_FIRST_GENERATIONS = 30
WF_DEFAULT_NEXT_GENERATIONS = 15


def add_walk_forward_flags(ap) -> None:
    ap.add_argument("--walk-forward", action="store_true",
                    help="Run each job as a walk-forward CELL <cell>-wf1/-wf2/-wf3 (+ -refit for a "
                         "passing cell) instead of one optimize job. See the module docstring.")
    ap.add_argument("--wf-train-start", default=None, metavar="YYYY-MM-DD",
                    help=f"First day every fold trains from (default {WF_DEFAULT_TRAIN_START}).")
    ap.add_argument("--wf-test-years", default=None, metavar="Y,Y,..",
                    help=f"Test years, one fold each (default {WF_DEFAULT_TEST_YEARS}).")
    ap.add_argument("--wf-data-end", default=None, metavar="YYYY-MM-DD",
                    help="Last day of data: clamps the LAST test window and the refit end, or, with "
                         "--wf-defer-last-test and a date before the last test year, trains that "
                         "fold now and tests it later.")
    ap.add_argument("--wf-defer-last-test", action="store_true",
                    help="Two-phase year (stock phase A/B): with a --wf-data-end BEFORE the last test "
                         "year, train the last fold now and test it later (cell PENDING). Rerun the "
                         "SAME command without --wf-data-end once the data exists. Keep the flag on "
                         "both runs (it is part of the cell name; the data end is not).")
    ap.add_argument("--wf-run-all-folds", action="store_true",
                    help="Run every fold even after the cell can no longer pass (default: stop, FAIL).")
    ap.add_argument("--wf-first-generations", type=int, default=None,
                    help=f"Generation budget of fold 1 (cold start; default "
                         f"{WF_DEFAULT_FIRST_GENERATIONS}).")
    ap.add_argument("--wf-next-generations", type=int, default=None,
                    help=f"Generation budget of the seeded folds and the refit (default "
                         f"{WF_DEFAULT_NEXT_GENERATIONS}).")
    ap.add_argument("--wf-top-n", type=int, default=None,
                    help="Behaviour-distinct top genomes per fold re-run out of sample (REQUIRED).")
    ap.add_argument("--wf-report-dir", default=None,
                    help="Directory for the per-cell reports, the stored fold reports and the grid "
                         "summary (REQUIRED).")
    ap.add_argument("--wf-test-parallel", type=int, default=None,
                    help="Out-of-sample re-runs at a time (default 1).")
    ap.add_argument("--wf-min-free-gb", type=float, default=None,
                    help="Free-memory floor for the out-of-sample re-runs (default 20; Linux only).")
    ap.add_argument("--wf-force-memory", action="store_true", help="Ignore --wf-min-free-gb.")
    ap.add_argument("--wf-min-trade-rows", type=int, default=None,
                    help="Selection floor: a genome with fewer IN-SAMPLE round-trip rows (one per "
                         "option LEG, the engine's total_trades) over the fold's train window is not "
                         "tested. Default: tools/persist_distinct_topn.py's own default (30), the "
                         "value the stock grid uses. Not part of the cell name (the stored fold "
                         "reports re-evaluate when it changes).")
    ap.add_argument("--pass-min-oos-return", type=float, default=None,
                    help="Percent: a fold's TOTAL out-of-sample return must reach this (REQUIRED).")
    ap.add_argument("--pass-min-oos-return-exclusive", action="store_true",
                    help="Strict return rule: a fold passes only ABOVE --pass-min-oos-return.")
    ap.add_argument("--pass-max-dd-mult", type=float, default=None,
                    help="OOS |maxDD| must be <= this x IS |maxDD| (REQUIRED).")
    ap.add_argument("--pass-max-oos-dd", type=float, default=None,
                    help="Percent, optional: OOS |maxDD| must ALSO be <= this absolute cap.")
    ap.add_argument("--wf-name-pass-max-dd-mult", type=float, default=None,
                    help="Name the cells as if --pass-max-dd-mult were this value (a pass rule "
                         "changed mid-grid keeps the cells' names and completed jobs).")
    ap.add_argument("--pass-min-folds", type=int, default=None,
                    help="Fold winners that must pass for the cell to pass (REQUIRED).")
    ap.add_argument("--pass-require-fold", type=int, action="append", default=[], metavar="YEAR",
                    help="A test year whose fold winner MUST pass (repeatable).")


def _wf_import():
    """The walk-forward engine lives in the test backend; imported lazily so a plain run of this
    driver never touches the backend."""
    backend = os.path.join(os.path.dirname(_TOOLS_DIR), "testplatform", "backend")
    if backend not in sys.path:
        sys.path.insert(0, backend)
    from app.services import walk_forward as WF
    from app.services import wf_chain as C
    return WF, C


def _resolve_walk_forward(ap, args, explicit_job_window) -> None:
    """Validate the walk-forward flags and, with --walk-forward, build the fold plan.

    Sets ``args.wf_folds`` / ``args.wf_deferred`` (the fold the run trains but does not test) and
    gives ``args.start`` / ``args.end`` the chain's overall span (train start .. refit end): the
    base command of a cell carries it, like the stock cells, and wf_optimize_args strips it."""
    if not args.walk_forward:
        stray = [("--" + k.replace("_", "-")) for k in _WF_VALUED if getattr(args, k) is not None]
        stray += [("--" + k.replace("_", "-")) for k in _WF_SWITCHES if getattr(args, k)]
        stray += ["--pass-require-fold"] if args.pass_require_fold else []
        if stray:
            ap.error(f"{', '.join(stray)} need --walk-forward")
        return
    if explicit_job_window:
        ap.error(f"--walk-forward: {', '.join(explicit_job_window)} cannot be given: the chain sets "
                 f"each job's window and budget (use --wf-train-start, --wf-test-years, "
                 f"--wf-data-end, --wf-first-generations, --wf-next-generations)")
    need = [("--" + k.replace("_", "-")) for k in _WF_REQUIRED if getattr(args, k) is None]
    if need:
        ap.error(f"--walk-forward requires {', '.join(need)} (no defaults: the verdict is only as "
                 f"honest as its thresholds)")
    for key in ("wf_top_n", "wf_first_generations", "wf_next_generations", "wf_test_parallel"):
        value = getattr(args, key)
        if value is not None and value < 1:
            ap.error(f"--{key.replace('_', '-')} must be >= 1")
    if args.wf_min_trade_rows is not None and args.wf_min_trade_rows < 0:
        ap.error("--wf-min-trade-rows must be >= 0")
    if args.wf_train_start is None:
        args.wf_train_start = WF_DEFAULT_TRAIN_START
    if args.wf_test_years is None:
        args.wf_test_years = WF_DEFAULT_TEST_YEARS
    if args.wf_first_generations is None:
        args.wf_first_generations = WF_DEFAULT_FIRST_GENERATIONS
    if args.wf_next_generations is None:
        args.wf_next_generations = WF_DEFAULT_NEXT_GENERATIONS
    if args.wf_test_parallel is None:
        args.wf_test_parallel = 1
    if args.wf_min_free_gb is None:
        args.wf_min_free_gb = 20.0
    try:
        args.wf_train_start_d = date.fromisoformat(args.wf_train_start)
        args.wf_data_end_d = date.fromisoformat(args.wf_data_end) if args.wf_data_end else None
        args.wf_years_l = [int(y) for y in args.wf_test_years.split(",") if y.strip()]
    except ValueError as e:
        ap.error(f"--wf-train-start/--wf-data-end/--wf-test-years: {e}")
    WF, C = _wf_import()
    try:
        args.wf_folds = C.build_cell_folds(args.wf_train_start_d, args.wf_years_l,
                                           args.wf_data_end_d, None, args.wf_defer_last_test)
    except WF.WalkForwardError as e:
        ap.error(f"--walk-forward: {e}")
    args.wf_deferred = C.deferred_fold(args.wf_folds, args.wf_data_end_d, args.wf_defer_last_test)
    args.start = args.wf_folds[0].train_start.isoformat()
    args.end = C.refit_window(args.wf_folds)[1].isoformat()


def wf_holdout_refusal(folds, deferred, holdout: date) -> Optional[str]:
    """Why this plan would run a job into the option holdout (``holdout`` =
    ``ba2test_launcher._OPTION_HOLDOUT_START`` of the launcher the jobs run), or None.

    The rail guards every optimize job of a pure-option structure; this driver has always kept ALL
    its jobs (the O_CC/O_PP/O_STK overlays too) out of the holdout. A fold whose TRAIN window reaches
    it is refused; so is a plan that can REFIT in this run (no deferred last fold), because the
    refit trains up to the last test end. Out-of-sample TESTS inside the holdout are allowed: they
    are single re-runs, which the rail deliberately does not guard -- testing on the holdout is its
    purpose. Refused here, before anything runs, rather than by the launcher after the folds ran."""
    for f in folds:
        if f.train_end >= holdout:
            return (f"fold {f.index} trains to {f.train_end}, inside the reserved option holdout "
                    f"(from {holdout}); test years after the holdout start cannot be trained for")
    if deferred is None and folds[-1].test_end >= holdout:
        return (f"a passing cell's refit would train {folds[0].train_start}..{folds[-1].test_end}, "
                f"into the reserved option holdout (from {holdout}), which the launcher refuses. "
                f"Now (phase A): add --wf-defer-last-test --wf-data-end "
                f"{(holdout - timedelta(days=1)).isoformat()} (the last fold is trained, its test "
                f"and the refit wait). Phase B: move _OPTION_HOLDOUT_START in ba2test_launcher.py as "
                f"a reviewed change first")
    return None


def _explicit_flags(ap, argv, dests) -> list:
    """The ``--<dest>`` flags among ``dests`` the command line GAVE (whatever their value, even
    the default's): ``argv`` is parsed once more with those defaults set to None, then the
    parser's own defaults are restored. Abbreviations and ``--flag=value`` are argparse's."""
    saved = {d: ap.get_default(d) for d in dests}
    ap.set_defaults(**{d: None for d in dests})
    try:
        probe = ap.parse_args(argv)
    finally:
        ap.set_defaults(**saved)
    return ["--" + d.replace("_", "-") for d in dests if getattr(probe, d) is not None]


def resolve_args(ap, argv=None):
    args = ap.parse_args(argv)
    # --walk-forward refuses an explicit job window / budget (the chain sets them per job).
    explicit_job_window = _explicit_flags(ap, argv, ("start", "end", "generations")) \
        if args.walk_forward else []
    discovery = args.profile == "discovery"
    modes = [m.strip() for m in args.neutral_entry_modes.split(",")]
    if (not modes or len(modes) != len(set(modes))
            or set(modes) - {"legacy", "hold", "low_confidence", "joint"}
            or (set(modes) & {"legacy", "joint"} and len(modes) > 1)):
        ap.error("--neutral-entry-modes must be legacy, hold, low_confidence, hold,low_confidence or joint")
    if modes != ["legacy"] and not discovery:
        ap.error("--neutral-entry-modes requires --profile discovery")
    args.neutral_entry_modes = ",".join(modes)
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
    if getattr(args, "benchmark_regime_gates", False):
        # The launcher refuses it per job for any other key; refusing here keeps it off the
        # job names and out of --dry-run listings too.
        bad = [k for k in args.strategies.split(",") if not k.startswith("O") or k in _NO_SPY_GATE]
        if bad:
            ap.error(f"--benchmark-regime-gates: {bad} emit no SPY regime gates (option "
                     f"structures only; O_CC/O_PP/O_STK are not gated). Pass --strategies "
                     f"explicitly (the discovery default list includes O_CC/O_PP)")
    _resolve_walk_forward(ap, args, explicit_job_window)
    try:
        start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)
    except ValueError:
        ap.error("--start and --end must be ISO dates (YYYY-MM-DD)")
    if start > end:
        ap.error("--start must not be after --end")
    # Scope to this option-grid driver, including the equity-entry overlays/control.
    # Do not extend the backend's pure-option rail into unrelated equity optimizations.
    # (Walk-forward: args.end is the chain's span, not a search window; every job of the chain is
    # checked against the launcher's rail by wf_holdout_refusal instead.)
    if not args.walk_forward and end >= date(2026, 1, 1):
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
    if args.early_stop_min_rel == 0:
        ap.error("--early-stop-min-rel 0 is the legacy rule under new job names; omit the flag "
                 "for the legacy rule")
    if args.early_stop_min_rel is not None and (not math.isfinite(args.early_stop_min_rel)
                                               or not 0 < args.early_stop_min_rel < 1):
        ap.error("--early-stop-min-rel must be finite and in (0, 1) (a fraction: 0.01 means 1%)")
    if args.mutation_prob is not None and (not math.isfinite(args.mutation_prob)
                                          or not 0 <= args.mutation_prob <= 1):
        ap.error("--mutation-prob must be in [0, 1]")
    return args


def build_cmd(args, launcher, name, expert, strat, universe, neutral_entry_mode="legacy"):
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
        *option_store_tokens(args),
        # EXPLICIT per job for the same reason, and so the spread model is in the job identity
        # (discovery_name digests these args): the launcher's own default is a code constant
        # that a later model version would change silently under an unchanged job name.
        "--option-spread-model", args.option_spread_model]
    if args.labels:
        # The STRUCTURE is appended per job, because one --labels string cannot vary across
        # the 16 jobs a campaign launches and the structure is the one thing that does. The
        # EXPERT is deliberately NOT added: backtests.expert_name is already an indexed
        # column, so a label would duplicate it; there is no column for the structure.
        labels = [t.strip() for t in args.labels.split(",") if t.strip()]
        if strat not in labels:
            labels.append(strat)
        cmd += ["--labels", ",".join(labels)]
    cmd += _gate_passthrough(args)
    cmd += _market_condition_passthrough(args)
    if neutral_entry_mode != "legacy":
        cmd += ["--neutral-entry-mode", neutral_entry_mode]
    # --early-stop-min-rel is forwarded only when given (None -> no token), so every command --
    # and therefore every discovery digest -- made without it is unchanged.
    for field, flag in (("fitness", "--fitness"), ("early_stop", "--early-stop"),
                        ("early_stop_min_rel", "--early-stop-min-rel"),
                        ("mutation_prob", "--mutation-prob"), ("equity_cap", "--equity-cap"),
                        ("ohlcv_history_source", "--ohlcv-history-source")):
        value = getattr(args, field)
        if value is not None:
            cmd += [flag, str(value)]
    cmd += estimates_source_tokens(estimates_source_of(args, __file__))
    cmd += cap_passthrough(args)
    if args.fitness_trade_scale:
        cmd += ["--fitness-trade-scale", "--fitness-trade-scale-cap", str(args.fitness_trade_scale_cap),
                "--fitness-trade-scale-target", str(args.fitness_trade_scale_target)]
    if args.fitness_win_rate_factor:
        cmd += ["--fitness-win-rate-factor"]
    # Task 11 order-time sizing: forwarded only when on, so every existing command is unchanged.
    if getattr(args, "option_size_within_fill_volume", False):
        cmd += ["--option-size-within-fill-volume"]
    # Robustness is DEFAULT-ON in the launcher, so the ON case passes nothing (every existing
    # job name is unchanged) and only the opt-OUT is forwarded -- and it is a digest token, so a
    # raw-ranked run cannot share a name, and therefore a checkpoint, with a robust one.
    # SPY regime gates: forwarded only when on, so every existing command and digest is unchanged.
    if getattr(args, "benchmark_regime_gates", False):
        cmd += ["--benchmark-regime-gates"]
    if not args.robust_fitness:
        cmd += ["--no-robust-fitness"]
    if args.workers:
        cmd += ["--workers", args.workers]
    return cmd


_CHECKOUT_LAUNCHER = os.path.join(os.path.dirname(_TOOLS_DIR), "testplatform", "ba2test_launcher.py")
_LAUNCHER_MODULES: dict = {}


def _launcher_module(launcher):
    """The launcher module whose expert specs the job will actually run with.

    A --launcher naming a ``ba2test_launcher.py`` (a worktree's, or the remote grid checkout's)
    IS that code; anything else (the installed ``ba2-test`` entry point) runs this checkout's.
    Loaded once per path -- import is cheap (the backend is imported lazily by the commands).
    """
    import importlib.util
    path = os.path.abspath(launcher if os.path.basename(launcher) == "ba2test_launcher.py"
                           else _CHECKOUT_LAUNCHER)
    if path not in _LAUNCHER_MODULES:
        spec = importlib.util.spec_from_file_location(
            f"_rom_launcher_{len(_LAUNCHER_MODULES)}", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _LAUNCHER_MODULES[path] = mod
    return _LAUNCHER_MODULES[path]


def option_fixed_settings(launcher, expert, strat):
    """The expert settings the launcher pins for THIS option job (``{}`` for most experts).

    Same function the launcher applies (``_option_fixed_settings_for``), so the name and the run
    cannot disagree. An expert the launcher does not know yields {}: the launcher refuses such a
    job outright, so there is no run whose identity could be mixed up.
    """
    mod = _launcher_module(launcher)
    spec = mod._EXPERT_OPT.get(expert)
    if spec is None:
        return {}
    return mod._option_fixed_settings_for(spec, strat)


def discovery_name(args, launcher, name, expert, strat, universe, neutral_entry_mode="legacy"):
    """Version the experiment, not the machine's consumer count or selected job subset.

    Name is also the backend checkpoint key. Changed window/seed/capital/GA knobs
    must neither skip an older completion nor resume its incompatible experiment.
    Cache contents/code changes at the same paths still require a fresh name suffix.
    """
    cmd = build_cmd(args, launcher, "", expert, strat, universe, neutral_entry_mode)
    # --estimates-source fmp / --option-history-source thetadata: the legacy implicit choices, kept
    # out of the experiment identity so every job from before the flags keeps its name.
    tokens = _drop_legacy_implicit(cmd[cmd.index("optimize") + 1:])
    identity = {"schema": 1, "args": _identity_config(tokens),
                "launcher": os.path.abspath(launcher),
                "store": {key: os.environ.get(key) for key in _IDENTITY_ENV_KEYS}}
    # Settings the launcher fixes for option jobs only are not on the command line, so they are
    # folded in explicitly -- and ONLY when present, so every job without them (every FMPRating
    # job, every pre-existing name) keeps its digest byte for byte.
    fixed = option_fixed_settings(launcher, expert, strat)
    if fixed:
        identity["option_fixed_settings"] = fixed
    digest = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:12]
    return f"{name}-d{digest}"


#: The environment that chooses or prices a job's option data (part of every digest identity).
_IDENTITY_ENV_KEYS = ("BA2_HOME", "BACKTEST_OPTIONS_STORE", "BACKTEST_OPTIONS_PARQUET_ROOT",
                      "BACKTEST_OPTIONS_RISK_FREE_RATE", "TASTYTRADE_OPTIONS_HISTORY_FLOOR")
#: Of those, the ones that only say WHERE the data lives (masked under --wf-defer-last-test).
_LOCATION_ENV_KEYS = ("BA2_HOME", "BACKTEST_OPTIONS_PARQUET_ROOT")
_BOOLEAN_FLAGS = ("--fitness-trade-scale", "--fitness-win-rate-factor", "--no-robust-fitness",
                  "--option-size-within-fill-volume", "--benchmark-regime-gates")


def _drop_legacy_implicit(tokens: list) -> list:
    """--estimates-source fmp and --option-history-source thetadata are the legacy implicit
    choices: kept out of every identity so every job from before the flags keeps its name."""
    tokens = list(tokens)
    for flag, legacy in (("--estimates-source", "fmp"), ("--option-history-source", "thetadata")):
        for _i in range(len(tokens) - 1):
            if tokens[_i] == flag and tokens[_i + 1] == legacy:
                tokens = tokens[:_i] + tokens[_i + 2:]
                break
    return tokens


def _identity_config(tokens: list) -> dict:
    """The search configuration an ``optimize`` arg list states, as {flag: value}. --labels is
    METADATA, not search configuration: it must not move the job identity, or adding a label
    would rename every job and orphan its GA checkpoint. Same reasoning as --name/--parallel/
    --workers."""
    config = {}
    i = 0
    while i < len(tokens):
        flag = tokens[i]
        if flag in _BOOLEAN_FLAGS:
            config[flag] = True
            i += 1
        else:
            if flag not in ("--name", "--parallel", "--workers", "--labels"):
                config[flag] = tokens[i + 1]
            i += 2
    return config


#: Values that say WHICH data a cell reads (the market-condition snapshots and the universe list).
#: Under --wf-defer-last-test they are not part of the cell name, exactly as in the stock grid
#: (run_screener_capband_matrix._DATA_FLAGS): January's rebuilt data must find phase A's jobs, and
#: the deferred tests read the current values (ba2test_launcher._rebase_run_data, which rebases
#: precisely these two for a static-universe run). NOT --screener-gate-store: the rebase does not
#: replace it, so a test would silently read the TRAIN job's gate store; a new gate-store path is
#: therefore a new cell (refresh the store in place to keep the names).
_WF_DEFER_MASKED_FLAGS = ("--market-condition-manifest", "--universe")


def wf_spec_tokens(args) -> list:
    """The walk-forward spec of a cell's NAME: the stock grid's tokens, in its order. A changed
    budget, window, top-N or pass rule must never resume an older cell's jobs; under
    --wf-defer-last-test the data end is NOT part of it (phase B must find phase A's jobs)."""
    return ["--wf-spec", args.wf_train_start, args.wf_test_years,
            "defer" if args.wf_defer_last_test else str(args.wf_data_end),
            str(args.wf_first_generations), str(args.wf_next_generations), str(args.wf_top_n),
            str(args.pass_min_oos_return),
            str(args.pass_max_dd_mult if args.wf_name_pass_max_dd_mult is None
                else args.wf_name_pass_max_dd_mult),
            str(args.pass_min_folds),
            ",".join(map(str, sorted(set(args.pass_require_fold)))) or "-",
            "exclusive" if args.pass_min_oos_return_exclusive else "inclusive"]


def wf_cell_name(args, launcher, name, expert, strat, cmd) -> str:
    """``name`` (``optwf-...``) + ``-d<digest>`` of the cell's identity: its walk-forward base
    args (``wf_optimize_args``: no window, name or generation budget), the store environment, the
    launcher, the option-only fixed settings and the walk-forward spec. Under
    --wf-defer-last-test the data values and locations (manifests, universe, BA2_HOME, parquet
    root, launcher path) are masked so the phase-B run, on new data, rebuilds every name."""
    tokens = _drop_legacy_implicit(wf_optimize_args(cmd))
    defer = args.wf_defer_last_test
    if defer:
        tokens = [("<data>" if k and tokens[k - 1] in _WF_DEFER_MASKED_FLAGS else t)
                  for k, t in enumerate(tokens)]
    identity = {"schema": "optwf-1", "args": _identity_config(tokens),
                "store": {key: ("<location>" if defer and key in _LOCATION_ENV_KEYS
                                else os.environ.get(key)) for key in _IDENTITY_ENV_KEYS},
                "launcher": "<location>" if defer else os.path.abspath(launcher),
                "wf": wf_spec_tokens(args)}
    fixed = option_fixed_settings(launcher, expert, strat)
    if fixed:
        identity["option_fixed_settings"] = fixed
    digest = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:12]
    return f"{name}-d{digest}"


def _planned_arms(args, experts, strategies, prefix):
    """(base name, expert, structure, neutral entry mode) of every job/cell, neutral arms split."""
    suffix = _spread_name_tag(args.option_spread_model) + args.name_suffix
    suffix += with_option_history_source_name('', args)   # '' for thetadata; '-optalphavantage' otherwise
    for name, expert, strategy in _jobs(experts, strategies, suffix, prefix):
        modes = args.neutral_entry_modes.split(",") if strategy in _NEUTRAL_STRUCTURES else ["legacy"]
        for mode in modes:
            yield (name if mode == "legacy" else name + "-" + mode + "-rules1"), expert, strategy, mode


def planned_jobs(args, launcher, experts, strategies, universe):
    """Separate neutral arms without changing any other job/checkpoint identity."""
    for arm_name, expert, strategy, mode in _planned_arms(args, experts, strategies, "optm"):
        if args.profile == "discovery":
            arm_name = discovery_name(args, launcher, arm_name, expert, strategy, universe, mode)
        yield arm_name, expert, strategy, mode


_FAILED_JOBS: list = []   # (job name, exit code, reason) of every job that exited non-zero


def _wf_selection(args) -> dict:
    """The selection knobs every fold uses: tools/persist_distinct_topn.py's declared defaults
    (read from its parser by run_walk_forward.selection_defaults, as the stock grid does), with
    --wf-min-trade-rows replacing the trade-row floor when given."""
    import run_walk_forward as RWF
    selection = dict(RWF.selection_defaults())
    if args.wf_min_trade_rows is not None:
        selection["min_trade_rows"] = args.wf_min_trade_rows
    return selection


def _make_wf_runner(args, selection):
    """The real runner (ba2-test subprocess + test DB), shared by every cell. Its selection knobs
    are the ones recorded in the cell specs (and so in every stored fold report's key)."""
    from types import SimpleNamespace

    import run_walk_forward as RWF
    return RWF.RealRunner(SimpleNamespace(**selection, min_free_gb=args.wf_min_free_gb,
                                          force_memory=args.wf_force_memory))


def wf_cell_specs(args, launcher, experts, strategies, universe, selection) -> list:
    """One ``wf_chain.CellSpec`` per (expert, structure, neutral arm), built from the SAME
    ``build_cmd`` as a plain job; each spec's plan is validated here, before anything runs."""
    WF, C = _wf_import()
    specs = []
    for base, expert, strat, mode in _planned_arms(args, experts, strategies, "optwf"):
        cmd = build_cmd(args, launcher, base, expert, strat, universe, mode)
        name = wf_cell_name(args, launcher, base, expert, strat, cmd)
        spec = C.CellSpec(
            name=name, optimize_args=wf_optimize_args(cmd), launcher=cmd[:cmd.index("optimize")],
            train_start=args.wf_train_start_d, test_years=args.wf_years_l,
            data_end=args.wf_data_end_d, first_generations=args.wf_first_generations,
            next_generations=args.wf_next_generations, top_n=args.wf_top_n,
            thresholds=WF.Thresholds(args.pass_min_oos_return, args.pass_max_dd_mult,
                                     args.pass_min_folds, args.pass_max_oos_dd),
            require_test_years=tuple(args.pass_require_fold),
            exclusive_return=args.pass_min_oos_return_exclusive,
            defer_last_test=args.wf_defer_last_test, run_all_folds=args.wf_run_all_folds,
            test_parallel=args.wf_test_parallel, selection=dict(selection),
            expert=expert, strategy=strat, band="options" if mode == "legacy" else f"options/{mode}",
            floor=None)
        try:
            C.make_cell_plan(spec)
        except WF.WalkForwardError as e:
            raise SystemExit(f"--walk-forward {name}: {e}")
        specs.append(spec)
    return specs


def _run_walk_forward(ap, args, launcher, experts, strategies, universe) -> int:
    """--walk-forward: every job becomes a cell run by app.services.wf_chain (the stock grid's
    engine) with tools/run_walk_forward.py's RealRunner. Dry-run prints each chain."""
    WF, C = _wf_import()
    holdout = _launcher_module(launcher)._OPTION_HOLDOUT_START
    why = wf_holdout_refusal(args.wf_folds, args.wf_deferred, holdout)
    if why is not None:
        ap.error(f"--walk-forward: {why}")
    selection = _wf_selection(args)
    specs = wf_cell_specs(args, launcher, experts, strategies, universe, selection)
    done = _completed_names()
    print(f"options walk-forward: {len(specs)} cell(s) (experts={experts}, strategies={strategies}, "
          f"universe={len(universe.split(','))} symbols); reports under {args.wf_report_dir}")
    print(f"Folds: train from {args.wf_train_start}, test years {args.wf_test_years}"
          + (f", data end {args.wf_data_end}" if args.wf_data_end else "")
          + (" (last test DEFERRED)" if args.wf_deferred is not None else "")
          + f"; generations {args.wf_first_generations} (fold 1) / {args.wf_next_generations} "
          f"(seeded folds, refit); population={args.population}, patience={args.early_stop}, "
          f"seed={args.seed}; option holdout from {holdout} (launcher rail)")
    print(f"Selection: top {args.wf_top_n} behaviour-distinct genomes per fold, {selection}")
    print(f"Objective: fitness={args.fitness or 'per-job default'}, robust_fitness="
          f"{'ON (launcher default)' if args.robust_fitness else 'OFF (--no-robust-fitness)'}"
          " -- scores are NOT comparable across the robustness setting.")
    strict, evolving = matrix_data_identity(args, universe)
    for spec in specs:
        guard_data_identity(args.wf_report_dir, spec.name, strict, evolving, write=not args.dry_run,
                            rename_hint="use a new --name-suffix for a different universe or option source")
    if args.dry_run:
        print_wf_dry_run(specs, done)
        return 0
    if args.screener_gate_store and not Path(args.screener_gate_store).exists():
        ap.error(f"Screener gate store does not exist: {args.screener_gate_store}")
    runner = _make_wf_runner(args, selection)
    return run_wf_cells(specs, runner, args.wf_report_dir, _FAILED_JOBS,
                        "options matrix driver (walk-forward)")


def print_wf_dry_run(specs, done) -> None:
    """Each cell's chain (DONE/TODO per fold job, the engine's own dry-run text, the deferred fold or
    the refit command); runs nothing. Shared with tools/run_option_style_wf.py."""
    WF, C = _wf_import()
    for spec in specs:
        plan, folds = C.make_cell_plan(spec)
        print(f"\n=== cell {spec.name} ({spec.expert} {spec.strategy} / {spec.band}) ===")
        for f in folds:
            job = WF.train_job_name(spec.name, f.index)
            print(f"  {'DONE' if job in done else 'TODO'}  {job}")
        print(WF.render_dry_run(plan))
        later = C.deferred_fold(folds, spec.data_end, spec.defer_last_test)
        if later is not None:
            print(f"  fold {later.index}: TEST DEFERRED -- the data ends {spec.data_end}; this run "
                  f"only TRAINS {WF.train_job_name(spec.name, later.index)}. Rerun the same "
                  f"command without --wf-data-end to test it, decide the cell and refit.")
            continue
        print("  refit (only if the cell passes): " + " ".join(C.refit_command(spec, folds)))
    print("Dry-run only: cache coverage and vendor compatibility have NOT been validated.")


def run_wf_cells(specs, runner, report_dir, failed: list, label: str, verdict_lines: bool = False) -> int:
    """Run the cells through ``wf_chain.run_cells`` (one cell's ERROR does not stop the others),
    print each verdict and finish like a matrix (every ERROR reported, non-zero exit). Each cell's
    data identity record (``guard_data_identity``) is copied into its ``cell.json``. With
    ``verdict_lines`` a machine-readable ``VERDICT <verdict> <cell>`` line follows each cell (the
    small-account chain records it). Shared with tools/run_option_style_wf.py."""
    WF, C = _wf_import()
    cells = C.run_cells(specs, lambda _spec: runner, report_dir)
    for c in cells:
        _annotate_cell_report(report_dir, c["cell"])
        print(f"  {c['verdict']:7s} {c['cell']}" + (f"  refit={c['refit_job']}" if c["refit_job"] else "")
              + (f"  ({c['error']})" if c.get("error") else ""), flush=True)
        if verdict_lines:
            print(f"VERDICT {c['verdict']} {c['cell']}", flush=True)
        if c["verdict"] == "ERROR":
            note_job_exit(failed, c["cell"], 1, c["error"])
    return finish_matrix(failed, label)


# ------------------------------------------------------------------------------------------------
# DATA IDENTITY of a cell across runs (review 2026-11-15). Under --wf-defer-last-test the data VALUES
# (manifests, universe list, and for style cells the universe file, panel and store) are masked in
# the cell name so phase B finds phase A's jobs. Without a record, two cells differing only in their
# universe would share a name (the second reusing the first's stored folds), and phase B could run on
# changed data unnoticed. The record lives in <report dir>/<cell>/data_identity.json.
# ------------------------------------------------------------------------------------------------

DATA_IDENTITY_FILE = "data_identity.json"


def guard_data_identity(report_dir: str, cell: str, strict: dict, evolving: dict, *, write: bool,
                        rename_hint: str, log=print) -> dict:
    """Compare this run's data identity with the cell's record; write the record on the first run.

    ``strict``: inputs that define the experiment (universe content, market-condition profiles,
    option store and history source): ANY difference from the first run is REFUSED (SystemExit),
    naming each change. ``evolving``: inputs that legitimately move in phase B (manifests, panel,
    store extended to 2026): a difference from the latest run is a WARNING naming it, and both the
    first-run and the current values are kept (``first``, ``latest``, ``changes``). ``write=False``
    (dry run) compares without writing. Returns the record."""
    import datetime as _dt
    path = os.path.join(report_dir, cell, DATA_IDENTITY_FILE)
    now = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
    if not os.path.exists(path):
        doc = {"schema": 1, "first_run": now, "strict": strict, "first": evolving, "latest": evolving,
               "changes": []}
        if write:
            WF, _C = _wf_import()
            WF.atomic_write_text(path, json.dumps(doc, indent=2, sort_keys=True))
        return doc
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
        stored_strict, latest = dict(doc["strict"]), dict(doc["latest"])
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise SystemExit(f"--walk-forward {cell}: REFUSED: its data identity record {path} is unreadable "
                         f"({type(e).__name__}: {e}); the cell's data cannot be verified. Restore the file, "
                         f"or {rename_hint}")
    diff = {k: (stored_strict.get(k), strict.get(k)) for k in sorted(set(stored_strict) | set(strict))
            if stored_strict.get(k) != strict.get(k)}
    if diff:
        shown = "; ".join(f"{k}: {a!r} -> {b!r}" for k, (a, b) in diff.items())
        raise SystemExit(f"--walk-forward {cell}: REFUSED: the data that defines this cell changed since its "
                         f"first run ({doc.get('first_run')}): {shown}. Its name does not carry these values "
                         f"(masked under --wf-defer-last-test), so running on would reuse another experiment's "
                         f"folds; {rename_hint}")
    changed = {k: (latest.get(k), evolving.get(k)) for k in sorted(set(latest) | set(evolving))
               if latest.get(k) != evolving.get(k)}
    if changed:
        shown = "; ".join(f"{k}: {a!r} -> {b!r}" for k, (a, b) in changed.items())
        log(f"WARNING [{cell}] data changed since the cell's last run: {shown}. Expected in phase B (the data "
            f"extended to the new test year): the deferred test reads the new values, the trained folds keep "
            f"the ones they ran on. Recorded in {path} (first-run values kept)."
            + ("" if write else " Not written (dry run)."))
        doc["changes"] = list(doc.get("changes") or []) + [
            {"at": now, "changed": {k: list(v) for k, v in changed.items()}}]
        doc["latest"] = evolving
        if write:
            WF, _C = _wf_import()
            WF.atomic_write_text(path, json.dumps(doc, indent=2, sort_keys=True))
    return doc


def _annotate_cell_report(report_dir: str, cell: str) -> None:
    """Copy the cell's data identity record (first-run and latest values) into its cell.json."""
    ident = os.path.join(report_dir, cell, DATA_IDENTITY_FILE)
    report = os.path.join(report_dir, cell, "cell.json")
    if not (os.path.exists(ident) and os.path.exists(report)):
        return
    with open(ident, encoding="utf-8") as fh:
        record = json.load(fh)
    with open(report, encoding="utf-8") as fh:
        doc = json.load(fh)
    doc["data_identity"] = record
    WF, _C = _wf_import()
    WF.atomic_write_text(report, json.dumps(doc, indent=2))


def sha256_text(text: Optional[str]) -> Optional[str]:
    return None if text is None else hashlib.sha256(text.encode("utf-8")).hexdigest()


def matrix_data_identity(args, universe: str) -> tuple:
    """(strict, evolving) data identity of an option-matrix cell (``guard_data_identity``)."""
    strict = {"universe_sha256": sha256_text(universe),
              "market_condition_profile": getattr(args, "market_condition_profile", None) or "none",
              "options_store": getattr(args, "options_store", None),
              "option_history_source": getattr(args, "option_history_source", None),
              "env:BACKTEST_OPTIONS_STORE": os.environ.get("BACKTEST_OPTIONS_STORE")}
    evolving = {"market_condition_manifest": getattr(args, "market_condition_manifest", None)}
    return strict, evolving


def main(argv=None) -> int:
    ap = build_parser()
    add_refused_decision_times_flag(ap)
    add_estimates_source_flag(ap)
    add_option_history_source_flag(ap)
    args = resolve_args(ap, argv)
    refuse_decision_times_and_announce(args)

    experts = [e.strip() for e in args.experts.split(",") if e.strip()]
    strategies = [s.strip() for s in args.strategies.split(",") if s.strip()]
    universe = _universe(args.universe_file)

    launcher = args.launcher
    if not launcher:
        launcher = os.path.join(os.path.dirname(sys.executable), "ba2-test.exe")
        if not os.path.exists(launcher):
            launcher = os.path.join(os.path.dirname(sys.executable), "ba2-test")
    if args.walk_forward:
        return _run_walk_forward(ap, args, launcher, experts, strategies, universe)
    jobs = list(planned_jobs(args, launcher, experts, strategies, universe))
    if args.profile == "discovery":
        print("DISCOVERY: permitted singles only; all remain eligible for composition. "
              "Stage-1 rankings provide seeds, not a survival gate.")
        print("Risk-policy exclusions: O_SSTG, O_SSTD (2026-08-31; not performance exclusions).")
        print(f"Search budget: population={args.population}, generations={args.generations}, "
              f"patience={args.early_stop}"
              + (f" (min gain {args.early_stop_min_rel:.2%} per counted generation)"
                 if args.early_stop_min_rel is not None else "")
              + f", seed={args.seed}; "
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
        for nm, exp, s, mode in jobs:
            print(f"  {'DONE' if nm in done else 'TODO'}  {nm}  ({exp} {s})")
            print("    " + shlex.join(build_cmd(args, launcher, nm, exp, s, universe, mode)))
        print("Dry-run only: cache coverage and vendor compatibility have NOT been validated.")
        return 0

    if args.screener_gate_store and not Path(args.screener_gate_store).exists():
        ap.error(f"Screener gate store does not exist: {args.screener_gate_store}")

    for i, (name, expert, strat, mode) in enumerate(jobs, 1):
        if name in _completed_names():   # re-read each loop (resumable)
            print(f"[{i}/{len(jobs)}] SKIP {name} (already completed)", flush=True)
            continue
        # BEFORE the launch, so the post-mortem below can tell THIS attempt's row from an older
        # one with the same name (2026-09-21 recheck, H4).
        prior_max_id = _newest_optimization_id()
        cmd = build_cmd(args, launcher, name, expert, strat, universe, mode)
        print(f"[{i}/{len(jobs)}] RUN  {name} ...", flush=True)
        rc = subprocess.run(cmd, env=os.environ.copy()).returncode
        print(f"[{i}/{len(jobs)}] {name} exit={rc}", flush=True)
        if rc != 0:
            _reason = _failure_reason(name, created_after=prior_max_id) if prior_max_id is not None else ""
            if is_analysis_failure_reason(_reason):
                # The job was refused for failing analysis passes: FAILED with its reason (the row is
                # 'failed', so a re-run does not skip it as completed), and the campaign carries on.
                note_job_exit(_FAILED_JOBS, name, rc, _reason)
                print(f"[{i}/{len(jobs)}] {name} FAILED (analysis failures): {_reason}; "
                      f"the campaign continues.", flush=True)
                continue
            decision = _classify_failure(name, prior_max_id)
            if decision == "skip":
                # A stall-only job (2026-09-21 review, G2). Its checkpoint was PRESERVED, so a later
                # pass can resume or re-run it; it must not stop the other 15 jobs, which is what
                # happened on 2026-09-20 when the same class of failure killed the campaign.
                print(f"[{i}/{len(jobs)}] {name} produced {NO_MEASUREMENT_MARKER} (stall-only, "
                      f"verified against this launch); recorded and SKIPPED -- the campaign "
                      f"continues. Re-run this job later; its checkpoint was preserved.", flush=True)
                continue
            if prior_max_id is None:
                print(f"[{i}/{len(jobs)}] options matrix stopped: {name} failed and its marker "
                      f"could not be verified (no readable optimization table).", flush=True)
            else:
                print(f"[{i}/{len(jobs)}] options matrix stopped: {name} failed; remaining jobs "
                      f"were not launched.", flush=True)
            return rc if rc > 0 else 1
    return finish_matrix(_FAILED_JOBS, "options matrix driver")


if __name__ == "__main__":
    raise SystemExit(main())
