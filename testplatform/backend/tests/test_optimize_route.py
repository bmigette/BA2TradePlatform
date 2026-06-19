"""Phase 4 Task 6: POST /api/strategies/{id}/optimize route + task registration.

Proves acceptance gate #6 (wiring): the route writes a StrategyOptimization row,
folds the per-strategy RM config into optimization_config, and enqueues a
'strategy_optimization' task (via the real TaskQueue.queue_task, which persists a
TaskQueue row — no running daemon needed). Also asserts the handler is registered
in the app's startup lifespan.

Self-contained (does NOT depend on a conftest): a throwaway sqlite DATABASE_URL is
set at module import time BEFORE any app import, mirroring tests/test_backtester.py.

Run from the backend dir:
    ./venv/bin/python -m pytest tests/test_optimize_route.py -v
"""
from __future__ import annotations

import os
import sys

# Add backend to path (mirror test_backtester.py)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Throwaway test DB — MUST be set before any app import so the shared engine binds here.
TEST_DB_PATH = os.path.join(os.path.dirname(__file__), "test_optimize_route.db")
if os.path.exists(TEST_DB_PATH):
    os.remove(TEST_DB_PATH)
os.environ["DATABASE_URL"] = f"sqlite:///{TEST_DB_PATH}"

import pytest


@pytest.fixture(scope="module")
def test_db():
    from app.models.database import engine, Base, SessionLocal

    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    yield db
    db.close()
    engine.dispose()
    if os.path.exists(TEST_DB_PATH):
        try:
            os.remove(TEST_DB_PATH)
        except PermissionError:
            pass


@pytest.fixture(scope="module")
def client(test_db):
    from fastapi.testclient import TestClient
    from app.main import app

    # Use raise_server_exceptions=False so the route's own HTTP responses are returned
    # rather than re-raised; but the optimize route returns 200/404, so default is fine.
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def seed_strategy(test_db):
    from app.models import Strategy

    s = Strategy(
        name="opt-route-test",
        description="strategy for optimize-route test",
        initial_tp_percent=5.0,
        initial_tp_optimize=True,
        initial_tp_min=2.0,
        initial_tp_max=12.0,
        initial_tp_step=1.0,
        initial_sl_percent=2.0,
        initial_sl_optimize=True,
        initial_sl_min=1.0,
        initial_sl_max=6.0,
        initial_sl_step=1.0,
    )
    test_db.add(s)
    test_db.commit()
    test_db.refresh(s)
    return s


def _payload():
    return {
        "fitness_metric": "sharpe",
        "optimization_type": "genetic",
        "expert_params": {
            "risk_per_trade_pct": {
                "optimize": True,
                "min": 0.5,
                "max": 3.0,
                "step": 0.25,
                "type": "float",
            }
        },
        "optimization_config": {
            "populationSize": 8,
            "generations": 3,
            "crossoverProb": 0.7,
            "mutationProb": 0.2,
            "earlyStoppingGenerations": 10,
            "elitismPercent": 10.0,
            "seed": 42,
            "backtest": {
                "engine": "daily",
                "model_id": 1,
                "prediction_dataset_id": 1,
                "execution_dataset_id": 1,
                "start_date": "2020-01-01",
                "end_date": "2021-01-01",
                "initial_capital": 10000.0,
            },
        },
    }


def test_optimize_route_creates_row_and_enqueues(client, seed_strategy):
    r = client.post(f"/api/strategies/{seed_strategy.id}/optimize", json=_payload())
    assert r.status_code == 200, r.text
    body = r.json()

    # Row created
    assert body["optimizationId"] > 0
    assert body["taskId"]  # a task id string was returned
    assert body["fitnessMetric"] == "sharpe"
    assert body["optimizationType"] == "genetic"
    assert body["status"] == "pending"

    # expert_params folded into optimization_config (RM sizing optimizes as expert settings)
    cfg = body["optimizationConfig"]
    assert "expert_params" in cfg and cfg["expert_params"], "expert_params not folded in"
    assert cfg["expert_params"]["risk_per_trade_pct"]["optimize"] is True
    assert cfg["expert_params"]["risk_per_trade_pct"]["min"] == 0.5
    # The GA params + backtest block survive untouched
    assert cfg["seed"] == 42
    assert cfg["backtest"]["engine"] == "daily"


def test_optimize_route_persists_strategy_optimization_row(client, seed_strategy, test_db):
    from app.models import StrategyOptimization

    r = client.post(f"/api/strategies/{seed_strategy.id}/optimize", json=_payload())
    assert r.status_code == 200, r.text
    opt_id = r.json()["optimizationId"]

    test_db.expire_all()
    row = (
        test_db.query(StrategyOptimization)
        .filter(StrategyOptimization.id == opt_id)
        .first()
    )
    assert row is not None
    assert row.strategy_id == seed_strategy.id
    assert row.fitness_metric == "sharpe"
    assert row.status == "pending"
    assert row.optimization_config["expert_params"]["risk_per_trade_pct"]["optimize"] is True


def test_optimize_route_enqueues_strategy_optimization_task(client, seed_strategy, test_db):
    from app.models.task_queue import TaskQueue

    r = client.post(f"/api/strategies/{seed_strategy.id}/optimize", json=_payload())
    assert r.status_code == 200, r.text
    task_id = r.json()["taskId"]

    test_db.expire_all()
    task = test_db.query(TaskQueue).filter(TaskQueue.task_id == task_id).first()
    assert task is not None
    assert task.task_type == "strategy_optimization"
    assert task.payload["optimization_id"] == r.json()["optimizationId"]


def test_optimize_route_404_for_missing_strategy(client):
    r = client.post("/api/strategies/999999/optimize", json=_payload())
    assert r.status_code == 404


def test_strategy_optimization_handler_registered(client):
    """Gate #6: the handler is registered in the app startup lifespan.

    Depends on `client` so the TestClient(app) context has run the startup lifespan
    that registers the handler.
    """
    from app.services.task_queue import get_task_queue

    tq = get_task_queue()
    # handlers are stored on the service; confirm the task type resolves to the real fn
    from app.services.strategy_optimization_handler import handle_strategy_optimization

    assert "strategy_optimization" in tq._handlers
    assert tq._handlers["strategy_optimization"] is handle_strategy_optimization
