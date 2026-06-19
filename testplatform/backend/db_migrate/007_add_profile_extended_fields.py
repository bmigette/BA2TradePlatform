"""
Migration 007: Add extended fields to optimization_profiles table

Adds columns for:
- job_type: 'classification' or 'regression'
- selected_target_set_ids: JSON list of target set IDs
- prediction_modes: JSON list of prediction modes ['shift', 'multistep']
"""


def get_table_columns(cursor, table_name):
    """Get list of column names for a table."""
    cursor.execute(f"PRAGMA table_info({table_name})")
    return [col[1] for col in cursor.fetchall()]


def upgrade(cursor, conn):
    """Add extended fields to optimization_profiles table."""
    columns = get_table_columns(cursor, "optimization_profiles")
    added = False

    if "job_type" not in columns:
        cursor.execute("ALTER TABLE optimization_profiles ADD COLUMN job_type VARCHAR(50) DEFAULT 'classification'")
        print("  - Added job_type column to optimization_profiles table")
        added = True
    else:
        print("  - job_type column already exists")

    if "selected_target_set_ids" not in columns:
        cursor.execute("ALTER TABLE optimization_profiles ADD COLUMN selected_target_set_ids JSON")
        print("  - Added selected_target_set_ids column to optimization_profiles table")
        added = True
    else:
        print("  - selected_target_set_ids column already exists")

    if "prediction_modes" not in columns:
        cursor.execute("ALTER TABLE optimization_profiles ADD COLUMN prediction_modes JSON")
        print("  - Added prediction_modes column to optimization_profiles table")
        added = True
    else:
        print("  - prediction_modes column already exists")

    if added:
        conn.commit()
    return added


def downgrade(cursor, conn):
    """SQLite doesn't support DROP COLUMN easily, so this is a no-op."""
    print("  - Downgrade not supported for this migration")
    return False
