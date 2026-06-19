"""
Migration 001: Add task_id column to datasets table

This allows tracking of background tasks that create/update datasets.
"""


def get_table_columns(cursor, table_name):
    """Get list of column names for a table."""
    cursor.execute(f"PRAGMA table_info({table_name})")
    return [col[1] for col in cursor.fetchall()]


def upgrade(cursor, conn):
    """Add task_id column to datasets table for background task tracking."""
    columns = get_table_columns(cursor, "datasets")
    if "task_id" not in columns:
        cursor.execute("ALTER TABLE datasets ADD COLUMN task_id VARCHAR(50)")
        conn.commit()
        print("  - Added task_id column to datasets table")
        return True
    else:
        print("  - task_id column already exists")
        return False


def downgrade(cursor, conn):
    """SQLite doesn't support DROP COLUMN easily, so this is a no-op."""
    print("  - Downgrade not supported for this migration")
    return False
