"""
Script to copy AppSettings (API keys, configuration) from source database to target database.

Usage:
    python copy_app_settings.py --target "C:\path\to\target\db.sqlite"
    
This will copy all AppSettings from the default dev database to the target database.
"""

import argparse
import os
import sys

# Parse command-line arguments FIRST before any imports
parser = argparse.ArgumentParser(description="Copy AppSettings from dev to production database")
parser.add_argument(
    "--target",
    type=str,
    required=True,
    help="Path to target database file (e.g., production db)"
)
args = parser.parse_args()

# Validate target database exists
if not os.path.exists(args.target):
    print(f"❌ Target database file not found: {args.target}")
    sys.exit(1)

print(f"Source database: Default dev database")
print(f"Target database: {args.target}")

# Import after arg parsing
from ba2_trade_platform.core.db import get_db
from ba2_trade_platform.logger import logger
from sqlalchemy import create_engine, text
from sqlmodel import Session

def copy_app_settings():
    """Copy all AppSettings from source to target database."""
    
    logger.info("="*80)
    logger.info("Starting AppSettings Copy")
    logger.info("="*80)
    
    try:
        # Connect to source (dev) database
        source_session = get_db()
        
        # Connect to target database
        target_engine = create_engine(
            f"sqlite:///{args.target}",
            connect_args={"check_same_thread": False, "timeout": 30.0}
        )
        target_session = Session(target_engine)
        
        try:
            # Get all AppSettings from source
            logger.info("Reading AppSettings from source database...")
            result = source_session.execute(text('SELECT key, value_str, value_json, value_float FROM appsetting'))
            source_settings = result.fetchall()
            
            logger.info(f"Found {len(source_settings)} settings in source database")
            
            if len(source_settings) == 0:
                logger.warning("No settings found in source database!")
                return 0, 0
            
            # Copy to target database
            copied_count = 0
            updated_count = 0
            error_count = 0
            
            for key, value_str, value_json, value_float in source_settings:
                try:
                    # Check if key exists in target
                    result = target_session.execute(
                        text('SELECT id FROM appsetting WHERE key = :key'),
                        {"key": key}
                    )
                    existing = result.fetchone()
                    
                    if existing:
                        # Update existing
                        target_session.execute(
                            text('''UPDATE appsetting 
                                    SET value_str = :value_str, 
                                        value_json = :value_json, 
                                        value_float = :value_float 
                                    WHERE key = :key'''),
                            {
                                "key": key,
                                "value_str": value_str,
                                "value_json": value_json,
                                "value_float": value_float
                            }
                        )
                        updated_count += 1
                        logger.info(f"  ✓ Updated: {key}")
                    else:
                        # Insert new
                        target_session.execute(
                            text('''INSERT INTO appsetting (key, value_str, value_json, value_float) 
                                    VALUES (:key, :value_str, :value_json, :value_float)'''),
                            {
                                "key": key,
                                "value_str": value_str,
                                "value_json": value_json,
                                "value_float": value_float
                            }
                        )
                        copied_count += 1
                        logger.info(f"  ✓ Copied: {key}")
                        
                except Exception as e:
                    error_count += 1
                    logger.error(f"  ✗ Error processing {key}: {e}", exc_info=True)
            
            # Commit all changes
            target_session.commit()
            
            logger.info("="*80)
            logger.info(f"Copy Complete!")
            logger.info(f"  ✅ New settings copied: {copied_count}")
            logger.info(f"  🔄 Settings updated: {updated_count}")
            logger.info(f"  ❌ Errors: {error_count}")
            logger.info("="*80)
            
            return copied_count, updated_count, error_count
            
        finally:
            target_session.close()
            source_session.close()
            
    except Exception as e:
        logger.error(f"Fatal error during copy: {e}", exc_info=True)
        raise


if __name__ == "__main__":
    try:
        copied, updated, errors = copy_app_settings()
        if errors > 0:
            print(f"\n⚠️  Copy completed with {errors} errors. Check logs for details.")
            sys.exit(1)
        else:
            print(f"\n✅ Successfully copied {copied} new settings and updated {updated} existing settings!")
            sys.exit(0)
    except Exception as e:
        print(f"\n❌ Copy failed: {e}")
        sys.exit(1)
