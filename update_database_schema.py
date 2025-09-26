#!/usr/bin/env python3
"""
Database schema update script for BA2 Trade Platform
Adds order_recommendation_id and open_type fields to TradingOrder table
"""

import os
import sys
from pathlib import Path

# Add the project root to Python path
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))

from ba2_trade_platform.core.db import get_db, engine
from ba2_trade_platform.core.models import TradingOrder
from ba2_trade_platform.core.types import OrderOpenType
from ba2_trade_platform.logger import logger
from sqlalchemy import text


def update_database_schema():
    """Update the database schema by adding new columns to TradingOrder table."""
    
    logger.info("Starting database schema update...")
    
    try:
        with engine.connect() as connection:
            # Check if the columns already exist
            logger.info("Checking if new columns exist...")
            
            # Check for order_recommendation_id column
            result = connection.execute(text("""
                SELECT COUNT(*) as count 
                FROM pragma_table_info('tradingorder') 
                WHERE name = 'order_recommendation_id'
            """))
            has_order_recommendation_id = result.fetchone()[0] > 0
            
            # Check for open_type column
            result = connection.execute(text("""
                SELECT COUNT(*) as count 
                FROM pragma_table_info('tradingorder') 
                WHERE name = 'open_type'
            """))
            has_open_type = result.fetchone()[0] > 0
            
            # Add order_recommendation_id column if it doesn't exist
            if not has_order_recommendation_id:
                logger.info("Adding order_recommendation_id column...")
                connection.execute(text("""
                    ALTER TABLE tradingorder 
                    ADD COLUMN order_recommendation_id INTEGER 
                    REFERENCES expertrecommendation(id) ON DELETE SET NULL
                """))
                logger.info("✓ Added order_recommendation_id column")
            else:
                logger.info("✓ order_recommendation_id column already exists")
            
            # Add open_type column if it doesn't exist
            if not has_open_type:
                logger.info("Adding open_type column...")
                connection.execute(text(f"""
                    ALTER TABLE tradingorder 
                    ADD COLUMN open_type TEXT DEFAULT '{OrderOpenType.MANUAL.value}'
                """))
                logger.info("✓ Added open_type column")
            else:
                logger.info("✓ open_type column already exists")
            
            # Commit the changes
            connection.commit()
            
            logger.info("Database schema update completed successfully!")
            
    except Exception as e:
        logger.error(f"Error updating database schema: {e}", exc_info=True)
        raise


def verify_schema_update():
    """Verify that the schema update was successful."""
    
    logger.info("Verifying schema update...")
    
    try:
        with engine.connect() as connection:
            # Check the updated table structure
            result = connection.execute(text("""
                SELECT name, type, [notnull], dflt_value
                FROM pragma_table_info('tradingorder')
                WHERE name IN ('order_recommendation_id', 'open_type')
                ORDER BY name
            """))
            
            columns = result.fetchall()
            
            if len(columns) == 2:
                logger.info("✓ Schema verification successful!")
                for col in columns:
                    logger.info(f"  - Column: {col[0]}, Type: {col[1]}, Not Null: {col[2]}, Default: {col[3]}")
            else:
                logger.error(f"✗ Schema verification failed! Expected 2 columns, found {len(columns)}", exc_info=True)
                return False
                
        return True
        
    except Exception as e:
        logger.error(f"Error verifying schema: {e}", exc_info=True)
        return False


if __name__ == "__main__":
    try:
        # Update the schema
        update_database_schema()
        
        # Verify the update
        if verify_schema_update():
            logger.info("🎉 Database schema update completed successfully!")
            sys.exit(0)
        else:
            logger.error("❌ Database schema update verification failed!", exc_info=True)
            sys.exit(1)
            
    except Exception as e:
        logger.error(f"Fatal error during database update: {e}", exc_info=True)
        sys.exit(1)