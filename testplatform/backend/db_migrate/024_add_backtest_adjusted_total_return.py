"""
Migration 024: backtests.adjusted_total_return (profit-capped return).

Adds one nullable column for the per-trade profit-cap feature:

  backtests.adjusted_total_return  (FLOAT) - total return % with each trade's gain capped at
                                    profit_cap_pct of its cost basis (entry x size). NULL for
                                    runs with no cap. The raw total_return column is unchanged;
                                    the UI exposes both.

Nullable. Idempotent: the ADD COLUMN is guarded by a column check, so re-running is a no-op.
Applies on a FRESH DB (create_all builds the column from the model -> guard skips) AND upgrades
an EXISTING DB. Follows the 017-023 house pattern.
"""

_TABLE_COLUMNS = [
    ("backtests", "adjusted_total_return", "FLOAT"),
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
    """Add backtests.adjusted_total_return if not already present."""
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
