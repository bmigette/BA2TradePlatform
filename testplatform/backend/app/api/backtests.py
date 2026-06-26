"""
Backtests API endpoints.

Manages backtesting of trained models against historical data.
"""

import logging
from datetime import datetime
from typing import Any, List, Optional
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session, defer

from app.models import get_db, Backtest, Strategy, TrainedModel, Dataset

logger = logging.getLogger(__name__)

router = APIRouter()

# Weekday names the engine's _entry_schedule recognises (matches the CLI launcher's set).
_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


def _run_schedule_override(run_schedule: Optional[str], run_schedule_day: Optional[str]) -> Optional[dict]:
    """Translate the API's run_schedule/run_schedule_day knobs into the engine's
    ``run_schedule_override`` dict, mirroring the CLI launcher's daily/weekly handling.

      * run_schedule None or "daily"  -> None (no override; the engine analyses every bar).
      * run_schedule "weekly"         -> {"days": {weekday: bool}, "times": ["09:30"]} with only
        ``run_schedule_day`` (default "monday") enabled. ``times`` pins ANALYSIS to the first
        regular-session bar so an intraday fill clock still analyses once that day (identical to
        the CLI's _cmd_optimize/_cmd_backtest behaviour).

    Fail-early (no silent bad defaults, backend/CLAUDE.md): an unknown run_schedule or an unknown
    weekday raises ValueError (the route turns it into a 400).
    """
    if run_schedule is None or run_schedule == "daily":
        return None
    if run_schedule != "weekly":
        raise ValueError(f"run_schedule must be 'daily' or 'weekly', got {run_schedule!r}")
    day = (run_schedule_day or "monday").lower()
    if day not in _WEEKDAYS:
        raise ValueError(f"run_schedule_day must be one of {_WEEKDAYS}, got {run_schedule_day!r}")
    return {"days": {d: (d == day) for d in _WEEKDAYS}, "times": ["09:30"]}


class BacktestCreate(BaseModel):
    """Request model for creating a backtest.

    Two engines share this endpoint, discriminated by ``engine``:

      * ``engine="ml"`` (default) — the legacy model-driven path. Requires ``model_id`` +
        ``prediction_dataset_id`` + ``execution_dataset_id`` (validated in the route so the
        existing ML behaviour is byte-for-byte unchanged).
      * ``engine="daily_expert"`` — the daily multi-asset expert engine. Requires ``expert``
        ({"class", "settings"}) + ``universe`` ({"mode", "symbols", "screener_settings"}).
        The ML model/dataset fields are unused (and not required) on this path.
    """
    name: str
    engine: str = "ml"  # "ml" (default, legacy) | "daily_expert"
    # ML-engine fields (required only when engine == "ml"; validated in the route).
    model_id: Optional[str] = None  # String model ID like "mdl-abc123"
    prediction_dataset_id: Optional[int] = None
    execution_dataset_id: Optional[int] = None
    strategy_id: Optional[int] = None
    strategy_params: Optional[dict] = None
    # daily_expert-engine fields (required only when engine == "daily_expert").
    expert: Optional[dict] = None  # {"class": "FMPRating", "settings": {...}}
    universe: Optional[dict] = None  # static: {"mode":"static","symbols":[...]} | screener: {"mode":"screener","screener_store":<metric_store dir>,"screener_settings":{...},"screener_cadence_days"?:int}
    # Strategy conditions + initial TP/SL bracket (daily_expert path). When supplied, the
    # buy-entry condition TREE seeds the enter ruleset (seed_ruleset_from_tree) and the TP/SL
    # percents apply per opened position so trades close. All optional: omitted -> the handler
    # falls back to its defaults (the bullish+flat enter ruleset / no brackets).
    buy_entry_conditions: Optional[dict] = None   # AND/OR condition tree -> config "buy_tree"
    sell_entry_conditions: Optional[dict] = None  # -> config "sell_tree"
    enable_short: Optional[bool] = None           # -> config "enable_short": seed the symmetric
                                                  # SHORT/sell enter rule + enable the RM sell gate
                                                  # (the "Allow short" UI toggle). Default long-only.
    exit_conditions: Optional[list] = None        # -> config "exit_rules"
    initial_tp_percent: Optional[float] = None    # -> config "initial_tp_percent"
    initial_sl_percent: Optional[float] = None    # -> config "initial_sl_percent"
    # CANONICAL take-profit reference key. None / "percent" -> the legacy percent-off-entry TP;
    # "expert_target_price" -> anchor the TP on the recommendation's target_price (RE4). The
    # legacy ``initial_tp_ref`` spelling is still accepted (aliased to this canonical key in the
    # handler's _build_config). -> config "initial_tp_reference".
    initial_tp_reference: Optional[str] = None
    initial_tp_ref: Optional[str] = None          # legacy alias -> "initial_tp_reference"
    # Shared trading parameters.
    start_date: str
    end_date: str
    initial_capital: float = 10000.0
    position_sizing_type: str = "fixed"  # fixed, percent
    position_sizing_value: float = 1000.0
    commission: float = 0.1
    slippage: float = 0.05
    fitness_metric: Optional[str] = None
    # daily_expert engine knobs (used only on that path; sensible explicit values required).
    fill_model: Optional[str] = None       # "next_bar_open" | "same_bar_close"
    seed: Optional[int] = None
    warmup_days: Optional[int] = None
    execution_interval: Optional[str] = None  # simulation bar size, e.g. "1d" (default) | "1h" | "5m"
    # ENTRY CADENCE (daily_expert path; mirrors the CLI run_daily_backtest --run-schedule).
    # run_schedule "daily" (default) -> analyse every bar; "weekly" -> analyse once per week on
    # ``run_schedule_day`` (a weekday name, default "monday"). The engine reads a
    # ``run_schedule_override`` dict ({"days": {weekday: bool}, "times": [...]}) which this build
    # path derives from these two fields (see ``_run_schedule_override``). Both OPTIONAL: omitted
    # or run_schedule="daily" -> no override -> the engine analyses every bar (byte-for-byte
    # unchanged from before). Ignored on the ML path.
    run_schedule: Optional[str] = None        # "daily" (default) | "weekly"
    run_schedule_day: Optional[str] = None    # weekday name for run_schedule="weekly"; default "monday"


class DailyExpertSpec(BaseModel):
    """One expert in a daily backtest: a ba2_experts class name + optional setting overrides."""
    class_name: str  # serialised as "class" below via alias
    settings: Optional[dict] = None

    class Config:
        fields = {"class_name": "class"}


class DailyBacktestCreate(BaseModel):
    """Request model for creating a daily multi-asset (expert) backtest.

    No-defaults rule: every trading parameter is explicit. ``experts`` is a list of either
    bare class-name strings or ``{"class": ..., "settings": {...}}`` objects. Datasets/model
    are NOT used by the daily engine (the universe is ``enabled_instruments``)."""
    name: str
    enabled_instruments: List[str]
    experts: List[dict]  # [{"class": "FMPEarningsDrift", "settings": {...}}] or ["FMPEarningsDrift"]
    start_date: str
    end_date: str
    initial_capital: float
    commission: float        # flat $ per fill (BacktestAccount commission_per_trade)
    slippage: float          # slippage in basis points (BacktestAccount slippage_bps)
    fill_model: str          # "next_bar_open" | "same_bar_close"
    seed: int
    fitness_metric: Optional[str] = None
    warmup_days: Optional[int] = None
    # Entry cadence (mirrors BacktestCreate / the CLI --run-schedule). Optional: omitted ->
    # analyse every bar. See ``_run_schedule_override``.
    run_schedule: Optional[str] = None        # "daily" (default) | "weekly"
    run_schedule_day: Optional[str] = None    # weekday name for run_schedule="weekly"


class BacktestListResponse(BaseModel):
    """List of backtests."""
    backtests: List[dict]
    total: int


@router.get("")
async def list_backtests(
    expert: Optional[str] = None,
    optimization_id: Optional[int] = None,
    saved: Optional[bool] = None,
    single: Optional[bool] = None,
    db: Session = Depends(get_db)
):
    """List all backtests (summary only, no curves/trades).

    Optional filters (applied only when provided):
      * ``expert``         — only runs of that expert (``Backtest.expert_name``).
      * ``optimization_id``— only runs belonging to that optimization job.
      * ``saved``          — only saved (``True``) / only unsaved (``False``) runs.
      * ``single``         — ``True`` -> only STANDALONE runs (``optimization_id IS NULL``,
                             i.e. not the TOP-N rows persisted by an optimization);
                             ``False`` -> only optimization-derived runs
                             (``optimization_id IS NOT NULL``). Used by the BT-History tab
                             which lists single backtests only.
    """
    from sqlalchemy import text

    # Use raw SQL to avoid loading huge blob columns (equity_curve, drawdown_curve, trades
    # can be 2-5MB each; with 200+ backtests the ORM query loads 1GB+ even with defer)
    # Check if description column exists (migration may not have run yet)
    col_check = db.execute(text("PRAGMA table_info(backtests)"))
    columns = [r[1] for r in col_check]
    has_description = 'description' in columns

    desc_col = ", description" if has_description else ""

    # Build the optional WHERE clause from the provided filters (parameterised — never
    # string-interpolate user input).
    where_clauses = []
    params: dict = {}
    if expert is not None:
        where_clauses.append("b.expert_name = :expert")
        params["expert"] = expert
    if optimization_id is not None:
        where_clauses.append("b.optimization_id = :optimization_id")
        params["optimization_id"] = optimization_id
    if saved is not None:
        where_clauses.append("b.is_saved = :saved")
        params["saved"] = 1 if saved else 0
    if single is not None:
        # single=true  -> standalone runs only (no optimization parent);
        # single=false -> optimization-derived (TOP-N) runs only.
        where_clauses.append(
            "b.optimization_id IS NULL" if single else "b.optimization_id IS NOT NULL"
        )
    where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

    result = db.execute(text(f"""
        SELECT b.id, b.name, b.model_id, b.prediction_dataset_id, b.execution_dataset_id,
               b.strategy_id, b.start_date, b.end_date, b.initial_capital, b.fitness_metric,
               b.status, b.total_return, b.sharpe_ratio, b.max_drawdown, b.win_rate,
               b.profit_factor, b.total_trades, b.winning_trades, b.losing_trades,
               b.avg_trade_duration, b.final_equity,
               b.best_trade, b.worst_trade, b.error_message, b.is_saved, b.created_at, b.completed_at,
               m.name as model_name, b.expert_name, b.optimization_id, b.engine_type
               {desc_col}
        FROM backtests b
        LEFT JOIN trained_models m ON b.model_id = m.id
        {where_sql}
        ORDER BY b.created_at DESC
    """), params)

    backtests = []
    for row in result:
        bt = {
            "id": row[0], "name": row[1], "modelId": row[2],
            "predictionDatasetId": row[3], "executionDatasetId": row[4],
            "strategyId": row[5],
            "startDate": str(row[6]) if row[6] else None,
            "endDate": str(row[7]) if row[7] else None,
            "initialCapital": row[8], "fitnessMetric": row[9],
            "status": row[10], "totalReturn": row[11],
            "sharpeRatio": row[12], "maxDrawdown": row[13],
            "winRate": row[14], "profitFactor": row[15],
            "totalTrades": row[16], "winningTrades": row[17], "losingTrades": row[18],
            "avgTradeDuration": row[19],
            "finalEquity": row[20], "bestTrade": row[21],
            "worstTrade": row[22], "errorMessage": row[23],
            "isSaved": row[24] or False,
            "createdAt": str(row[25]) if row[25] else None,
            "completedAt": str(row[26]) if row[26] else None,
            "modelName": row[27],
            "expertName": row[28],
            "optimizationId": row[29],
            "engineType": row[30] or "ml",
            "description": row[31] if has_description else None,
        }
        backtests.append(bt)

    return {"backtests": backtests, "total": len(backtests)}


@router.post("")
async def create_backtest(
    backtest: BacktestCreate,
    db: Session = Depends(get_db)
):
    """Create and run a new backtest.

    Dispatches on ``engine``: ``daily_expert`` builds the daily-engine payload (expert spec +
    static-universe instruments) and queues a ``daily_backtest`` task; everything else (the
    default ``ml``) keeps the legacy model-driven path byte-for-byte.
    """
    if backtest.engine == "daily_expert":
        return _create_daily_expert_backtest(backtest, db)

    # ----- legacy ML engine path (unchanged behaviour) -----
    if not backtest.model_id:
        raise HTTPException(status_code=400, detail="model_id is required for engine='ml'")
    if backtest.prediction_dataset_id is None:
        raise HTTPException(status_code=400, detail="prediction_dataset_id is required for engine='ml'")
    if backtest.execution_dataset_id is None:
        raise HTTPException(status_code=400, detail="execution_dataset_id is required for engine='ml'")

    # Validate model exists (lookup by model_id string, not integer id)
    model = db.query(TrainedModel).filter(TrainedModel.model_id == backtest.model_id).first()
    if not model:
        raise HTTPException(status_code=404, detail=f"Model {backtest.model_id} not found")

    # Validate datasets exist
    pred_dataset = db.query(Dataset).filter(Dataset.id == backtest.prediction_dataset_id).first()
    if not pred_dataset:
        raise HTTPException(status_code=404, detail=f"Prediction dataset {backtest.prediction_dataset_id} not found")

    exec_dataset = db.query(Dataset).filter(Dataset.id == backtest.execution_dataset_id).first()
    if not exec_dataset:
        raise HTTPException(status_code=404, detail=f"Execution dataset {backtest.execution_dataset_id} not found")

    # Validate strategy if provided
    strategy = None
    if backtest.strategy_id:
        strategy = db.query(Strategy).filter(Strategy.id == backtest.strategy_id).first()
        if not strategy:
            raise HTTPException(status_code=404, detail=f"Strategy {backtest.strategy_id} not found")

    # Parse dates
    try:
        start_date = datetime.fromisoformat(backtest.start_date)
        end_date = datetime.fromisoformat(backtest.end_date)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"Invalid date format: {e}")

    # Copy strategy params into backtest record for persistence
    # (so backtest results are self-contained even if strategy is later deleted)
    strategy_params = backtest.strategy_params or {}
    if strategy and not strategy_params:
        strategy_params = {
            'initialTpPercent': strategy.initial_tp_percent,
            'initialSlPercent': strategy.initial_sl_percent,
            'buyEntryConditions': strategy.buy_entry_conditions,
            'sellEntryConditions': strategy.sell_entry_conditions,
            'exitConditions': strategy.exit_conditions,
            'strategyName': strategy.name,
        }

    # Create backtest record (use integer id for database FK)
    db_backtest = Backtest(
        name=backtest.name,
        model_id=model.id,  # Use the integer database id
        prediction_dataset_id=backtest.prediction_dataset_id,
        execution_dataset_id=backtest.execution_dataset_id,
        strategy_id=backtest.strategy_id,
        strategy_params=strategy_params,
        start_date=start_date,
        end_date=end_date,
        initial_capital=backtest.initial_capital,
        position_sizing_type=backtest.position_sizing_type,
        position_sizing_value=backtest.position_sizing_value,
        commission=backtest.commission,
        slippage=backtest.slippage,
        fitness_metric=backtest.fitness_metric,
        status="pending"
    )

    db.add(db_backtest)
    db.commit()
    db.refresh(db_backtest)

    logger.info(f"Created backtest: {db_backtest.name} (id={db_backtest.id})")

    # Queue backtest execution in background
    from app.services.task_queue import get_task_queue
    task_queue = get_task_queue()
    task_id = task_queue.queue_task(
        task_type='backtest',
        name=f'Backtest: {db_backtest.name}',
        payload={'backtest_id': db_backtest.id},
        description=f'Running backtest on model {backtest.model_id}'
    )

    logger.info(f"Queued backtest task: {task_id}")

    return db_backtest.to_dict()


def _create_daily_expert_backtest(backtest: "BacktestCreate", db: Session) -> dict:
    """Create + queue a daily multi-asset (expert) backtest from the unified create request.

    Builds the daily-engine payload from ``backtest.expert`` + ``backtest.universe`` and the
    shared trading parameters, persists a ``Backtest`` results row (``engine_type='daily_expert'``,
    ``model_id=None``, ``expert_name`` set for per-expert filtering), and enqueues the
    ``daily_backtest`` task whose handler (``handle_daily_backtest``) loads the row by id and runs
    the engine.

    Fail-early validation (``backend/CLAUDE.md``): the expert class must be supported, and a
    ``static`` universe must be non-empty.
    """
    from app.services.backtest.daily_backtest_handler import _SUPPORTED_EXPERTS

    expert = backtest.expert or {}
    expert_class = expert.get("class")
    if not expert_class:
        raise HTTPException(status_code=400, detail="expert.class is required for engine='daily_expert'")
    if expert_class not in _SUPPORTED_EXPERTS:
        raise HTTPException(
            status_code=400,
            detail=f"unsupported expert '{expert_class}'; supported: {sorted(_SUPPORTED_EXPERTS)}",
        )
    expert_settings = expert.get("settings") or {}

    universe = backtest.universe or {}
    mode = universe.get("mode")
    if mode not in ("static", "screener"):
        raise HTTPException(
            status_code=400,
            detail="universe.mode must be 'static' or 'screener' for engine='daily_expert'",
        )

    # Screener mode: the candidate universe = the prebuilt metric_store symbol union; the engine
    # GATES entries PER BAR to the point-in-time screened set (same path as the optimizer). The
    # create path validates the screener block carries a metric_store dir + criteria and passes
    # it through; resolution happens in the task (a missing/empty store fails on the run row).
    screener_universe = None
    symbols: list = []
    if mode == "screener":
        # Default to the canonical metric_store dir (where build-screener-metrics writes) when the
        # caller omits it — the task's _resolve_screener_store validates it actually exists and
        # fails with an actionable "build it first" message otherwise. Matches _build_config so a
        # screener BT 'just works' against the built store without re-specifying the path.
        from ba2_common.config import SCREENER_STORE_DIR
        screener_store = universe.get("screener_store") or SCREENER_STORE_DIR
        screener_universe = {
            "mode": "screener",
            "screener_store": screener_store,
            "screener_settings": universe.get("screener_settings") or {},
        }
        if universe.get("screener_cadence_days") is not None:
            screener_universe["screener_cadence_days"] = universe["screener_cadence_days"]
    else:
        symbols = universe.get("symbols") or []
        if not symbols:
            raise HTTPException(status_code=400, detail="universe.symbols must be non-empty for static mode")

    # Fail-early on the daily-engine trading knobs (no-defaults rule).
    if not backtest.fill_model:
        raise HTTPException(status_code=400, detail="fill_model is required for engine='daily_expert'")
    if backtest.seed is None:
        raise HTTPException(status_code=400, detail="seed is required for engine='daily_expert'")

    try:
        start_date = datetime.fromisoformat(backtest.start_date)
        end_date = datetime.fromisoformat(backtest.end_date)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"Invalid date format: {e}")

    # Entry cadence: derive the engine's run_schedule_override from the optional run_schedule
    # knobs (fail-early on a bad value). None -> analyse every bar (legacy).
    try:
        run_schedule_override = _run_schedule_override(
            backtest.run_schedule, backtest.run_schedule_day
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # Persist the run's full strategy + settings onto the row so it ROUND-TRIPS through the
    # export endpoints (Quick Load / ruleset + expert-settings export). Without this,
    # strategy_params stays NULL and a reloaded run restores only the expert NAME — the
    # conditions, TP/SL, expert settings, universe and interval are all lost. Keys mirror what
    # _derive_export_payload reads (camelCase structured shape).
    strategy_params: dict = {}
    if backtest.buy_entry_conditions is not None:
        strategy_params["buyEntryConditions"] = backtest.buy_entry_conditions
    if backtest.sell_entry_conditions is not None:
        strategy_params["sellEntryConditions"] = backtest.sell_entry_conditions
    if backtest.enable_short is not None:
        strategy_params["enableShort"] = bool(backtest.enable_short)
    if backtest.exit_conditions is not None:
        strategy_params["exitConditions"] = backtest.exit_conditions
    if backtest.initial_tp_percent is not None:
        strategy_params["initialTpPercent"] = backtest.initial_tp_percent
    if backtest.initial_sl_percent is not None:
        strategy_params["initialSlPercent"] = backtest.initial_sl_percent
    if backtest.initial_tp_reference is not None:
        strategy_params["initialTpReference"] = backtest.initial_tp_reference
    elif backtest.initial_tp_ref is not None:
        strategy_params["initialTpReference"] = backtest.initial_tp_ref
    if expert_settings:
        strategy_params["expertSettings"] = expert_settings
    # Universe + interval are not otherwise persisted on the row; store them so Quick Load can
    # restore them for STANDALONE runs (optimization-derived runs recover these from the opt).
    strategy_params["universe"] = (
        screener_universe if screener_universe is not None
        else {"mode": "static", "symbols": list(symbols)}
    )
    if backtest.execution_interval:
        strategy_params["executionInterval"] = backtest.execution_interval

    db_backtest = Backtest(
        name=backtest.name,
        model_id=None,  # daily expert runs are not model-driven
        expert_name=expert_class,  # per-expert filtering / best-N retention
        start_date=start_date,
        end_date=end_date,
        initial_capital=backtest.initial_capital,
        commission=backtest.commission,
        slippage=backtest.slippage,
        fitness_metric=backtest.fitness_metric,
        status="pending",
        engine_type="daily_expert",
        strategy_params=strategy_params or None,
    )

    db.add(db_backtest)
    db.commit()
    db.refresh(db_backtest)

    logger.info(f"Created daily expert backtest: {db_backtest.name} (id={db_backtest.id})")

    experts_payload = [{"class": expert_class, "settings": expert_settings}]

    # Universe plumbing: static runs carry the explicit symbol list; screener runs carry the
    # ``universe`` block (mode/screener_store/screener_settings) — the handler uses the
    # metric_store symbol union as the candidate set and gates entries per bar (fail-fast if
    # the store is missing/empty).
    payload = {
        'backtest_id': db_backtest.id,
        'name': backtest.name,
        'experts': experts_payload,
        'start_date': backtest.start_date,
        'end_date': backtest.end_date,
        'initial_capital': backtest.initial_capital,
        'commission': backtest.commission,
        'slippage': backtest.slippage,
        'fill_model': backtest.fill_model,
        'seed': backtest.seed,
        'warmup_days': backtest.warmup_days,
        'execution_interval': backtest.execution_interval or "1d",
    }
    # Only forward a non-None override so we never clobber the engine's "every bar" default
    # with None on the daily path (and the payload stays identical for run_schedule="daily").
    if run_schedule_override is not None:
        payload['run_schedule_override'] = run_schedule_override
    if screener_universe is not None:
        payload['universe'] = screener_universe
        universe_desc = f"screener metric_store ({screener_universe['screener_store']})"
    else:
        payload['enabled_instruments'] = list(symbols)
        universe_desc = f"{len(symbols)} instruments"

    # Forward the strategy's conditions + initial TP/SL bracket into the daily-engine payload
    # using the EXACT keys the handler reads: the buy-entry tree -> ``buy_tree`` (consumed by
    # _build_experts -> seed_ruleset_from_tree), ``sell_tree`` / ``exit_rules``, and the
    # ``initial_tp_percent`` / ``initial_sl_percent`` bracket percents (read by _build_config /
    # daily_engine._apply_initial_brackets). Only include provided (non-None) keys so we never
    # override the handler's own defaults with None (fail-early/no-silent-defaults rule).
    if backtest.buy_entry_conditions is not None:
        payload['buy_tree'] = backtest.buy_entry_conditions
    if backtest.sell_entry_conditions is not None:
        payload['sell_tree'] = backtest.sell_entry_conditions
    if backtest.enable_short is not None:
        payload['enable_short'] = bool(backtest.enable_short)
    if backtest.exit_conditions is not None:
        payload['exit_rules'] = backtest.exit_conditions
    if backtest.initial_tp_percent is not None:
        payload['initial_tp_percent'] = backtest.initial_tp_percent
    if backtest.initial_sl_percent is not None:
        payload['initial_sl_percent'] = backtest.initial_sl_percent
    # TP-reference mode: forward the canonical key, else the legacy alias (the handler's
    # _build_config collapses the alias to ``initial_tp_reference`` in one place).
    if backtest.initial_tp_reference is not None:
        payload['initial_tp_reference'] = backtest.initial_tp_reference
    elif backtest.initial_tp_ref is not None:
        payload['initial_tp_ref'] = backtest.initial_tp_ref

    from app.services.task_queue import get_task_queue
    task_queue = get_task_queue()
    task_id = task_queue.queue_task(
        task_type='daily_backtest',
        name=f'Daily Backtest: {db_backtest.name}',
        payload=payload,
        description=f'Daily expert backtest ({expert_class}) over {universe_desc}',
    )

    logger.info(f"Queued daily backtest task: {task_id}")

    return {"taskId": task_id, "backtestId": db_backtest.id, **db_backtest.to_dict()}


@router.patch("/{backtest_id}")
async def update_backtest(
    backtest_id: int,
    update: dict,
    db: Session = Depends(get_db)
):
    """Update backtest fields (description, name)."""
    backtest = db.query(Backtest).filter(Backtest.id == backtest_id).first()
    if not backtest:
        raise HTTPException(status_code=404, detail=f"Backtest {backtest_id} not found")

    if 'description' in update:
        backtest.description = update['description']
    if 'name' in update:
        backtest.name = update['name']
    db.commit()
    return {"status": "updated", "id": backtest_id}


@router.post("/daily")
async def create_daily_backtest(
    request: DailyBacktestCreate,
    db: Session = Depends(get_db)
):
    """Create + queue a daily multi-asset (expert) backtest.

    Creates a ``Backtest`` results row (``status="pending"``, ``model_id=None`` — the daily
    engine is not model-driven; ``engine_type="daily_expert"`` to distinguish it from legacy
    ML runs) and queues a ``daily_backtest`` task whose payload carries the run config + the
    new row id.
    The ``daily_backtest`` handler runs the engine and persists the results onto the row.
    """
    # Validate fail-early (no defaults).
    if not request.enabled_instruments:
        raise HTTPException(status_code=400, detail="enabled_instruments must be non-empty")
    if not request.experts:
        raise HTTPException(status_code=400, detail="experts must be non-empty")

    try:
        start_date = datetime.fromisoformat(request.start_date)
        end_date = datetime.fromisoformat(request.end_date)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"Invalid date format: {e}")

    try:
        run_schedule_override = _run_schedule_override(
            request.run_schedule, request.run_schedule_day
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    db_backtest = Backtest(
        name=request.name,
        model_id=None,  # daily expert runs are not model-driven (Task-7 migration makes this nullable)
        start_date=start_date,
        end_date=end_date,
        initial_capital=request.initial_capital,
        commission=request.commission,
        slippage=request.slippage,
        fitness_metric=request.fitness_metric,
        status="pending",
        engine_type="daily_expert",  # discriminates from legacy ML runs (migration 018)
    )

    db.add(db_backtest)
    db.commit()
    db.refresh(db_backtest)

    logger.info(f"Created daily backtest: {db_backtest.name} (id={db_backtest.id})")

    from app.services.task_queue import get_task_queue
    task_queue = get_task_queue()
    task_id = task_queue.queue_task(
        task_type='daily_backtest',
        name=f'Daily Backtest: {db_backtest.name}',
        payload={
            'backtest_id': db_backtest.id,
            'name': request.name,
            'enabled_instruments': request.enabled_instruments,
            'experts': request.experts,
            'start_date': request.start_date,
            'end_date': request.end_date,
            'initial_capital': request.initial_capital,
            'commission': request.commission,
            'slippage': request.slippage,
            'fill_model': request.fill_model,
            'seed': request.seed,
            'warmup_days': request.warmup_days,
            # None on the daily-cadence path -> engine analyses every bar (unchanged).
            'run_schedule_override': run_schedule_override,
        },
        description=f'Daily expert backtest over {len(request.enabled_instruments)} instruments',
    )

    logger.info(f"Queued daily backtest task: {task_id}")

    return {"taskId": task_id, "backtestId": db_backtest.id, **db_backtest.to_dict()}


@router.get("/{backtest_id}")
async def get_backtest(
    backtest_id: int,
    db: Session = Depends(get_db)
):
    """Get backtest details by ID."""
    backtest = db.query(Backtest).filter(Backtest.id == backtest_id).first()
    if not backtest:
        raise HTTPException(status_code=404, detail=f"Backtest {backtest_id} not found")

    return backtest.to_dict()


class WhatIfRequest(BaseModel):
    """Body for the hidden-trade what-if: 1-based trade ids to EXCLUDE (matching the trade ids the
    list endpoint assigns, id = index+1)."""
    exclude_trade_ids: List[int] = []


@router.post("/{backtest_id}/whatif")
async def backtest_whatif(
    backtest_id: int,
    body: WhatIfRequest,
    db: Session = Depends(get_db),
):
    """Recompute the equity/drawdown curves + headline metrics with the given trades EXCLUDED,
    WITHOUT re-running the backtest. Reconstructs net-liq per bar from the trade ledger + the OHLCV
    cache (see services/backtest/whatif.py), so hiding a trade is exact at bar-close granularity and
    can't produce the cliffs/negative equity the old client-side approximation did. With an empty
    exclude list it reproduces the stored curve (a built-in correctness check)."""
    backtest = db.query(Backtest).filter(Backtest.id == backtest_id).first()
    if not backtest:
        raise HTTPException(status_code=404, detail=f"Backtest {backtest_id} not found")
    if not backtest.equity_curve or not backtest.trades:
        raise HTTPException(status_code=400,
                            detail="Backtest has no stored equity_curve/trades to recompute")
    from app.services.backtest.whatif import recompute_curves
    try:
        res = recompute_curves(
            initial_capital=backtest.initial_capital,
            trades=backtest.trades,
            equity_curve=backtest.equity_curve,
            start=backtest.start_date,
            end=backtest.end_date,
            exclude_ids=body.exclude_trade_ids,
        )
    except Exception as e:  # noqa: BLE001
        logger.error(f"whatif recompute failed for backtest {backtest_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Recompute failed: {e}")
    return {
        "excludedTradeIds": body.exclude_trade_ids,
        "equityCurve": res.pop("equity_curve"),
        "drawdownCurve": res.pop("drawdown_curve"),
        "metrics": res,
    }


@router.delete("/{backtest_id}")
async def delete_backtest(
    backtest_id: int,
    db: Session = Depends(get_db)
):
    """Delete a backtest."""
    backtest = db.query(Backtest).filter(Backtest.id == backtest_id).first()
    if not backtest:
        raise HTTPException(status_code=404, detail=f"Backtest {backtest_id} not found")

    db.delete(backtest)
    db.commit()

    logger.info(f"Deleted backtest: {backtest.name} (id={backtest_id})")
    return {"message": f"Backtest {backtest_id} deleted"}


def _opt_backtest_block(backtest: Backtest, db: Any):
    """Return (bt_block, strategy) for an optimization-derived backtest, else (None, None).

    bt_block is the optimization's run-level ``optimization_config['backtest']`` dict (carries the
    full execution config: enabled_instruments, seed, warmup_days, execution_interval,
    account_settings, run_schedule_override, initial_tp_reference, base expert specs). This is the
    authoritative source for reproducing an opt-derived run faithfully (the optimizer used the
    SAME block via _build_daily_trial_config)."""
    if db is None or backtest.optimization_id is None:
        return None, None
    try:
        from app.models.strategy_optimization import StrategyOptimization
        from app.models.strategy import Strategy
        so = db.query(StrategyOptimization).filter(
            StrategyOptimization.id == backtest.optimization_id
        ).first()
        if so is None:
            return None, None
        bt_block = (so.optimization_config or {}).get("backtest")
        strat = db.query(Strategy).filter(Strategy.id == so.strategy_id).first()
        return (bt_block if isinstance(bt_block, dict) else None), strat
    except Exception:  # noqa: BLE001
        return None, None


def _reconstruct_opt_ruleset(backtest: Backtest, db: Any):
    """FALLBACK for older optimization-derived runs that stored only the flat genes (no concrete
    trees): overlay the run's tuned ``cond:``/``exit:`` genes onto the optimization's base
    Strategy tree via ``decode_params`` to rebuild the concrete buy/sell/exit ruleset.

    New top-N runs persist the concrete trees directly (``buyEntryConditions`` etc.), so this is
    only used when those are absent. Returns (buy_tree, sell_tree, exit_rules) or (None, None,
    None) when reconstruction isn't possible (no optimization link / base strategy / db)."""
    sp = backtest.strategy_params or {}
    has_genes = isinstance(sp, dict) and any(
        isinstance(k, str) and (k.startswith("cond:") or k.startswith("exit:")) for k in sp
    )
    if db is None or backtest.optimization_id is None or not has_genes:
        return None, None, None
    try:
        from app.models.strategy_optimization import StrategyOptimization
        from app.models.strategy import Strategy
        from app.services.strategy_param_space import decode_params

        so = db.query(StrategyOptimization).filter(
            StrategyOptimization.id == backtest.optimization_id
        ).first()
        if so is None:
            return None, None, None
        strat = db.query(Strategy).filter(Strategy.id == so.strategy_id).first()
        if strat is None:
            return None, None, None
        decoded = decode_params(strat, sp)
        return decoded.get("buy_tree"), decoded.get("sell_tree"), decoded.get("exit_rules") or []
    except Exception:  # noqa: BLE001 — reconstruction is best-effort; never break the export
        return None, None, None


def _derive_export_payload(backtest: Backtest, kind: str, db: Any = None) -> dict:
    """Build the chosen read-only export payload from a backtest's strategy_params.

    Two ``kind`` values are supported:

      * ``expert_settings`` — the expert this run used + its decision/RM settings. We surface
        the expert class name (``Backtest.expert_name``) and the settings we can recover from
        ``strategy_params``: the TP/SL bracket (structured ``initialTpPercent``/``initialSlPercent``
        OR the GA's flat ``tp``/``sl`` genes) plus any flat optimized ``model:*`` genes (the
        expert/RM decision settings an optimization tunes).
      * ``ruleset`` — the conditions ruleset (buy/sell entry trees + exit conditions). Structured
        runs carry ``buyEntryConditions``/``sellEntryConditions``/``exitConditions``; optimization
        TOP-N runs instead carry the flat ``cond:*``/``exit:*`` genes, which we pass through so the
        export is still self-describing.

    Pure derivation from the persisted ``strategy_params`` — NO server filesystem writes.
    """
    sp = backtest.strategy_params or {}

    def _pick(*keys):
        for k in keys:
            if isinstance(sp, dict) and k in sp and sp[k] is not None:
                return sp[k]
        return None

    if kind == "expert_settings":
        # GA flat model:* genes stripped of the prefix = the optimized expert decision settings.
        model_overrides = (
            {k[len("model:"):]: v for k, v in sp.items()
             if isinstance(k, str) and k.startswith("model:")}
            if isinstance(sp, dict) else {}
        )
        bt_block, _strat = _opt_backtest_block(backtest, db)
        # FULL expert settings = the optimization's base expert spec settings overlaid with the
        # optimized overrides (faithful reproduction); falls back to a standalone run's stored
        # expertSettings, then to the bare overrides.
        if bt_block is not None:
            base_specs = bt_block.get("experts") or []
            base_settings = {}
            for spec in base_specs:
                if isinstance(spec, dict) and spec.get("class") == backtest.expert_name:
                    base_settings = dict(spec.get("settings") or {})
                    break
            expert_params = {**base_settings, **model_overrides}
            acct = bt_block.get("account_settings") or {}
            # Universe: a SCREENER-settings run (``backtest.screener_opt`` present) must export the
            # screener block — store + the EFFECTIVE settings (run-level base overlaid with this
            # individual's optimized ``screener:*`` genes, mirroring _build_daily_trial_config's
            # ``eff``) — NOT the static candidate list (``enabled_instruments``, the whole metric-
            # store union). Otherwise Load drops the screener config and pins a static universe.
            screener_opt = bt_block.get("screener_opt")
            if isinstance(screener_opt, dict) and screener_opt.get("store"):
                screener_overrides = {
                    k[len("screener:"):]: v for k, v in sp.items()
                    if isinstance(k, str) and k.startswith("screener:")
                } if isinstance(sp, dict) else {}
                eff_screener = {**(screener_opt.get("base_settings") or {}), **screener_overrides}
                universe = {
                    "mode": "screener",
                    "screener_store": screener_opt["store"],
                    "screener_settings": eff_screener,
                    "screener_cadence_days": int(screener_opt.get("cadence_days", 7)),
                }
            else:
                universe = {"mode": "static", "symbols": list(bt_block.get("enabled_instruments") or [])}
            execution = {
                "seed": bt_block.get("seed"),
                "fill_model": acct.get("fill_model"),
                "warmup_days": bt_block.get("warmup_days"),
                "commission": acct.get("commission_per_trade"),
                "slippage": acct.get("slippage_bps"),
                "enable_short": bool(bt_block.get("enable_short")),
                "run_schedule_override": bt_block.get("run_schedule_override"),
                "initial_tp_reference": bt_block.get("initial_tp_reference"),
            }
            interval = bt_block.get("execution_interval")
        else:
            expert_params = model_overrides or _pick("expertSettings", "expert_settings") or {}
            universe = _pick("universe")
            execution = {
                "seed": _pick("seed"),
                "fill_model": _pick("fillModel", "fill_model"),
                "warmup_days": _pick("warmupDays", "warmup_days"),
                "commission": _pick("commission"),
                "slippage": _pick("slippage"),
                "enable_short": _pick("enableShort", "enable_short"),
                "run_schedule_override": _pick("runScheduleOverride", "run_schedule_override"),
                "initial_tp_reference": _pick("initialTpReference", "initial_tp_reference"),
            }
            interval = _pick("executionInterval", "execution_interval")
        return {
            "backtest_id": backtest.id,
            "name": backtest.name,
            "expert": backtest.expert_name,
            "engine_type": backtest.engine_type or "ml",
            "settings": {
                "initial_tp_percent": _pick("initialTpPercent", "initial_tp_percent", "tp"),
                "initial_sl_percent": _pick("initialSlPercent", "initial_sl_percent", "sl"),
                "expert_params": expert_params,
            },
            # Execution config (seed/fill_model/warmup/commission/slippage/enable_short/
            # run_schedule/tp_reference) + universe + interval so a saved run can be reproduced
            # faithfully from the exports alone (the reproducibility goal).
            "execution": execution,
            "universe": universe,
            "execution_interval": interval,
        }

    if kind == "ruleset":
        cond_genes = (
            {k: v for k, v in sp.items()
             if isinstance(k, str) and (k.startswith("cond:") or k.startswith("exit:"))}
            if isinstance(sp, dict) else {}
        )
        buy = _pick("buyEntryConditions", "buy_entry_conditions")
        sell = _pick("sellEntryConditions", "sell_entry_conditions")
        exits = _pick("exitConditions", "exit_conditions")
        # Older optimization runs stored only the flat genes — reconstruct the concrete trees
        # from the optimization's base strategy + those genes so Load still restores conditions.
        if buy is None and sell is None and not exits and cond_genes:
            r_buy, r_sell, r_exits = _reconstruct_opt_ruleset(backtest, db)
            buy = buy if buy is not None else r_buy
            sell = sell if sell is not None else r_sell
            exits = exits if exits else r_exits
        # Normalise to the SINGLE canonical condition format (operator groups, symbol comparison,
        # inferred fieldType) via the shared model so Load pre-fills the builder cleanly regardless
        # of how the tree was stored (builder camel / storage type+op / optimizer-decoded). This is
        # the boundary — the engine/optimizer keep reading their own (still-present) snake keys.
        from ba2_common.core.rule_models import normalize_tree, normalize_exit_rules
        return {
            "backtest_id": backtest.id,
            "name": backtest.name,
            "buy_entry_conditions": normalize_tree(buy),
            "sell_entry_conditions": normalize_tree(sell),
            "exit_conditions": normalize_exit_rules(exits or []),
            "optimized_genes": cond_genes,
        }

    raise HTTPException(
        status_code=400,
        detail=f"Unsupported export kind: {kind!r}. Use 'expert_settings' or 'ruleset'.",
    )


@router.get("/{backtest_id}/export")
async def export_backtest_json(
    backtest_id: int,
    kind: str = "expert_settings",
    db: Session = Depends(get_db)
):
    """Return a downloadable export payload for a backtest as JSON (no filesystem writes).

    ``kind`` selects WHAT to export — ``expert_settings`` (the expert + its settings) or
    ``ruleset`` (the buy/sell/exit conditions). The frontend downloads the returned JSON via
    a Blob + temporary anchor; the server never writes to disk. Read-only.
    """
    backtest = db.query(Backtest).filter(Backtest.id == backtest_id).first()
    if not backtest:
        raise HTTPException(status_code=404, detail=f"Backtest {backtest_id} not found")
    return _derive_export_payload(backtest, kind, db)


@router.post("/{backtest_id}/export")
async def export_backtest(
    backtest_id: int,
    format: str = "csv",
    db: Session = Depends(get_db)
):
    """Export backtest results."""
    import json
    import csv
    from pathlib import Path
    import io

    backtest = db.query(Backtest).filter(Backtest.id == backtest_id).first()
    if not backtest:
        raise HTTPException(status_code=404, detail=f"Backtest {backtest_id} not found")

    if backtest.status != "completed":
        raise HTTPException(status_code=400, detail="Cannot export incomplete backtest")

    # Ensure exports directory exists
    exports_dir = Path("exports")
    exports_dir.mkdir(exist_ok=True)

    # Build export data
    export_data = {
        "backtest": {
            "id": backtest.id,
            "name": backtest.name,
            "model_id": backtest.model_id,
            "start_date": backtest.start_date.isoformat() if backtest.start_date else None,
            "end_date": backtest.end_date.isoformat() if backtest.end_date else None,
            "initial_capital": backtest.initial_capital,
            "final_equity": backtest.final_equity,
            "total_return": backtest.total_return,
            "sharpe_ratio": backtest.sharpe_ratio,
            "max_drawdown": backtest.max_drawdown,
            "win_rate": backtest.win_rate,
            "profit_factor": backtest.profit_factor,
            "total_trades": backtest.total_trades,
            "winning_trades": backtest.winning_trades,
            "losing_trades": backtest.losing_trades,
        },
        "trades": backtest.trades or [],
        "equity_curve": backtest.equity_curve or [],
    }

    if format == "json":
        export_path = exports_dir / f"backtest_{backtest_id}.json"
        with open(export_path, 'w') as f:
            json.dump(export_data, f, indent=2)
    elif format == "csv":
        # Export trades as CSV
        trades_path = exports_dir / f"backtest_{backtest_id}_trades.csv"
        equity_path = exports_dir / f"backtest_{backtest_id}_equity.csv"

        # Write trades
        trades = backtest.trades or []
        if trades:
            with open(trades_path, 'w', newline='') as f:
                writer = csv.DictWriter(f, fieldnames=trades[0].keys())
                writer.writeheader()
                writer.writerows(trades)

        # Write equity curve
        equity_curve = backtest.equity_curve or []
        if equity_curve:
            with open(equity_path, 'w', newline='') as f:
                writer = csv.DictWriter(f, fieldnames=equity_curve[0].keys())
                writer.writeheader()
                writer.writerows(equity_curve)

        export_path = trades_path  # Return trades path as main export
    else:
        raise HTTPException(status_code=400, detail=f"Unsupported format: {format}. Use 'csv' or 'json'")

    logger.info(f"Exported backtest {backtest_id} to {export_path}")

    return {
        "message": "Backtest exported successfully",
        "format": format,
        "path": str(export_path),
        "trades": backtest.total_trades or 0
    }


@router.post("/compare")
async def compare_backtests(
    backtest_ids: List[int],
    db: Session = Depends(get_db)
):
    """Compare multiple backtests side-by-side."""
    if len(backtest_ids) < 2:
        raise HTTPException(status_code=400, detail="At least 2 backtests required for comparison")

    backtests = []
    for bt_id in backtest_ids:
        bt = db.query(Backtest).filter(Backtest.id == bt_id).first()
        if not bt:
            raise HTTPException(status_code=404, detail=f"Backtest {bt_id} not found")

        backtests.append({
            "id": bt.id,
            "name": bt.name,
            "totalReturn": bt.total_return,
            "sharpeRatio": bt.sharpe_ratio,
            "maxDrawdown": bt.max_drawdown,
            "winRate": bt.win_rate,
            "profitFactor": bt.profit_factor,
            "totalTrades": bt.total_trades
        })

    # Calculate comparison stats
    returns = [bt["totalReturn"] for bt in backtests if bt["totalReturn"] is not None]
    sharpes = [bt["sharpeRatio"] for bt in backtests if bt["sharpeRatio"] is not None]
    drawdowns = [bt["maxDrawdown"] for bt in backtests if bt["maxDrawdown"] is not None]
    win_rates = [bt["winRate"] for bt in backtests if bt["winRate"] is not None]

    comparison = {
        "bestReturn": max(returns) if returns else None,
        "bestSharpe": max(sharpes) if sharpes else None,
        "lowestDrawdown": min(drawdowns) if drawdowns else None,
        "highestWinRate": max(win_rates) if win_rates else None,
        "avgReturn": round(sum(returns) / len(returns), 2) if returns else None,
        "avgSharpe": round(sum(sharpes) / len(sharpes), 2) if sharpes else None
    }

    return {
        "backtests": backtests,
        "comparison": comparison
    }


class BacktestSave(BaseModel):
    """Request model for saving a backtest."""
    name: str


@router.post("/{backtest_id}/save")
async def save_backtest(
    backtest_id: int,
    save_data: BacktestSave,
    db: Session = Depends(get_db)
):
    """Save a backtest with a custom name (marks it as saved)."""
    backtest = db.query(Backtest).filter(Backtest.id == backtest_id).first()
    if not backtest:
        raise HTTPException(status_code=404, detail=f"Backtest {backtest_id} not found")

    backtest.name = save_data.name
    backtest.is_saved = True
    db.commit()
    db.refresh(backtest)

    logger.info(f"Saved backtest: {backtest.name} (id={backtest_id})")
    return backtest.to_dict()


@router.delete("/unsaved")
async def clear_unsaved_backtests(
    db: Session = Depends(get_db)
):
    """Delete all unsaved backtests."""
    unsaved = db.query(Backtest).filter(Backtest.is_saved == False).all()
    count = len(unsaved)

    for bt in unsaved:
        db.delete(bt)

    db.commit()

    logger.info(f"Cleared {count} unsaved backtests")
    return {"message": f"Deleted {count} unsaved backtests", "count": count}
