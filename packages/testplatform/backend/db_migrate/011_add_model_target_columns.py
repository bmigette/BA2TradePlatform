"""
Migration 011: Add target_columns column to trained_models table

Stores the actual column names generated during training for each prediction target.
This allows exact matching when running predictions, instead of regenerating
column names from the target configuration (which may have variations).

The column stores a JSON array of column names, e.g.:
["price_up_5pct_10dd_360b", "price_down_5pct_10dd_360b"]
"""


def get_table_columns(cursor, table_name):
    """Get list of column names for a table."""
    cursor.execute(f"PRAGMA table_info({table_name})")
    return [col[1] for col in cursor.fetchall()]


def upgrade(cursor, conn):
    """Add target_columns column to trained_models table."""
    columns = get_table_columns(cursor, "trained_models")
    added = False

    if "target_columns" not in columns:
        cursor.execute("ALTER TABLE trained_models ADD COLUMN target_columns TEXT")
        print("  - Added target_columns column to trained_models table")
        added = True
    else:
        print("  - target_columns column already exists")

    if added:
        conn.commit()
    return added


def downgrade(cursor, conn):
    """SQLite doesn't support DROP COLUMN easily, so this is a no-op."""
    print("  - Downgrade not supported for this migration")
    return False
