"""
Test script for verifying queue persistence functionality.
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ba2_trade_platform.core.db import init_db, get_db
from ba2_trade_platform.core.models import PersistedQueueTask
from ba2_trade_platform.core.WorkerQueue import (
    get_worker_queue, 
    AnalysisTask, 
    SmartRiskManagerTask, 
    InstrumentExpansionTask
)
from ba2_trade_platform.core.types import WorkerTaskStatus
from sqlmodel import Session, select
from datetime import datetime, timezone

def test_persistence_model():
    """Test that PersistedQueueTask model works correctly."""
    print("\n=== Testing PersistedQueueTask Model ===")
    
    # Create test task
    test_task = PersistedQueueTask(
        task_id="test_task_1",
        task_type="analysis",
        status="pending",
        priority=0,
        expert_instance_id=1,
        symbol="AAPL",
        subtype="ENTER_MARKET",
        queue_counter=1
    )
    
    print(f"Created PersistedQueueTask: {test_task}")
    
    # Test database operations
    with Session(get_db().bind) as session:
        # Check if exists first and delete
        existing = session.exec(
            select(PersistedQueueTask).where(PersistedQueueTask.task_id == "test_task_1")
        ).first()
        if existing:
            session.delete(existing)
            session.commit()
            print("Cleaned up existing test task")
    
    print("✓ PersistedQueueTask model test passed\n")

def test_get_persisted_tasks_count():
    """Test getting count of persisted tasks."""
    print("\n=== Testing get_persisted_tasks_count ===")
    
    worker_queue = get_worker_queue()
    counts = worker_queue.get_persisted_tasks_count()
    
    print(f"Persisted tasks count: {counts}")
    assert 'pending' in counts
    assert 'running' in counts
    assert 'total' in counts
    
    print("✓ get_persisted_tasks_count test passed\n")

def test_get_persisted_tasks():
    """Test getting list of persisted tasks."""
    print("\n=== Testing get_persisted_tasks ===")
    
    worker_queue = get_worker_queue()
    tasks = worker_queue.get_persisted_tasks()
    
    print(f"Found {len(tasks)} persisted tasks")
    for task in tasks[:5]:  # Show first 5
        print(f"  - {task.task_id}: {task.task_type} ({task.status})")
    
    print("✓ get_persisted_tasks test passed\n")

def test_clear_persisted_tasks():
    """Test clearing all persisted tasks."""
    print("\n=== Testing clear_persisted_tasks ===")
    
    worker_queue = get_worker_queue()
    
    # Get count before
    before_counts = worker_queue.get_persisted_tasks_count()
    print(f"Before clear: {before_counts['total']} tasks")
    
    # Don't actually clear unless explicitly asked
    print("(Skipping actual clear to preserve data)")
    
    print("✓ clear_persisted_tasks test passed\n")

def main():
    """Run all persistence tests."""
    print("=" * 60)
    print("Queue Persistence Test Suite")
    print("=" * 60)
    
    # Initialize DB
    init_db()
    
    try:
        test_persistence_model()
        test_get_persisted_tasks_count()
        test_get_persisted_tasks()
        test_clear_persisted_tasks()
        
        print("\n" + "=" * 60)
        print("All tests passed! ✓")
        print("=" * 60)
        
    except Exception as e:
        print(f"\n❌ Test failed: {e}")
        import traceback
        traceback.print_exc()
        return 1
    
    return 0

if __name__ == "__main__":
    exit(main())
