"""
Strategies API endpoints.

Manages trading strategies with entry/exit conditions.
"""

import logging
from typing import List, Optional
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.models import get_db, Strategy, TrainedModel, StrategyOptimization
from app.services.task_queue import get_task_queue

logger = logging.getLogger(__name__)

router = APIRouter()


# Pydantic models
class ConditionBase(BaseModel):
    id: str
    field: Optional[str] = None
    field_type: Optional[str] = None  # model_probability, model_class, position, time
    comparison: Optional[str] = None  # >, >=, <, <=, ==, !=, between
    value: Optional[float | int | List] = None
    optimize: bool = False
    value_min: Optional[float] = None
    value_max: Optional[float] = None
    value_step: Optional[float] = None
    optimize_enabled: bool = False
    confirmation_required: Optional[int] = None
    confirmation_bars: Optional[int] = None
    confirmation_bars_min: Optional[int] = None
    confirmation_bars_max: Optional[int] = None
    confirmation_bars_step: Optional[int] = None
    operator: Optional[str] = None  # AND, OR
    conditions: Optional[List["ConditionBase"]] = None


class ExitCondition(BaseModel):
    id: str
    name: Optional[str] = None
    conditions: ConditionBase
    action: str  # close, adjust_tp, adjust_sl, or option action (e.g. buy_call)
    toggle_optimize: bool = False                 # -> exit:<id>:enabled gene (optimizer drops the whole rule)
    reference_value: Optional[str] = None         # order_open_price | current_price | expert_target_price (adjust actions)
    action_value: Optional[float] = None
    action_value_optimize: bool = False
    action_value_min: Optional[float] = None
    action_value_max: Optional[float] = None
    action_value_step: Optional[float] = None
    # --- option-action fields (None for equity actions) ---
    option_strategy: Optional[str] = None
    option_strike_method: Optional[str] = None      # delta | percent_otm | consensus_target
    option_strike_param: Optional[float] = None
    option_dte_min: Optional[int] = None
    option_dte_max: Optional[int] = None
    option_sizing: Optional[float] = None           # % of equity
    option_strike_param_optimize: bool = False
    option_strike_param_min: Optional[float] = None
    option_strike_param_max: Optional[float] = None
    option_strike_param_step: Optional[float] = None
    option_dte_optimize: bool = False
    option_dte_min_range: Optional[int] = None
    option_dte_max_range: Optional[int] = None
    option_dte_step: Optional[int] = None


class StrategyCreate(BaseModel):
    name: str
    description: Optional[str] = None
    # Old single entry conditions (deprecated, for backwards compat)
    entry_conditions: Optional[dict] = None
    # New separate buy/sell entry conditions
    buy_entry_conditions: Optional[dict] = None
    sell_entry_conditions: Optional[dict] = None
    exit_conditions: Optional[List[dict]] = None
    initial_tp_percent: float = 5.0
    initial_tp_optimize: bool = False
    initial_tp_min: Optional[float] = None
    initial_tp_max: Optional[float] = None
    initial_tp_step: Optional[float] = None
    initial_sl_percent: float = 2.0
    initial_sl_optimize: bool = False
    initial_sl_min: Optional[float] = None
    initial_sl_max: Optional[float] = None
    initial_sl_step: Optional[float] = None


class StrategyUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    entry_conditions: Optional[dict] = None
    buy_entry_conditions: Optional[dict] = None
    sell_entry_conditions: Optional[dict] = None
    exit_conditions: Optional[List[dict]] = None
    initial_tp_percent: Optional[float] = None
    initial_tp_optimize: Optional[bool] = None
    initial_tp_min: Optional[float] = None
    initial_tp_max: Optional[float] = None
    initial_tp_step: Optional[float] = None
    initial_sl_percent: Optional[float] = None
    initial_sl_optimize: Optional[bool] = None
    initial_sl_min: Optional[float] = None
    initial_sl_max: Optional[float] = None
    initial_sl_step: Optional[float] = None


def extract_required_fields(
    first_arg=None,
    second_arg=None,
    *,
    buy_entry_conditions: dict = None,
    sell_entry_conditions: dict = None,
    exit_conditions: list = None,
    entry_conditions: dict = None
) -> List[str]:
    """Extract all model prediction fields used in conditions.

    Supports both old signature: extract_required_fields(entry_conditions, exit_conditions)
    and new signature with keyword args for buy/sell split.
    """
    # Handle backwards compatibility with old positional signature
    # Old: extract_required_fields(entry_conditions_dict, exit_conditions_list)
    if first_arg is not None:
        if isinstance(first_arg, dict):
            entry_conditions = first_arg
        if isinstance(second_arg, list):
            exit_conditions = second_arg

    fields = set()

    def traverse_conditions(cond):
        if cond is None:
            return
        if isinstance(cond, dict):
            if cond.get("field_type") in ("model_probability", "model_class"):
                if cond.get("field"):
                    fields.add(cond["field"])
            if cond.get("conditions"):
                for c in cond["conditions"]:
                    traverse_conditions(c)

    # Traverse all condition sources
    traverse_conditions(buy_entry_conditions)
    traverse_conditions(sell_entry_conditions)
    traverse_conditions(entry_conditions)
    for exit_cond in (exit_conditions or []):
        traverse_conditions(exit_cond.get("conditions"))

    return sorted(list(fields))


@router.get("")
async def list_strategies(
    search: Optional[str] = None,
    db: Session = Depends(get_db)
):
    """List all strategies."""
    query = db.query(Strategy)

    if search:
        query = query.filter(Strategy.name.ilike(f"%{search}%"))

    strategies = query.order_by(Strategy.created_at.desc()).all()

    return {
        "strategies": [s.to_dict() for s in strategies],
        "total": len(strategies)
    }


@router.post("")
async def create_strategy(
    strategy: StrategyCreate,
    db: Session = Depends(get_db)
):
    """Create a new strategy."""
    required_fields = extract_required_fields(
        buy_entry_conditions=strategy.buy_entry_conditions,
        sell_entry_conditions=strategy.sell_entry_conditions,
        exit_conditions=strategy.exit_conditions,
        entry_conditions=strategy.entry_conditions
    )

    db_strategy = Strategy(
        name=strategy.name,
        description=strategy.description,
        required_fields=required_fields,
        entry_conditions=strategy.entry_conditions,
        buy_entry_conditions=strategy.buy_entry_conditions,
        sell_entry_conditions=strategy.sell_entry_conditions,
        exit_conditions=strategy.exit_conditions or [],
        initial_tp_percent=strategy.initial_tp_percent,
        initial_tp_optimize=strategy.initial_tp_optimize,
        initial_tp_min=strategy.initial_tp_min,
        initial_tp_max=strategy.initial_tp_max,
        initial_tp_step=strategy.initial_tp_step,
        initial_sl_percent=strategy.initial_sl_percent,
        initial_sl_optimize=strategy.initial_sl_optimize,
        initial_sl_min=strategy.initial_sl_min,
        initial_sl_max=strategy.initial_sl_max,
        initial_sl_step=strategy.initial_sl_step,
    )

    db.add(db_strategy)
    db.commit()
    db.refresh(db_strategy)

    logger.info(f"Created strategy: {db_strategy.name} (id={db_strategy.id})")
    return db_strategy.to_dict()


@router.get("/compatible/{model_id}")
async def get_compatible_strategies(
    model_id: int,
    db: Session = Depends(get_db)
):
    """Get strategies compatible with a model's prediction fields."""
    model = db.query(TrainedModel).filter(TrainedModel.id == model_id).first()
    if not model:
        raise HTTPException(status_code=404, detail=f"Model {model_id} not found")

    # Get model's prediction target fields
    model_fields = set()
    if model.prediction_targets:
        for target in model.prediction_targets:
            if isinstance(target, dict):
                # Add all possible field names the model might output
                target_type = target.get("type", "")
                if target_type:
                    model_fields.add(target_type)
                    model_fields.add(f"{target_type}_probability")
                    model_fields.add(f"{target_type}_class")

    # Get all strategies and filter by required fields
    strategies = db.query(Strategy).all()
    compatible = []

    for strategy in strategies:
        required = set(strategy.required_fields or [])
        if required.issubset(model_fields) or len(required) == 0:
            compatible.append(strategy.to_dict())

    return {
        "strategies": compatible,
        "total": len(compatible),
        "modelFields": sorted(list(model_fields))
    }


# IMPORTANT: the single-segment /optimizations route MUST be registered BEFORE the dynamic
# GET /{strategy_id} below. FastAPI matches routes in registration order, so if /{strategy_id}
# comes first it swallows /api/strategies/optimizations (strategy_id="optimizations" -> 422
# int-parse error), which made the Optimization-Jobs tab come back empty. /running before the
# /{opt_id} catch-all so it isn't shadowed in turn. Helpers (_opt_settings_summary,
# _top_individuals) are defined further down — fine, they're only called at request time.
@router.get("/optimizations")
def list_optimizations(db: Session = Depends(get_db)):
    """All optimization (StrategyOptimization) jobs for the Optimization-Jobs tab.

    One compact row per job: id/name/status/fitness, the timing fields (created/started/
    completed) so the UI can render the run date + a derived duration, and a `settings`
    summary (GA config + optimized expert/RM ranges + screener settings). Newest first.
    """
    rows = (db.query(StrategyOptimization)
            .order_by(StrategyOptimization.id.desc()).all())
    out = []
    for r in rows:
        settings = _opt_settings_summary(r.optimization_config)
        settings["fitnessMetric"] = r.fitness_metric
        out.append({
            "id": r.id,
            "strategyId": r.strategy_id,
            "name": r.name,
            "status": r.status,
            "optimizationType": r.optimization_type,
            "fitnessMetric": r.fitness_metric,
            "bestFitness": r.best_fitness,
            "progress": r.progress,
            "errorMessage": r.error_message,
            "createdAt": r.created_at.isoformat() if r.created_at else None,
            "startedAt": r.started_at.isoformat() if r.started_at else None,
            "completedAt": r.completed_at.isoformat() if r.completed_at else None,
            "settings": settings,
        })
    return {"optimizations": out}


@router.get("/optimizations/running")
def list_running_optimizations(db: Session = Depends(get_db)):
    """Running optimizations enriched with best fitness + top individuals (UI Running tab)."""
    rows = (db.query(StrategyOptimization)
            .filter(StrategyOptimization.status == "running")
            .order_by(StrategyOptimization.id.desc()).all())
    return {"optimizations": [
        {
            "id": r.id, "name": r.name, "status": r.status, "progress": r.progress,
            "fitnessMetric": r.fitness_metric, "bestFitness": r.best_fitness,
            "bestParams": r.best_params, "nEvaluated": len(r.all_results or []),
            "topIndividuals": _top_individuals(r, n=8),
        }
        for r in rows
    ]}


@router.get("/optimizations/{opt_id}")
def get_optimization(opt_id: int, db: Session = Depends(get_db)):
    """Full optimization detail (config, best params, top individuals) by id."""
    r = db.query(StrategyOptimization).filter(StrategyOptimization.id == opt_id).first()
    if not r:
        raise HTTPException(status_code=404, detail=f"Optimization {opt_id} not found")
    d = r.to_dict()
    d["topIndividuals"] = _top_individuals(r, n=15)
    return d


@router.get("/optimizations/{opt_id}/export")
def export_optimization_settings(opt_id: int, db: Session = Depends(get_db)):
    """Read-only, self-describing export of an optimization JOB's settings as JSON.

    The frontend downloads this via a Blob + <a download> (NO server filesystem write). The
    shape matches frontend/src/lib/btExport.ts `OptSettingsExport` so it round-trips into the
    New-Backtest form's importer: GA config + engine/window + universe (static symbols OR
    screener settings) + the optimized expert/RM param RANGES.
    """
    r = db.query(StrategyOptimization).filter(StrategyOptimization.id == opt_id).first()
    if not r:
        raise HTTPException(status_code=404, detail=f"Optimization {opt_id} not found")

    from datetime import datetime as _dt

    cfg = r.optimization_config or {}
    summary = _opt_settings_summary(cfg)
    backtest = cfg.get("backtest") or {}
    universe = backtest.get("universe") or {}

    # Universe block: keep the full static symbol list / screener settings (the compact summary
    # drops the symbols). Falls back to {mode: <mode or None>} when neither is present.
    if universe.get("mode") == "static" and isinstance(universe.get("symbols"), list):
        universe_out = {"mode": "static", "symbols": [str(s) for s in universe["symbols"]]}
    elif universe.get("mode") == "screener":
        universe_out = {
            "mode": "screener",
            "screener_settings": universe.get("screener_settings") or {},
        }
        if universe.get("screener_store") is not None:
            universe_out["screener_store"] = universe["screener_store"]
        if universe.get("screener_cadence_days") is not None:
            universe_out["screener_cadence_days"] = universe["screener_cadence_days"]
    else:
        universe_out = {"mode": universe.get("mode")}

    return {
        "schema": "ba2.opt-settings",
        "version": 1,
        "exportedAt": _dt.utcnow().isoformat() + "Z",
        "optimizationId": r.id,
        "name": r.name,
        "fitnessMetric": r.fitness_metric,
        "optimizationType": r.optimization_type,
        "ga": summary.get("ga") or {},
        "engine": backtest.get("engine"),
        "startDate": backtest.get("start_date"),
        "endDate": backtest.get("end_date"),
        "executionInterval": backtest.get("execution_interval"),
        "initialCapital": backtest.get("initial_capital"),
        "universe": universe_out,
        "expertRanges": summary.get("expertRanges") or {},
    }


@router.get("/{strategy_id}")
async def get_strategy(
    strategy_id: int,
    db: Session = Depends(get_db)
):
    """Get strategy by ID."""
    strategy = db.query(Strategy).filter(Strategy.id == strategy_id).first()
    if not strategy:
        raise HTTPException(status_code=404, detail=f"Strategy {strategy_id} not found")
    return strategy.to_dict()


@router.put("/{strategy_id}")
async def update_strategy(
    strategy_id: int,
    update: StrategyUpdate,
    db: Session = Depends(get_db)
):
    """Update a strategy."""
    strategy = db.query(Strategy).filter(Strategy.id == strategy_id).first()
    if not strategy:
        raise HTTPException(status_code=404, detail=f"Strategy {strategy_id} not found")

    update_data = update.model_dump(exclude_unset=True)

    # Recalculate required fields if any conditions changed
    conditions_keys = ["entry_conditions", "buy_entry_conditions", "sell_entry_conditions", "exit_conditions"]
    if any(k in update_data for k in conditions_keys):
        update_data["required_fields"] = extract_required_fields(
            buy_entry_conditions=update_data.get("buy_entry_conditions", strategy.buy_entry_conditions),
            sell_entry_conditions=update_data.get("sell_entry_conditions", strategy.sell_entry_conditions),
            exit_conditions=update_data.get("exit_conditions", strategy.exit_conditions),
            entry_conditions=update_data.get("entry_conditions", strategy.entry_conditions)
        )

    for key, value in update_data.items():
        setattr(strategy, key, value)

    db.commit()
    db.refresh(strategy)

    logger.info(f"Updated strategy: {strategy.name} (id={strategy.id})")
    return strategy.to_dict()


@router.delete("/{strategy_id}")
async def delete_strategy(
    strategy_id: int,
    db: Session = Depends(get_db)
):
    """Delete a strategy."""
    strategy = db.query(Strategy).filter(Strategy.id == strategy_id).first()
    if not strategy:
        raise HTTPException(status_code=404, detail=f"Strategy {strategy_id} not found")

    db.delete(strategy)
    db.commit()

    logger.info(f"Deleted strategy: {strategy.name} (id={strategy_id})")
    return {"message": f"Strategy {strategy_id} deleted"}


class OptimizeRequest(BaseModel):
    """Request to launch a joint genetic optimization over a strategy.

    optimization_config MUST include the GA params (populationSize, generations,
    crossoverProb, mutationProb, earlyStoppingGenerations, elitismPercent, seed)
    plus a `backtest` block (engine/model/datasets/date-range/initial_capital/...).
    The handler validates these fail-early (no-defaults rule, backend/CLAUDE.md).
    """
    name: Optional[str] = None
    fitness_metric: str                      # sharpe/return/profit_factor/win_rate/max_drawdown/...
    optimization_type: str = "genetic"       # genetic | brute_force
    optimization_config: dict                # GA params + backtest{}
    # expert_params carries BOTH the expert's numeric decision genes AND the classic-RM sizing
    # genes — the RM genes are keyed by the REAL ba2 setting names (risk_per_trade_pct,
    # atr_multiplier, min_stop_loss_pct, max_virtual_equity_per_instrument_percent), NOT a
    # separate rm: namespace. The handler folds these into the model:* search space and the RM
    # reads them off the expert. Shape: {param: {optimize: bool, min, max, step, type}}.
    expert_params: Optional[dict] = None     # {param:{optimize,min,max,step,type}}
    # SCREENER-settings optimization (OPTIONAL — omit for a static/explicit universe). When
    # present, this is woven into the optimization in two places, exactly like the CLI
    # (_cmd_optimize --screener):
    #   * ``store`` / ``base_settings`` / ``cadence_days`` / ``apply_to_expert_settings`` are
    #     merged into ``optimization_config.backtest["screener_opt"]`` — the block the handler's
    #     ``_build_hoisted_state`` reads to warm the metric store + gate per-day entries.
    #   * ``param_ranges`` ({setting: {optimize, min, max, step, type}}) are merged into
    #     ``expert_params`` PRE-PREFIXED with ``screener:`` so the param-space router emits the
    #     ``screener:<setting>`` genes (the handler splits ``screener:``-prefixed keys out of
    #     expert_params into the screener namespace).
    # The screener setting names mirror the CLI's _SCREENER_OPT: screener_market_cap_min,
    # screener_relative_volume_min, screener_price_drop_pct, screener_max_stocks,
    # screener_weinstein_stage2_only.
    screener_opt: Optional[dict] = None


@router.post("/{strategy_id}/optimize")
async def optimize_strategy(
    strategy_id: int,
    req: OptimizeRequest,
    db: Session = Depends(get_db)
):
    """Launch a joint genetic optimization (expert + ruleset params).

    Writes a StrategyOptimization row and enqueues a 'strategy_optimization' task.
    Any expert_params are folded into optimization_config so the handler is self-contained.
    """
    strategy = db.query(Strategy).filter(Strategy.id == strategy_id).first()
    if not strategy:
        raise HTTPException(status_code=404, detail=f"Strategy {strategy_id} not found")

    cfg = dict(req.optimization_config or {})
    if req.expert_params is not None:
        cfg["expert_params"] = req.expert_params
    # Weave the optional screener-settings optimization into the config exactly like the CLI:
    # the store/base/cadence block onto backtest["screener_opt"] + the param ranges merged into
    # expert_params pre-prefixed with "screener:". No-op when screener_opt is absent.
    if req.screener_opt is not None:
        _merge_screener_opt(cfg, req.screener_opt)

    row = StrategyOptimization(
        strategy_id=strategy_id,
        name=req.name,
        fitness_metric=req.fitness_metric,
        optimization_type=req.optimization_type,
        optimization_config=cfg,
        status="pending",
    )
    row, task_id = _enqueue_optimization(
        db,
        strategy_id=strategy_id,
        name=req.name,
        fitness_metric=req.fitness_metric,
        optimization_type=req.optimization_type,
        cfg=cfg,
        description=f"Joint genetic optimization for strategy {strategy_id}",
        task_name=req.name or f"Optimize strategy {strategy.name} ({req.fitness_metric})",
    )
    return {"optimizationId": row.id, "taskId": task_id, **row.to_dict()}


def _enqueue_optimization(
    db: Session,
    *,
    strategy_id: int,
    name: Optional[str],
    fitness_metric: str,
    optimization_type: str,
    cfg: dict,
    description: str,
    task_name: str,
):
    """Persist ONE StrategyOptimization row and enqueue its 'strategy_optimization' task.

    The single shared create+enqueue path used by both /{strategy_id}/optimize and the
    /optimize-batch fan-out, so a batched job is byte-identical to a single one. Returns
    (row, task_id).
    """
    row = StrategyOptimization(
        strategy_id=strategy_id,
        name=name,
        fitness_metric=fitness_metric,
        optimization_type=optimization_type,
        optimization_config=cfg,
        status="pending",
    )
    db.add(row)
    db.commit()
    db.refresh(row)

    task_id = get_task_queue().queue_task(
        task_type="strategy_optimization",
        name=task_name,
        payload={"optimization_id": row.id},
        description=description,
    )
    logger.info(f"Enqueued strategy_optimization {row.id} (task {task_id})")
    return row, task_id


class OptimizeBatchRequest(BaseModel):
    """Fan-out request: one joint genetic optimization PER expert (mirrors the CLI
    optimize-batch's per-expert expansion).

    Each created job reuses the EXACT single-optimize path (``_enqueue_optimization``): the
    shared ``optimization_config`` (GA params + a ``backtest`` template) is copied per expert and
    the expert is injected into ``backtest.experts`` (replacing/setting it). All jobs target the
    same ``strategy_id`` (the strategy whose TP/SL + condition ranges are searched). ``expert_params``
    (the expert/RM genes, RM keyed by real ba2 names) and ``screener_opt`` are applied to EVERY
    job identically — supply per-expert tuning by issuing separate calls if needed.

    NOTE: this is intentionally simple (one strategy, fanned across experts). The CLI's S1-S4
    per-strategy template expansion is a CLI-only convenience; the UI drives the StrategyBuilder
    to create the strategy row, then batches experts against it.
    """
    experts: List[str]                       # ["FMPRating", "FMPEarningsDrift", ...]
    strategy_id: int                         # the strategy whose ruleset/TP-SL ranges are searched
    fitness_metric: str
    optimization_type: str = "genetic"
    optimization_config: dict                # GA params + backtest{} template (experts injected per job)
    expert_params: Optional[dict] = None     # applied to every job (incl. RM genes by real ba2 name)
    screener_opt: Optional[dict] = None      # applied to every job (see OptimizeRequest.screener_opt)
    name_prefix: Optional[str] = None        # job name = f"{name_prefix}-{expert}" (default "batch")


@router.post("/optimize-batch")
async def optimize_batch(req: OptimizeBatchRequest, db: Session = Depends(get_db)):
    """Create + enqueue one optimization job per expert (fan-out).

    Validates the strategy exists + experts is non-empty (fail-early), then for each expert:
    deep-copies the optimization_config, injects the expert into ``backtest.experts``, folds in
    the shared expert_params / screener_opt, and enqueues via the same path as the single
    optimize route. Returns the list of created job ids/names/task ids.
    """
    import copy as _copy

    strategy = db.query(Strategy).filter(Strategy.id == req.strategy_id).first()
    if not strategy:
        raise HTTPException(status_code=404, detail=f"Strategy {req.strategy_id} not found")
    experts = [e.strip() for e in (req.experts or []) if e and e.strip()]
    if not experts:
        raise HTTPException(status_code=400, detail="experts must be a non-empty list")

    prefix = req.name_prefix or "batch"
    created = []
    for expert in experts:
        cfg = _copy.deepcopy(req.optimization_config or {})
        if req.expert_params is not None:
            cfg["expert_params"] = _copy.deepcopy(req.expert_params)
        if req.screener_opt is not None:
            _merge_screener_opt(cfg, req.screener_opt)

        # Inject THIS expert into the backtest block's experts list. Preserve any per-expert
        # fixed settings already present in the template for the same class; otherwise add a bare
        # spec. The handler validates the class fail-early, so an unknown expert fails its own job
        # (not the whole batch enqueue).
        backtest = dict(cfg.get("backtest") or {})
        existing = backtest.get("experts") or []
        matched = next(
            (s for s in existing
             if (s.get("class") if isinstance(s, dict) else s) == expert),
            None,
        )
        backtest["experts"] = [matched] if matched is not None else [{"class": expert, "settings": {}}]
        cfg["backtest"] = backtest

        job_name = f"{prefix}-{expert}-{req.fitness_metric}"
        row, task_id = _enqueue_optimization(
            db,
            strategy_id=req.strategy_id,
            name=job_name,
            fitness_metric=req.fitness_metric,
            optimization_type=req.optimization_type,
            cfg=cfg,
            description=f"Batch optimization ({expert}) for strategy {req.strategy_id}",
            task_name=job_name,
        )
        created.append({
            "expert": expert,
            "optimizationId": row.id,
            "taskId": task_id,
            "name": job_name,
        })

    logger.info(f"optimize-batch enqueued {len(created)} jobs for strategy {req.strategy_id}")
    return {"jobs": created, "count": len(created)}


def _merge_screener_opt(cfg: dict, screener_opt: dict) -> None:
    """Weave a screener-settings optimization block into an optimization_config IN PLACE.

    Mirrors the CLI's _cmd_optimize --screener wiring so a UI-launched screener optimization
    behaves identically to the headless one:

      1. ``cfg["backtest"]["screener_opt"]`` gets {store, base_settings, cadence_days,
         apply_to_expert_settings} — the block the handler's ``_build_hoisted_state`` reads to
         load the parquet metric store once + gate per-day entries. ``store`` defaults to the
         shared ``ba2_common.config.SCREENER_STORE_DIR`` (``<BA2_HOME>/trade/screener/metric_store``)
         when omitted, and a provided value is ``expanduser``-ed (so a UI-supplied ``~/...`` works).
         ``base_settings`` defaults to {} and ``cadence_days`` to 7 (weekly).
      2. ``param_ranges`` ({setting: {optimize,min,max,step,type}}) are merged into
         ``cfg["expert_params"]`` with each key prefixed ``screener:`` so the handler routes them
         to the screener namespace (it splits ``screener:``-prefixed keys out of expert_params).
    """
    import os as _os
    from ba2_common.config import SCREENER_STORE_DIR as _DEFAULT_SCREENER_STORE
    store = screener_opt.get("store")
    store = _os.path.expanduser(str(store)) if store else _os.path.expanduser(str(_DEFAULT_SCREENER_STORE))
    backtest = dict(cfg.get("backtest") or {})
    backtest["screener_opt"] = {
        "store": store,
        "base_settings": screener_opt.get("base_settings") or {},
        "cadence_days": int(screener_opt.get("cadence_days", 7)),
        "apply_to_expert_settings": bool(screener_opt.get("apply_to_expert_settings", False)),
    }
    cfg["backtest"] = backtest

    param_ranges = screener_opt.get("param_ranges") or {}
    if param_ranges:
        expert_params = dict(cfg.get("expert_params") or {})
        for name, spec in param_ranges.items():
            key = name if str(name).startswith("screener:") else f"screener:{name}"
            expert_params[key] = spec
        cfg["expert_params"] = expert_params


def _top_individuals(row, n: int = 8) -> list:
    """Top-N distinct (by fitness) evaluated individuals from a (running) optimization's
    all_results, best first. Each entry carries its fitness + trade count (all_results stores a
    trade COUNT per trial, not the full trade list — full backtests are the persisted top-N)."""
    results = row.all_results or []
    seen, uniq = set(), []
    for e in sorted(results,
                    key=lambda x: (x.get("fitness") if x.get("fitness") is not None else -1e18),
                    reverse=True):
        f = e.get("fitness")
        if f is None or f in seen:
            continue
        seen.add(f)
        uniq.append(e)
        if len(uniq) >= n:
            break
    return [
        {"rank": i + 1, "fitness": e.get("fitness"), "nTrades": e.get("trades"),
         "params": e.get("params")}
        for i, e in enumerate(uniq)
    ]


def _opt_settings_summary(cfg: Optional[dict]) -> dict:
    """Compact, UI-facing summary of a StrategyOptimization's optimization_config.

    No hidden defaults (backend/CLAUDE.md): a key is surfaced only when present, never
    invented. Pulls (a) the GA config (population/generations + the rest of the GA knobs),
    (b) the optimized expert/RM param keys with their {min,max,step} ranges, and (c) the
    screener/universe block when the backtest universe is screener-mode.
    """
    cfg = cfg or {}
    ga = {
        k: cfg[k]
        for k in (
            "populationSize", "generations", "crossoverProb", "mutationProb",
            "earlyStoppingGenerations", "elitismPercent", "seed",
        )
        if k in cfg
    }

    # Optimized expert/RM params: {param: {optimize, min, max, step, type}}. Keep only the
    # ones flagged optimize=true, condensed to their ranges for the cell.
    expert_params = cfg.get("expert_params") or {}
    expert_ranges = {}
    for name, spec in expert_params.items():
        if not isinstance(spec, dict):
            continue
        if spec.get("optimize"):
            expert_ranges[name] = {
                k: spec[k] for k in ("min", "max", "step", "type") if k in spec
            }

    # Screener / universe block off the backtest config.
    backtest = cfg.get("backtest") or {}
    universe = backtest.get("universe") or {}
    summary = {
        "ga": ga,
        "fitnessMetric": None,  # filled by the caller (column on the row, not in cfg)
        "engine": backtest.get("engine"),
        "startDate": backtest.get("start_date"),
        "endDate": backtest.get("end_date"),
        "universeMode": universe.get("mode"),
        "expertRanges": expert_ranges,
    }
    if universe.get("mode") == "screener":
        summary["screener"] = {
            k: universe[k]
            for k in ("screener_settings", "screener_store", "screener_cadence_days")
            if k in universe
        }
    return summary


