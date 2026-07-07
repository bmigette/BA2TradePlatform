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
    """Create a legacy workers table (pre-027) matching the real Worker model's full column
    set minus sync_results_enabled, so ORM round-trip reads work after the migration runs."""
    cursor = conn.cursor()
    cursor.execute(
        """
        CREATE TABLE workers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name VARCHAR(100) NOT NULL,
            url VARCHAR(500) NOT NULL,
            description TEXT,
            worker_type VARCHAR(20) DEFAULT 'remote',
            capabilities JSON,
            password VARCHAR(255),
            is_enabled BOOLEAN DEFAULT 1,
            is_local BOOLEAN DEFAULT 0,
            status VARCHAR(20) DEFAULT 'offline',
            gpu_info JSON,
            cpu_info JSON,
            last_heartbeat DATETIME,
            active_jobs_count INTEGER DEFAULT 0,
            total_jobs_completed INTEGER DEFAULT 0,
            created_at DATETIME,
            updated_at DATETIME
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


def test_migration_preserves_default_via_orm_read():
    """Through the real SQLAlchemy Worker model: build a legacy table, run the migration on
    the same DBAPI connection, then read the migrated+backfilled row via the ORM and confirm
    sync_results_enabled deserializes as an actual Python bool and to_dict() wires it through."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.models.worker import Worker

    engine = create_engine("sqlite://")  # in-memory, single shared connection

    raw = engine.raw_connection()
    try:
        _build_legacy_workers(raw)
        cursor = raw.cursor()
        migration = _load_migration()
        migration.upgrade(cursor, raw)
        raw.commit()
    finally:
        raw.close()

    SessionLocal = sessionmaker(bind=engine)
    session = SessionLocal()
    try:
        worker = session.query(Worker).filter_by(name="existing-worker").one()
        assert worker.sync_results_enabled is True
        assert worker.to_dict()["syncResultsEnabled"] is True
    finally:
        session.close()
        engine.dispose()


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
