"""
Clean up corrupted test data from database (invalid enum values).
"""

import sys
from pathlib import Path

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from ba2_trade_platform.core.db import get_db
from ba2_trade_platform.logger import logger
from sqlalchemy import text

def clean_corrupted_data():
    """Remove corrupted test data with invalid enum values."""
    logger.info("Cleaning corrupted test data from database...")
    
    session = get_db()
    
    try:
        # Delete EventAction entries with invalid type
        result = session.execute(
            text("DELETE FROM eventaction WHERE type = 'test_type'")
        )
        deleted_count = result.rowcount
        session.commit()
        
        logger.info(f"✅ Deleted {deleted_count} corrupted EventAction entries")
        
        # Also clean up any orphaned links
        result = session.execute(
            text("""DELETE FROM ruleseteventactionlink 
                    WHERE eventaction_id NOT IN (SELECT id FROM eventaction)""")
        )
        deleted_links = result.rowcount
        session.commit()
        
        logger.info(f"✅ Deleted {deleted_links} orphaned RulesetEventActionLink entries")
        
        logger.info("✅ Database cleanup complete!")
        
    except Exception as e:
        logger.error(f"Error cleaning database: {e}", exc_info=True)
        session.rollback()
    finally:
        session.close()

if __name__ == "__main__":
    clean_corrupted_data()
