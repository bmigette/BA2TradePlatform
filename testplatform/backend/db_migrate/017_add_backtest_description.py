"""
Add description field to backtests table for user/agent notes.
"""


def upgrade(cursor, conn):
    """Add description column to backtests table."""
    cursor.execute("PRAGMA table_info(backtests)")
    columns = [row[1] for row in cursor.fetchall()]

    if 'description' not in columns:
        cursor.execute("ALTER TABLE backtests ADD COLUMN description TEXT")
        conn.commit()
        print("  - Added 'description' column to backtests table")
        return True
    else:
        print("  - Column 'description' already exists")
        return False
