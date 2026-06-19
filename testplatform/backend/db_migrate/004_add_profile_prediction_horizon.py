"""
Migration 004: Add prediction_horizon column to optimization_profiles table

This allows storing the prediction horizon setting in job profiles.
"""


def get_table_columns(cursor, table_name):
    """Get list of column names for a table."""
    cursor.execute(f"PRAGMA table_info({table_name})")
    return [col[1] for col in cursor.fetchall()]


def upgrade(cursor, conn):
    """Add prediction_horizon column to optimization_profiles table."""
    columns = get_table_columns(cursor, "optimization_profiles")
    if "prediction_horizon" not in columns:
        cursor.execute("ALTER TABLE optimization_profiles ADD COLUMN prediction_horizon INTEGER DEFAULT 3")
        conn.commit()
        print("  - Added prediction_horizon column to optimization_profiles table")
        return True
    else:
        print("  - prediction_horizon column already exists")
        return False


def downgrade(cursor, conn):
    """SQLite doesn't support DROP COLUMN easily, so this is a no-op."""
    print("  - Downgrade not supported for this migration")
    return False
