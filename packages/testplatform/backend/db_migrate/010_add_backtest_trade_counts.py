"""
Migration 010: Add winning_trades, losing_trades, final_equity to backtests table

Adds additional performance tracking fields for backtest results.
"""


def get_table_columns(cursor, table_name):
    """Get list of column names for a table."""
    cursor.execute(f"PRAGMA table_info({table_name})")
    return [col[1] for col in cursor.fetchall()]


def upgrade(cursor, conn):
    """Add new columns to backtests table."""
    columns = get_table_columns(cursor, "backtests")
    added = False

    if "winning_trades" not in columns:
        cursor.execute("ALTER TABLE backtests ADD COLUMN winning_trades INTEGER")
        print("  - Added winning_trades column to backtests table")
        added = True
    else:
        print("  - winning_trades column already exists")

    if "losing_trades" not in columns:
        cursor.execute("ALTER TABLE backtests ADD COLUMN losing_trades INTEGER")
        print("  - Added losing_trades column to backtests table")
        added = True
    else:
        print("  - losing_trades column already exists")

    if "final_equity" not in columns:
        cursor.execute("ALTER TABLE backtests ADD COLUMN final_equity REAL")
        print("  - Added final_equity column to backtests table")
        added = True
    else:
        print("  - final_equity column already exists")

    if added:
        conn.commit()
    return added


def downgrade(cursor, conn):
    """SQLite doesn't support DROP COLUMN easily, so this is a no-op."""
    print("  - Downgrade not supported for this migration")
    return False
