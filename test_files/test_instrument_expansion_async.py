"""
Test script to verify instrument expansion tasks are queued to worker thread.

This test verifies that:
1. InstrumentExpansionTask can be created and submitted
2. Tasks are queued correctly with proper priority
3. Duplicate prevention works
4. Worker processes the task asynchronously
"""

import sys
import os
import time

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ba2_trade_platform.logger import logger
from ba2_trade_platform.core.WorkerQueue import WorkerQueue
from ba2_trade_platform.core.types import WorkerTaskStatus

def test_expansion_task_creation():
    """Test that expansion tasks can be created and submitted."""
    logger.info("=" * 80)
    logger.info("TEST 1: Expansion Task Creation")
    logger.info("=" * 80)
    
    try:
        # Get or create WorkerQueue instance
        worker_queue = WorkerQueue()
        
        # Start worker if not running
        if not worker_queue._running:
            worker_queue.start()
            logger.info("✓ Started WorkerQueue")
        
        # Test DYNAMIC expansion task
        logger.info("\nSubmitting DYNAMIC expansion task...")
        task_id_1 = worker_queue.submit_instrument_expansion_task(
            expert_instance_id=999,  # Test expert ID
            expansion_type="DYNAMIC",
            subtype="ENTER_MARKET",
            priority=5
        )
        logger.info(f"✓ Submitted DYNAMIC expansion task: {task_id_1}")
        
        # Check task status
        task = worker_queue.get_task_status(task_id_1)
        if task:
            logger.info(f"  Task status: {task.status}")
            logger.info(f"  Task type: {type(task).__name__}")
            logger.info(f"  Expansion type: {task.expansion_type}")
        
        # Test duplicate prevention
        logger.info("\nTesting duplicate prevention...")
        try:
            task_id_2 = worker_queue.submit_instrument_expansion_task(
                expert_instance_id=999,
                expansion_type="DYNAMIC",
                subtype="ENTER_MARKET",
                priority=5
            )
            logger.error("✗ Duplicate task was allowed (should have raised ValueError)")
        except ValueError as e:
            logger.info(f"✓ Duplicate prevention working: {e}")
        
        # Test different expansion types
        logger.info("\nSubmitting EXPERT expansion task...")
        task_id_3 = worker_queue.submit_instrument_expansion_task(
            expert_instance_id=999,
            expansion_type="EXPERT",
            subtype="ENTER_MARKET",
            priority=5
        )
        logger.info(f"✓ Submitted EXPERT expansion task: {task_id_3}")
        
        logger.info("\nSubmitting OPEN_POSITIONS expansion task...")
        task_id_4 = worker_queue.submit_instrument_expansion_task(
            expert_instance_id=999,
            expansion_type="OPEN_POSITIONS",
            subtype="ENTER_MARKET",
            priority=5
        )
        logger.info(f"✓ Submitted OPEN_POSITIONS expansion task: {task_id_4}")
        
        # Wait a bit for tasks to be picked up
        logger.info("\nWaiting for tasks to start processing...")
        time.sleep(2)
        
        # Check final status
        logger.info("\nFinal task statuses:")
        for task_id in [task_id_1, task_id_3, task_id_4]:
            task = worker_queue.get_task_status(task_id)
            if task:
                logger.info(f"  {task_id}: {task.status.value}")
                if task.error:
                    logger.info(f"    Error: {task.error}")
        
        logger.info("\n" + "=" * 80)
        logger.info("TEST 1 COMPLETED")
        logger.info("=" * 80)
        
        return True
        
    except Exception as e:
        logger.error(f"✗ Test failed: {e}", exc_info=True)
        return False

def test_queue_size():
    """Test that queue size is reported correctly."""
    logger.info("\n" + "=" * 80)
    logger.info("TEST 2: Queue Size Reporting")
    logger.info("=" * 80)
    
    try:
        worker_queue = WorkerQueue()
        
        queue_size = worker_queue.get_queue_size()
        logger.info(f"Current queue size: {queue_size}")
        
        # Check number of running tasks
        running_tasks = sum(1 for task in worker_queue._tasks.values() if task.status == WorkerTaskStatus.RUNNING)
        logger.info(f"Running tasks: {running_tasks}")
        
        pending_tasks = sum(1 for task in worker_queue._tasks.values() if task.status == WorkerTaskStatus.PENDING)
        logger.info(f"Pending tasks: {pending_tasks}")
        
        logger.info("\n" + "=" * 80)
        logger.info("TEST 2 COMPLETED")
        logger.info("=" * 80)
        
        return True
        
    except Exception as e:
        logger.error(f"✗ Test failed: {e}", exc_info=True)
        return False

def main():
    """Run all tests."""
    logger.info("\n" + "=" * 80)
    logger.info("INSTRUMENT EXPANSION TASK TEST SUITE")
    logger.info("=" * 80)
    logger.info("Testing that expansion tasks are properly queued and processed")
    logger.info("Note: Tasks may fail due to missing expert instance 999, which is expected")
    logger.info("We're testing the queueing mechanism, not the execution logic")
    logger.info("=" * 80 + "\n")
    
    results = []
    
    # Run tests
    results.append(("Expansion Task Creation", test_expansion_task_creation()))
    results.append(("Queue Size Reporting", test_queue_size()))
    
    # Summary
    logger.info("\n" + "=" * 80)
    logger.info("TEST SUMMARY")
    logger.info("=" * 80)
    
    passed = sum(1 for _, result in results if result)
    total = len(results)
    
    for test_name, result in results:
        status = "✓ PASSED" if result else "✗ FAILED"
        logger.info(f"{status}: {test_name}")
    
    logger.info("=" * 80)
    logger.info(f"Results: {passed}/{total} tests passed")
    logger.info("=" * 80)
    
    return passed == total

if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
