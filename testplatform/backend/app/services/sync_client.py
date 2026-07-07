"""Master-side client for replicating Backtest/StrategyOptimization/Strategy rows to every
remote worker with sync enabled — so any worker host can browse/replay past runs even without
the master.

Every push is fire-and-forget PER WORKER: a failure (offline, timeout, auth) is logged and
swallowed, never raised, so an unreachable worker never blocks the optimization/backtest job
it's attached to. Every push sends the row's FULL current state, not a diff, so the next
successful push to a worker that missed one heals the gap automatically — see
docs/plans/2026-07-07-remote-result-sync-design.md.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)


def _sync_targets(db: Any) -> list[dict]:
    """Every remote, enabled, sync-enabled Worker as a plain dict (mirrors _resolve_workers'
    shape in strategy_optimization_handler.py, deliberately NOT scoped to any one job's
    worker_ids — this is fleet-wide replication, independent of trial-execution routing)."""
    from app.models import Worker
    rows = (
        db.query(Worker)
        .filter(
            Worker.is_local == False,  # noqa: E712
            Worker.is_enabled == True,  # noqa: E712
            Worker.sync_results_enabled == True,  # noqa: E712
        )
        .all()
    )
    return [{"id": w.id, "name": w.name, "url": w.url, "password": w.password} for w in rows]


def _post(worker: dict, path: str, payload: dict, timeout: float = 30.0) -> None:
    try:
        with httpx.Client(timeout=timeout) as c:
            r = c.post(
                f"{str(worker['url']).rstrip('/')}{path}",
                headers={"Authorization": f"Bearer {worker.get('password') or ''}"},
                json=payload,
            )
            r.raise_for_status()
    except Exception as e:  # noqa: BLE001 — fire-and-forget: never block the caller
        logger.warning(f"sync push to worker {worker.get('name')} ({path}) failed: {e!r}")


def _iso(value):
    return value.isoformat() if value is not None else None


def _dump(row: Any) -> dict:
    """Every column on a SQLAlchemy row, JSON-safe (datetimes -> isoformat)."""
    out = {}
    for col in row.__table__.columns:
        value = getattr(row, col.name)
        out[col.name] = _iso(value) if hasattr(value, "isoformat") else value
    return out


def push_strategy(strat: Optional[Any], db: Any) -> None:
    if strat is None:
        return
    payload = _dump(strat)
    for worker in _sync_targets(db):
        _post(worker, "/sync/strategy", payload)


def push_optimization(opt: Optional[Any], db: Any) -> None:
    if opt is None:
        return
    from app.models.strategy import Strategy
    strat = db.query(Strategy).filter(Strategy.id == opt.strategy_id).first() if opt.strategy_id else None
    push_strategy(strat, db)  # dependency first

    payload = _dump(opt)
    payload["strategy_name"] = strat.name if strat else None
    payload["strategy_created_at"] = _iso(strat.created_at) if strat else None
    for worker in _sync_targets(db):
        _post(worker, "/sync/optimization", payload)


def push_backtest(bt: Optional[Any], db: Any) -> None:
    if bt is None:
        return
    from app.models.strategy import Strategy
    from app.models.strategy_optimization import StrategyOptimization

    strat = db.query(Strategy).filter(Strategy.id == bt.strategy_id).first() if bt.strategy_id else None
    push_strategy(strat, db)

    opt = None
    if bt.optimization_id:
        opt = db.query(StrategyOptimization).filter(StrategyOptimization.id == bt.optimization_id).first()
        push_optimization(opt, db)  # also syncs opt's own Strategy dependency, harmlessly redundant

    payload = _dump(bt)
    payload["strategy_name"] = strat.name if strat else None
    payload["strategy_created_at"] = _iso(strat.created_at) if strat else None
    payload["optimization_name"] = opt.name if opt else None
    payload["optimization_created_at"] = _iso(opt.created_at) if opt else None
    for worker in _sync_targets(db):
        _post(worker, "/sync/backtest", payload)
