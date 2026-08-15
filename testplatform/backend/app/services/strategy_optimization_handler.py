"""Strategy optimization handler — joint genetic search over
expert (incl. RM sizing settings) + ruleset/condition params, scored by ONE backtest metric.

Registered as task type ``strategy_optimization`` (main.py). Mirrors the proven GA
wiring in ``job_handler.py`` (validate -> seed -> hoist -> fitness_function -> optimize
-> persist) but the fitness runs the DETERMINISTIC Phase-2 daily backtest
(``daily_backtest_handler.run_daily_backtest``, the synchronous in-process runner) and
reads ``results[<fitness_metric>]`` via ``strategy_fitness.compute_fitness``.

Determinism (the Phase-4 core gate):
  * the GA population/crossover/mutation is governed by seeding ``random`` AND ``np.random``
    from ``optimization_config.seed`` BEFORE ``optimize`` (so a seeded run reproduces an
    identical best individual);
  * each per-trial daily backtest is intrinsically deterministic (the engine seeds
    random/np.random from ``config['seed']`` at the start of ``run()``);
  * a param-independent pass is hoisted ONCE per run (``_build_hoisted_state``) and reused
    for every individual;
  * a content-hash trial memo (``trial_memo``) makes an elitism-reselected identical
    individual a FREE hit AND a self-check that the run is deterministic.

The GA must NEVER enqueue a sub-task: ``init_task_queue(max_workers=1)`` (main.py) would
deadlock. The fitness calls the synchronous runner in-process (confirmed in Replan).
"""
import logging
import random
import time as _time
from datetime import datetime
from typing import Any, Dict, Optional

import numpy as np

from app.models import (
    SessionLocal,
    Strategy as StrategyModel,
    StrategyOptimization,
    TaskQueue,
)
from app.models.task_queue import TaskStatus
from app.services.genetic import GeneticOptimizer, DEAP_AVAILABLE
from app.services.task_queue import get_task_queue
from app.services.strategy_param_space import collect_param_space, decode_params
from app.services.strategy_fitness import compute_fitness, ZERO_TRADE_SENTINEL
from app.services.trial_memo import trial_key, TrialMemo
from app.services.sync_client import push_optimization

logger = logging.getLogger(__name__)

# Mirror job_handler.required_ga_keys (no-defaults rule, backend/CLAUDE.md) + add 'seed'
# (Phase-4 determinism). Every value is explicitly provided + validated fail-early.
REQUIRED_GA_KEYS = (
    "populationSize",
    "generations",
    "crossoverProb",
    "mutationProb",
    "earlyStoppingGenerations",
    "elitismPercent",
    "seed",
)


# Backend dir (this file is backend/app/services/strategy_optimization_handler.py) — the
# worker processes prepend it to sys.path so ``app...`` imports resolve under spawn.
import os as _os
_BACKEND_DIR = _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
# Provider API keys mirrored into each worker's env (spawn starts a clean environment).
_WORKER_ENV_KEYS = ("FMP_API_KEY", "ALPHA_VANTAGE_API_KEY", "FINNHUB_API_KEY", "OPENAI_API_KEY")


def _worker_init(backend_dir: str, env: Dict[str, str]) -> None:
    """ProcessPool worker initializer (runs once per worker under spawn).

    Puts ``backend/`` on the path + cwd so ``app...``/relative-path imports resolve, mirrors
    the provider API keys into the (clean, spawned) env, and quiets per-trial logging.
    """
    import os
    import sys
    # Disable file logging in workers BEFORE ba2_common is imported: many processes sharing the
    # one RotatingFileHandler on app.log race on rollover (Windows WinError 32). Read by
    # ba2_common.config at import time.
    os.environ["BA2_FILE_LOGGING"] = "0"
    os.environ["BA2_STDOUT_LOGGING"] = "0"
    if backend_dir and backend_dir not in sys.path:
        sys.path.insert(0, backend_dir)
    try:
        os.chdir(backend_dir)
    except OSError:
        pass
    for k, v in (env or {}).items():
        if v is not None:
            os.environ.setdefault(k, v)
    # Point ba2_common's DB at the SAME test DB the master uses, so THIS pool worker's
    # get_app_setting() (FMP_API_KEY / finnhub_api_key / alpaca_*) resolves from the test DB
    # instead of ba2_common's neutral default home DB (which has no keys). The FMP/FinnHub experts
    # construct their providers INSIDE the worker, and provider __init__ reads the key via
    # get_app_setting — without this the worker raises "FMP API key not configured" on every trial.
    # Mirrors the launcher's _bootstrap, which only runs in the MASTER process, not in spawned pool
    # workers. Best-effort: a failure here surfaces later as the same clear provider error.
    try:
        from app.models.database import DATABASE_URL as _DB_URL
        if _DB_URL.startswith("sqlite:///"):
            from ba2_common.core import db as _ba2_db
            _ba2_db.configure_db(_DB_URL.replace("sqlite:///", "", 1))
    except Exception:  # noqa: BLE001 — non-fatal; the provider's own error is the fallback
        pass
    import logging as _lg
    _lg.disable(_lg.ERROR)  # workers are silent; the parent process logs the run summary
    for n in ("ba2_common", "ba2_providers", "ba2_experts", "app.services.backtest"):
        _lg.getLogger(n).setLevel(_lg.WARNING)


def _maybe_mark_want_full(config: Dict[str, Any], is_last_gen: bool) -> Dict[str, Any]:
    """Attach the opt-in full-results flag (see ``_trial_worker``) to a trial config when this
    job belongs to the GA's FINAL generation. Returns a shallow copy when tagging (never mutates
    the caller's config in place) so the same base dict can be reused for other jobs."""
    if not is_last_gen:
        return config
    tagged = dict(config)
    tagged["_want_full_results"] = True
    return tagged


def _capture_full_result(buffer: Dict[str, Any], key: str, out: Dict[str, Any]) -> None:
    """Stash a trial's full results blob (if the worker computed one — see
    ``_maybe_mark_want_full``) into the last-generation buffer, keyed by trial key. Used by
    ``_persist_top_backtests`` to skip a re-run for any top-N individual that was already fully
    computed as part of the final generation."""
    full = out.get("full_results")
    if full is not None:
        buffer[key] = full


# Process-local scratch space for the CLI's post-optimization top-N persist step (see
# _persist_top_backtests in ba2test_launcher.py). Deliberately NOT part of
# handle_strategy_optimization's return value: that return dict flows into TaskQueue.result (a
# JSON DB column) for every UI/API-submitted job via the main task queue's inline handler path
# (use_subprocess=False), and this buffer can hold a full generation's worth of trade/equity/
# drawdown blobs -- it must never be serialized to the DB.
#
# KNOWN GAP: only the CLI's direct, same-process caller (ba2test_launcher.py, Task 3 of
# docs/plans/2026-07-17-ga-last-gen-full-results-capture.md) ever pops an entry. A UI/API-
# submitted job (this same handler, run inline in the main backend server process) stashes an
# entry here too but nothing currently collects it -- see task #43 ("Serve handler: persist
# optimization top-N as tagged Backtests"), still pending as of this writing. Until that lands,
# entries for UI-submitted parallel>1 runs accumulate in the server process's memory for its
# whole uptime. Bounded impact today (this fixes a WORSE bug -- the same data unconditionally
# hitting the DB on every run -- and a server restart clears it), but worth a size/TTL bound or
# an explicit pop-on-collect wired into the serve path once #43 is built.
_last_gen_full_results_by_opt: Dict[int, Dict[str, Any]] = {}


# Seconds between ctl-proxy reads on the progress hook. Module-level so tests can drop it to 0;
# see _cancel_progress_cb for why reading a Manager proxy per call is not acceptable.
_CANCEL_CHECK_INTERVAL_S = 5.0


class TrialCancelled(Exception):
    """The master abandoned this trial (timeout) and the worker flagged it for cancellation."""


def _cancel_progress_cb(ctl):
    """progress_cb that aborts the run when *ctl* is flagged for cancel; None when uncancellable.

    Rides the engine's EXISTING per-bar ``progress_cb`` hook (daily_engine.DailyBacktestEngine),
    which is already throttled -- calling it per bar was once ~36% of runtime, so the throttle
    is load-bearing and this must not bypass it. Reading a Manager proxy is an IPC round-trip,
    which is affordable at the throttled cadence and would NOT be per raw bar.

    Returning None (rather than a no-op lambda) when there is no control block keeps the
    uncancellable path byte-identical to the old behaviour: run_daily_backtest substitutes its
    own no-op.
    """
    if ctl is None:
        return None

    # TIME-THROTTLED, and that is load-bearing. ctl is a multiprocessing.Manager PROXY, so every
    # read is a cross-process round-trip. Reading it on each progress_cb call made a trial
    # >600s that runs in 123s without it (>5x) -- measured after it slowed opt 228 to ~75
    # min/trial. The engine already throttles progress_cb (per-bar calls were once 36% of
    # runtime), but that throttle was tuned for a LOCAL callback, not for IPC.
    #
    # A 5s cancellation latency is irrelevant: this exists to stop a trial the master abandoned
    # after a multi-minute timeout, so being 5s late costs nothing and the hot path stays free.
    state = {"next_check": 0.0, "bars": 0}
    interval = _CANCEL_CHECK_INTERVAL_S

    def _cb(pct: float, msg: str) -> None:
        now = _time.monotonic()
        if now < state["next_check"]:
            return
        state["next_check"] = now + interval
        try:
            # HEARTBEAT. Publish liveness on the SAME throttled IPC round-trip that reads the
            # cancel flag, so it costs nothing extra. Without this the master cannot tell a trial
            # that is computing from one that was accepted and never scheduled, and can only wait
            # out the full trial_timeout — 3h, three times, per slot (measured 2026-08-06, opt
            # 251: remote150's pool was saturated by orphaned children, every trial reported
            # "running" until the 10800s budget expired, and the grid sat idle ~9h).
            #
            # "bars" was declared in the control block on 2026-07-28 and never written by
            # anything; this is the line that was missing.
            state["bars"] += 1
            ctl["bars"] = state["bars"]
            cancelled = bool(ctl.get("cancel"))
        except Exception:  # noqa: BLE001 — a dead manager must never fail a healthy trial
            return
        if cancelled:
            raise TrialCancelled("cancelled by master (abandoned)")

    return _cb


def _trial_worker(config: Dict[str, Any], fitness_metric: str, ctl: Any = None) -> Dict[str, Any]:
    """Run ONE deterministic daily backtest in a worker PROCESS and return a tiny summary.

    Only the CPU-bound backtest runs here (no GIL contention with the GA loop); the result is
    reduced to ``{ok, fitness, trades, error}`` so the pickled payload back to the parent is
    small (the full equity/trade blobs are re-derived later for the persisted top-N only) --
    UNLESS the caller sets ``config["_want_full_results"] = True`` (used only for the GA's final
    generation, see ``handle_strategy_optimization``'s ``batch_fitness``), in which case the full
    ``run_daily_backtest`` results dict is ALSO returned under ``full_results``. The flag is
    popped before the config reaches ``run_daily_backtest`` -- it is an internal signal between
    the GA loop and this worker, not a real backtest setting.

    The cross-job OHLCV-memo eviction (the remote/local worker memory leak fix) lives in
    ``run_daily_backtest`` — the single chokepoint EVERY path goes through — so it covers the pool
    workers here AND the master's in-process top-N persist / parallel=1 runs uniformly.
    """
    want_full = config.pop("_want_full_results", False)
    try:
        from app.services.backtest.daily_backtest_handler import run_daily_backtest
        from app.services.strategy_fitness import compute_fitness

        # Wall time for THIS individual, measured inside the worker so it is pure compute and
        # excludes dispatch/queue wait. Logged next to the memory numbers because the two rise
        # together under cache pressure: a working set that outgrows RAM shows up as trials
        # getting slower BEFORE it shows up as an OOM, so a rising secs on flat cache counts is
        # the early warning (and rules memory in or out as the cause of a slowdown).
        _t0 = _time.monotonic()
        results = run_daily_backtest(config, progress_cb=_cancel_progress_cb(ctl))
        fit = compute_fitness(fitness_metric, results)
        out = {"ok": True, "fitness": float(fit), "secs": round(_time.monotonic() - _t0, 1),
               "trades": int(results.get("total_trades") or 0), "error": None,
               # Per-trial memory telemetry (a few psutil/len calls — negligible): RSS of
               # THIS worker process + the two per-process OHLCV caches, so a memory-driven
               # incident (e.g. WinError 1450 on the remote box) leaves a trail showing what
               # was depleting the machine.
               "mem": _trial_memory_snapshot()}
        if want_full:
            out["full_results"] = results
        return out
    except Exception as e:  # noqa: BLE001 — surface as a failed trial, don't kill the pool
        # A cache miss is FATAL (a data/config problem, not a bad-parameter trial): every trial
        # will hit the same gap, so flag it so the parent can abort with the actionable message
        # instead of grinding the whole population to 0 fitness.
        # A CANCEL is not a bad genome -- the master abandoned it. Report it as retryable so a
        # requeue re-runs it elsewhere rather than recording a bogus 0-fitness result.
        if isinstance(e, TrialCancelled):
            return {"ok": False, "fitness": 0.0, "trades": 0, "error": "cancelled",
                    "cancelled": True, "retryable": True, "fatal": False}
        # FMPHermeticViolation is FATAL for the same reason as a cache miss: it is a
        # data/config problem that affects EVERY trial, not a bad genome. It also has to abort
        # fast — opt 255 spent 8h with all four workers blocked in the FMP rate gate, having
        # completed zero trials, because a breach merely made each trial slow instead of failing.
        fatal = type(e).__name__ in (
            "BacktestCacheMiss", "FMPHistoryCacheMiss", "FMPHermeticViolation")
        return {"ok": False, "fitness": 0.0, "trades": 0, "error": str(e) if fatal else repr(e),
                "fatal": fatal, "mem": _trial_memory_snapshot()}


def _trial_memory_snapshot() -> Dict[str, Any]:
    """Cheap per-trial memory snapshot taken INSIDE the worker process: RSS + the two OHLCV
    cache sizes (price_source.memory_stats). Best-effort — never fails a trial."""
    try:
        import psutil

        from app.services.backtest.price_source import memory_stats
        snap = memory_stats()
        snap["rss_mb"] = psutil.Process(_os.getpid()).memory_info().rss // 1048576
        return snap
    except Exception as e:  # noqa: BLE001
        return {"error": repr(e)}


def _log_trial_memory(gen: int, n_gens: int, done: int, total: int, mem: Any,
                      secs: Any = None) -> None:
    """One compact INFO line per completed individual: trial wall time + worker RSS + what the
    two OHLCV caches are holding (symbols / bars / MB).

    WHY per-individual and not per-generation: the snapshot is taken INSIDE the worker that ran
    the trial, so with a pool of N workers a per-generation sample would show whichever worker
    happened to finish last and miss the one that is actually growing. Per-individual gives a
    per-worker series for free.

    WHY it was invisible before: ``_trial_worker`` has always attached this snapshot to its
    result, but the only reader logged it on the FAILURE path — so a healthy run climbing toward
    an OOM left no trail at all, and the first reading ever printed was from a worker that had
    already broken. The cost is one preformatted line per ~90s trial.

    WHY warning() AND NOT info(): an optimize run installs a GLOBAL ``logging.disable(INFO)`` for
    its whole duration (see run_strategy_optimization, and again around the top-N re-runs in
    ba2test_launcher) to kill ~17k records/trial. That short-circuits at the manager level before
    any LogRecord exists, so an info() here is not merely filtered — it never happens, for the
    entire run, on every worker. WARNING+ is the documented escape hatch and is what the sibling
    diagnostics ("trial failed in worker", the distributed_eval worker notices) already use.
    Downgrading this to info() re-breaks it silently: the code looks right and the log is empty.
    """
    if not isinstance(mem, dict) or mem.get("error"):
        # Still surface the timing even if the memory probe failed -- a rising trial time is a
        # signal on its own, and losing it to an unrelated psutil hiccup would be a waste.
        if secs is not None:
            logger.warning(
                f"mem gen {gen + 1}/{n_gens} ind {done}/{total} | {secs}s | (no mem probe)")
        return
    bc, sm = mem.get("bar_cache") or {}, mem.get("series_memo") or {}
    logger.warning(
        f"mem gen {gen + 1}/{n_gens} ind {done}/{total}"
        + (f" | {secs}s" if secs is not None else "")
        + f" | rss {mem.get('rss_mb')}MB"
        f" | bars {bc.get('symbols')} sym {bc.get('bars')} bars {bc.get('mb')}MB"
        f" | memo {sm.get('symbols')} sym {sm.get('rows')} rows {sm.get('mb')}MB"
    )


def _persist_trial_worker(config: Dict[str, Any], ctl: Any = None) -> Dict[str, Any]:
    """Run a TOP-N re-run and return the FULL results dict (equity curve / trades / metrics) for
    the master to persist as a tagged Backtest.

    Distinct from ``_trial_worker``, which returns only the scalar fitness: the top-N persist needs
    the whole results blob. Used by ``_persist_top_backtests`` to fan the independent re-runs across
    a bounded local process pool (the re-runs are the slow post-GA phase). On error returns
    ``{ok: False, error}`` so one bad re-run never poisons the pool or aborts the others.

    ``ctl`` is the worker's per-job cancellation block. It is accepted here because
    ``worker_server._submit_job`` appends one to EVERY pooled call — without this parameter
    /submit-trial-full would raise a TypeError on arity the moment cancellation shipped.
    """
    try:
        from app.services.backtest.daily_backtest_handler import run_daily_backtest
        return {"ok": True, "results": run_daily_backtest(config)}
    except Exception as e:  # noqa: BLE001 — surface as a failed re-run, keep the others going
        return {"ok": False, "error": repr(e)}


def _resolve_workers(db: Any, worker_ids: Optional[list]) -> list:
    """Resolve selected worker ids -> plain dicts ``{id,name,url,password}`` for the dispatchers.

    Only enabled, non-local workers are eligible (the local machine is always a worker via the
    pool). Returns [] when nothing is selected -> the handler keeps the local-only path. Plain
    dicts (not ORM rows) so they can cross into the dispatcher threads without a session.
    """
    if not worker_ids:
        return []
    from app.models import Worker
    rows = (db.query(Worker)
            .filter(Worker.id.in_(list(worker_ids)),
                    Worker.is_local == False,  # noqa: E712
                    Worker.is_enabled == True)  # noqa: E712
            .all())
    return [{"id": w.id, "name": w.name, "url": w.url, "password": w.password} for w in rows]


class _FatalTrialError(RuntimeError):
    """A trial failed for a reason that will affect EVERY remaining trial (incomplete prewarm,
    missing OHLCV cache). Raised out of the fitness batch to stop the search immediately rather
    than spending the full generation budget producing a result nobody should trust."""


def _fail(opt_id: int, db: Any, msg: str) -> Dict[str, Any]:
    """Mark the StrategyOptimization row failed + return the failure dict."""
    logger.error(f"strategy_optimization {opt_id} failed: {msg}")
    row = db.query(StrategyOptimization).filter(StrategyOptimization.id == opt_id).first()
    if row:
        row.status = "failed"
        row.error_message = msg[:1000]
        row.completed_at = datetime.now()
        db.commit()
        push_optimization(row, db)
    return {"status": "failed", "error": msg}


def handle_strategy_optimization(task_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Run the joint genetic optimization for one StrategyOptimization row."""
    if not DEAP_AVAILABLE:
        return {"status": "failed", "error": "DEAP not available"}
    opt_id = payload.get("optimization_id")
    if not opt_id:
        return {"status": "failed", "error": "optimization_id is required"}

    db = SessionLocal()
    try:
        opt = db.query(StrategyOptimization).filter(
            StrategyOptimization.id == opt_id
        ).first()
        if not opt:
            return {"status": "failed", "error": f"StrategyOptimization {opt_id} not found"}
        opt.status = "running"
        opt.started_at = datetime.now()
        db.commit()

        strategy = db.query(StrategyModel).filter(
            StrategyModel.id == opt.strategy_id
        ).first()
        if not strategy:
            return _fail(opt_id, db, f"Strategy {opt.strategy_id} not found")

        # --- Fail-early config validation (no-defaults rule) ---
        ga = opt.optimization_config or {}
        for key in REQUIRED_GA_KEYS:
            if key not in ga:
                return _fail(opt_id, db, f"optimization_config.{key} is required")
        if not opt.fitness_metric:
            return _fail(opt_id, db, "fitness_metric is required")

        backtest_cfg = ga.get("backtest")
        if not backtest_cfg:
            return _fail(
                opt_id,
                db,
                "optimization_config.backtest is required "
                "(engine/datasets/date-range/initial_capital/...)",
            )
        expert_cfg = ga.get("expert_params")  # may be None (expert frozen)

        # SCREENER + SCHEDULE genes share the expert_params dict (the launcher merges them in
        # pre-namespaced with ``screener:``/``schedule:``). Split them out so they route to their
        # own namespace instead of being mis-prefixed as ``model:screener:*``/``model:schedule:*``
        # by _collect_expert: the model space gets the remaining keys, and a screener_cfg/
        # schedule_cfg (prefix stripped back to the bare name) is passed alongside so
        # collect_param_space emits the ``screener:<setting>``/``schedule:<day>`` genes.
        model_cfg = None
        screener_cfg = None
        schedule_cfg = None
        if expert_cfg:
            model_cfg = {k: v for k, v in expert_cfg.items()
                        if not k.startswith("screener:") and not k.startswith("schedule:")}
            screener_cfg = {
                k[len("screener:"):]: v
                for k, v in expert_cfg.items()
                if k.startswith("screener:")
            } or None
            schedule_cfg = {
                k[len("schedule:"):]: v
                for k, v in expert_cfg.items()
                if k.startswith("schedule:")
            } or None

        # BYPASS expert (piece 1c): if the backtest's expert declares ``bypasses_classic_rm``
        # (e.g. FactorRanker) the search space must EXCLUDE tp/sl/cond:*/exit:* and search
        # ONLY the expert's own params (model:*). Detected from the backtest_cfg experts here so
        # the same flag drives both the param space and the per-trial config.
        bypass_expert = _is_bypass_expert(backtest_cfg)

        # --- Build the joint param space (Task 1) ---
        try:
            param_space = collect_param_space(
                strategy, expert_cfg=model_cfg, bypass=bypass_expert,
                screener_cfg=screener_cfg, schedule_cfg=schedule_cfg,
            )
        except ValueError as e:
            return _fail(opt_id, db, str(e))
        opt.parameter_ranges = param_space
        db.commit()

        # Detach the Strategy into a session-free snapshot BEFORE the (possibly parallel) trial
        # loop. The fitness threads call decode_params(strategy, ...) concurrently, but the
        # shared SQLAlchemy session + its SQLite connection are NOT thread-safe: a db.commit() in
        # ga_callback expires this instance, and a concurrent attribute reload over the shared
        # connection raises "(sqlite3.InterfaceError) bad parameter or other API misuse". refresh()
        # materialises every mapped column into the instance, expunge() detaches it so the threads
        # read pure in-memory state with no further DB access. (decode_params only reads scalar
        # columns — no relationships — so a detached snapshot is sufficient.)
        db.refresh(strategy)
        db.expunge(strategy)

        # --- DETERMINISM: seed both RNGs (Task 4 / determinism_rule) ---
        seed = int(ga["seed"])
        random.seed(seed)
        np.random.seed(seed & 0xFFFFFFFF)

        # --- HOIST the param-independent pass out of the trial loop (lever 2) ---
        hoisted = _build_hoisted_state(backtest_cfg)

        memo = TrialMemo()
        all_results: list = []
        last_gen_full_results: Dict[str, Any] = {}
        best = {"fitness": None, "params": None}
        fatal = {"msg": None}  # first FATAL trial error (e.g. OHLCV cache miss) -> abort loudly

        tq = get_task_queue()

        def _persist_live(pct=None) -> None:
            """Push the live ``best`` + full ``all_results`` to the optimization ROW after
            EACH individual, so the running-optimizations API (and the UI top-individuals
            table / Evaluated count) updates WITHIN a generation — not only at generation
            boundaries (``ga_callback``). Best-effort: a transient DB hiccup must never crash
            the optimization, so failures are swallowed (the next call reconciles)."""
            try:
                row = db.query(StrategyOptimization).filter(
                    StrategyOptimization.id == opt_id
                ).first()
                if row is None:
                    return
                if pct is not None:
                    row.progress = pct
                if best["fitness"] is not None:
                    row.best_fitness = best["fitness"]
                    row.best_params = best["params"]
                row.all_results = list(all_results)  # new list obj -> JSON change detected
                db.commit()
            except Exception as e:  # noqa: BLE001 — live UI refresh is non-critical
                db.rollback()
                logger.debug(f"live opt persist skipped: {e}")

        def fitness_function(decoded_flat: Dict[str, Any]) -> float:
            if tq.is_task_paused(task_id):
                raise InterruptedError("paused/cancelled")
            decoded = decode_params(strategy, decoded_flat)
            key = trial_key(
                {
                    "engine": backtest_cfg.get("engine"),
                    "model_id": backtest_cfg.get("model_id"),
                    "pred_dataset_id": backtest_cfg.get("prediction_dataset_id"),
                    "exec_dataset_id": backtest_cfg.get("execution_dataset_id"),
                    "start": str(backtest_cfg.get("start_date")),
                    "end": str(backtest_cfg.get("end_date")),
                    "seed": backtest_cfg.get("seed"),
                    "params": decoded_flat,
                }
            )
            cached = memo.get(key)
            if cached is not None:
                return cached
            results = _run_trial_backtest(backtest_cfg, hoisted, decoded)
            fit = compute_fitness(opt.fitness_metric, results)
            memo.put(key, fit)
            all_results.append(
                {
                    "params": decoded_flat,
                    "fitness": fit,
                    "key": key,
                    "trades": results.get("total_trades") if results else 0,
                }
            )
            if best["fitness"] is None or fit > best["fitness"]:
                best["fitness"] = fit
                best["params"] = decoded_flat
            _persist_live()  # live top-population refresh after each individual
            return fit

        # --- brute_force option for tiny spaces (optimization_type) ---
        if (opt.optimization_type or "genetic") == "brute_force":
            return _run_brute_force(
                opt, db, task_id, param_space, fitness_function, all_results
            )

        # Parallel trials: ga['parallelIndividuals'] > 1 evaluates the population across a
        # ThreadPoolExecutor. Safe now that each trial isolates its per-run DB on its own
        # thread (ba2_common configure_db_threadlocal) + the OHLCV/FMP caches are lock-guarded.
        parallel = int(ga.get("parallelIndividuals", 1) or 1)
        optimizer = GeneticOptimizer(
            param_ranges=param_space,
            population_size=int(ga["populationSize"]),
            n_generations=int(ga["generations"]),
            crossover_prob=float(ga["crossoverProb"]),
            mutation_prob=float(ga["mutationProb"]),
            early_stopping_generations=int(ga["earlyStoppingGenerations"]),
            elitism_percent=float(ga["elitismPercent"]),
            parallel_individuals=parallel,
        )

        gen_state = {"gen": 0}

        def on_generation_start(generation: int):
            gen_state["gen"] = generation

        def ga_callback(generation: int, best_fitness: float, best_params: Dict):
            pct = ((generation + 1) / int(ga["generations"])) * 100.0
            tq.update_progress(
                task_id,
                pct,
                f"Gen {generation + 1}/{ga['generations']} best={best_fitness:.4f}",
            )
            row = db.query(StrategyOptimization).filter(
                StrategyOptimization.id == opt_id
            ).first()
            row.progress = pct
            row.best_fitness = best_fitness
            row.best_params = best_params
            row.all_results = all_results
            db.commit()
            push_optimization(row, db)
            if tq.is_task_paused(task_id):
                raise InterruptedError("paused/cancelled")

        # Checkpoints are keyed on the JOB (its name), not this row's id, so a relaunch of an
        # interrupted job -- which inserts a NEW StrategyOptimization row -- still finds them.
        ckpt_task_id = checkpoint_task_id(opt.name, opt_id)
        ckpt_fingerprint = checkpoint_fingerprint(param_space, ga)

        def checkpoint_cb(generation: int, population: list):
            data = optimizer.get_checkpoint_data(generation, population)
            data["fingerprint"] = ckpt_fingerprint   # refuse to resume into a changed gene space
            _save_checkpoint(ckpt_task_id, data)

        def _trial_key_for(decoded_flat: Dict[str, Any]) -> str:
            return trial_key(
                {
                    "engine": backtest_cfg.get("engine"),
                    "model_id": backtest_cfg.get("model_id"),
                    "pred_dataset_id": backtest_cfg.get("prediction_dataset_id"),
                    "exec_dataset_id": backtest_cfg.get("execution_dataset_id"),
                    "start": str(backtest_cfg.get("start_date")),
                    "end": str(backtest_cfg.get("end_date")),
                    "seed": backtest_cfg.get("seed"),
                    "params": decoded_flat,
                }
            )

        # TRUE multiprocessing batch evaluator (used when parallel > 1). The CPU-bound
        # backtests run in worker PROCESSES (no GIL); the GA loop, the trial memo, all_results
        # and best stay here in the main process. Only plain-dict configs go out and a tiny
        # {ok,fitness,trades} summary comes back, so nothing un-picklable crosses the boundary.
        #
        # The execution backend is pluggable via *execute_jobs*: a callable taking the list of
        # ``(idx, flat, key, config)`` jobs and YIELDING ``(idx, flat, key, out)`` as each
        # finishes. The LOCAL backend (``_local_execute_jobs``) submits to the process pool; the
        # DISTRIBUTED backend (DistributedEvaluator.execute_jobs) fans trials out to the broker
        # (master-as-worker pool consumers + remote HTTP workers). The memo/progress/persist
        # collection loop below is identical for both — only WHERE a trial runs differs.
        def _local_execute_jobs(jobs):
            from concurrent.futures import as_completed
            futures = {
                _pool.submit(_trial_worker, cfg, opt.fitness_metric): (i, flat, key)
                for (i, flat, key, cfg) in jobs
            }
            for fut in as_completed(futures):
                i, flat, key = futures[fut]
                yield (i, flat, key, fut.result())

        def make_batch_fitness(execute_jobs):
            def batch_fitness(param_dicts: list) -> list:
                if tq.is_task_paused(task_id):
                    raise InterruptedError("paused/cancelled")
                fits: list = [None] * len(param_dicts)
                jobs = []  # (idx, decoded_flat, key, config)
                n_gens = int(ga["generations"])
                is_last_gen = gen_state["gen"] == n_gens - 1
                for i, flat in enumerate(param_dicts):
                    key = _trial_key_for(flat)
                    cached = memo.get(key)
                    if cached is not None:
                        fits[i] = cached
                        continue
                    config = _build_daily_trial_config(
                        backtest_cfg, decode_params(strategy, flat), hoisted
                    )
                    config = _maybe_mark_want_full(config, is_last_gen)
                    jobs.append((i, flat, key, config))

                # Intra-generation progress: report individuals evaluated WITHIN the current
                # generation (UI's per-generation bar). The overall % blends the generation
                # index with the in-batch fraction so it advances smoothly between gen boundaries
                # (ga_callback still snaps it to the exact boundary at gen end). Throttled to
                # ~20 updates/gen so we don't hammer the task DB.
                total_in_batch = len(param_dicts)
                gen = gen_state["gen"]
                step = max(1, total_in_batch // 20)
                # Periodic memory snapshot (master process only; cheap psutil calls, twice per
                # generation — NOT per-trial) so a run leaves a coarse memory-over-time trail for
                # diagnosing worker MemoryErrors, without adding any per-trial overhead.
                _mem_mark = max(1, total_in_batch // 2)

                def _emit_intra(done: int):
                    frac = (done / total_in_batch) if total_in_batch else 1.0
                    pct = ((gen + frac) / n_gens) * 100.0 if n_gens else 0.0
                    bf = best["fitness"]
                    msg = (f"Gen {gen + 1}/{n_gens} · ind {done}/{total_in_batch}"
                           + (f" best={bf:.4f}" if bf is not None else ""))
                    tq.update_progress(task_id, pct, msg)

                done = total_in_batch - len(jobs)  # cached individuals are already evaluated
                _emit_intra(done)

                for i, flat, key, out in execute_jobs(jobs):
                    if out["ok"]:
                        fit = float(out["fitness"])
                        fits[i] = fit
                        memo.put(key, fit)
                        _log_trial_memory(gen, n_gens, done + 1, total_in_batch,
                                          out.get("mem"), out.get("secs"))
                        all_results.append(
                            {"params": flat, "fitness": fit, "key": key, "trades": out["trades"]}
                        )
                        if is_last_gen:
                            _capture_full_result(last_gen_full_results, key, out)
                    else:
                        # FAILED trial: score it at the always-worst sentinel so the GA never
                        # prefers a crashed genome (the old 0.0 fallback ranked ABOVE every
                        # legitimately-negative individual, biasing selection toward crashes),
                        # and do NOT memo it — a crash is an environment event, not a property
                        # of the genome, so a re-selection should re-run it.
                        from app.services.strategy_fitness import ZERO_TRADE_SENTINEL
                        fit = ZERO_TRADE_SENTINEL
                        fits[i] = fit
                        if out.get("error"):
                            mem = out.get("mem")
                            logger.warning(f"trial failed in worker: {out['error']}"
                                           + (f" | worker mem: {mem}" if mem else ""))
                            if out.get("fatal") and fatal["msg"] is None:
                                fatal["msg"] = out["error"]
                                # ABORT NOW, not at the end. A fatal is a DATA/CONFIG problem --
                                # an incomplete prewarm, a missing OHLCV cache -- so it affects
                                # every remaining trial identically. Before this, fatal["msg"] was
                                # recorded and then only consulted if all_results ended up EMPTY;
                                # when most trials happened to succeed (the goal2020 OP case) the
                                # run ground through all 8 generations and reported a confident
                                # winner chosen partly by which genomes dodged the broken data.
                                raise _FatalTrialError(out["error"])
                    if best["fitness"] is None or fit > best["fitness"]:
                        best["fitness"] = fit
                        best["params"] = flat
                    done += 1
                    # Live top-population: push best + all_results to the opt row after EACH
                    # individual so the UI updates within the generation (not only at gen end).
                    frac = (done / total_in_batch) if total_in_batch else 1.0
                    pct = ((gen + frac) / n_gens) * 100.0 if n_gens else 0.0
                    _persist_live(pct)
                    if done % step == 0 or done == total_in_batch:
                        _emit_intra(done)
                    if done == _mem_mark or done == total_in_batch:
                        from app.services.distributed_eval import _log_memory_diagnostics
                        _log_memory_diagnostics(
                            logger.warning, f"gen {gen + 1}/{n_gens} ind {done}/{total_in_batch}")
                return fits

            return batch_fitness

        start_gen, init_pop = 0, None
        ckpt = _load_checkpoint(ckpt_task_id, ckpt_fingerprint)
        # EXHAUSTED CHECKPOINT: one written at (or past) the final generation leaves nothing to
        # run, and resuming into it produces a 0-trial run that reports "every backtest failed" --
        # a confusing failure for what is really "this search already finished". Only reachable
        # when a run reached the last generation and then died before _clear_checkpoint (or when a
        # NEW job reuses a dead job's name), so discarding and searching from scratch is both the
        # safe reading and the one that produces a result.
        if ckpt and int(ckpt.get("generation", -1)) + 1 >= int(ga["generations"]):
            logger.warning(
                f"strategy_optimization {opt_id}: checkpoint {ckpt_task_id} is at generation "
                f"{ckpt.get('generation')} of {ga['generations']} — exhausted, starting fresh")
            _clear_checkpoint(ckpt_task_id)
            ckpt = None
        if ckpt:
            start_gen, init_pop = optimizer.resume_from_checkpoint(ckpt)
            logger.warning(
                f"strategy_optimization {opt_id}: RESUMING {opt.name!r} at generation "
                f"{start_gen}/{ga['generations']} from checkpoint {ckpt_task_id}")
            # KNOWN LIMITATION: ``all_results`` restarts empty, so this row records only the
            # trials evaluated AFTER the resume -- the earlier ones stay on the interrupted run's
            # own row. The SEARCH is unaffected (population, elites, best_individual and both RNG
            # states are all restored), and elites re-appear in later generations, so best_params
            # is intact; only the top-N candidate POOL is thinner than an uninterrupted run's.
            # Carrying all_results in the checkpoint would embed every trial's trades JSON in it.
        else:
            # Warm-start (NOT resume): seed this job's population from a DIFFERENT, already-run
            # optimization's individuals, but run this job's OWN fresh --generations budget from
            # generation 0 (start_gen stays 0) with its OWN --seed. Distinct from checkpoint-resume
            # above (which continues THIS SAME interrupted job, carrying over its generation
            # counter + RNG state) -- checkpoint-resume takes priority when both could apply.
            warm_start_id = ga.get("warmStartFromOptimizationId")
            if warm_start_id:
                source_opt = db.query(StrategyOptimization).filter(
                    StrategyOptimization.id == warm_start_id
                ).first()
                if source_opt is None:
                    return _fail(
                        opt_id, db,
                        f"warm-start source optimization {warm_start_id} not found",
                    )
                init_pop = _build_warm_start_population(
                    source_opt, optimizer, int(ga["populationSize"])
                )
                if init_pop is None:
                    logger.warning(
                        f"strategy_optimization {opt_id}: warm-start source {warm_start_id} "
                        "has no all_results to seed from; starting with a fresh population"
                    )
                else:
                    logger.warning(
                        f"strategy_optimization {opt_id}: warm-started population "
                        f"({len(init_pop)} individuals) from optimization {warm_start_id}"
                    )

        # Suppress per-trial verbose logging for the optimization's duration — across many
        # trials it's pure noise (only a SINGLE standalone backtest should log in detail).
        # A per-name setLevel() list was used before but did NOT hold: the levels get clobbered
        # back to DEBUG during trial setup, and the list also missed the per-instance expert
        # loggers ("fmprating_exp1" is not a child of "ba2_experts"). A GLOBAL logging.disable()
        # short-circuits Logger.isEnabledFor at the manager level BEFORE any LogRecord is
        # built/formatted/flushed, for EVERY logger regardless of name or level — killing the
        # ~17k INFO/DEBUG records/trial (the dominant optimize wall-time cost). Floor is INFO so
        # WARNING+ (e.g. the "trial failed in worker" notice and the post-run summary) survive.
        import logging as _logging
        _prior_disable = _logging.root.manager.disable
        _logging.disable(_logging.INFO)

        # Spin up the process pool once for the whole run (spawn -> each worker pays the
        # import cost once). batch_fitness routes the per-generation batch through it — either
        # straight to the pool (local), or through the TrialBroker so remote workers can help
        # (distributed). Distribution engages ONLY when a remote worker is online, so the default
        # path is byte-identical to the local-only behaviour (zero overhead / zero risk).
        _pool = None
        batch_fitness = None
        _evaluator = None
        if parallel > 1:
            import multiprocessing as _mp
            from concurrent.futures import ProcessPoolExecutor

            _env = {k: _os.environ[k] for k in _WORKER_ENV_KEYS if _os.environ.get(k)}

            def _make_pool() -> ProcessPoolExecutor:
                return ProcessPoolExecutor(
                    max_workers=parallel,
                    mp_context=_mp.get_context("spawn"),
                    initializer=_worker_init,
                    initargs=(_BACKEND_DIR, _env),
                )

            _pool = _make_pool()
            try:
                _workers = _resolve_workers(db, opt.worker_ids)
            except Exception as e:  # noqa: BLE001 — distribution is optional; never block a run
                logger.warning(f"worker resolution failed, running local-only: {e}")
                _workers = []
            if _workers:
                from app.services.distributed_eval import DistributedEvaluator
                from app.services.self_update import get_version_info, unsyncable_reason
                _master_version = get_version_info().get("app_version")
                _unsyncable = unsyncable_reason()
                if _unsyncable:
                    logger.warning(
                        f"opt {opt_id}: distributed run selected {len(_workers)} worker(s), but "
                        f"{_unsyncable} (this master's app_version={_master_version!r} may not "
                        f"be reachable by ANY worker's git pull)"
                    )
                _max_remote_slots = _max_remote_slots_for_experts(backtest_cfg)
                _evaluator = DistributedEvaluator(
                    _pool, opt.fitness_metric, parallel, opt_id,
                    workers=_workers, master_version=_master_version,
                    pool_factory=_make_pool,
                    max_remote_slots_per_worker=_max_remote_slots,
                )
                _evaluator.start()  # pre-flight: version-match + cache-push each worker
                batch_fitness = make_batch_fitness(_evaluator.execute_jobs)
                logger.warning(f"strategy_optimization {opt_id}: DISTRIBUTED across "
                               f"{len(_workers)} selected worker(s) + local"
                               + (f" (remote slots capped at {_max_remote_slots}/worker)"
                                  if _max_remote_slots else ""))
            else:
                batch_fitness = make_batch_fitness(_local_execute_jobs)
        try:
            result = optimizer.optimize(
                fitness_function=fitness_function,
                callback=ga_callback,
                on_generation_start=on_generation_start,
                checkpoint_callback=checkpoint_cb,
                start_generation=start_gen,
                initial_population=init_pop,
                batch_fitness=batch_fitness,
            )
        except _FatalTrialError as e:
            # Stops at the CURRENT generation rather than completing the search on data that is
            # known to be incomplete. Whatever was evaluated so far is discarded deliberately:
            # a partial population scored against a crippled universe is not a result.
            logger.error(f"strategy_optimization {opt_id}: ABORTING at generation "
                         f"{gen_state['gen'] + 1}/{ga['generations']} — {e}")
            return _fail(opt_id, db, str(e))
        finally:
            _logging.disable(_prior_disable)
            if _evaluator is not None:
                _evaluator.stop()
            if _pool is not None:
                _pool.shutdown(wait=True, cancel_futures=True)

        # Trust guard: if EVERY trial failed (e.g. a bad backtest config), all_results is
        # empty and best_fitness is a meaningless default. The GA swallows per-trial
        # exceptions as warnings, so without this guard the optimization would report
        # "completed" having evaluated NOTHING. Fail loudly instead.
        if not all_results:
            if fatal["msg"]:
                # A FATAL data error (OHLCV cache miss) — surface the actionable message directly
                # instead of the generic "check the logs" hint.
                return _fail(opt_id, db, fatal["msg"])
            return _fail(
                opt_id, db,
                "optimization produced 0 successful trials — every backtest failed. Check the "
                "logs for per-trial 'Fitness evaluation failed' warnings (e.g. a bad backtest "
                "config) before trusting any result.",
            )

        # The search finished: drop the checkpoint so a later re-run of this name starts a fresh
        # search instead of resuming from the final generation of a completed one.
        _clear_checkpoint(ckpt_task_id)

        opt.status = "completed"
        opt.completed_at = datetime.now()
        opt.progress = 100.0
        opt.best_params = result["best_params"]
        opt.best_fitness = result["best_fitness"]
        opt.all_results = all_results
        db.commit()
        push_optimization(opt, db)
        logger.info(
            f"strategy_optimization {opt_id} done: "
            f"best_fitness={result['best_fitness']:.4f} "
            f"memo hits/misses={memo.hits}/{memo.misses}"
        )
        if last_gen_full_results:
            _last_gen_full_results_by_opt[opt_id] = last_gen_full_results
        return {
            "status": "completed",
            "optimization_id": opt_id,
            "best_fitness": result["best_fitness"],
            "best_params": result["best_params"],
        }

    except InterruptedError:
        return {"status": "paused"}
    except Exception as e:  # noqa: BLE001 — any crash must fail the row, not the worker
        logger.error(f"strategy_optimization {opt_id} crashed: {e}", exc_info=True)
        return _fail(opt_id, db, str(e))
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Bypass-expert detection (piece 1c)
# ---------------------------------------------------------------------------
def _is_bypass_expert(backtest_cfg: Dict[str, Any]) -> bool:
    """True iff ANY expert named in the daily backtest_cfg declares ``bypasses_classic_rm``.

    Resolves each expert class name through the daily handler's ``_SUPPORTED_EXPERTS`` map and
    reads the class-level marker (``getattr(cls, 'bypasses_classic_rm', False)``). A bypass
    expert (e.g. FactorRanker) rebalances to target weights via its own portfolio manager, so
    the optimizer must drop the tp/sl/cond:*/exit:* namespaces and search only model:*.

    Only the ``daily`` engine has the expert-aware bypass concept; the ML engine path is never
    a bypass. An unresolvable / unknown class is treated as NON-bypass (the validating handler
    rejects unknown experts at run time — this stays defensive and never raises here).
    """
    if backtest_cfg.get("engine", "daily") != "daily":
        return False
    import importlib

    from app.services.backtest.daily_backtest_handler import _SUPPORTED_EXPERTS

    for spec in backtest_cfg.get("experts", []) or []:
        class_name = spec.get("class") if isinstance(spec, dict) else spec
        module_path = _SUPPORTED_EXPERTS.get(class_name)
        if not module_path:
            continue
        try:
            module = importlib.import_module(module_path)
            expert_cls = getattr(module, class_name)
        except Exception:  # noqa: BLE001 — never let detection raise; default to non-bypass
            continue
        if bool(getattr(expert_cls, "bypasses_classic_rm", False)):
            return True
    return False


def _max_remote_slots_for_experts(backtest_cfg: Dict[str, Any]) -> Optional[int]:
    """Tightest ``max_remote_worker_slots`` declared by any expert named in *backtest_cfg*.

    Mirrors ``_is_bypass_expert``'s resolution (same ``_SUPPORTED_EXPERTS`` lookup). Memory-heavy
    experts (e.g. FMPSenateTraderWeight — see its class attribute) cap how many concurrent remote
    dispatcher slots ``DistributedEvaluator`` engages per worker, regardless of the worker's
    reported ``/health`` capacity, so a worker advertising more slots than the expert's per-trial
    footprint can safely run isn't driven into OOM. Returns None (uncapped — use the worker's
    full reported capacity, the pre-existing behaviour) when no named expert declares a cap.
    """
    import importlib
    import os as _os

    from app.services.backtest.daily_backtest_handler import _SUPPORTED_EXPERTS

    cap: Optional[int] = None
    # OPERATIONAL OVERRIDE (added 2026-08-09). The per-expert attribute is a property of the
    # STRATEGY's memory footprint; this is a property of the BOX on a given day. When a worker is
    # under pressure you need to shed slots for every expert in the run without editing expert
    # classes (which would be a code change, would apply to every future run, and would be easy to
    # forget to revert). Applied as the TIGHTEST of the two, so it can only ever reduce
    # concurrency -- an env var must not be able to talk an expert into exceeding its declared
    # safe slot count. Unset -> pre-existing behaviour, exactly.
    _env = _os.environ.get("BA2_MAX_REMOTE_SLOTS")
    if _env:
        try:
            _env_cap = int(_env)
            if _env_cap > 0:
                cap = _env_cap
        except ValueError:
            pass
    for spec in backtest_cfg.get("experts", []) or []:
        class_name = spec.get("class") if isinstance(spec, dict) else spec
        module_path = _SUPPORTED_EXPERTS.get(class_name)
        if not module_path:
            continue
        try:
            module = importlib.import_module(module_path)
            expert_cls = getattr(module, class_name)
        except Exception:  # noqa: BLE001 — never let detection raise; default to uncapped
            continue
        expert_cap = getattr(expert_cls, "max_remote_worker_slots", None)
        if expert_cap is not None:
            cap = expert_cap if cap is None else min(cap, expert_cap)
    return cap


# ---------------------------------------------------------------------------
# The Phase-2 seam (the GA fitness target)
# ---------------------------------------------------------------------------
def _build_hoisted_state(backtest_cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Compute the param-INDEPENDENT pass ONCE per run (determinism lever 2).

    For the daily engine, the param-independent input is the fixed price/indicator
    cache the per-trial ``run_daily_backtest`` preloads over the (start,end) window.
    The cache content is identical across trials for a fixed (instruments, date range),
    so the hoisted state here just carries the resolved backtest_cfg through to the
    trial runner; the engine's intrinsic seeding makes each trial deterministic.

    NOTE (perf-todo, not a correctness issue): the current Phase-2 runner re-preloads
    the AsOfPriceSource per call. A future optimization can pre-build and reuse the
    AsOfPriceSource bundle here; until then this is an explicit known perf-todo.

    SCREENER: when the run optimizes screener settings (``backtest.screener_opt`` present)
    the parquet metric store is loaded ONCE here to warm the per-worker memo (so every trial's
    per-day filter reads it in-memory), and the store path + base settings + scan cadence are
    stashed for ``_build_daily_trial_config`` to weave into each individual's runtime block.

    Returns an opaque dict consumed by ``_run_trial_backtest``.
    """
    hoisted: Dict[str, Any] = {"backtest_cfg": backtest_cfg}
    screener_opt = backtest_cfg.get("screener_opt")
    if screener_opt:
        from ba2_providers.screener import metric_store as _ms

        _ms.load_store(screener_opt["store"])  # warms the per-worker memo
        hoisted["screener_store"] = screener_opt["store"]
        hoisted["screener_base"] = screener_opt.get("base_settings", {})
        hoisted["screener_cadence_days"] = int(screener_opt.get("cadence_days", 7))  # default weekly
        # BYPASS experts (e.g. FactorRanker) build their DYNAMIC universe from the metric store by
        # reading universe_source / screener_store / screener_* off their OWN settings — NOT the
        # classic ``screener_runtime`` entry gate. When the launcher tags this run for a bypass
        # expert, push those settings onto the expert's per-trial config (see _build_daily_trial_config).
        hoisted["screener_apply_to_expert_settings"] = bool(
            screener_opt.get("apply_to_expert_settings")
        )
        # GATE-ONLY mode (options grid max-stock-price): the store rides along PURELY as a
        # per-bar entry gate — _build_daily_trial_config skips its candidate-bound universe
        # restriction so the static run universe is kept byte-identical.
        hoisted["screener_gate_only"] = bool(screener_opt.get("gate_only"))
    return hoisted


def _run_trial_backtest(
    backtest_cfg: Dict[str, Any],
    hoisted: Dict[str, Any],
    decoded: Dict[str, Any],
) -> Dict[str, Any]:
    """Run ONE deterministic backtest with the decoded trial params; return results dict.

    The default (and the design's first-class path) is the Phase-2 SYNCHRONOUS daily
    runner (``daily_backtest_handler.run_daily_backtest``) for ba2-expert strategies with
    multi-asset classic RM. The decoded trial params are injected per the Replan seam:
      * ``decoded['expert_overrides']`` (model:* keyed by the REAL ba2 setting names, incl.
        RM sizing such as ``risk_per_trade_pct``) is MERGED into each expert's settings dict
        (the engine feeds settings to ``_process``; the RM reads its sizing params off the
        expert via ``get_setting_with_interface_default``);
      * ``decoded['entry_rules']`` / ``decoded['exit_rules']`` are the substituted TradeRule
        lists (unified rule model, migration 028) — the engine seeds the ENTER_MARKET /
        OPEN_POSITIONS rulesets 1:1 from them.

    The legacy ML-expert single-asset path (``backtest_handler.run_backtest``) is kept as a
    lazily-imported fallback for ``engine == 'ml'`` so it never pulls torch unless explicitly
    requested.
    """
    engine = backtest_cfg.get("engine", "daily")
    if engine == "daily":
        from app.services.backtest.daily_backtest_handler import run_daily_backtest

        config = _build_daily_trial_config(backtest_cfg, decoded, hoisted)
        return run_daily_backtest(config)

    if engine == "ml":
        return _run_ml_trial_backtest(backtest_cfg, decoded)

    raise ValueError(
        f"Unknown backtest engine: {engine!r} (valid: 'daily', 'ml')"
    )


def _build_daily_trial_config(
    backtest_cfg: Dict[str, Any],
    decoded: Dict[str, Any],
    hoisted: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Assemble the ``run_daily_backtest`` config for one trial from the run-level
    backtest_cfg + the decoded trial params.

    The expert settings the engine feeds to ``_process`` are merged with the decoded
    expert_overrides (model:* numeric decision settings). RM sizing is part of that set:
    it is optimized through ``model:*`` keyed by the REAL ba2 setting names (e.g.
    ``risk_per_trade_pct``), so the classic RM sizes against the trial's risk config
    with no separate mapping needed.

    BYPASS expert (piece 1c): for an expert that declares ``bypasses_classic_rm``, ``bypass``
    below gates the screener-settings wiring further down — it has no tp/sl implications since
    entry TP/SL rides on ``entry_rules`` (Strategy.entry_actions), not a bespoke gene.
    """
    bypass = _is_bypass_expert(backtest_cfg)
    overrides = dict(decoded.get("expert_overrides") or {})

    # OPTIONS seam (parity with the single-run path daily_backtest_handler._build_config):
    # if the decoded trial's exit rules name an OPTION action, derive the offline options-cache
    # path so run_daily_backtest builds + injects the HistoricalOptionsProvider for THIS trial
    # — without it the option rule can't fetch a chain and the option genes have no effect.
    # An explicit run-level backtest_cfg['options_cache_db'] is forwarded as-is (e.g. a fixture
    # cache pinned by the caller); otherwise it is derived from the decoded option rules.
    # Equity-only trials -> options_cache_db stays None -> byte-identical to the prior behaviour.
    from app.services.backtest.daily_backtest_handler import (
        strategy_uses_options,
        default_options_cache_db,
        validate_options_window,
    )

    # A pure-option ENTRY (the enter_market ruleset fires an option action directly, no equity leg)
    # is carried run-level on backtest_cfg['entry_action'] and is also an options run — include it in
    # the options-seam detection so the cache is derived even when no EXIT rule names an option.
    entry_action = backtest_cfg.get("entry_action")
    options_cache_db = backtest_cfg.get("options_cache_db")
    if not options_cache_db and strategy_uses_options(
        {"exit_rules": decoded.get("exit_rules"), "entry_action": entry_action}
    ):
        options_cache_db = default_options_cache_db()
    validate_options_window(backtest_cfg["start_date"], bool(options_cache_db))

    # BYPASS-expert screener wiring: a bypass expert (e.g. FactorRanker) builds its DYNAMIC
    # universe from the fast metric_store by reading ``universe_source`` / ``screener_store`` /
    # ``screener_*`` off its OWN settings — it does NOT consult the classic ``screener_runtime``
    # entry gate (which only affects the classic entry-gate path). So when the run is tagged to
    # apply the screener to the bypass expert's settings, push the store path + universe_source +
    # the decoded per-individual screener genes onto that expert's per-trial settings so the GA
    # optimizes its screener thresholds each generation. (The screener_overrides keys are the
    # ``screener_*``-prefixed names FactorRanker._metric_store_settings() translates.) For
    # non-bypass / non-screener runs this dict is empty and nothing changes.
    bypass_screener_settings: Dict[str, Any] = {}
    if (
        bypass
        and hoisted
        and hoisted.get("screener_store")
        and hoisted.get("screener_apply_to_expert_settings")
    ):
        bypass_screener_settings = {
            "universe_source": "screener",
            "screener_store": hoisted["screener_store"],
            # Base (run-level, non-optimized) screener settings overlaid with the per-individual
            # decoded screener genes — same precedence as the classic screener_runtime path.
            **(hoisted.get("screener_base") or {}),
            **(decoded.get("screener_overrides") or {}),
        }

    # Merge the per-trial overrides into each expert spec's settings (do NOT mutate the
    # run-level backtest_cfg — build fresh spec dicts). The bypass screener settings are layered
    # UNDER the model:* overrides so an explicitly-optimized expert param still wins.
    experts_in = backtest_cfg["experts"]
    experts_out = []
    for spec in experts_in:
        if isinstance(spec, dict):
            merged_settings = dict(spec.get("settings") or {})
            merged_settings.update(bypass_screener_settings)
            merged_settings.update(overrides)
            experts_out.append({"class": spec["class"], "settings": merged_settings})
        else:
            merged_settings = dict(bypass_screener_settings)
            merged_settings.update(overrides)
            experts_out.append({"class": spec, "settings": merged_settings})

    # SCREENER runtime: when the run hoisted a metric store, this individual's EFFECTIVE screener
    # settings are base (run-level, non-optimized) overlaid with the per-individual decoded
    # screener overrides. The engine reads ``screener_runtime`` to gate entries to the per-day
    # screened universe (latest scan <= bar; cadence held between scans). Absent for non-screener
    # runs -> the engine's gating is a no-op and the config is byte-identical to before.
    screener_runtime = None
    screener_candidate: Optional[List[str]] = None
    if hoisted and hoisted.get("screener_store"):
        from ba2_providers.screener import metric_store as _ms
        from ba2_providers.screener.metric_store import normalize_screener_settings
        eff = {
            **(hoisted.get("screener_base") or {}),
            **(decoded.get("screener_overrides") or {}),
        }
        # NORMALIZE the effective settings to the metric store's unprefixed keys. The merged
        # base+gene dict carries ``screener_``-prefixed keys (gene namespace); without this the
        # engine's per-bar gate (metric_store.screen_universe_for_day, which reads UNPREFIXED keys)
        # silently ignored every criterion except ``market_cap_max`` — the screener-settings-opt
        # bug. This makes the optimizer gate apply the SAME criteria as the standalone/UI path.
        eff_norm = normalize_screener_settings(eff)
        screener_runtime = {
            "store": hoisted["screener_store"],
            "settings": eff_norm,
            "cadence_days": hoisted.get("screener_cadence_days", 7),
        }
        # CANDIDATE BOUND (non-bypass): restrict the loaded universe to the symbols THIS trial's
        # screen can EVER select over [start,end] (screened_symbol_union with the trial's own eff
        # settings), intersected with the band. The per-bar gate already restricts ENTRIES to a
        # subset of this, so results are IDENTICAL — but the engine no longer preloads/analyses the
        # whole band (e.g. 814 -> ~150 symbols), the dominant screener-opt memory + CPU cost. Matches
        # the standalone path's _resolve_enabled_instruments. Bypass experts keep the full band (they
        # rank the whole universe). Exact per-trial bound — no gene-tightening assumption.
        if not bypass and not hoisted.get("screener_gate_only"):
            try:
                _df = _ms.load_store(hoisted["screener_store"])
                _sd = str(backtest_cfg["start_date"])[:10]
                _ed = str(backtest_cfg["end_date"])[:10]
                _union = set(_ms.screened_symbol_union(_df, _sd, _ed, eff_norm))
                screener_candidate = [s for s in backtest_cfg["enabled_instruments"] if s in _union]
            except Exception:  # noqa: BLE001 — never break a trial on the optimization; fall back to full band
                screener_candidate = None

    # UNIQUE per-trial id: parallel trials each name their OWN per-run sqlite, so they never
    # collide on the same file (WinError 32 / cross-thread session). The run-level id is a base.
    import uuid as _uuid
    trial_id = f"{backtest_cfg['backtest_id']}-{_uuid.uuid4().hex[:8]}"

    # SCHEDULE-DAY genes (schedule:<day>): when the param space collected them, THIS individual's
    # decoded per-day toggles replace the run-level static days entirely — time-of-day stays
    # static (pulled from the run-level override, unaffected by the genes) since only the day
    # selection is being optimized for now.
    base_run_sched = backtest_cfg.get("run_schedule_override")
    if decoded.get("schedule_days"):
        run_schedule_override = {
            "days": decoded["schedule_days"],
            "times": (base_run_sched or {}).get("times") or ["09:30"],
        }
    else:
        run_schedule_override = base_run_sched

    return {
        "backtest_id": trial_id,
        "name": backtest_cfg.get("name", f"opt-trial-{trial_id}"),
        "start_date": backtest_cfg["start_date"],
        "end_date": backtest_cfg["end_date"],
        "enabled_instruments": (
            list(screener_candidate) if screener_candidate is not None
            else list(backtest_cfg["enabled_instruments"])
        ),
        "experts": experts_out,
        "initial_capital": float(backtest_cfg["initial_capital"]),
        "account_settings": backtest_cfg["account_settings"],
        "warmup_days": int(backtest_cfg["warmup_days"]),
        "seed": int(backtest_cfg["seed"]),
        "subtype": backtest_cfg.get("subtype"),
        # Cadence (weekly entry) + intraday fill clock carry through to each trial's engine.
        "run_schedule_override": run_schedule_override,
        "manage_schedule_override": backtest_cfg.get("manage_schedule_override"),
        "execution_interval": backtest_cfg.get("execution_interval", "1d"),
        # Per-trade profit cap (% of cost basis): the GA ranks on the ADJUSTED fitness so one
        # lucky, non-reproducible mega-winner can't win the search. None = no cap. Carried from
        # the run-level optimize backtest block into every trial.
        "profit_cap_pct": backtest_cfg.get("profit_cap_pct"),
        # Portfolio-share cap (% of net profit any single trade may contribute) — same robustness
        # role as profit_cap_pct, bounding one trade's share of TOTAL return. Carried per-trial.
        "profit_share_cap_pct": backtest_cfg.get("profit_share_cap_pct"),
        # Optional trade-frequency fitness scale (down-weight statistically thin few-trade configs).
        "fitness_trade_scale": backtest_cfg.get("fitness_trade_scale"),
        "fitness_trade_scale_cap": backtest_cfg.get("fitness_trade_scale_cap"),
        "fitness_trade_scale_target": backtest_cfg.get("fitness_trade_scale_target"),
        # Optional win-rate fitness factor (2 * win_rate_fraction; 50% win = 1.0x break-even).
        "fitness_win_rate_factor": backtest_cfg.get("fitness_win_rate_factor"),
        # Spread-STRESS level (bps). This dict is a WHITELIST -- it rebuilds the per-trial config
        # key by key rather than copying backtest_cfg -- so a knob missing HERE is silently dead
        # no matter how correctly it was parsed, stored and echoed upstream. That is exactly what
        # happened on the first stressed grid: --stress-spread-bps reached the optimize process
        # and the run config, but never the trial config, so build_results echoed 0.0 and every
        # "stressed" job was scored unstressed.
        "stress_spread_bps": backtest_cfg.get("stress_spread_bps"),
        # Optimizer-decoded TradeRule lists (unified rule model, migration 028): the engine
        # seeds the ENTER_MARKET / OPEN_POSITIONS rulesets 1:1 from these (one EventAction per
        # rule, all actions + continue_processing verbatim; disabled rules/actions already
        # pruned by decode_params).
        "entry_rules": decoded.get("entry_rules"),
        "exit_rules": decoded.get("exit_rules"),
        # Pure-option ENTRY action (no equity leg): forwarded run-level so daily_backtest_handler.
        # _build_experts seeds the enter ruleset with it. None for equity strategies (unchanged).
        "entry_action": entry_action,
        # "Allow short" -> seed the symmetric SHORT enter rule + RM sell gate (mirrors the
        # single-backtest path). Carried from the run-level optimize backtest block.
        "enable_short": bool(backtest_cfg.get("enable_short")),
        # OPTIONS seam: a non-None path here flags an options trial — run_daily_backtest builds
        # the HistoricalOptionsProvider from it and injects it into the BacktestAccount so the
        # option exit rule (and its option_delta/option_dte genes) can fetch a chain. None for an
        # equity-only trial (byte-identical to the prior behaviour).
        "options_cache_db": options_cache_db,
        # SCREENER seam: the per-individual effective screener settings + store path the engine
        # uses to gate entries to the per-day screened universe. None for non-screener runs.
        "screener_runtime": screener_runtime,
    }


def _run_ml_trial_backtest(
    backtest_cfg: Dict[str, Any], decoded: Dict[str, Any]
) -> Dict[str, Any]:
    """Legacy ML-expert single-asset adapter (``backtest_handler.run_backtest``).

    Lazily imported so torch is only pulled when ``engine == 'ml'`` is explicitly
    requested with a real model/datasets present.
    """
    from app.services.backtest_handler import run_backtest, _empty_results
    import pandas as pd

    db = SessionLocal()
    try:
        from app.models import Dataset, TrainedModel

        model = db.query(TrainedModel).filter(
            TrainedModel.id == backtest_cfg["model_id"]
        ).first()
        pred = db.query(Dataset).filter(
            Dataset.id == backtest_cfg["prediction_dataset_id"]
        ).first()
        exe = db.query(Dataset).filter(
            Dataset.id == backtest_cfg["execution_dataset_id"]
        ).first()
        if not (model and pred and exe):
            return _empty_results(float(backtest_cfg.get("initial_capital", 10000.0)))
        pred_df = pd.read_csv(pred.file_path)
        exec_df = pd.read_csv(exe.file_path)
        for df in (pred_df, exec_df):
            if "Date" in df.columns:
                df["Date"] = pd.to_datetime(df["Date"])
        # The legacy 'ml' engine was explicitly OUT OF SCOPE for the entry-actions migration
        # AND for the unified rule model (migration 028): the Strategy no longer stores the
        # condition trees this engine consumes, so this path runs the STATIC historical
        # defaults (5.0/2.0 bracket, no optimizer-substituted trees) — the same values
        # backtest_handler.run_backtest's single-run path uses. Condition/rule genes have no
        # effect on 'ml'-engine trials (they already effectively didn't).
        strategy_params = {
            "initial_tp_percent": 5.0,
            "initial_sl_percent": 2.0,
        }
        return run_backtest(
            model=model,
            pred_df=pred_df,
            exec_df=exec_df,
            strategy_params=strategy_params,
            initial_capital=float(backtest_cfg.get("initial_capital", 10000.0)),
            position_sizing_type=backtest_cfg.get("position_sizing_type", "percent"),
            position_sizing_value=backtest_cfg.get("position_sizing_value", 10.0),
            commission=backtest_cfg.get("commission", 0.0),
            slippage=backtest_cfg.get("slippage", 0.0),
            buy_entry_conditions=None,
            sell_entry_conditions=None,
            exit_conditions=None,
        )
    finally:
        db.close()


# ---------------------------------------------------------------------------
# brute force + checkpoint persistence
# ---------------------------------------------------------------------------
def _run_brute_force(
    opt: Any,
    db: Any,
    task_id: str,
    param_space: Dict[str, Any],
    fitness_function,
    all_results: list,
) -> Dict[str, Any]:
    """Exhaustive search over the stepped ranges (itertools.product) for tiny spaces.

    Has its own completion logic entirely separate from the GA path (no per-generation
    callback either — it's a flat loop, not generational), so the completion push below is
    this path's ONLY sync point; a brute-force run's failure path is NOT separate from the
    GA's, though: ``fitness_function`` raising here propagates straight out to
    ``handle_strategy_optimization``'s own try/except, which already routes any failure
    through the shared ``_fail()`` helper (itself already wired to push_optimization) — so no
    separate failure-push is needed here.
    """
    import itertools

    axes: Dict[str, list] = {}
    for name, spec in param_space.items():
        vals, v = [], spec["min"]
        while v <= spec["max"] + 1e-9:
            vals.append(int(round(v)) if spec["type"] == "int" else round(v, 10))
            v += spec["step"]
        axes[name] = vals
    names = list(axes.keys())
    best = {"fitness": None, "params": None}
    for combo in itertools.product(*(axes[n] for n in names)):
        flat = dict(zip(names, combo))
        fit = fitness_function(flat)
        if best["fitness"] is None or fit > best["fitness"]:
            best = {"fitness": fit, "params": flat}
    opt.status = "completed"
    opt.completed_at = datetime.now()
    opt.progress = 100.0
    opt.best_params = best["params"]
    opt.best_fitness = best["fitness"]
    opt.all_results = all_results
    db.commit()
    push_optimization(opt, db)
    return {
        "status": "completed",
        "optimization_id": opt.id,
        "best_fitness": best["fitness"],
        "best_params": best["params"],
    }


def checkpoint_task_id(opt_name: Optional[str], opt_id: int) -> str:
    """The key a GA checkpoint is stored under, STABLE ACROSS RE-RUNS of the same job.

    Deliberately keyed on the optimization NAME, not its row id: re-running an interrupted job
    INSERTS a new StrategyOptimization row (the aborted goal2020 job was 248, its relaunch 249),
    so an id-keyed checkpoint could never be found by the very run that needs it. The name is what
    the grid driver already treats as job identity when it decides what to skip.

    Hashed because ``TaskQueue.task_id`` is String(50) and job names run longer than that
    (``scr-large-FMPRating-S1-goal2020-riskatr-from2022`` is 47 chars before any prefix).
    """
    import hashlib

    key = (opt_name or f"opt-{opt_id}").strip()
    return "ckpt-" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:24]


def checkpoint_fingerprint(param_space: Dict[str, Any], ga: Dict[str, Any]) -> str:
    """Identity of the SEARCH ITSELF -- a checkpoint may only be resumed into a matching one.

    A GA checkpoint is a list of chromosomes plus an RNG state; both are meaningless against a
    different gene space. Adding the 4 regime-overlay genes to ``_RM_OPT`` on 2026-08-04 changed
    every classic expert's chromosome LENGTH, so an older checkpoint restored into the new space
    would misread each locus -- silent corruption, not a crash. Population size matters for the
    same reason (elitism/selection slice a fixed-length list), and generations because the
    restored counter is compared against it.

    Gene ORDER is included (not just the set): ``encode_params`` maps by position.
    """
    import hashlib
    import json

    payload = {
        "genes": [[name, sorted((spec or {}).items(), key=lambda kv: kv[0])]
                  for name, spec in param_space.items()],
        "population": ga.get("populationSize"),
        "generations": ga.get("generations"),
    }
    blob = json.dumps(payload, sort_keys=False, default=str)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16]


def _save_checkpoint(task_id: str, checkpoint_data: Dict[str, Any]) -> None:
    """Persist a GA checkpoint to ``TaskQueue.checkpoint_data``, CREATING the row if absent.

    The row-creation is the whole fix. Checkpointing was inert for every CLI-driven run because
    ``ba2-test optimize`` calls the handler directly instead of enqueuing, so no TaskQueue row
    with that task_id existed -- and this function used to be ``if t: ...``, i.e. a silent no-op
    that ran once per generation, wrote nothing, and logged nothing. (The old comment blamed
    CLI jobs sharing one task_id and clobbering each other's checkpoints; that was wrong -- with
    no row, nothing was ever written to clobber.)
    """
    db = SessionLocal()
    try:
        t = db.query(TaskQueue).filter(TaskQueue.task_id == task_id).first()
        if t is None:
            t = TaskQueue(
                task_id=task_id,
                task_type="strategy_optimization",
                name=task_id,
                description="GA checkpoint holder for a directly-invoked (CLI) optimization",
                # RUNNING, never PENDING: a PENDING row is claimable by the queue worker, which
                # would re-dispatch this job while it is already running here.
                status=TaskStatus.RUNNING.value,
            )
            db.add(t)
        t.checkpoint_data = checkpoint_data
        db.commit()
    except Exception as e:  # noqa: BLE001 -- a checkpoint failure must never kill the run
        logger.warning(f"checkpoint save failed for {task_id}: {e}")
        db.rollback()
    finally:
        db.close()


def _clear_checkpoint(task_id: str) -> None:
    """Drop a finished job's checkpoint so a later re-run starts clean instead of resuming
    from the last generation of a search that already completed."""
    db = SessionLocal()
    try:
        t = db.query(TaskQueue).filter(TaskQueue.task_id == task_id).first()
        if t is not None:
            t.checkpoint_data = None
            # Also retire the holder row: left RUNNING it would show up forever as a phantom
            # in-flight task in the queue UI. (It is never CLAIMABLE either way -- the queue
            # only claims QUEUED rows -- so this is presentation, not safety.)
            t.status = TaskStatus.COMPLETED.value
            db.commit()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"checkpoint clear failed for {task_id}: {e}")
        db.rollback()
    finally:
        db.close()


def _load_checkpoint(task_id: str, fingerprint: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Load a GA checkpoint, refusing one whose search space no longer matches.

    ``fingerprint=None`` skips the check (queue-driven callers that predate it); a stored
    checkpoint with NO fingerprint is likewise accepted, so pre-existing checkpoints keep working.
    """
    db = SessionLocal()
    try:
        t = db.query(TaskQueue).filter(TaskQueue.task_id == task_id).first()
        ckpt = t.checkpoint_data if (t and t.checkpoint_data) else None
        if not ckpt:
            return None
        stored = ckpt.get("fingerprint")
        if fingerprint and stored and stored != fingerprint:
            logger.warning(
                f"discarding checkpoint for {task_id}: it was written for a different search "
                f"(fingerprint {stored} != {fingerprint}) — starting from generation 0")
            return None
        return ckpt
    finally:
        db.close()


def _build_warm_start_population(
    source_opt: StrategyOptimization, optimizer: GeneticOptimizer, target_size: int
) -> Optional[list]:
    """Seed a NEW job's starting population from a DIFFERENT, already-run optimization's
    individuals -- a warm start, not a resume: this job still runs its OWN fresh
    --generations budget from generation 0 (and can use a different --seed), it just starts
    from evolved genomes instead of random ones.

    ``StrategyOptimization.all_results`` accumulates ``{params, fitness, trades}`` for every
    DISTINCT individual evaluated across the WHOLE source run (all generations, deduplicated
    by the trial memo) -- there's no per-generation tag, so the LAST ``target_size`` entries
    (append order = evaluation order) are used as an approximation of its final generation.
    Real GA checkpointing (``_save_checkpoint``/``resume_from_checkpoint``) would give an
    exact final population, but it's a no-op for CLI-driven runs today (the CLI's ``optimize``
    command calls this handler directly under a single shared, non-unique task_id
    ("cli-optimize"), so every CLI job's checkpoint clobbers the previous one's before it can
    ever be read back) -- all_results is reliable regardless of how the source job ran.

    Returns encoded chromosomes (``GeneticOptimizer.encode_params`` per individual, most-recent
    first, padded with fresh random individuals up to ``target_size`` if the source had fewer),
    or None if the source has no results to seed from.
    """
    results = source_opt.all_results or []
    if not results:
        return None
    tail = results[-target_size:] if len(results) > target_size else list(results)
    population = [optimizer.encode_params(e.get("params") or {}) for e in tail]
    while len(population) < target_size:
        population.append(optimizer.toolbox.individual())
    return population[:target_size]
