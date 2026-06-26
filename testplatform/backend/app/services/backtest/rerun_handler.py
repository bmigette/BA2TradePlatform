"""Re-run a saved daily-expert backtest IN PLACE (overwrite the same row's results).

A saved ``Backtest`` row's stored metrics can go stale when the underlying data (e.g. a rebuilt
screener metric_store) or the engine code changes. This handler re-executes the run with its
ORIGINAL config against the CURRENT data/code and writes the fresh results back onto the SAME row
(same id / name / optimization link) — no new row.

Two row shapes (both ``engine_type='daily_expert'``):
  * OPTIMIZATION-DERIVED (``optimization_id`` set, e.g. ``TOP3-scr-mid-FactorRanker``): the
    re-runnable config is rebuilt EXACTLY as ``ba2test_launcher._persist_top_backtests`` does —
    ``decode_params(strategy, genes)`` -> ``_build_daily_trial_config(opt.optimization_config
    ['backtest'], decoded)`` — so the re-run reproduces how that top individual was persisted.
  * STANDALONE (no ``optimization_id``): the daily payload is rebuilt from ``strategy_params``
    (universe / expertSettings / trees / tp-sl) + the row columns and run through the normal
    ``_build_config`` path. ``seed`` / ``fill_model`` / ``warmup_days`` / ``run_schedule_override``
    are read from ``strategy_params`` when present (persisted on creation going forward), else fall
    back to documented defaults for legacy rows.

Runs on a DEDICATED task queue (``rerun_backtest`` type) so re-runs never consume the main queue's
worker slots and don't starve running optimizations.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict

from app.models.backtest import Backtest
from app.models.database import SessionLocal
from app.services.task_queue import get_task_queue
from app.services.backtest.daily_backtest_handler import (
    _Paused,
    _build_config,
    _fail,
    _persist_results,
    run_daily_backtest,
)

import logging

logger = logging.getLogger(__name__)

# Gene namespaces decode_params accepts (it RAISES on anything else). The stored strategy_params
# mixes these raw genes with camelCase display keys (buyEntryConditions, ...), so filter first.
_GENE_PREFIXES = ("model", "screener", "cond", "exit")

# Legacy-row fallbacks: standalone rows created before the run knobs were persisted don't carry a
# seed / fill model. The re-run uses these so it can still execute (may differ slightly from the
# original for those old rows; opt-derived rows are exact, their knobs come from the opt config).
_DEFAULT_FILL_MODEL = "next_bar_open"
_DEFAULT_SEED = 42


def _gene_params(strategy_params: Dict[str, Any]) -> Dict[str, Any]:
    """The optimizer GENE subset of a stored strategy_params (drops camelCase display keys so
    decode_params doesn't raise on them)."""
    out: Dict[str, Any] = {}
    for k, v in (strategy_params or {}).items():
        if k in ("tp", "sl") or k.split(":", 1)[0] in _GENE_PREFIXES:
            out[k] = v
    return out


def _build_optimization_rerun_config(db: Any, bt: Backtest) -> Dict[str, Any]:
    """Rebuild an opt-derived row's run config (mirrors _persist_top_backtests)."""
    from app.models.strategy import Strategy
    from app.models.strategy_optimization import StrategyOptimization
    from app.services.strategy_optimization_handler import _build_daily_trial_config
    from app.services.strategy_param_space import decode_params

    opt = db.query(StrategyOptimization).filter(
        StrategyOptimization.id == bt.optimization_id
    ).first()
    if opt is None or not opt.optimization_config:
        raise ValueError(
            f"cannot re-run backtest {bt.id}: its optimization #{bt.optimization_id} or its "
            f"saved optimization_config is missing"
        )
    cfg = opt.optimization_config
    if "backtest" not in cfg:
        raise ValueError(
            f"cannot re-run backtest {bt.id}: optimization #{opt.id} has no 'backtest' block"
        )
    strat = db.query(Strategy).filter(Strategy.id == opt.strategy_id).first()
    bt_block = dict(cfg["backtest"])
    decoded = decode_params(strat, _gene_params(bt.strategy_params))
    trial_cfg = _build_daily_trial_config(bt_block, decoded)
    # Overwrite the SAME row; persist the trial sub-DB for post-mortem (matches _persist_top_backtests).
    trial_cfg["backtest_id"] = bt.id
    trial_cfg["name"] = bt.name
    trial_cfg["persist_trading_db"] = True
    return trial_cfg


def _build_standalone_rerun_config(bt: Backtest) -> Dict[str, Any]:
    """Rebuild a standalone daily_expert row's run config from its persisted strategy_params."""
    sp = bt.strategy_params or {}
    if not bt.expert_name:
        raise ValueError(f"cannot re-run backtest {bt.id}: no expert_name on the row")

    universe = sp.get("universe")
    if not universe:
        raise ValueError(
            f"cannot re-run backtest {bt.id}: no universe persisted on the row (created before "
            f"re-run support); re-create the backtest instead"
        )

    payload: Dict[str, Any] = {
        "backtest_id": bt.id,
        "name": bt.name,
        "experts": [{"class": bt.expert_name, "settings": sp.get("expertSettings") or {}}],
        "start_date": bt.start_date.isoformat() if hasattr(bt.start_date, "isoformat") else str(bt.start_date),
        "end_date": bt.end_date.isoformat() if hasattr(bt.end_date, "isoformat") else str(bt.end_date),
        "initial_capital": float(bt.initial_capital),
        "commission": float(bt.commission),
        "slippage": float(bt.slippage),
        "fill_model": sp.get("fillModel") or _DEFAULT_FILL_MODEL,
        "seed": sp.get("seed") if sp.get("seed") is not None else _DEFAULT_SEED,
        "warmup_days": sp.get("warmupDays"),
        "execution_interval": sp.get("executionInterval") or "1d",
    }
    if sp.get("runScheduleOverride") is not None:
        payload["run_schedule_override"] = sp["runScheduleOverride"]
    # Universe: static -> explicit symbols; screener -> the metric_store block.
    if universe.get("mode") == "screener":
        payload["universe"] = universe
    else:
        payload["enabled_instruments"] = list(universe.get("symbols") or [])
    # Strategy trees + initial brackets.
    if sp.get("buyEntryConditions") is not None:
        payload["buy_tree"] = sp["buyEntryConditions"]
    if sp.get("sellEntryConditions") is not None:
        payload["sell_tree"] = sp["sellEntryConditions"]
    if sp.get("enableShort") is not None:
        payload["enable_short"] = bool(sp["enableShort"])
    if sp.get("exitConditions") is not None:
        payload["exit_rules"] = sp["exitConditions"]
    if sp.get("initialTpPercent") is not None:
        payload["initial_tp_percent"] = sp["initialTpPercent"]
    if sp.get("initialSlPercent") is not None:
        payload["initial_sl_percent"] = sp["initialSlPercent"]
    if sp.get("initialTpReference") is not None:
        payload["initial_tp_reference"] = sp["initialTpReference"]
    return _build_config(payload)


def build_rerun_config(db: Any, bt: Backtest) -> Dict[str, Any]:
    """Return a ``run_daily_backtest`` config that reproduces ``bt``'s run, targeting its own id."""
    if bt.engine_type != "daily_expert":
        raise ValueError(
            f"re-run is only supported for daily_expert backtests (backtest {bt.id} is "
            f"'{bt.engine_type}')"
        )
    if bt.optimization_id:
        return _build_optimization_rerun_config(db, bt)
    return _build_standalone_rerun_config(bt)


def handle_rerun_backtest(task_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Task handler: re-run the saved backtest ``payload['backtest_id']`` in place."""
    backtest_id = payload.get("backtest_id")
    if backtest_id is None:
        return {"status": "failed", "error": "payload.backtest_id is required"}

    tq = get_task_queue()
    db = SessionLocal()
    try:
        bt = db.query(Backtest).filter(Backtest.id == backtest_id).first()
        if bt is None:
            return {"status": "failed", "error": f"Backtest {backtest_id} not found"}

        bt.status = "running"
        bt.started_at = datetime.now()
        bt.error_message = None
        db.commit()

        try:
            config = build_rerun_config(db, bt)
        except (KeyError, ValueError) as e:
            _fail(db, bt, str(e))
            return {"status": "failed", "error": str(e)}

        def progress(pct: float, msg: str) -> None:
            if tq.is_task_paused(task_id):
                raise _Paused(msg)
            tq.update_progress(task_id, pct, msg)

        results = run_daily_backtest(config, progress_cb=progress)

        _persist_results(db, bt, results)
        bt.status = "completed"
        bt.completed_at = datetime.now()
        db.commit()
        logger.info(
            f"Re-run backtest {backtest_id} completed: {results.get('total_trades', 0)} trades, "
            f"return={results.get('total_return')}%"
        )
        return {"status": "completed", "backtest_id": backtest_id, "results": results}

    except _Paused as e:
        _fail(db, bt, f"paused: {e}")
        return {"status": "failed", "error": "paused"}
    except Exception as e:  # noqa: BLE001 — any failure fails the row, not the worker
        logger.error(f"Re-run backtest {backtest_id} failed: {e}", exc_info=True)
        try:
            row = db.query(Backtest).filter(Backtest.id == backtest_id).first()
            if row is not None:
                _fail(db, row, str(e))
        except Exception:  # noqa: BLE001
            pass
        return {"status": "failed", "error": str(e)}
    finally:
        db.close()
