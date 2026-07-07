"""Test for migration 027: add workers.sync_results_enabled (default True, backfilled)."""
import importlib.util
import sqlite3
from pathlib import Path

MIGRATION_PATH = (
    Path(__file__).resolve().parent.parent
    / "db_migrate"
    / "027_add_worker_sync_results_enabled.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location("migration_027", MIGRATION_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _table_columns(cursor, table):
    cursor.execute(f"PRAGMA table_info({table})")
    return [row[1] for row in cursor.fetchall()]


def _build_legacy_workers(conn):
    cursor = conn.cursor()
    cursor.execute(
        """
        CREATE TABLE workers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name VARCHAR(100) NOT NULL,
            url VARCHAR(500) NOT NULL,
            is_enabled BOOLEAN DEFAULT 1
        )
        """
    )
    cursor.execute("INSERT INTO workers (name, url) VALUES (?, ?)", ("existing-worker", "http://h:8100"))
    conn.commit()


def test_migration_adds_column_and_backfills_existing_rows():
    conn = sqlite3.connect(":memory:")
    try:
        _build_legacy_workers(conn)
        cursor = conn.cursor()

        before = _table_columns(cursor, "workers")
        assert "sync_results_enabled" not in before

        migration = _load_migration()
        result = migration.upgrade(cursor, conn)
        assert result

        after = _table_columns(cursor, "workers")
        assert "sync_results_enabled" in after

        cursor.execute("SELECT sync_results_enabled FROM workers WHERE name = 'existing-worker'")
        assert cursor.fetchone()[0] == 1

    finally:
        conn.close()


def test_migration_is_idempotent():
    conn = sqlite3.connect(":memory:")
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            CREATE TABLE workers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name VARCHAR(100) NOT NULL,
                sync_results_enabled BOOLEAN DEFAULT 1
            )
            """
        )
        conn.commit()
        migration = _load_migration()
        result = migration.upgrade(cursor, conn)
        assert not result
    finally:
        conn.close()
