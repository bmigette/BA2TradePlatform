"""
Migration 021: add `expert_name` + `optimization_id` to the `backtests` table.

Grouping/stats columns so runs can be filtered per expert (best-N retention) and per
optimization job (group stats):

  expert_name     (VARCHAR) - the expert class this run backtested (e.g. "FMPRating")
  optimization_id (INTEGER) - the StrategyOptimization job this run belongs to (NULL for
                              standalone runs)

Both nullable (legacy/manual runs may have neither). Idempotent: each ADD COLUMN is guarded
by a column check, so re-running is a no-op. Applies on a FRESH DB (init_db/create_all builds
`backtests` with these columns from the SQLAlchemy model -> guards skip) AND upgrades an
EXISTING populated DB (adds only the missing columns). Follows the 017-020 house pattern.
"""

_COLUMNS = [
    ("expert_name", "VARCHAR(100)"),
    ("optimization_id", "INTEGER"),
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
    """Add expert_name + optimization_id to `backtests` if not already present."""
    if not _table_exists(cursor, "backtests"):
        print("  - backtests table does not exist yet; nothing to migrate")
        return False

    existing = set(get_table_columns(cursor, "backtests"))
    added = False
    for name, sqlite_type in _COLUMNS:
        if name not in existing:
            cursor.execute(f"ALTER TABLE backtests ADD COLUMN {name} {sqlite_type}")
            print(f"  - Added {name} ({sqlite_type}) to backtests table")
            added = True

    # Helpful indexes for the per-expert / per-opt-job filters (idempotent).
    cursor.execute("CREATE INDEX IF NOT EXISTS ix_backtests_expert_name ON backtests(expert_name)")
    cursor.execute("CREATE INDEX IF NOT EXISTS ix_backtests_optimization_id ON backtests(optimization_id)")

    if added:
        conn.commit()
    else:
        print("  - expert_name/optimization_id already exist; nothing to migrate")
    return added


def downgrade(cursor, conn):
    """SQLite has no simple DROP COLUMN across versions; downgrade is a no-op."""
    print("  - Downgrade not supported for this migration")
    return False
