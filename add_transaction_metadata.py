"""
Migration script to add meta_data JSON column to Transaction table.

This script adds a nullable meta_data JSON field to the Transaction model
for storing additional data like TradeConditionsData.

Usage:
    .venv\Scripts\python.exe add_transaction_meta_data.py [db_folder]
    
Arguments:
    db_folder: Optional path to folder containing db.sqlite file
               Defaults to ~/Documents/ba2_trade_platform/
"""

import sys
import os
from sqlalchemy import text, inspect, create_engine
from sqlmodel import Session
from ba2_trade_platform.logger import logger


def get_engine(db_folder: str = None):
    """Get database engine for the specified folder."""
    if db_folder:
        db_path = os.path.join(db_folder, "db.sqlite")
        if not os.path.exists(db_path):
            raise FileNotFoundError(f"Database file not found: {db_path}")
        logger.info(f"Using database: {db_path}")
        return create_engine(f"sqlite:///{db_path}")
    else:
        # Use default from config
        from ba2_trade_platform.core.db import get_db
        return get_db().bind


def check_column_exists(engine, table_name: str, column_name: str) -> bool:
    """Check if a column exists in a table."""
    try:
        inspector = inspect(engine)
        columns = [col['name'] for col in inspector.get_columns(table_name)]
        return column_name in columns
    except Exception as e:
        logger.error(f"Error checking column existence: {e}")
        return False


def add_meta_data_column(db_folder: str = None):
    """Add meta_data column to transaction table."""
    try:
        engine = get_engine(db_folder)
        
        # Check if column already exists
        if check_column_exists(engine, 'transaction', 'meta_data'):
            logger.info("✅ Column 'meta_data' already exists in 'transaction' table - skipping migration")
            return True
        
        logger.info("Adding 'meta_data' column to 'transaction' table...")
        
        with Session(engine) as session:
            # Add the meta_data column as JSON type, nullable
            # Note: "transaction" is a reserved word in SQLite, so we must quote it
            session.exec(text("""
                ALTER TABLE "transaction" 
                ADD COLUMN meta_data JSON NULL
            """))
            session.commit()
            
        logger.info("✅ Successfully added 'meta_data' column to 'transaction' table")
        return True
        
    except Exception as e:
        logger.error(f"❌ Failed to add meta_data column: {e}", exc_info=True)
        return False


def verify_migration(db_folder: str = None):
    """Verify the migration was successful."""
    try:
        engine = get_engine(db_folder)
        if check_column_exists(engine, 'transaction', 'meta_data'):
            logger.info("✅ Migration verification successful - 'meta_data' column exists")
            return True
        else:
            logger.error("❌ Migration verification failed - 'meta_data' column not found")
            return False
    except Exception as e:
        logger.error(f"❌ Migration verification error: {e}", exc_info=True)
        return False


def main():
    """Run the migration."""
    # Parse command line arguments
    db_folder = None
    if len(sys.argv) > 1:
        db_folder = sys.argv[1]
        if not os.path.isdir(db_folder):
            logger.error(f"❌ Directory not found: {db_folder}")
            return 1
        logger.info(f"Using custom database folder: {db_folder}")
    
    logger.info("=" * 80)
    logger.info("Transaction Metadata Migration Script")
    logger.info("=" * 80)
    
    # Run migration
    if add_meta_data_column(db_folder):
        # Verify migration
        if verify_migration(db_folder):
            logger.info("=" * 80)
            logger.info("✅ Migration completed successfully!")
            logger.info("=" * 80)
            return 0
        else:
            logger.error("Migration verification failed")
            return 1
    else:
        logger.error("Migration failed")
        return 1


if __name__ == "__main__":
    exit(main())
