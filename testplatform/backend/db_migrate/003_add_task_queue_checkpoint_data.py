"""
Migration 003: Add checkpoint_data column to task_queue table

This allows storing genetic algorithm state for crash recovery and job resumption.
The checkpoint_data field stores:
- Current generation
- Population state
- Best individual and fitness
- Random state for reproducibility
- Training history
"""


def get_table_columns(cursor, table_name):
    """Get list of column names for a table."""
    cursor.execute(f"PRAGMA table_info({table_name})")
    return [col[1] for col in cursor.fetchall()]


def upgrade(cursor, conn):
    """Add checkpoint_data column to task_queue table for crash recovery."""
    columns = get_table_columns(cursor, "task_queue")
    if "checkpoint_data" not in columns:
        cursor.execute("ALTER TABLE task_queue ADD COLUMN checkpoint_data JSON")
        conn.commit()
        print("  - Added checkpoint_data column to task_queue table")
        return True
    else:
        print("  - checkpoint_data column already exists")
        return False


def downgrade(cursor, conn):
    """SQLite doesn't support DROP COLUMN easily, so this is a no-op."""
    print("  - Downgrade not supported for this migration")
    return False
