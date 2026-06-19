"""
Migration 005: Add normalization_params column to trained_models table

This stores the data normalization/scaler settings used during training,
enabling consistent data transformation for inference.

The normalization_params contain:
- buffer_pct: Extra room above/below observed min/max (default 35%)
- columns: Dictionary with per-column normalization settings:
  - method: "minmax_buffered", "zscore", etc.
  - observed_min/max: The original data range
  - buffered_min/max: The range used for normalization
"""


def get_table_columns(cursor, table_name):
    """Get list of column names for a table."""
    cursor.execute(f"PRAGMA table_info({table_name})")
    return [col[1] for col in cursor.fetchall()]


def upgrade(cursor, conn):
    """Add normalization_params column to trained_models table."""
    columns = get_table_columns(cursor, "trained_models")
    if "normalization_params" not in columns:
        cursor.execute("ALTER TABLE trained_models ADD COLUMN normalization_params JSON")
        conn.commit()
        print("  - Added normalization_params column to trained_models table")
        return True
    else:
        print("  - normalization_params column already exists")
        return False


def downgrade(cursor, conn):
    """SQLite doesn't support DROP COLUMN easily, so this is a no-op."""
    print("  - Downgrade not supported for this migration")
    return False
