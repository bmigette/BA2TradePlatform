"""
Test for migration 025: add the `robustness_runs` table.

Robustness runs link a parent backtest to its Monte Carlo results (kind='monte_carlo')
or its schedule-perturbation variant rows (kind='schedule'). Unlike `screener_history`,
`robustness_runs` IS a SQLAlchemy model (RobustnessRun) — so a FRESH DB gets the table
from `Base.metadata.create_all`, and this migration's table-exists short-circuit makes
that path a no-op; an EXISTING populated DB gets the table created by the migration.

Mirrors the migration-test house style (test_migration_022 / _019): load
`db_migrate/025_add_robustness_runs.py` via importlib, run its `upgrade(cursor, conn)`
against an open sqlite3 connection, then assert the table exists with the expected columns.
"""

import importlib.util
import sqlite3
from pathlib import Path

MIGRATION_PATH = (
    Path(__file__).resolve().parent.parent
    / "db_migrate"
    / "025_add_robustness_runs.py"
)

EXPECTED_COLUMNS = {
    "id",
    "backtest_id",
    "kind",
    "params",
    "results",
    "variant_backtest_ids",
    "status",
    "error_message",
    "created_at",
    "completed_at",
}


def _load_migration():
    spec = importlib.util.spec_from_file_location("migration_025", MIGRATION_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _table_exists(cursor, name):
    cursor.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)
    )
    return cursor.fetchone() is not None


def _table_columns(cursor, table):
    cursor.execute(f"PRAGMA table_info({table})")
    return [row[1] for row in cursor.fetchall()]


def _build_backtests_stub(conn):
    """Minimal backtests table so the FK target exists (mirrors an existing DB)."""
    cursor = conn.cursor()
    cursor.execute(
        """
        CREATE TABLE backtests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name VARCHAR(255) NOT NULL
        )
        """
    )
    conn.commit()


def test_migration_creates_robustness_runs_table_with_columns():
    conn = sqlite3.connect(":memory:")
    try:
        _build_backtests_stub(conn)
        cursor = conn.cursor()

        assert not _table_exists(cursor, "robustness_runs")

        migration = _load_migration()
        result = migration.upgrade(cursor, conn)
        assert result  # table was created

        assert _table_exists(cursor, "robustness_runs")
        cols = set(_table_columns(cursor, "robustness_runs"))
        missing = EXPECTED_COLUMNS - cols
        assert not missing, f"missing columns: {missing} (have {cols})"
    finally:
        conn.close()


def test_migration_is_idempotent_noop_when_table_exists():
    conn = sqlite3.connect(":memory:")
    try:
        _build_backtests_stub(conn)
        cursor = conn.cursor()

        migration = _load_migration()
        # First run creates the table.
        assert migration.upgrade(cursor, conn)
        # Second run is a no-op (the model/create_all already made it on a fresh DB).
        assert not migration.upgrade(cursor, conn)

        assert _table_exists(cursor, "robustness_runs")
    finally:
        conn.close()


def test_migration_inserts_and_reads_row():
    """The created table accepts a realistic RobustnessRun row (FK + JSON payloads)."""
    conn = sqlite3.connect(":memory:")
    try:
        _build_backtests_stub(conn)
        cursor = conn.cursor()
        cursor.execute("INSERT INTO backtests (name) VALUES ('parent')")
        conn.commit()
        parent_id = cursor.lastrowid

        migration = _load_migration()
        migration.upgrade(cursor, conn)

        cursor.execute(
            """
            INSERT INTO robustness_runs
                (backtest_id, kind, params, results, variant_backtest_ids, status)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (parent_id, "monte_carlo", "{}", "{}", "[]", "pending"),
        )
        conn.commit()

        cursor.execute(
            "SELECT backtest_id, kind, status FROM robustness_runs WHERE id=?",
            (cursor.lastrowid,),
        )
        row = cursor.fetchone()
        assert row == (parent_id, "monte_carlo", "pending")
    finally:
        conn.close()


def test_orm_model_matches_migration_table():
    """The RobustnessRun ORM model can insert into the migration-built table."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.models.backtest import Backtest, RobustnessRun  # noqa: F401

    engine = create_engine("sqlite://")  # in-memory, single shared connection

    raw = engine.raw_connection()
    try:
        _build_backtests_stub(raw)
        cursor = raw.cursor()
        cursor.execute("INSERT INTO backtests (name) VALUES ('parent')")
        raw.commit()
        parent_id = cursor.lastrowid
        migration = _load_migration()
        migration.upgrade(cursor, raw)
        raw.commit()
    finally:
        raw.close()

    SessionLocal = sessionmaker(bind=engine)
    session = SessionLocal()
    try:
        run = RobustnessRun(
            backtest_id=parent_id,
            kind="schedule",
            params={"day_variants": True},
            variant_backtest_ids=[1, 2, 3],
            status="pending",
        )
        assert run.id is None
        session.add(run)
        session.commit()
        session.refresh(run)
        assert run.id is not None
        assert run.variant_backtest_ids == [1, 2, 3]
    finally:
        session.close()
        engine.dispose()
