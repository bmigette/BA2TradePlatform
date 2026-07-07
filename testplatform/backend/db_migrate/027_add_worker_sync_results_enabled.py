"""Migration 027: add workers.sync_results_enabled.

Adds one nullable-but-defaulted column for the result-replication feature:

  workers.sync_results_enabled   (BOOLEAN, default 1) - whether Backtest/StrategyOptimization
                                  rows get pushed to this worker. On by default for every
                                  worker, including ones that already existed before this
                                  migration ran (explicit UPDATE backfill below — don't rely
                                  solely on SQLite's ADD COLUMN ... DEFAULT to backfill existing
                                  rows the way a fresh INSERT would get the Python-side default).

Idempotent: the ADD COLUMN is guarded by a column check, so re-running is a no-op. Follows the
017-026 house pattern.
"""


def _table_exists(cursor, name):
    cursor.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)
    )
    return cursor.fetchone() is not None


def get_table_columns(cursor, table_name):
    cursor.execute(f"PRAGMA table_info({table_name})")
    return [col[1] for col in cursor.fetchall()]


def upgrade(cursor, conn):
    """Add workers.sync_results_enabled if not already present; backfill existing rows to 1."""
    if not _table_exists(cursor, "workers"):
        print("  - workers table does not exist yet; skipping sync_results_enabled")
        return False
    if "sync_results_enabled" in set(get_table_columns(cursor, "workers")):
        print("  - workers.sync_results_enabled already exists; nothing to do")
        return False

    cursor.execute("ALTER TABLE workers ADD COLUMN sync_results_enabled BOOLEAN DEFAULT 1")
    print("  - Added sync_results_enabled (BOOLEAN) to workers")
    cursor.execute("UPDATE workers SET sync_results_enabled = 1 WHERE sync_results_enabled IS NULL")
    print("  - Backfilled sync_results_enabled = 1 for existing workers")
    conn.commit()
    return True


def downgrade(cursor, conn):
    """SQLite has no simple DROP COLUMN across versions; downgrade is a no-op."""
    print("  - Downgrade not supported for this migration")
    return False
