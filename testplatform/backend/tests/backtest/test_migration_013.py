"""
Migration 013 gate (Phase 2 Task 7): backtests.model_id nullable + engine_type.

Verifies the migration:
  * applies on a FRESH DB built from the current SQLAlchemy model (no-op, since the
    model already declares model_id nullable + engine_type);
  * upgrades an EXISTING populated legacy DB (model_id INTEGER NOT NULL, no engine_type)
    by rebuilding the table -> model_id becomes nullable, engine_type added, NO data loss,
    existing rows stamped 'ml', a NULL-model_id daily_expert insert then succeeds;
  * is idempotent (re-run is a no-op).

These are pure-sqlite tests (no network, no app server) so they run in the unit suite.
"""

import importlib.util
import os
import sqlite3
import tempfile

import pytest

MIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "db_migrate",
    "018_backtest_model_id_nullable_engine_type.py",
)


def _load_migration():
    spec = importlib.util.spec_from_file_location("m013", MIG_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _cols(cur, table="backtests"):
    cur.execute(f"PRAGMA table_info({table})")
    return {r[1]: {"notnull": r[3], "dflt": r[4], "pk": r[5]} for r in cur.fetchall()}


def _make_legacy_db(path):
    """Pre-Phase-2 schema: model_id NOT NULL, no engine_type, with rows."""
    conn = sqlite3.connect(path)
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE backtests (
            id INTEGER NOT NULL,
            name VARCHAR(255) NOT NULL,
            model_id INTEGER NOT NULL,
            strategy_id INTEGER,
            start_date DATETIME NOT NULL,
            end_date DATETIME NOT NULL,
            initial_capital FLOAT,
            status VARCHAR(50),
            results JSON,
            total_return FLOAT,
            winning_trades INTEGER,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (id),
            FOREIGN KEY(model_id) REFERENCES trained_models (id)
        )
        """
    )
    cur.execute("CREATE INDEX ix_backtests_id ON backtests (id)")
    cur.executemany(
        "INSERT INTO backtests (id, name, model_id, start_date, end_date, status, total_return, winning_trades) "
        "VALUES (?,?,?,?,?,?,?,?)",
        [
            (1, "legacy 1", 42, "2024-01-01", "2024-02-01", "completed", 12.5, 7),
            (2, "legacy 2", 99, "2024-03-01", "2024-04-01", "completed", -3.1, 2),
        ],
    )
    conn.commit()
    return conn


def test_013_fresh_db_is_noop():
    """On a DB created from the current model, 013 finds everything present -> no-op."""
    m = _load_migration()
    from sqlalchemy import create_engine
    from app.models.database import Base
    import app.models.backtest  # noqa: F401  (registers the table)

    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "fresh.db")
        eng = create_engine(f"sqlite:///{p}")
        Base.metadata.create_all(eng)
        eng.dispose()

        conn = sqlite3.connect(p)
        cur = conn.cursor()
        before = _cols(cur)
        assert before["model_id"]["notnull"] == 0  # model already nullable
        assert "engine_type" in before

        assert m.upgrade(cur, conn) is False  # nothing to do
        after = _cols(cur)
        assert after["model_id"]["notnull"] == 0
        assert "engine_type" in after
        # idempotent
        assert m.upgrade(cur, conn) is False
        conn.close()


def test_013_upgrades_legacy_populated_db_without_data_loss():
    """Legacy NOT-NULL populated DB is rebuilt: nullable model_id + engine_type, rows kept."""
    m = _load_migration()
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "legacy.db")
        conn = _make_legacy_db(p)
        cur = conn.cursor()

        before = _cols(cur)
        assert before["model_id"]["notnull"] == 1
        assert "engine_type" not in before
        cur.execute("SELECT id, name, model_id, total_return, winning_trades FROM backtests ORDER BY id")
        rows_before = cur.fetchall()

        assert m.upgrade(cur, conn) is True

        after = _cols(cur)
        assert after["model_id"]["notnull"] == 0, "model_id must be nullable after rebuild"
        assert "engine_type" in after, "engine_type must be added"

        # No data loss.
        cur.execute("SELECT id, name, model_id, total_return, winning_trades FROM backtests ORDER BY id")
        assert cur.fetchall() == rows_before

        # Existing rows stamped 'ml'.
        cur.execute("SELECT DISTINCT engine_type FROM backtests")
        assert [r[0] for r in cur.fetchall()] == ["ml"]

        # Index recreated.
        cur.execute("SELECT name FROM sqlite_master WHERE type='index' AND name='ix_backtests_id'")
        assert cur.fetchone() is not None

        # NULL model_id daily_expert insert now works (the whole point).
        cur.execute(
            "INSERT INTO backtests (id, name, model_id, start_date, end_date, status, engine_type) "
            "VALUES (3, 'daily', NULL, '2024-05-01', '2024-06-01', 'pending', 'daily_expert')"
        )
        conn.commit()
        cur.execute("SELECT model_id, engine_type FROM backtests WHERE id=3")
        assert cur.fetchone() == (None, "daily_expert")

        # Idempotent.
        assert m.upgrade(cur, conn) is False
        conn.close()


def test_013_adds_engine_type_when_model_id_already_nullable():
    """A DB with nullable model_id but no engine_type: just ADD COLUMN (no rebuild)."""
    m = _load_migration()
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "partial.db")
        conn = sqlite3.connect(p)
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE backtests (
                id INTEGER PRIMARY KEY,
                name VARCHAR(255) NOT NULL,
                model_id INTEGER,
                start_date DATETIME NOT NULL,
                end_date DATETIME NOT NULL,
                status VARCHAR(50)
            )
            """
        )
        cur.execute(
            "INSERT INTO backtests (id, name, model_id, start_date, end_date, status) "
            "VALUES (1, 'ml run', 7, '2024-01-01', '2024-02-01', 'completed')"
        )
        conn.commit()

        assert "engine_type" not in _cols(cur)
        assert m.upgrade(cur, conn) is True
        assert "engine_type" in _cols(cur)
        cur.execute("SELECT engine_type FROM backtests WHERE id=1")
        assert cur.fetchone()[0] == "ml"  # existing row stamped
        assert m.upgrade(cur, conn) is False  # idempotent
        conn.close()


def test_013_no_table_is_noop():
    """If backtests table doesn't exist yet, migration is a safe no-op."""
    m = _load_migration()
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "empty.db")
        conn = sqlite3.connect(p)
        cur = conn.cursor()
        assert m.upgrade(cur, conn) is False
        conn.close()
