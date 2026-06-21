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
from datetime import datetime
from typing import Any, Dict, Optional

import numpy as np

from app.models import (
    SessionLocal,
    Strategy as StrategyModel,
    StrategyOptimization,
    TaskQueue,
)
from app.services.genetic import GeneticOptimizer, DEAP_AVAILABLE
from app.services.task_queue import get_task_queue
from app.services.strategy_param_space import collect_param_space, decode_params
from app.services.strategy_fitness import compute_fitness, ZERO_TRADE_SENTINEL
from app.services.trial_memo import trial_key, TrialMemo

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
    import logging as _lg
    _lg.disable(_lg.ERROR)  # workers are silent; the parent process logs the run summary
    for n in ("ba2_common", "ba2_providers", "ba2_experts", "app.services.backtest"):
        _lg.getLogger(n).setLevel(_lg.WARNING)


def _trial_worker(config: Dict[str, Any], fitness_metric: str) -> Dict[str, Any]:
    """Run ONE deterministic daily backtest in a worker PROCESS and return a tiny summary.

    Only the CPU-bound backtest runs here (no GIL contention with the GA loop); the result is
    reduced to ``{ok, fitness, trades, error}`` so the pickled payload back to the parent is
    small (the full equity/trade blobs are re-derived later for the persisted top-N only).
    """
    try:
        from app.services.backtest.daily_backtest_handler import run_daily_backtest
        from app.services.strategy_fitness import compute_fitness

        results = run_daily_backtest(config)
        fit = compute_fitness(fitness_metric, results)
        return {"ok": True, "fitness": float(fit),
                "trades": int(results.get("total_trades") or 0), "error": None}
    except Exception as e:  # noqa: BLE001 — surface as a failed trial, don't kill the pool
        # A cache miss is FATAL (a data/config problem, not a bad-parameter trial): every trial
        # will hit the same gap, so flag it so the parent can abort with the actionable message
        # instead of grinding the whole population to 0 fitness.
        fatal = type(e).__name__ == "BacktestCacheMiss"
        return {"ok": False, "fitness": 0.0, "trades": 0, "error": str(e) if fatal else repr(e),
                "fatal": fatal}


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


def _fail(opt_id: int, db: Any, msg: str) -> Dict[str, Any]:
    """Mark the StrategyOptimization row failed + return the failure dict."""
    logger.error(f"strategy_optimization {opt_id} failed: {msg}")
    row = db.query(StrategyOptimization).filter(StrategyOptimization.id == opt_id).first()
    if row:
        row.status = "failed"
        row.error_message = msg[:1000]
        row.completed_at = datetime.now()
        db.commit()
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

        # SCREENER genes share the expert_params dict (the launcher merges them in pre-namespaced
        # with ``screener:``). Split them out so they route to the screener namespace instead of
        # being mis-prefixed as ``model:screener:*`` by _collect_expert: the model space gets the
        # non-screener keys, and a screener_cfg (prefix stripped back to the bare setting name) is
        # passed alongside so collect_param_space emits the ``screener:<setting>`` genes.
        model_cfg = None
        screener_cfg = None
        if expert_cfg:
            model_cfg = {k: v for k, v in expert_cfg.items() if not k.startswith("screener:")}
            screener_cfg = {
                k[len("screener:"):]: v
                for k, v in expert_cfg.items()
                if k.startswith("screener:")
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
                screener_cfg=screener_cfg,
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
            if tq.is_task_paused(task_id):
                raise InterruptedError("paused/cancelled")

        def checkpoint_cb(generation: int, population: list):
            _save_checkpoint(
                task_id, optimizer.get_checkpoint_data(generation, population)
            )

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
                for i, flat in enumerate(param_dicts):
                    key = _trial_key_for(flat)
                    cached = memo.get(key)
                    if cached is not None:
                        fits[i] = cached
                        continue
                    config = _build_daily_trial_config(
                        backtest_cfg, decode_params(strategy, flat), hoisted
                    )
                    jobs.append((i, flat, key, config))

                # Intra-generation progress: report individuals evaluated WITHIN the current
                # generation (UI's per-generation bar). The overall % blends the generation
                # index with the in-batch fraction so it advances smoothly between gen boundaries
                # (ga_callback still snaps it to the exact boundary at gen end). Throttled to
                # ~20 updates/gen so we don't hammer the task DB.
                total_in_batch = len(param_dicts)
                n_gens = int(ga["generations"])
                gen = gen_state["gen"]
                step = max(1, total_in_batch // 20)

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
                    fit = float(out["fitness"])
                    fits[i] = fit
                    memo.put(key, fit)
                    if out["ok"]:
                        all_results.append(
                            {"params": flat, "fitness": fit, "key": key, "trades": out["trades"]}
                        )
                    elif out.get("error"):
                        logger.warning(f"trial failed in worker: {out['error']}")
                        if out.get("fatal") and fatal["msg"] is None:
                            fatal["msg"] = out["error"]
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
                return fits

            return batch_fitness

        start_gen, init_pop = 0, None
        ckpt = _load_checkpoint(task_id)
        if ckpt:
            start_gen, init_pop = optimizer.resume_from_checkpoint(ckpt)

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
            _pool = ProcessPoolExecutor(
                max_workers=parallel,
                mp_context=_mp.get_context("spawn"),
                initializer=_worker_init,
                initargs=(_BACKEND_DIR, _env),
            )
            try:
                _workers = _resolve_workers(db, opt.worker_ids)
            except Exception as e:  # noqa: BLE001 — distribution is optional; never block a run
                logger.warning(f"worker resolution failed, running local-only: {e}")
                _workers = []
            if _workers:
                from app.services.distributed_eval import DistributedEvaluator
                from app.services.self_update import get_version_info
                _master_commit = get_version_info().get("git_commit")
                _evaluator = DistributedEvaluator(
                    _pool, opt.fitness_metric, parallel, opt_id,
                    workers=_workers, master_commit=_master_commit,
                )
                _evaluator.start()  # pre-flight: version-match + cache-push each worker
                batch_fitness = make_batch_fitness(_evaluator.execute_jobs)
                logger.warning(f"strategy_optimization {opt_id}: DISTRIBUTED across "
                               f"{len(_workers)} selected worker(s) + local")
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

        opt.status = "completed"
        opt.completed_at = datetime.now()
        opt.progress = 100.0
        opt.best_params = result["best_params"]
        opt.best_fitness = result["best_fitness"]
        opt.all_results = all_results
        db.commit()
        logger.info(
            f"strategy_optimization {opt_id} done: "
            f"best_fitness={result['best_fitness']:.4f} "
            f"memo hits/misses={memo.hits}/{memo.misses}"
        )
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
      * ``decoded['tp']`` / ``decoded['sl']`` set the initial TP/SL the ruleset applies;
      * ``decoded['buy_tree']`` / ``decoded['sell_tree']`` / ``decoded['exit_rules']`` are
        the substituted condition trees.

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

    BYPASS expert (piece 1c): for an expert that declares ``bypasses_classic_rm`` the param
    space already excludes tp/sl, so ``decoded`` carries none; but we ALSO refuse to inject
    any tp/sl override defensively (the bypass rebalance path ignores them), forwarding ONLY
    the expert's own model:* overrides.
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

    options_cache_db = backtest_cfg.get("options_cache_db")
    if not options_cache_db and strategy_uses_options(
        {"exit_rules": decoded.get("exit_rules")}
    ):
        options_cache_db = default_options_cache_db()
    validate_options_window(backtest_cfg["start_date"], bool(options_cache_db))

    # Initial TP/SL the engine applies as an OCO bracket on each opened position. These are
    # NOT expert settings (the experts don't declare them, so they'd be dropped by
    # _expert_decision_settings) — they ride on the run config and the daily engine's
    # _apply_initial_brackets reads them to stage the protective leg(s). Without this the
    # position never closes (buy-and-hold) and every trade metric is bogus.
    initial_tp = None if bypass else decoded.get("tp")
    initial_sl = None if bypass else decoded.get("sl")

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
    if hoisted and hoisted.get("screener_store"):
        eff = {
            **(hoisted.get("screener_base") or {}),
            **(decoded.get("screener_overrides") or {}),
        }
        screener_runtime = {
            "store": hoisted["screener_store"],
            "settings": eff,
            "cadence_days": hoisted.get("screener_cadence_days", 7),
        }

    # UNIQUE per-trial id: parallel trials each name their OWN per-run sqlite, so they never
    # collide on the same file (WinError 32 / cross-thread session). The run-level id is a base.
    import uuid as _uuid
    trial_id = f"{backtest_cfg['backtest_id']}-{_uuid.uuid4().hex[:8]}"
    return {
        "backtest_id": trial_id,
        "name": backtest_cfg.get("name", f"opt-trial-{trial_id}"),
        "start_date": backtest_cfg["start_date"],
        "end_date": backtest_cfg["end_date"],
        "enabled_instruments": list(backtest_cfg["enabled_instruments"]),
        "experts": experts_out,
        "initial_capital": float(backtest_cfg["initial_capital"]),
        "account_settings": backtest_cfg["account_settings"],
        "warmup_days": int(backtest_cfg["warmup_days"]),
        "seed": int(backtest_cfg["seed"]),
        "subtype": backtest_cfg.get("subtype"),
        # Cadence (weekly entry) + intraday fill clock carry through to each trial's engine.
        "run_schedule_override": backtest_cfg.get("run_schedule_override"),
        "execution_interval": backtest_cfg.get("execution_interval", "1d"),
        # Optimizer-decoded condition trees: the engine builds the enter ruleset FROM buy_tree
        # (seed_ruleset_from_tree) so cond:<id>:value thresholds + on/off toggles drive entries.
        "buy_tree": decoded.get("buy_tree"),
        "sell_tree": decoded.get("sell_tree"),
        "exit_rules": decoded.get("exit_rules"),
        # "Allow short" -> seed the symmetric SHORT enter rule + RM sell gate (mirrors the
        # single-backtest path). Carried from the run-level optimize backtest block.
        "enable_short": bool(backtest_cfg.get("enable_short")),
        # Initial TP/SL bracket percents (the tp/sl genes) — applied per opened position by
        # the daily engine so trades actually close.
        "initial_tp_percent": initial_tp,
        "initial_sl_percent": initial_sl,
        # Canonical TP-reference mode forwarded from the run-level config so every trial uses
        # the same reference (None -> engine's default percent path; "expert_target_price" ->
        # RE4 expert-target bracket). The single ``initial_tp_reference`` key + ``_apply_initial_brackets``.
        "initial_tp_reference": backtest_cfg.get("initial_tp_reference"),
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
        strategy_params = {
            "initial_tp_percent": decoded["tp"],
            "initial_sl_percent": decoded["sl"],
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
            buy_entry_conditions=decoded["buy_tree"],
            sell_entry_conditions=decoded["sell_tree"],
            exit_conditions=decoded["exit_rules"],
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
    """Exhaustive search over the stepped ranges (itertools.product) for tiny spaces."""
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
    return {
        "status": "completed",
        "optimization_id": opt.id,
        "best_fitness": best["fitness"],
        "best_params": best["params"],
    }


def _save_checkpoint(task_id: str, checkpoint_data: Dict[str, Any]) -> None:
    """Persist GA checkpoint to TaskQueue.checkpoint_data (keyed by task_id)."""
    db = SessionLocal()
    try:
        t = db.query(TaskQueue).filter(TaskQueue.task_id == task_id).first()
        if t:
            t.checkpoint_data = checkpoint_data
            db.commit()
    finally:
        db.close()


def _load_checkpoint(task_id: str) -> Optional[Dict[str, Any]]:
    """Load a GA checkpoint from TaskQueue.checkpoint_data (keyed by task_id)."""
    db = SessionLocal()
    try:
        t = db.query(TaskQueue).filter(TaskQueue.task_id == task_id).first()
        return t.checkpoint_data if (t and t.checkpoint_data) else None
    finally:
        db.close()
