"""
Migration 008: Add threshold column to trained_models table

Stores the optimized classification threshold used during training.
The threshold is optimized by the GA to find the best cutoff for
converting model probabilities to class predictions.

Default is 0.5 (standard classification threshold).
"""


def get_table_columns(cursor, table_name):
    """Get list of column names for a table."""
    cursor.execute(f"PRAGMA table_info({table_name})")
    return [col[1] for col in cursor.fetchall()]


def upgrade(cursor, conn):
    """Add threshold column to trained_models table."""
    columns = get_table_columns(cursor, "trained_models")
    added = False

    if "threshold" not in columns:
        cursor.execute("ALTER TABLE trained_models ADD COLUMN threshold REAL DEFAULT 0.5")
        print("  - Added threshold column to trained_models table")
        added = True
    else:
        print("  - threshold column already exists")

    if added:
        conn.commit()
    return added


def downgrade(cursor, conn):
    """SQLite doesn't support DROP COLUMN easily, so this is a no-op."""
    print("  - Downgrade not supported for this migration")
    return False
