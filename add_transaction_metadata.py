"""
Migration script to add meta_data JSON column to Transaction table.

This script adds a nullable meta_data JSON field to the Transaction model
for storing additional data like TradeConditionsData.

Usage:
    .venv\Scripts\python.exe add_transaction_meta_data.py
"""

from sqlalchemy import text, inspect
from sqlmodel import Session
from ba2_trade_platform.core.db import get_db
from ba2_trade_platform.logger import logger


def check_column_exists(table_name: str, column_name: str) -> bool:
    """Check if a column exists in a table."""
    try:
        engine = get_db().bind
        inspector = inspect(engine)
        columns = [col['name'] for col in inspector.get_columns(table_name)]
        return column_name in columns
    except Exception as e:
        logger.error(f"Error checking column existence: {e}")
        return False


def add_meta_data_column():
    """Add meta_data column to transaction table."""
    try:
        engine = get_db().bind
        
        # Check if column already exists
        if check_column_exists('transaction', 'meta_data'):
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


def verify_migration():
    """Verify the migration was successful."""
    try:
        if check_column_exists('transaction', 'meta_data'):
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
    logger.info("=" * 80)
    logger.info("Transaction Metadata Migration Script")
    logger.info("=" * 80)
    
    # Run migration
    if add_meta_data_column():
        # Verify migration
        if verify_migration():
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
