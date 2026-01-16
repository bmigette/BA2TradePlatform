"""
Migration script to update Transaction model:
1. Add side column
2. Populate side based on quantity sign
3. Convert negative quantities to positive

Run this ONCE after updating code to the new Transaction model.
"""

import argparse
import os
import sys

# Parse command-line arguments FIRST before any imports
parser = argparse.ArgumentParser(description="Migrate Transaction model to use side column")
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
from ba2_trade_platform.core.types import OrderDirection
from ba2_trade_platform.logger import logger
from sqlalchemy import text


def migrate_transactions():
    """Migrate transactions from signed quantity to positive quantity + side field."""
    
    logger.info("="*80)
    logger.info("Starting Transaction Migration: Signed Quantity → Positive Quantity + Side")
    logger.info("="*80)
    
    try:
        with get_db() as session:
            # Step 1: Add side column if it doesn't exist
            logger.info("Step 1: Adding 'side' column to transaction table...")
            try:
                # Check if column exists
                result = session.execute(text('PRAGMA table_info("transaction")'))
                columns = [row[1] for row in result.fetchall()]
                
                if 'side' not in columns:
                    # Add the column with a temporary default value (we'll update it based on quantity)
                    session.execute(text('ALTER TABLE "transaction" ADD COLUMN side VARCHAR NOT NULL DEFAULT \'BUY\''))
                    session.commit()
                    logger.info("  ✓ Column 'side' added successfully")
                else:
                    logger.info("  ✓ Column 'side' already exists")
            except Exception as e:
                logger.error(f"  ✗ Failed to add column: {e}", exc_info=True)
                raise
            
            # Step 2: Update side based on quantity sign and convert negative to positive
            logger.info("Step 2: Updating side values and converting quantities...")
            
            # Get all transactions with raw SQL
            result = session.execute(text('SELECT id, symbol, quantity FROM "transaction"'))
            transactions = result.fetchall()
            
            logger.info(f"Found {len(transactions)} transactions to migrate")
            
            migrated_count = 0
            error_count = 0
            
            for txn_id, symbol, quantity in transactions:
                try:
                    # Determine side from quantity sign
                    if quantity >= 0:
                        side = "BUY"  # LONG position
                        new_quantity = quantity
                    else:
                        side = "SELL"  # SHORT position
                        new_quantity = abs(quantity)
                    
                    # Update transaction with new values
                    session.execute(
                        text('UPDATE "transaction" SET side = :side, quantity = :quantity WHERE id = :id'),
                        {"side": side, "quantity": new_quantity, "id": txn_id}
                    )
                    
                    migrated_count += 1
                    
                    logger.info(
                        f"Transaction {txn_id} ({symbol}): "
                        f"quantity {quantity:+.2f} → {new_quantity:.2f}, "
                        f"side={side}"
                    )
                    
                except Exception as e:
                    error_count += 1
                    logger.error(f"Error migrating transaction {txn_id}: {e}", exc_info=True)
            
            # Commit all changes
            session.commit()
            
            logger.info("="*80)
            logger.info(f"Migration Complete!")
            logger.info(f"  ✅ Migrated: {migrated_count}")
            logger.info(f"  ❌ Errors: {error_count}")
            logger.info("="*80)
            
            return migrated_count, error_count
            
    except Exception as e:
        logger.error(f"Fatal error during migration: {e}", exc_info=True)
        raise


if __name__ == "__main__":
    try:
        migrated, errors = migrate_transactions()
        if errors > 0:
            print(f"\n⚠️  Migration completed with {errors} errors. Check logs for details.")
            exit(1)
        else:
            print(f"\n✅ Successfully migrated {migrated} transactions!")
            exit(0)
    except Exception as e:
        print(f"\n❌ Migration failed: {e}")
        exit(1)
