"""The final population lives in its own table (migration 032), never in optimization_config.

Pinned: the table and its migration, that completion writes the row and not the config, that the
optimization LIST endpoint never touches the table, and that cloning/enqueueing an optimization
drops the lineage keys of the job it was copied from."""
from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import event

from app.models.database import Base, SessionLocal, engine
from app.models.strategy_optimization import (StrategyOptimization,
                                              StrategyOptimizationPopulation)
from app.services import strategy_optimization_handler as H
from tests import test_strategy_optimization_handler as T

MIGRATION = Path(__file__).resolve().parent.parent / "db_migrate" / "032_add_strategy_optimization_populations.py"
COLUMNS = {"optimization_id", "gene_space_signature", "population", "created_at"}


@pytest.fixture(scope="module", autouse=True)
def _host_db():
    Base.metadata.create_all(bind=engine)
    yield


@pytest.fixture(autouse=True)
def _stub(monkeypatch):
    monkeypatch.setattr(H, "_run_trial_backtest", T._deterministic_stub)
    monkeypatch.setattr(H, "_build_hoisted_state", lambda cfg: {})


def _migration():
    spec = importlib.util.spec_from_file_location("migration_032", MIGRATION)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_migration_creates_the_table_and_is_idempotent(tmp_path):
    con = sqlite3.connect(str(tmp_path / "m.db"))
    cur = con.cursor()
    cur.execute("CREATE TABLE strategy_optimizations (id INTEGER PRIMARY KEY)")
    m = _migration()
    assert m.upgrade(cur, con) is True
    cols = {r[1] for r in cur.execute("PRAGMA table_info(strategy_optimization_populations)")}
    assert cols == COLUMNS
    assert m.upgrade(cur, con) is False                       # second run: nothing to do
    assert m.downgrade(cur, con) is True
    assert not cur.execute("SELECT 1 FROM sqlite_master WHERE name='strategy_optimization_populations'").fetchone()


def test_model_columns_match_the_migration():
    assert {c.name for c in StrategyOptimizationPopulation.__table__.columns} == COLUMNS


def test_completion_writes_the_population_row_not_the_config():
    sid = T._seed_strategy()
    opt_id = T._seed_opt(sid, config=T._ga_config(populationSize=8, generations=2))
    assert H.handle_strategy_optimization("t-pop-table", {"optimization_id": opt_id})["status"] == "completed"
    db = SessionLocal()
    try:
        row = db.get(StrategyOptimizationPopulation, opt_id)
        cfg = db.get(StrategyOptimization, opt_id).optimization_config
    finally:
        db.close()
    assert row is not None and len(row.population["genes"]) == 8
    assert [g["name"] for g in row.gene_space_signature] == row.population["gene_names"]
    assert "finalPopulation" not in cfg


def test_list_endpoint_never_reads_the_population_table():
    from app.api import strategies as S
    sid = T._seed_strategy()
    opt_id = T._seed_opt(sid, config=T._ga_config(populationSize=6, generations=2))
    H.handle_strategy_optimization("t-pop-list", {"optimization_id": opt_id})
    seen = []

    @event.listens_for(engine, "before_cursor_execute")
    def _spy(conn, cursor, statement, *a, **k):
        seen.append(statement)

    db = SessionLocal()
    try:
        out = S.list_optimizations(db=db)
    finally:
        db.close()
        event.remove(engine, "before_cursor_execute", _spy)
    assert any(r["id"] == opt_id for r in out["optimizations"])
    assert seen and not any("strategy_optimization_populations" in s for s in seen)
    assert "finalPopulation" not in str(out)


def test_enqueue_drops_lineage_keys_copied_from_another_job(monkeypatch):
    from app.api import strategies as S

    class _Q:
        def queue_task(self, **kw):
            return "task-x"

    monkeypatch.setattr(S, "get_task_queue", lambda: _Q())
    sid = T._seed_strategy()
    src = T._ga_config()
    cfg = {**src, "seededFrom": {"name": "old-wf1", "optimization_id": 1},
           "chainSeedFromOptimizationId": 1, "requireFinalPopulation": True,
           "finalPopulation": {"genes": [[1]]}}
    db = SessionLocal()
    try:
        row, _ = S._enqueue_optimization(
            db, strategy_id=sid, name="clone", fitness_metric="sharpe", optimization_type="genetic",
            cfg=cfg, description="d", task_name="clone")
        stored = db.get(StrategyOptimization, row.id).optimization_config
    finally:
        db.close()
    for k in ("seededFrom", "chainSeedFromOptimizationId", "requireFinalPopulation", "finalPopulation"):
        assert k not in stored
    assert stored["populationSize"] == src["populationSize"]       # everything else survives
