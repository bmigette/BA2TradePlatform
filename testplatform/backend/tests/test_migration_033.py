"""Migration 033: the `strategy_optimization_runners` table (resume of an interrupted job).

The production grid DB is migrated in place between jobs: the migration must create exactly the
table the model declares, be idempotent, and leave every existing row alone, so an old checkout
keeps working on the migrated DB (it never reads the new table)."""
import importlib.util
import sqlite3

import pytest
from pathlib import Path

MIGRATION_PATH = (Path(__file__).resolve().parent.parent / "db_migrate"
                  / "033_add_strategy_optimization_runners.py")


def _load():
    spec = importlib.util.spec_from_file_location("migration_033", MIGRATION_PATH)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _old_db(tmp_path):
    """A DB with the pre-033 schema (create_all minus the new table) and one running row."""
    from sqlalchemy import create_engine
    from app.models.database import Base
    import app.models  # noqa: F401
    path = tmp_path / "old.sqlite"
    eng = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(eng, tables=[t for n, t in Base.metadata.tables.items()
                                          if n not in ("strategy_optimization_runners",
                                                                       "backtest_writers")])
    eng.dispose()
    c = sqlite3.connect(path)
    c.execute("INSERT INTO strategies (id, name) VALUES (1, 's')")
    c.execute("INSERT INTO strategy_optimizations (id, strategy_id, name, status, fitness_metric, "
              "optimization_type) VALUES (34, 1, 'cell-wf2', 'running', 'sharpe', 'genetic')")
    c.commit()
    c.close()
    return path


def _columns(cur, table):
    cur.execute(f"PRAGMA table_info({table})")
    return [r[1] for r in cur.fetchall()]


def test_upgrade_creates_the_model_table_once_and_touches_nothing_else(tmp_path):
    from app.models.strategy_optimization import BacktestWriter, StrategyOptimizationRunner
    m = _load()
    conn = sqlite3.connect(_old_db(tmp_path))
    cur = conn.cursor()
    before = _columns(cur, "strategy_optimizations")
    assert m.upgrade(cur, conn) is True
    assert m.upgrade(cur, conn) is False                       # idempotent
    assert _columns(cur, "strategy_optimization_runners") == \
        [c.name for c in StrategyOptimizationRunner.__table__.columns]
    assert _columns(cur, "strategy_optimizations") == before
    cur.execute("SELECT id, name, status FROM strategy_optimizations")
    assert cur.fetchall() == [(34, "cell-wf2", "running")]
    assert m.downgrade(cur, conn) is True and m.downgrade(cur, conn) is False
    conn.close()


# The shape the FIRST version of this migration created (before proc_identity / run_token /
# ended_at, and without backtest_writers): a DB that ran it must converge too.
_FIRST_033 = """
CREATE TABLE strategy_optimization_runners (
    optimization_id INTEGER PRIMARY KEY REFERENCES strategy_optimizations(id),
    host VARCHAR(255) NOT NULL,
    pid INTEGER NOT NULL,
    pid_started FLOAT NOT NULL,
    code_versions JSON,
    resumed_from_id INTEGER,
    started_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    topn_done_at DATETIME
);
INSERT INTO strategy_optimization_runners (optimization_id, host, pid, pid_started)
    VALUES (34, 'minisrv', 4242, 1.0);
"""

_TABLES = ("strategy_optimization_runners", "backtest_writers")


def _schema(cur):
    out = {}
    for t in _TABLES:
        cur.execute(f"PRAGMA table_info({t})")
        out[t] = {(r[1], r[2].upper()) for r in cur.fetchall()}
    return out


def _create_all_schema(tmp_path):
    from sqlalchemy import create_engine
    from app.models.database import Base
    import app.models  # noqa: F401
    path = tmp_path / "fresh.sqlite"
    eng = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(eng)
    eng.dispose()
    c = sqlite3.connect(path)
    try:
        return _schema(c.cursor())
    finally:
        c.close()


@pytest.mark.parametrize("start", ["no tables", "first 033", "current"])
def test_every_starting_state_converges_on_the_create_all_schema(tmp_path, start):
    m = _load()
    path = _old_db(tmp_path)
    conn = sqlite3.connect(path)
    cur = conn.cursor()
    if start == "first 033":
        cur.executescript(_FIRST_033)
        conn.commit()
    if start == "current":
        assert m.upgrade(cur, conn) is True
    assert m.upgrade(cur, conn) is (start != "current")
    assert m.upgrade(cur, conn) is False                      # idempotent from every state
    assert _schema(cur) == _create_all_schema(tmp_path)
    if start == "first 033":
        cur.execute("SELECT optimization_id, host, run_token FROM strategy_optimization_runners")
        assert cur.fetchall() == [(34, "minisrv", None)]       # the row is kept, new column NULL
    conn.close()


def test_init_db_path_reconciles_a_first_033_table(tmp_path):
    """create_all never alters an existing table; init_db's reconciliation does."""
    from sqlalchemy import create_engine
    from app.models.database import reconcile_resume_tables
    path = _old_db(tmp_path)
    c = sqlite3.connect(path)
    c.executescript(_FIRST_033)
    c.commit()
    c.close()
    eng = create_engine(f"sqlite:///{path}")
    try:
        assert reconcile_resume_tables(eng) is True
        assert reconcile_resume_tables(eng) is False
    finally:
        eng.dispose()
    c = sqlite3.connect(path)
    try:
        assert _schema(c.cursor()) == _create_all_schema(tmp_path)
    finally:
        c.close()
