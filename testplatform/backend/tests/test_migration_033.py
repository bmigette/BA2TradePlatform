"""Migration 033: the `strategy_optimization_runners` table (resume of an interrupted job).

The production grid DB is migrated in place between jobs: the migration must create exactly the
table the model declares, be idempotent, and leave every existing row alone, so an old checkout
keeps working on the migrated DB (it never reads the new table)."""
import importlib.util
import sqlite3
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
                                          if n != "strategy_optimization_runners"])
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
    from app.models.strategy_optimization import StrategyOptimizationRunner
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
