"""
Migration 023: per-worker password + per-optimization worker selection.

Adds two nullable columns for the push-based remote-worker feature:

  workers.password                    (VARCHAR) - the auth password the master sends to this
                                       worker's HTTP server (write-only via the API).
  strategy_optimizations.worker_ids   (JSON)    - list of Worker ids selected for the run
                                       (NULL/empty = local only).

Both nullable. Idempotent: each ADD COLUMN is guarded by a column check, so re-running is a
no-op. Applies on a FRESH DB (create_all builds both columns from the models -> guards skip)
AND upgrades an EXISTING DB (adds only the missing columns). Follows the 017-022 house pattern.
"""

_TABLE_COLUMNS = [
    ("workers", "password", "VARCHAR(255)"),
    ("strategy_optimizations", "worker_ids", "JSON"),
]


def get_table_columns(cursor, table_name):
    cursor.execute(f"PRAGMA table_info({table_name})")
    return [col[1] for col in cursor.fetchall()]


def _table_exists(cursor, name):
    cursor.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)
    )
    return cursor.fetchone() is not None


def upgrade(cursor, conn):
    """Add workers.password + strategy_optimizations.worker_ids if not already present."""
    added = False
    for table, column, sqlite_type in _TABLE_COLUMNS:
        if not _table_exists(cursor, table):
            print(f"  - {table} table does not exist yet; skipping {column}")
            continue
        if column in set(get_table_columns(cursor, table)):
            print(f"  - {table}.{column} already exists; nothing to do")
            continue
        cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {sqlite_type}")
        print(f"  - Added {column} ({sqlite_type}) to {table}")
        added = True

    if added:
        conn.commit()
    return added


def downgrade(cursor, conn):
    """SQLite has no simple DROP COLUMN across versions; downgrade is a no-op."""
    print("  - Downgrade not supported for this migration")
    return False
