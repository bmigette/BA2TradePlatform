"""
Migration: Add checkpoint_data column to task_queue table

This column stores JSON data for genetic algorithm checkpoints,
enabling job resumability after crashes.
"""

def upgrade(cursor, conn):
    """Add checkpoint_data column to task_queue table."""
    # Check if column already exists
    cursor.execute("PRAGMA table_info(task_queue)")
    columns = [row[1] for row in cursor.fetchall()]

    if 'checkpoint_data' in columns:
        print("  - Column checkpoint_data already exists, skipping")
        return False

    # Add the column
    cursor.execute("""
        ALTER TABLE task_queue
        ADD COLUMN checkpoint_data TEXT
    """)
    conn.commit()
    print("  - Added checkpoint_data column to task_queue")
    return True


def downgrade(cursor, conn):
    """Remove checkpoint_data column (SQLite doesn't support DROP COLUMN easily)."""
    print("  - Downgrade not supported for SQLite ALTER TABLE")
    return False
