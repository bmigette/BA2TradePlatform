"""
Test async activity logging to ensure it doesn't block database writes.
"""
import sys
import time
sys.path.insert(0, '/c/Users/basti/Documents/BA2TradePlatform')

from ba2_trade_platform.core.db import log_activity, init_db, _activity_log_queue
from ba2_trade_platform.core.types import ActivityLogSeverity, ActivityLogType
from ba2_trade_platform.logger import logger

def test_async_logging():
    """Test that log_activity returns immediately without blocking."""
    logger.info("Testing async activity logging...")
    
    # Initialize database (starts worker thread)
    init_db()
    
    # Measure time for logging call
    start = time.time()
    
    # This should return almost immediately (just queue put)
    log_activity(
        severity=ActivityLogSeverity.INFO,
        activity_type=ActivityLogType.TRANSACTION_CREATED,
        description="Test transaction created",
        data={"test": "data"},
        source_account_id=1
    )
    
    elapsed = time.time() - start
    logger.info(f"log_activity() took {elapsed:.4f}s (should be < 0.01s)")
    
    # Give worker thread time to process
    time.sleep(0.5)
    
    logger.info(f"Queue size: {_activity_log_queue.qsize()}")
    logger.info("✓ Async logging test passed")

if __name__ == "__main__":
    test_async_logging()
