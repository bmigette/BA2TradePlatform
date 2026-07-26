"""
Test for migration 022: drop the retired `Strategy.rm_*` columns.

Builds a legacy `strategies` table containing a few `rm_*` columns alongside the
kept columns + one row, loads `db_migrate/022_drop_strategy_rm_columns.py` via
importlib, runs its migration against an open sqlite3 connection (the real runner
contract: `upgrade(cursor, conn)`), then asserts NO column starts with `rm_` and
the kept row (name, id) survives.

UPDATED 2026-07-26. This test caught a REAL breakage and was being written off as noise.
022 rebuilds `strategies` from a hardcoded DDL, and that DDL was later "kept in sync with
the model" when 026 replaced `initial_tp_*`/`initial_sl_*` with `entry_actions`. Syncing a
HISTORICAL migration forward broke it: a database that has not yet run 022 still carries the
dead columns, 022 tried to copy them into a table that no longer declares them, and the whole
chain died with `no column named initial_tp_percent`. Replaying migrations on any old
database — restoring a backup, cloning an old snapshot — failed hard.

022 now copies only columns present in BOTH the source table and the rebuilt DDL. Safe
specifically because 026 DROPS the tp/sl columns rather than converting their values, so
discarding them one migration earlier changes the intermediate state but not the end state.

Consequently the assertions below no longer expect `initial_tp_percent` to survive 022 — it
is dropped there now instead of at 026. What must still hold is that the row, its id and the
columns the CURRENT model keeps all survive.
"""

import importlib.util
import sqlite3
from pathlib import Path

MIGRATION_PATH = (
    Path(__file__).resolve().parent.parent
    / "db_migrate"
    / "022_drop_strategy_rm_columns.py"
)
# Migration 026 (entry_actions replaces initial_tp_*/initial_sl_*) — chained after 022 in the
# ORM-insert test below so the rebuilt table matches the CURRENT Strategy model (which no
# longer declares initial_tp_*/initial_sl_*), exactly like the real migration runner applies
# migrations in sequence.
MIGRATION_026_PATH = (
    Path(__file__).resolve().parent.parent
    / "db_migrate"
    / "026_entry_actions_replace_tpsl_fields.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location(
        "migration_022", MIGRATION_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MIGRATION_028_PATH = (
    Path(__file__).resolve().parent.parent / "db_migrate" / "028_unified_trade_rules.py"
)


def _load_migration_028():
    spec = importlib.util.spec_from_file_location("migration_028", MIGRATION_028_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_migration_026():
    spec = importlib.util.spec_from_file_location(
        "migration_026", MIGRATION_026_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _table_columns(cursor, table):
    cursor.execute(f"PRAGMA table_info({table})")
    return [row[1] for row in cursor.fetchall()]


def _build_legacy_strategies(conn):
    """Create a legacy strategies table with rm_* + kept columns and one row."""
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
            rm_risk_per_trade_pct FLOAT DEFAULT 1.0,
            rm_risk_per_trade_pct_optimize BOOLEAN DEFAULT 0,
            rm_risk_per_trade_pct_min FLOAT,
            rm_risk_per_trade_pct_max FLOAT,
            rm_risk_per_trade_pct_step FLOAT,
            rm_max_concurrent_positions INTEGER DEFAULT 5,
            rm_max_concurrent_positions_optimize BOOLEAN DEFAULT 0,
            rm_max_concurrent_positions_min INTEGER,
            rm_max_concurrent_positions_max INTEGER,
            rm_max_concurrent_positions_step INTEGER,
            created_at DATETIME,
            updated_at DATETIME
        )
        """
    )
    cursor.execute(
        """
        INSERT INTO strategies (name, initial_tp_percent, rm_risk_per_trade_pct)
        VALUES (?, ?, ?)
        """,
        ("Legacy Strat", 7.5, 1.0),
    )
    conn.commit()


def test_migration_drops_all_rm_columns_and_keeps_data():
    conn = sqlite3.connect(":memory:")
    try:
        _build_legacy_strategies(conn)
        cursor = conn.cursor()

        # Sanity: rm_* columns exist before migration.
        before = _table_columns(cursor, "strategies")
        assert any(c.startswith("rm_") for c in before)

        migration = _load_migration()
        migration.upgrade(cursor, conn)

        after = _table_columns(cursor, "strategies")
        # No column starts with rm_ anymore.
        assert not any(c.startswith("rm_") for c in after), after
        # Kept columns survive.
        assert "name" in after
        assert "id" in after
        # Dropped here now rather than at 026 (see module docstring) — 026 discards the value
        # either way, so the end state is unchanged.
        assert "initial_tp_percent" not in after
        # ...and the column 026 introduces is present, because 022's DDL is the post-026 shape.
        assert "entry_actions" in after

        # The kept row (and its id/name) survived the rebuild.
        cursor.execute("SELECT id, name FROM strategies")
        rows = cursor.fetchall()
        assert len(rows) == 1
        assert rows[0][0] == 1
        assert rows[0][1] == "Legacy Strat"
    finally:
        conn.close()


def _pk_columns(cursor, table):
    cursor.execute(f"PRAGMA table_info({table})")
    # row = (cid, name, type, notnull, dflt_value, pk)
    return {row[1]: row[5] for row in cursor.fetchall()}


def test_migration_preserves_pk_and_autoincrement():
    """Regression for C1: the rebuilt table must keep id as an autoincrement PK.

    The old CTAS rebuild (`CREATE TABLE ... AS SELECT`) produced a PK-less,
    AUTOINCREMENT-less table; a fresh insert without an explicit id failed to
    autoincrement and `id` was not a PRIMARY KEY. This asserts both properties.
    """
    conn = sqlite3.connect(":memory:")
    try:
        _build_legacy_strategies(conn)
        cursor = conn.cursor()

        migration = _load_migration()
        migration.upgrade(cursor, conn)

        # id is the PRIMARY KEY on the rebuilt table.
        pks = _pk_columns(cursor, "strategies")
        assert pks.get("id") == 1, pks

        # Existing max id (the migrated legacy row).
        cursor.execute("SELECT MAX(id) FROM strategies")
        existing_max = cursor.fetchone()[0]
        assert existing_max == 1

        # Insert a row WITHOUT specifying id -> must get a working autoincrement.
        # Only `name` — 022 no longer carries initial_tp_percent through the rebuild.
        cursor.execute("INSERT INTO strategies (name) VALUES (?)", ("Brand New",))
        conn.commit()
        new_id = cursor.lastrowid
        assert new_id is not None
        assert new_id > existing_max, (new_id, existing_max)

        # name NOT NULL constraint survived the rebuild.
        try:
            cursor.execute("INSERT INTO strategies (description) VALUES (?)", ("no name",))
            inserted_null_name = True
        except sqlite3.IntegrityError:
            inserted_null_name = False
        finally:
            conn.rollback()
        assert not inserted_null_name, "name NOT NULL constraint was lost"
    finally:
        conn.close()


def test_migration_preserves_pk_via_orm_insert():
    """Regression for C1 through the real SQLAlchemy Strategy model.

    Build a legacy table, run the migration on the same DBAPI connection, then
    insert a new Strategy through the ORM the way the app does and confirm it
    gets an autoincrement id assigned by the DB.
    """
    from sqlalchemy import create_engine, event
    from sqlalchemy.orm import sessionmaker

    from app.models.strategy import Strategy

    engine = create_engine("sqlite://")  # in-memory, single shared connection

    # Run the legacy-table build + migration 022, THEN migration 026 (in the real order
    # migrations get applied), on the engine's connection — so the final schema matches the
    # CURRENT Strategy ORM model (which no longer declares initial_tp_*/initial_sl_*).
    raw = engine.raw_connection()
    try:
        _build_legacy_strategies(raw)
        cursor = raw.cursor()
        migration = _load_migration()
        migration.upgrade(cursor, raw)
        migration_026 = _load_migration_026()
        migration_026.upgrade(cursor, raw)
        # ...and 028, which replaced `entry_actions` with `entry_rules`/`exit_rules`. The
        # chain must run to the CURRENT head or the ORM below cannot map the table -- the
        # same "hardcoded DDL drifts behind the model" trap that broke 022 itself.
        migration_028 = _load_migration_028()
        migration_028.upgrade(cursor, raw)
        raw.commit()
    finally:
        raw.close()

    SessionLocal = sessionmaker(bind=engine)
    session = SessionLocal()
    try:
        strat = Strategy(name="ORM Strat",
                         entry_rules=[{"id": "e_sl", "actions": [{"action_type": "adjust_stop_loss"}]}])
        assert strat.id is None
        session.add(strat)
        session.commit()
        session.refresh(strat)
        assert strat.id is not None
        assert strat.id > 1  # greater than the migrated legacy row's id (1)
    finally:
        session.close()
        engine.dispose()


def test_migration_is_idempotent_noop_when_no_rm_columns():
    conn = sqlite3.connect(":memory:")
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            CREATE TABLE strategies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name VARCHAR(255) NOT NULL,
                initial_tp_percent FLOAT DEFAULT 5.0
            )
            """
        )
        cursor.execute(
            "INSERT INTO strategies (name, initial_tp_percent) VALUES (?, ?)",
            ("No RM", 5.0),
        )
        conn.commit()

        migration = _load_migration()
        result = migration.upgrade(cursor, conn)
        # No rm_* columns -> migration is a no-op (falsy return).
        assert not result

        after = _table_columns(cursor, "strategies")
        assert not any(c.startswith("rm_") for c in after)
        cursor.execute("SELECT name FROM strategies")
        assert cursor.fetchone()[0] == "No RM"
    finally:
        conn.close()
