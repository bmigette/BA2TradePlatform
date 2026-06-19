"""
Migration 016: Drop normalization_buffer_pct column from datasets table

This column was added to the DB schema but never added to the SQLAlchemy model,
causing NOT NULL constraint failures on every dataset INSERT.
"""


def get_table_columns(cursor, table_name):
    """Get list of column names for a table."""
    cursor.execute(f"PRAGMA table_info({table_name})")
    return [col[1] for col in cursor.fetchall()]


def upgrade(cursor, conn):
    """Drop normalization_buffer_pct column from datasets table."""
    columns = get_table_columns(cursor, "datasets")

    if "normalization_buffer_pct" in columns:
        cursor.execute("ALTER TABLE datasets DROP COLUMN normalization_buffer_pct")
        print("  - Dropped normalization_buffer_pct column from datasets table")
        conn.commit()
        return True
    else:
        print("  - normalization_buffer_pct column does not exist, skipping")
        return False


def downgrade(cursor, conn):
    """Re-add the column (as nullable to avoid breaking existing rows)."""
    columns = get_table_columns(cursor, "datasets")

    if "normalization_buffer_pct" not in columns:
        cursor.execute("ALTER TABLE datasets ADD COLUMN normalization_buffer_pct REAL DEFAULT NULL")
        print("  - Re-added normalization_buffer_pct column to datasets table")
        conn.commit()
        return True
    else:
        print("  - normalization_buffer_pct column already exists")
        return False
