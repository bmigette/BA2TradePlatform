"""POST /api/strategies/optimizations/{opt_id}/individuals/{rank}/backtest.

Runs a full backtest for an ARBITRARY evaluated individual from an optimization's
``all_results`` (not just the CLI's already-persisted top-N). Creates an optimization-derived
``Backtest`` row (``optimization_id`` set, ``strategy_params`` = the individual's raw genes) and
queues it on the dedicated re-run queue — reuses ``handle_rerun_backtest``'s existing
reconstruction path (``_build_optimization_rerun_config``), no new execution machinery.

Uses the shared conftest ``client``/``db`` fixtures (a throwaway SQLite gate engine). The
rerun queue (a SEPARATE queue instance from the main one the conftest ``client`` fixture already
stubs) is stubbed per-test via ``app.services.task_queue.get_rerun_task_queue`` — the route does
a function-local ``from app.services.task_queue import get_rerun_task_queue``, so patching the
origin module's attribute is picked up at call time.

NOTE: ``gate_engine`` is session-scoped and a committed row is never undone by the ``db``
fixture's teardown rollback (rollback only discards an uncommitted transaction, not prior
commits) — so rows seeded here are visible to every other conftest-``client``-based test in the
same pytest session. Uses a distinctive, non-"FMPRating" expert class name specifically so it
can't collide with other files' exact-set assertions on that widely-reused fixture default.
"""
from __future__ import annotations

import pytest


def _seed_opt(db, *, all_results, name="ind-run-opt"):
    from app.models.strategy import Strategy
    from app.models.strategy_optimization import StrategyOptimization

    strat = Strategy(name="ind-run-strat", entry_rules=[], exit_rules=[])
    db.add(strat)
    db.commit()
    db.refresh(strat)

    opt = StrategyOptimization(
        strategy_id=strat.id, name=name,
        fitness_metric="sharpe", optimization_type="genetic",
        optimization_config={
            "backtest": {
                "backtest_id": 1,
                "start_date": "2024-01-01", "end_date": "2024-06-01",
                "enabled_instruments": ["AAPL"],
                "experts": [{"class": "TestOnlyExpert", "settings": {}}],
                "initial_capital": 10000.0,
                "account_settings": {"starting_cash": 10000.0},
                "warmup_days": 30,
                "seed": 42,
            }
        },
        all_results=all_results,
        best_params=all_results[0]["params"] if all_results else None,
        best_fitness=all_results[0]["fitness"] if all_results else None,
        status="completed",
    )
    db.add(opt)
    db.commit()
    db.refresh(opt)
    return opt


class _StubRerunQueue:
    def __init__(self):
        self.calls = []

    def queue_task(self, *a, **kw):
        self.calls.append(kw)
        return "stub-rerun-task"


@pytest.fixture
def stub_rerun_queue(monkeypatch):
    from app.services import task_queue as tq

    stub = _StubRerunQueue()
    monkeypatch.setattr(tq, "get_rerun_task_queue", lambda: stub)
    return stub


def test_run_rank_1_creates_pending_backtest(client, db, stub_rerun_queue):
    opt = _seed_opt(db, all_results=[
        {"params": {"model:x": 1.0}, "fitness": 2.0, "trades": 5},
        {"params": {"model:x": 0.5}, "fitness": 1.0, "trades": 3},
    ])

    resp = client.post(f"/api/strategies/optimizations/{opt.id}/individuals/1/backtest")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "pending"  # queued, hasn't started running yet
    assert body["task_id"] == "stub-rerun-task"
    bt_id = body["backtest_id"]

    from app.models.backtest import Backtest

    bt = db.query(Backtest).filter(Backtest.id == bt_id).first()
    assert bt is not None
    assert bt.status == "pending"
    assert bt.optimization_id == opt.id
    assert bt.expert_name == "TestOnlyExpert"
    assert bt.strategy_params["model:x"] == 1.0  # rank 1 = highest fitness (2.0)
    assert len(stub_rerun_queue.calls) == 1
    assert stub_rerun_queue.calls[0]["payload"] == {"backtest_id": bt_id}


def test_run_rank_2_picks_the_second_best(client, db, stub_rerun_queue):
    opt = _seed_opt(db, all_results=[
        {"params": {"model:x": 1.0}, "fitness": 2.0, "trades": 5},
        {"params": {"model:x": 0.5}, "fitness": 1.0, "trades": 3},
    ])

    resp = client.post(f"/api/strategies/optimizations/{opt.id}/individuals/2/backtest")
    assert resp.status_code == 200, resp.text

    from app.models.backtest import Backtest

    bt = db.query(Backtest).filter(Backtest.id == resp.json()["backtest_id"]).first()
    assert bt.strategy_params["model:x"] == 0.5


def test_rank_out_of_range_404s(client, db, stub_rerun_queue):
    opt = _seed_opt(db, all_results=[{"params": {"model:x": 1.0}, "fitness": 2.0, "trades": 5}])

    resp = client.post(f"/api/strategies/optimizations/{opt.id}/individuals/5/backtest")
    assert resp.status_code == 404


def test_missing_optimization_404s(client, db, stub_rerun_queue):
    resp = client.post("/api/strategies/optimizations/999999/individuals/1/backtest")
    assert resp.status_code == 404
