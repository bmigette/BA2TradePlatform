"""
Migration 002: Add progress_message column to datasets table

This allows storing progress messages during dataset creation/updates.
"""


def get_table_columns(cursor, table_name):
    """Get list of column names for a table."""
    cursor.execute(f"PRAGMA table_info({table_name})")
    return [col[1] for col in cursor.fetchall()]


def upgrade(cursor, conn):
    """Add progress_message column to datasets table for progress tracking."""
    columns = get_table_columns(cursor, "datasets")
    if "progress_message" not in columns:
        cursor.execute("ALTER TABLE datasets ADD COLUMN progress_message TEXT")
        conn.commit()
        print("  - Added progress_message column to datasets table")
        return True
    else:
        print("  - progress_message column already exists")
        return False


def downgrade(cursor, conn):
    """SQLite doesn't support DROP COLUMN easily, so this is a no-op."""
    print("  - Downgrade not supported for this migration")
    return False
