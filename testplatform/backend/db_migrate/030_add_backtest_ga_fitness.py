"""
Migration 030: backtests.ga_fitness (the GA's composite score for this genome).

Adds one nullable column:

  backtests.ga_fitness  (REAL) - the fitness value the optimizer actually ranked this genome
                         on, carried straight from ``all_results`` when the top-N row is
                         persisted. NULL for manual runs and for every row written before
                         this migration.

WHY. ``consistent_annual_return`` is ``base x dd_guard x consistency x trade_gate`` — a
composite of four terms. Until now none of it was stored: a persisted row kept the component
metrics (annualized_return, max_drawdown, ...) but not the score, so the only surviving record
of the GA's ranking was the ``TOP<n>`` prefix in the name. That forced anyone comparing rows to
re-derive the ranking from a single component (usually CAR), which silently discards the other
three terms and can invert the order — a TOP1 with a better drawdown legitimately outranks a
TOP4 with a higher CAR. Storing the scalar makes the ranking checkable instead of inferred, and
makes GA-vs-persisted divergence detectable directly rather than through trade-count mismatches.

Nullable REAL. Idempotent: the ADD COLUMN is guarded by a column check, so re-running is a
no-op. Applies on a FRESH DB (create_all builds it from the model -> guard skips) AND upgrades
an EXISTING one. Follows the 017-029 house pattern.
"""

_TABLE_COLUMNS = [
    ("backtests", "ga_fitness", "REAL"),
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
    """Add backtests.ga_fitness if not already present."""
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
