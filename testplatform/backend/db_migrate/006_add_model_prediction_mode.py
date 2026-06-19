"""
Migration 006: Add prediction_mode and loss_function columns to trained_models table

Stores the prediction mode used during training:
- "shift": Target shifted by prediction_horizon, single output (c_out=2)
- "multistep": Multi-label output for T+1 to T+prediction_horizon (c_out=N)

Stores the loss function used during training:
- "focal_loss": FocalLoss for imbalanced data
- "cross_entropy": Standard cross-entropy for balanced data
- "weighted_cross_entropy": Weighted CE/BCE for class imbalance
"""


def get_table_columns(cursor, table_name):
    """Get list of column names for a table."""
    cursor.execute(f"PRAGMA table_info({table_name})")
    return [col[1] for col in cursor.fetchall()]


def upgrade(cursor, conn):
    """Add prediction_mode and loss_function columns to trained_models table."""
    columns = get_table_columns(cursor, "trained_models")
    added = False

    if "prediction_mode" not in columns:
        cursor.execute("ALTER TABLE trained_models ADD COLUMN prediction_mode TEXT DEFAULT 'shift'")
        print("  - Added prediction_mode column to trained_models table")
        added = True
    else:
        print("  - prediction_mode column already exists")

    if "loss_function" not in columns:
        cursor.execute("ALTER TABLE trained_models ADD COLUMN loss_function TEXT DEFAULT 'focal_loss'")
        print("  - Added loss_function column to trained_models table")
        added = True
    else:
        print("  - loss_function column already exists")

    if added:
        conn.commit()
    return added


def downgrade(cursor, conn):
    """SQLite doesn't support DROP COLUMN easily, so this is a no-op."""
    print("  - Downgrade not supported for this migration")
    return False
