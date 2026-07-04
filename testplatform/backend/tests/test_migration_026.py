"""
Test for migration 026: replace the dead `Strategy.initial_tp_*`/`initial_sl_*` columns
with a single `entry_actions` JSON column.

Builds a legacy `strategies` table containing the 10 `initial_tp_*`/`initial_sl_*` columns
(no `entry_actions`) alongside the kept columns + one row, loads
`db_migrate/026_entry_actions_replace_tpsl_fields.py` via importlib, runs its migration
against an open sqlite3 connection (the real runner contract: `upgrade(cursor, conn)`), then
asserts NO `initial_tp_*`/`initial_sl_*` column survives, `entry_actions` is present, and the
kept row (name, id, exit_conditions) survives.
"""

import importlib.util
import json
import sqlite3
from pathlib import Path

MIGRATION_PATH = (
    Path(__file__).resolve().parent.parent
    / "db_migrate"
    / "026_entry_actions_replace_tpsl_fields.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location(
        "migration_026", MIGRATION_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _table_columns(cursor, table):
    cursor.execute(f"PRAGMA table_info({table})")
    return [row[1] for row in cursor.fetchall()]


def _build_legacy_strategies(conn):
    """Create a legacy strategies table (post-022, pre-026) with initial_tp_*/initial_sl_*
    columns + kept columns + one row, matching the real DB shape this migration targets."""
    cursor = conn.cursor()
    cursor.execute(
        """
        CREATE TABLE strategies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name VARCHAR(255) NOT NULL,
            description TEXT,
            required_fields JSON,
            entry_conditions JSON,
            buy_entry_conditions JSON,
            sell_entry_conditions JSON,
            exit_conditions JSON,
            initial_tp_percent FLOAT DEFAULT 5.0,
            initial_tp_optimize BOOLEAN DEFAULT 0,
            initial_tp_min FLOAT,
            initial_tp_max FLOAT,
            initial_tp_step FLOAT,
            initial_sl_percent FLOAT DEFAULT 2.0,
            initial_sl_optimize BOOLEAN DEFAULT 0,
            initial_sl_min FLOAT,
            initial_sl_max FLOAT,
            initial_sl_step FLOAT,
            created_at DATETIME,
            updated_at DATETIME
        )
        """
    )
    cursor.execute(
        """
        INSERT INTO strategies (name, initial_tp_percent, exit_conditions)
        VALUES (?, ?, ?)
        """,
        ("Legacy Strat", 7.5, json.dumps([{"id": "e1", "action_type": "close"}])),
    )
    conn.commit()


def test_migration_drops_tpsl_columns_and_adds_entry_actions():
    conn = sqlite3.connect(":memory:")
    try:
        _build_legacy_strategies(conn)
        cursor = conn.cursor()

        before = _table_columns(cursor, "strategies")
        assert any(c.startswith("initial_tp_") or c.startswith("initial_sl_") for c in before)
        assert "entry_actions" not in before

        migration = _load_migration()
        result = migration.upgrade(cursor, conn)
        assert result

        after = _table_columns(cursor, "strategies")
        assert not any(c.startswith("initial_tp_") or c.startswith("initial_sl_") for c in after), after
        assert "entry_actions" in after
        assert "name" in after
        assert "id" in after
        assert "exit_conditions" in after

        cursor.execute("SELECT id, name, exit_conditions, entry_actions FROM strategies")
        rows = cursor.fetchall()
        assert len(rows) == 1
        assert rows[0][0] == 1
        assert rows[0][1] == "Legacy Strat"
        assert json.loads(rows[0][2]) == [{"id": "e1", "action_type": "close"}]
        assert rows[0][3] is None
    finally:
        conn.close()


def _pk_columns(cursor, table):
    cursor.execute(f"PRAGMA table_info({table})")
    return {row[1]: row[5] for row in cursor.fetchall()}


def test_migration_preserves_pk_and_autoincrement():
    conn = sqlite3.connect(":memory:")
    try:
        _build_legacy_strategies(conn)
        cursor = conn.cursor()

        migration = _load_migration()
        migration.upgrade(cursor, conn)

        pks = _pk_columns(cursor, "strategies")
        assert pks.get("id") == 1, pks

        cursor.execute("SELECT MAX(id) FROM strategies")
        existing_max = cursor.fetchone()[0]
        assert existing_max == 1

        cursor.execute(
            "INSERT INTO strategies (name, entry_actions) VALUES (?, ?)",
            ("Brand New", json.dumps([{"id": "e_tp", "action_type": "adjust_take_profit"}])),
        )
        conn.commit()
        new_id = cursor.lastrowid
        assert new_id is not None
        assert new_id > existing_max, (new_id, existing_max)

        try:
            cursor.execute("INSERT INTO strategies (entry_actions) VALUES (?)", ("[]",))
            inserted_null_name = True
        except sqlite3.IntegrityError:
            inserted_null_name = False
        finally:
            conn.rollback()
        assert not inserted_null_name, "name NOT NULL constraint was lost"
    finally:
        conn.close()


def test_migration_preserves_pk_via_orm_insert():
    """Through the real SQLAlchemy Strategy model: build a legacy table, run the migration on
    the same DBAPI connection, then insert a new Strategy via the ORM (the way the app does)
    and confirm it gets an autoincrement id and the new entry_actions column round-trips."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.models.strategy import Strategy

    engine = create_engine("sqlite://")  # in-memory, single shared connection

    raw = engine.raw_connection()
    try:
        _build_legacy_strategies(raw)
        cursor = raw.cursor()
        migration = _load_migration()
        migration.upgrade(cursor, raw)
        raw.commit()
    finally:
        raw.close()

    SessionLocal = sessionmaker(bind=engine)
    session = SessionLocal()
    try:
        strat = Strategy(
            name="ORM Strat",
            entry_actions=[{"id": "e_sl", "action_type": "adjust_stop_loss", "action_value": -3.0}],
        )
        assert strat.id is None
        session.add(strat)
        session.commit()
        session.refresh(strat)
        assert strat.id is not None
        assert strat.id > 1  # greater than the migrated legacy row's id (1)
        assert strat.entry_actions == [
            {"id": "e_sl", "action_type": "adjust_stop_loss", "action_value": -3.0}
        ]
        d = strat.to_dict()
        assert d["entryActions"] == strat.entry_actions
        assert "initialTpPercent" not in d
        assert "initialSlPercent" not in d
    finally:
        session.close()
        engine.dispose()


def test_migration_is_idempotent_noop_when_already_migrated():
    conn = sqlite3.connect(":memory:")
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            CREATE TABLE strategies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name VARCHAR(255) NOT NULL,
                entry_actions JSON
            )
            """
        )
        cursor.execute(
            "INSERT INTO strategies (name, entry_actions) VALUES (?, ?)",
            ("Already Migrated", "[]"),
        )
        conn.commit()

        migration = _load_migration()
        result = migration.upgrade(cursor, conn)
        assert not result

        after = _table_columns(cursor, "strategies")
        assert not any(c.startswith("initial_tp_") or c.startswith("initial_sl_") for c in after)
        assert "entry_actions" in after
        cursor.execute("SELECT name FROM strategies")
        assert cursor.fetchone()[0] == "Already Migrated"
    finally:
        conn.close()
