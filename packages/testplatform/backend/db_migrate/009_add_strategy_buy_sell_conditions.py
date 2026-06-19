"""
Migration 009: Add buy_entry_conditions and sell_entry_conditions to strategies table

Splits the single entry_conditions field into separate buy and sell conditions
to support distinct long/short entry logic in backtesting.

The old entry_conditions field is kept for backwards compatibility.
"""


def get_table_columns(cursor, table_name):
    """Get list of column names for a table."""
    cursor.execute(f"PRAGMA table_info({table_name})")
    return [col[1] for col in cursor.fetchall()]


def upgrade(cursor, conn):
    """Add buy_entry_conditions and sell_entry_conditions columns to strategies table."""
    columns = get_table_columns(cursor, "strategies")
    added = False

    if "buy_entry_conditions" not in columns:
        cursor.execute("ALTER TABLE strategies ADD COLUMN buy_entry_conditions JSON")
        print("  - Added buy_entry_conditions column to strategies table")
        added = True
    else:
        print("  - buy_entry_conditions column already exists")

    if "sell_entry_conditions" not in columns:
        cursor.execute("ALTER TABLE strategies ADD COLUMN sell_entry_conditions JSON")
        print("  - Added sell_entry_conditions column to strategies table")
        added = True
    else:
        print("  - sell_entry_conditions column already exists")

    if added:
        conn.commit()
    return added


def downgrade(cursor, conn):
    """SQLite doesn't support DROP COLUMN easily, so this is a no-op."""
    print("  - Downgrade not supported for this migration")
    return False
