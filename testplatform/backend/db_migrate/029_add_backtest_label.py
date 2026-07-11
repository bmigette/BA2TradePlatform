"""
Migration 029: backtests.labels (grid/run filter tags).

Adds one nullable column:

  backtests.labels  (JSON, list[str]) - free-form tags set by a grid/optimization driver, e.g.
                     ["goal5", "S4"] (one for the grid/batch id, one for the strategy — a
                     backtest can carry SEVERAL independent labels, unlike the single
                     expert_name/optimization_id columns). Lets runs from the same grid batch
                     AND/OR the same strategy be filtered together in the UI/API independent of
                     cap-band or optimization_id. NULL/empty for manual/legacy runs that
                     predate this feature or don't set any.

SQLite has no native JSON column type; SQLAlchemy's JSON type maps to TEXT and (de)serializes
in Python, so this is a plain TEXT column at the SQL level storing a JSON-encoded array. Filtered
via SQLite's built-in json_each() table-valued function (app/api/backtests.py) — requires the
JSON1 extension, which is compiled into Python's stdlib sqlite3 by default on all supported
platforms here.

Nullable. Idempotent: the ADD COLUMN is guarded by a column check, so re-running is a no-op.
Applies on a FRESH DB (create_all builds the column from the model -> guard skips) AND upgrades
an EXISTING DB. Follows the 017-028 house pattern.
"""

_TABLE_COLUMNS = [
    ("backtests", "labels", "TEXT"),
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
    """Add backtests.labels if not already present."""
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
