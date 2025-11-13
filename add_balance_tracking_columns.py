"""
Migration script to add initial_available_balance and final_available_balance columns
to SmartRiskManagerJob table.

This migration adds balance tracking to Smart Risk Manager jobs.
"""

import sqlite3
from pathlib import Path

# Database path
db_path = Path.home() / "Documents" / "ba2_trade_platform" / "db.sqlite"

print(f"Connecting to database: {db_path}")

if not db_path.exists():
    print(f"ERROR: Database not found at {db_path}")
    exit(1)

conn = sqlite3.connect(db_path)
cursor = conn.cursor()

try:
    # Check if columns already exist
    cursor.execute("PRAGMA table_info(smartriskmanagerjob)")
    columns = [row[1] for row in cursor.fetchall()]
    
    print(f"Current columns in smartriskmanagerjob: {columns}")
    
    # Add initial_available_balance column if it doesn't exist
    if "initial_available_balance" not in columns:
        print("Adding initial_available_balance column...")
        cursor.execute("""
            ALTER TABLE smartriskmanagerjob 
            ADD COLUMN initial_available_balance REAL
        """)
        print("✓ Added initial_available_balance column")
    else:
        print("✓ initial_available_balance column already exists")
    
    # Add final_available_balance column if it doesn't exist
    if "final_available_balance" not in columns:
        print("Adding final_available_balance column...")
        cursor.execute("""
            ALTER TABLE smartriskmanagerjob 
            ADD COLUMN final_available_balance REAL
        """)
        print("✓ Added final_available_balance column")
    else:
        print("✓ final_available_balance column already exists")
    
    conn.commit()
    print("\n✅ Migration completed successfully!")
    
except Exception as e:
    print(f"\n❌ Migration failed: {e}")
    conn.rollback()
    raise
finally:
    conn.close()
