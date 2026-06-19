"""
Migration 013: Add is_saved column to backtests table

Adds a boolean field to track whether a backtest has been explicitly saved
with a custom name vs auto-generated name.
"""


def get_table_columns(cursor, table_name):
    """Get list of column names for a table."""
    cursor.execute(f"PRAGMA table_info({table_name})")
    return [col[1] for col in cursor.fetchall()]


def upgrade(cursor, conn):
    """Add is_saved column to backtests table."""
    columns = get_table_columns(cursor, "backtests")

    if "is_saved" not in columns:
        cursor.execute("ALTER TABLE backtests ADD COLUMN is_saved BOOLEAN DEFAULT 0")
        print("  - Added is_saved column to backtests table")
        conn.commit()
        return True
    else:
        print("  - is_saved column already exists")
        return False


def downgrade(cursor, conn):
    """SQLite doesn't support DROP COLUMN easily, so this is a no-op."""
    print("  - Downgrade not supported for this migration")
    return False
