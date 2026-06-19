"""
Migration script to add new columns to optimization_profiles table.

Run this once after updating to the new version:
    ./venv/bin/python scripts/migrate_profiles.py
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.models.database import engine
from sqlalchemy import text


def migrate():
    """Add new columns to optimization_profiles table."""
    columns_to_add = [
        ("job_type", "VARCHAR(50) DEFAULT 'classification'"),
        ("selected_target_set_ids", "JSON"),
        ("prediction_modes", "JSON"),
    ]

    with engine.connect() as conn:
        for column_name, column_type in columns_to_add:
            try:
                conn.execute(text(f"ALTER TABLE optimization_profiles ADD COLUMN {column_name} {column_type}"))
                print(f"Added column: {column_name}")
            except Exception as e:
                if "duplicate column name" in str(e).lower():
                    print(f"Column {column_name} already exists, skipping")
                else:
                    print(f"Warning: Could not add column {column_name}: {e}")

        conn.commit()

    print("\nMigration complete!")


if __name__ == "__main__":
    migrate()
