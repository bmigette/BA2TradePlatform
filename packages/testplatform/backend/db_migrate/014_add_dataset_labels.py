"""
Migration 014: Add labels column to datasets table

Adds a JSON field for storing dataset labels (list of strings)
for organizing and filtering datasets.
"""


def get_table_columns(cursor, table_name):
    """Get list of column names for a table."""
    cursor.execute(f"PRAGMA table_info({table_name})")
    return [col[1] for col in cursor.fetchall()]


def upgrade(cursor, conn):
    """Add labels column to datasets table."""
    columns = get_table_columns(cursor, "datasets")

    if "labels" not in columns:
        cursor.execute("ALTER TABLE datasets ADD COLUMN labels TEXT DEFAULT NULL")
        print("  - Added labels column to datasets table")
        conn.commit()
        return True
    else:
        print("  - labels column already exists")
        return False


def downgrade(cursor, conn):
    """SQLite doesn't support DROP COLUMN easily, so this is a no-op."""
    print("  - Downgrade not supported for this migration")
    return False
