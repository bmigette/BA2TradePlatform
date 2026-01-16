"""
Migration script to add close_reason column to Transaction table.

Run this ONCE after updating code to include the close_reason field.
"""

import argparse
import os
import sys

# Parse command-line arguments FIRST before any imports
parser = argparse.ArgumentParser(description="Add close_reason column to Transaction table")
parser.add_argument(
    "--db-file",
    type=str,
    help="Path to custom SQLite database file (overrides default)"
)
args = parser.parse_args()

# Set custom database path if provided - MUST be done before any ba2_trade_platform imports
if args.db_file:
    if not os.path.exists(args.db_file):
        print(f"❌ Database file not found: {args.db_file}")
        sys.exit(1)
    
    # Override the DB_FILE in config module BEFORE it's used
    import ba2_trade_platform.config as config
    config.DB_FILE = args.db_file
    print(f"Using custom database: {args.db_file}")

# NOW import the rest after config is set
from ba2_trade_platform.core.db import get_db
from ba2_trade_platform.logger import logger
from sqlalchemy import text


def add_close_reason_column():
    """Add close_reason column to transaction table if it doesn't exist."""
    
    logger.info("="*80)
    logger.info("Adding close_reason Column to Transaction Table")
    logger.info("="*80)
    
    try:
        with get_db() as session:
            # Check if column exists
            logger.info("Checking if 'close_reason' column exists...")
            result = session.execute(text('PRAGMA table_info("transaction")'))
            columns = [row[1] for row in result.fetchall()]
            
            if 'close_reason' in columns:
                logger.info("  ✓ Column 'close_reason' already exists")
                return True, 0
            
            # Add the column
            logger.info("Adding 'close_reason' column...")
            try:
                session.execute(text('ALTER TABLE "transaction" ADD COLUMN close_reason VARCHAR'))
                session.commit()
                logger.info("  ✓ Column 'close_reason' added successfully")
                return True, 1
            except Exception as e:
                logger.error(f"  ✗ Failed to add column: {e}", exc_info=True)
                raise
            
    except Exception as e:
        logger.error(f"Fatal error during migration: {e}", exc_info=True)
        raise


if __name__ == "__main__":
    try:
        success, changes = add_close_reason_column()
        if success:
            if changes > 0:
                print(f"\n✅ Successfully added close_reason column!")
            else:
                print(f"\n✅ Column already exists - no changes needed!")
            sys.exit(0)
        else:
            print(f"\n❌ Migration failed!")
            sys.exit(1)
    except Exception as e:
        print(f"\n❌ Migration failed: {e}")
        sys.exit(1)
