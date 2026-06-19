"""Phase 4 Task 7: shared pytest fixtures for the joint-optimizer acceptance gate.

Reconciled to the REAL BA2TestPlatform names (verified in source, not assumed):

  * ``app.models.database`` exposes ``Base`` / ``SessionLocal`` / ``engine`` /
    ``get_db`` (database.py:52/49/24/55).
  * ``app.models`` re-exports ``get_db`` and the model classes
    (Strategy / StrategyOptimization / TaskQueue) — see app/models/__init__.py.
  * The task-queue ENQUEUE method is ``queue_task(task_type, name, payload=...)``
    (task_queue.py:112); ``add_task`` does NOT exist. The ``client`` fixture stubs
    ``queue_task`` so the route returns without a running worker daemon.

These fixtures are OPT-IN: a test gets them only by naming them as parameters.
The already-green Phase-4 tests (``test_strategy_optimization_handler.py`` uses its
own ``_host_db``; ``test_optimize_route.py`` defines its own module-scoped
``client``/``seed_strategy``/``test_db``) are self-contained — pytest resolves a
test module's LOCAL fixtures ahead of these conftest fixtures, so adding this file
does not change their behavior.
"""
from __future__ import annotations

import os
import tempfile

import pytest

# --- Host-DB isolation (must run BEFORE app.models.database is imported) ---------------------
# app/models/database.py binds its module-level `engine`/`SessionLocal` to DATABASE_URL at
# IMPORT time. Several tests (test_daily_backtest_handler, the e2e/perf/round-trip backtests)
# write `Backtest` rows through that module-level SessionLocal. Without isolation those rows
# land in the REAL host DB and pollute the live Backtesting history. pytest imports this package
# conftest before any test module, so setting DATABASE_URL here points the host engine at a
# throwaway sqlite for the whole run. Override with BA2_TEST_KEEP_DB=1 to use the real DB.
if not os.environ.get("BA2_TEST_KEEP_DB"):
    _ISOLATED_DB_DIR = tempfile.mkdtemp(prefix="ba2test-hostdb-")
    _ISOLATED_DB_PATH = os.path.join(_ISOLATED_DB_DIR, "test_host.sqlite")
    # sqlite URL wants forward slashes even on Windows.
    os.environ["DATABASE_URL"] = "sqlite:///" + _ISOLATED_DB_PATH.replace("\\", "/")


@pytest.fixture(scope="session")
def gate_engine(tmp_path_factory):
    """A throwaway SQLite engine with all model tables created.

    Named ``gate_engine`` (not ``engine``) so it never shadows the module-level
    ``engine`` symbol the existing tests import directly from
    ``app.models.database``.
    """
    from sqlalchemy import create_engine
    from app.models.database import Base
    # Importing app.models registers every model class on Base.metadata so
    # create_all builds the full schema (strategies, strategy_optimizations, ...).
    import app.models  # noqa: F401

    db_file = tmp_path_factory.mktemp("gatedb") / "phase4_gate.sqlite"
    eng = create_engine(
        f"sqlite:///{db_file}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def db(gate_engine):
    """A session bound to the throwaway gate engine, rolled back after each test."""
    from sqlalchemy.orm import sessionmaker

    Session = sessionmaker(bind=gate_engine)
    s = Session()
    try:
        yield s
    finally:
        s.rollback()
        s.close()


@pytest.fixture
def seed_strategy(db):
    """A Strategy row with TP/SL under optimization."""
    from app.models.strategy import Strategy

    s = Strategy(
        name="gate-opt-test",
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
    db.add(s)
    db.commit()
    db.refresh(s)
    return s


@pytest.fixture
def client(gate_engine, monkeypatch):
    """FastAPI TestClient with get_db overridden to the gate engine + queue stubbed.

    The enqueue method is ``queue_task`` (NOT the plan's placeholder ``add_task``);
    it is stubbed so the optimize route returns without a running worker daemon.
    """
    from fastapi.testclient import TestClient
    from sqlalchemy.orm import sessionmaker
    import app.main as main
    from app.models import get_db

    Session = sessionmaker(bind=gate_engine)

    def _get_db():
        s = Session()
        try:
            yield s
        finally:
            s.close()

    main.app.dependency_overrides[get_db] = _get_db
    from app.services import task_queue as tq

    monkeypatch.setattr(
        tq.get_task_queue(),
        "queue_task",
        lambda *a, **kw: "stub-task-id",
        raising=False,
    )
    try:
        with TestClient(main.app) as c:
            yield c
    finally:
        main.app.dependency_overrides.clear()
