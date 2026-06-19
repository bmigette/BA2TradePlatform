#!/usr/bin/env python3
"""
Database Migration Script

Run this script to apply pending database migrations.
Migrations are auto-discovered from the db_migrate folder.

Usage:
    ./venv/bin/python scripts/migrate_db.py [--dry-run] [--status]

Options:
    --dry-run   Show what migrations would be applied without running them
    --status    Show status of all migrations
"""

import sqlite3
import os
import sys
import importlib.util
from datetime import datetime
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Database path
DB_PATH = os.getenv("DATABASE_PATH", "dl_forecasting.db")

# Migrations folder
MIGRATIONS_DIR = Path(__file__).parent.parent / "db_migrate"


def discover_migrations():
    """Discover all migration files in the db_migrate folder."""
    migrations = []

    if not MIGRATIONS_DIR.exists():
        print(f"Warning: Migrations directory not found: {MIGRATIONS_DIR}")
        return migrations

    for file_path in sorted(MIGRATIONS_DIR.glob("*.py")):
        if file_path.name.startswith("_"):
            continue

        # Extract migration number and name from filename
        # Expected format: 001_description.py
        name = file_path.stem
        parts = name.split("_", 1)
        if len(parts) >= 1 and parts[0].isdigit():
            migrations.append({
                "number": parts[0],
                "name": name,
                "path": file_path,
            })

    return migrations


def load_migration_module(migration):
    """Load a migration module dynamically."""
    spec = importlib.util.spec_from_file_location(
        migration["name"],
        migration["path"]
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def get_applied_migrations(cursor):
    """Get list of applied migrations from database."""
    # Check if migrations table exists
    cursor.execute("""
        SELECT name FROM sqlite_master
        WHERE type='table' AND name='_migrations'
    """)
    if not cursor.fetchone():
        return set()

    cursor.execute("SELECT name FROM _migrations")
    return {row[0] for row in cursor.fetchall()}


def record_migration(cursor, conn, name):
    """Record that a migration has been applied."""
    # Create migrations table if it doesn't exist
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS _migrations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name VARCHAR(255) NOT NULL UNIQUE,
            applied_at DATETIME NOT NULL
        )
    """)
    cursor.execute(
        "INSERT INTO _migrations (name, applied_at) VALUES (?, ?)",
        (name, datetime.now().isoformat())
    )
    conn.commit()


def run_migrations(dry_run=False, status_only=False):
    """Run all pending migrations."""
    print(f"Database Migration Script")
    print(f"Database: {DB_PATH}")
    print(f"Migrations: {MIGRATIONS_DIR}")
    print(f"Time: {datetime.now().isoformat()}")
    print("-" * 60)

    if not os.path.exists(DB_PATH):
        print(f"Error: Database file not found: {DB_PATH}")
        print("Run the application first to create the database.")
        sys.exit(1)

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # Discover migrations
    migrations = discover_migrations()
    if not migrations:
        print("No migrations found.")
        conn.close()
        return

    # Get already applied migrations
    applied = get_applied_migrations(cursor)

    if status_only:
        print("\nMigration Status:")
        for m in migrations:
            status = "APPLIED" if m["name"] in applied else "PENDING"
            print(f"  [{status:8}] {m['name']}")
        conn.close()
        return

    # Run pending migrations
    applied_count = 0
    skipped_count = 0

    for m in migrations:
        print(f"\nMigration: {m['name']}")

        if m["name"] in applied:
            print("  - Already applied, skipping")
            skipped_count += 1
            continue

        if dry_run:
            print("  - Would apply (dry-run)")
            applied_count += 1
            continue

        try:
            module = load_migration_module(m)

            if hasattr(module, "upgrade"):
                result = module.upgrade(cursor, conn)
                if result:
                    record_migration(cursor, conn, m["name"])
                    applied_count += 1
                else:
                    # Migration returned False (already applied at DB level)
                    # Still record it to avoid re-running
                    record_migration(cursor, conn, m["name"])
                    skipped_count += 1
            else:
                print(f"  - ERROR: No upgrade() function found")

        except Exception as e:
            print(f"  - ERROR: {e}")
            conn.rollback()
            conn.close()
            sys.exit(1)

    conn.close()

    print("\n" + "-" * 60)
    if dry_run:
        print(f"Dry run complete: {applied_count} would be applied, {skipped_count} already applied")
    else:
        print(f"Migrations complete: {applied_count} applied, {skipped_count} skipped")


if __name__ == "__main__":
    dry_run = "--dry-run" in sys.argv
    status_only = "--status" in sys.argv
    run_migrations(dry_run=dry_run, status_only=status_only)
