"""
Database migration script to add LLMUsageLog table.

Creates the llmusagelog table for tracking token usage and costs across all LLM requests.
"""

import sqlite3
import os
from pathlib import Path

# Database path
DB_PATH = Path.home() / "Documents" / "ba2_trade_platform" / "db.sqlite"

def add_llm_usage_table():
    """Add LLMUsageLog table to the database."""
    
    if not DB_PATH.exists():
        print(f"❌ Database not found at {DB_PATH}")
        return False
    
    print(f"📂 Using database: {DB_PATH}")
    
    # Connect to database
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    try:
        # Check if table already exists
        cursor.execute("""
            SELECT name FROM sqlite_master 
            WHERE type='table' AND name='llmusagelog'
        """)
        
        if cursor.fetchone():
            print("⚠️  Table 'llmusagelog' already exists. Skipping creation.")
            return True
        
        # Create the table
        print("📝 Creating llmusagelog table...")
        cursor.execute("""
            CREATE TABLE llmusagelog (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                expert_instance_id INTEGER,
                account_id INTEGER,
                use_case VARCHAR NOT NULL,
                model_selection VARCHAR NOT NULL,
                provider VARCHAR NOT NULL,
                provider_model_name VARCHAR,
                input_tokens INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0,
                total_tokens INTEGER NOT NULL DEFAULT 0,
                estimated_cost_usd REAL,
                timestamp DATETIME NOT NULL,
                duration_ms INTEGER,
                symbol VARCHAR,
                market_analysis_id INTEGER,
                smart_risk_manager_job_id INTEGER,
                error VARCHAR,
                additional_metadata TEXT,
                FOREIGN KEY(expert_instance_id) REFERENCES expertinstance(id) ON DELETE SET NULL,
                FOREIGN KEY(account_id) REFERENCES accountdefinition(id) ON DELETE SET NULL,
                FOREIGN KEY(market_analysis_id) REFERENCES marketanalysis(id) ON DELETE SET NULL,
                FOREIGN KEY(smart_risk_manager_job_id) REFERENCES smartriskmanagerjob(id) ON DELETE SET NULL
            )
        """)
        
        # Create indexes for common queries
        print("📊 Creating indexes...")
        cursor.execute("""
            CREATE INDEX idx_llmusagelog_timestamp 
            ON llmusagelog(timestamp)
        """)
        
        cursor.execute("""
            CREATE INDEX idx_llmusagelog_expert 
            ON llmusagelog(expert_instance_id)
        """)
        
        cursor.execute("""
            CREATE INDEX idx_llmusagelog_model 
            ON llmusagelog(model_selection)
        """)
        
        cursor.execute("""
            CREATE INDEX idx_llmusagelog_use_case 
            ON llmusagelog(use_case)
        """)
        
        # Commit changes
        conn.commit()
        print("✅ Successfully created llmusagelog table and indexes")
        
        return True
        
    except sqlite3.Error as e:
        print(f"❌ Database error: {e}")
        conn.rollback()
        return False
        
    finally:
        conn.close()


if __name__ == "__main__":
    print("=" * 60)
    print("LLM Usage Table Migration")
    print("=" * 60)
    
    success = add_llm_usage_table()
    
    if success:
        print("\n✅ Migration completed successfully!")
    else:
        print("\n❌ Migration failed!")
