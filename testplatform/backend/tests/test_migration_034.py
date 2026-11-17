"""Migration 034: the `walk_forward_runs` table (walk-forward results in the DB, for the UI).

The migration must create exactly the table the model declares, be idempotent, add the columns an
earlier shape lacks without touching its rows, leave every other table alone, and ``init_db()`` must
converge a DB without the table on start."""
import importlib.util
import sqlite3
from pathlib import Path

import pytest

MIGRATION_PATH = (Path(__file__).resolve().parent.parent / "db_migrate" / "034_add_walk_forward_runs.py")


def _load():
    spec = importlib.util.spec_from_file_location("migration_034", MIGRATION_PATH)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _old_db(tmp_path):
    """A DB with the pre-034 schema (create_all minus the new table) and one optimization row."""
    from sqlalchemy import create_engine
    from app.models.database import Base
    import app.models  # noqa: F401
    path = tmp_path / "old.sqlite"
    eng = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(eng, tables=[t for n, t in Base.metadata.tables.items()
                                          if n != "walk_forward_runs"])
    eng.dispose()
    c = sqlite3.connect(path)
    c.execute("INSERT INTO strategies (id, name) VALUES (1, 's')")
    c.execute("INSERT INTO strategy_optimizations (id, strategy_id, name, status, fitness_metric, "
              "optimization_type) VALUES (7, 1, 'cell-wf1', 'completed', 'sharpe', 'genetic')")
    c.commit()
    c.close()
    return path


def _columns(cur, table):
    cur.execute(f"PRAGMA table_info({table})")
    return [r[1] for r in cur.fetchall()]


def test_upgrade_creates_the_model_table_once_and_touches_nothing_else(tmp_path):
    from app.models.walk_forward_run import WalkForwardRun
    m = _load()
    conn = sqlite3.connect(_old_db(tmp_path))
    cur = conn.cursor()
    before = _columns(cur, "strategy_optimizations")
    assert m.upgrade(cur, conn) is True
    assert m.upgrade(cur, conn) is False                       # idempotent
    assert _columns(cur, "walk_forward_runs") == [c.name for c in WalkForwardRun.__table__.columns]
    assert _columns(cur, "strategy_optimizations") == before
    cur.execute("SELECT id, name, status FROM strategy_optimizations")
    assert cur.fetchall() == [(7, "cell-wf1", "completed")]
    # one row per (name, lane), as in the model: the same cell name in another lane is another row
    ins = "INSERT INTO walk_forward_runs (name, lane_key, source, status) VALUES (?, ?, 'cli', 'running')"
    cur.execute(ins, ("c", "/reports/goal2027_wf"))
    cur.execute(ins, ("c", "/reports/other_lane"))
    with pytest.raises(sqlite3.IntegrityError):
        cur.execute(ins, ("c", "/reports/goal2027_wf"))
    conn.close()


def test_upgrade_adds_the_columns_an_earlier_shape_lacks_and_keeps_its_rows(tmp_path):
    from app.models.walk_forward_run import WalkForwardRun
    m = _load()
    conn = sqlite3.connect(_old_db(tmp_path))
    cur = conn.cursor()
    cur.execute("CREATE TABLE walk_forward_runs (id INTEGER PRIMARY KEY, name VARCHAR(255) NOT NULL, "
                "lane_key VARCHAR(1024) NOT NULL, source VARCHAR(16) NOT NULL, status VARCHAR(24) "
                "NOT NULL, report JSON, UNIQUE (name, lane_key))")
    cur.execute("INSERT INTO walk_forward_runs (name, lane_key, source, status, report) "
                "VALUES ('old', '/r', 'cli', 'pass', '{}')")
    conn.commit()
    assert m.upgrade(cur, conn) is True
    assert set(_columns(cur, "walk_forward_runs")) == {c.name for c in WalkForwardRun.__table__.columns}
    cur.execute("SELECT name, source, status, report, spec FROM walk_forward_runs")
    assert cur.fetchall() == [("old", "cli", "pass", "{}", None)]
    assert m.upgrade(cur, conn) is False
    conn.close()


@pytest.mark.parametrize("ddl", [
    "CREATE TABLE walk_forward_runs (id INTEGER PRIMARY KEY, label TEXT)",
    # an early draft keyed on the name alone: no lane (the uniqueness cannot be ALTERed in)
    "CREATE TABLE walk_forward_runs (id INTEGER PRIMARY KEY, name VARCHAR(255) NOT NULL UNIQUE, "
    "source VARCHAR(16) NOT NULL, status VARCHAR(24) NOT NULL)",
])
def test_a_table_without_its_core_is_refused(tmp_path, ddl):
    m = _load()
    conn = sqlite3.connect(_old_db(tmp_path))
    cur = conn.cursor()
    cur.execute(ddl)
    with pytest.raises(RuntimeError, match="core column"):
        m.upgrade(cur, conn)
    conn.close()


def test_init_db_reconcile_converges_a_db_without_the_table(tmp_path):
    from sqlalchemy import create_engine
    from app.models.database import reconcile_walk_forward_tables
    from app.models.walk_forward_run import WalkForwardRun
    eng = create_engine(f"sqlite:///{_old_db(tmp_path)}")
    try:
        assert reconcile_walk_forward_tables(eng) is True
        assert reconcile_walk_forward_tables(eng) is False
        with eng.connect() as c:
            cols = [r[1] for r in c.exec_driver_sql("PRAGMA table_info(walk_forward_runs)")]
        assert cols == [col.name for col in WalkForwardRun.__table__.columns]
    finally:
        eng.dispose()


def test_downgrade_drops_only_the_new_table(tmp_path):
    m = _load()
    conn = sqlite3.connect(_old_db(tmp_path))
    cur = conn.cursor()
    m.upgrade(cur, conn)
    assert m.downgrade(cur, conn) is True
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='walk_forward_runs'")
    assert cur.fetchone() is None
    cur.execute("SELECT COUNT(*) FROM strategy_optimizations")
    assert cur.fetchone() == (1,)
    conn.close()
