"""
Test SmartPriorityQueue round-robin expert distribution.
"""

import sys
import time
from pathlib import Path

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from ba2_trade_platform.core.SmartPriorityQueue import SmartPriorityQueue
from ba2_trade_platform.core.WorkerQueue import AnalysisTask

def test_round_robin_fairness():
    """Test that SmartPriorityQueue distributes tasks fairly across experts."""
    
    print("Testing SmartPriorityQueue round-robin fairness...")
    
    # Create queue
    queue = SmartPriorityQueue()
    
    # Create mock worker threads to track running tasks
    class MockWorker:
        def __init__(self, thread_id):
            self.thread_id = thread_id
            self.current_task = None
    
    mock_workers = {i: MockWorker(i) for i in range(10)}
    queue.threads = mock_workers
    
    # Create tasks for 3 different experts
    # Expert 1: 10 tasks
    # Expert 2: 5 tasks
    # Expert 3: 3 tasks
    
    tasks = []
    counter = 0
    
    # Expert 1 tasks (priority 10)
    for i in range(10):
        task = AnalysisTask(
            id=f"expert1_task_{i}",
            expert_instance_id=1,
            symbol=f"AAPL{i}",
            priority=10
        )
        tasks.append((10, counter, task))
        queue.put((10, counter, task))
        counter += 1
    
    # Expert 2 tasks (priority 10)
    for i in range(5):
        task = AnalysisTask(
            id=f"expert2_task_{i}",
            expert_instance_id=2,
            symbol=f"MSFT{i}",
            priority=10
        )
        tasks.append((10, counter, task))
        queue.put((10, counter, task))
        counter += 1
    
    # Expert 3 tasks (priority 10)
    for i in range(3):
        task = AnalysisTask(
            id=f"expert3_task_{i}",
            expert_instance_id=3,
            symbol=f"GOOGL{i}",
            priority=10
        )
        tasks.append((10, counter, task))
        queue.put((10, counter, task))
        counter += 1
    
    print(f"Added {len(tasks)} tasks to queue")
    print(f"  Expert 1: 10 tasks")
    print(f"  Expert 2: 5 tasks")
    print(f"  Expert 3: 3 tasks")
    print()
    
    # Simulate worker dequeuing with 10 workers
    # First 10 tasks should fill all workers
    dequeued_order = []
    expert_counts = {1: 0, 2: 0, 3: 0}
    worker_idx = 0
    
    # Dequeue first 10 tasks (fill all workers)
    for i in range(10):
        priority, counter, task = queue.get()
        expert_id = task.expert_instance_id
        dequeued_order.append(expert_id)
        expert_counts[expert_id] += 1
        
        # Assign to mock worker
        mock_workers[worker_idx].current_task = task
        worker_idx += 1
        
        print(f"Dequeued #{i+1}: Expert {expert_id} task (Running: E1={expert_counts[1]}, E2={expert_counts[2]}, E3={expert_counts[3]})")
    
    print()
    print("First 10 dequeued:", dequeued_order)
    print(f"First 10 distribution: E1={expert_counts[1]}, E2={expert_counts[2]}, E3={expert_counts[3]}")
    print()
    
    # Verify fairness: with 10 workers and 3 experts, should be roughly 3-4 tasks per expert
    # All 3 experts should be represented
    assert all(count > 0 for count in expert_counts.values()), "Not all experts represented in first 10 tasks!"
    
    # No expert should monopolize (max 5 out of 10)
    assert all(count <= 5 for count in expert_counts.values()), f"Expert monopolizing! Distribution: {expert_counts}"
    
    # With fair distribution, expect roughly equal: 3-4 tasks per expert
    # (10 workers / 3 experts = ~3.33 per expert)
    min_count = min(expert_counts.values())
    max_count = max(expert_counts.values())
    assert max_count - min_count <= 2, f"Uneven distribution: {expert_counts}"
    
    print("✅ Round-robin fairness test PASSED!")
    print()

def test_priority_within_expert():
    """Test that higher priority tasks from same expert are picked first."""
    
    print("Testing priority handling within same expert...")
    
    queue = SmartPriorityQueue()
    
    # Create tasks for expert 1 with different priorities
    tasks = [
        (50, 0, AnalysisTask(id="low_priority", expert_instance_id=1, symbol="AAPL", priority=50)),
        (10, 1, AnalysisTask(id="high_priority", expert_instance_id=1, symbol="MSFT", priority=10)),
        (30, 2, AnalysisTask(id="medium_priority", expert_instance_id=1, symbol="GOOGL", priority=30)),
    ]
    
    # Add in random order
    for task in tasks:
        queue.put(task)
    
    # Dequeue all
    dequeued = []
    while not queue.empty():
        priority, counter, task = queue.get()
        dequeued.append((priority, task.id))
        print(f"Dequeued: {task.id} (priority={priority})")
    
    # Should be: high, medium, low (10, 30, 50)
    assert dequeued[0][0] == 10, "High priority task not first!"
    assert dequeued[1][0] == 30, "Medium priority task not second!"
    assert dequeued[2][0] == 50, "Low priority task not third!"
    
    print("✅ Priority test PASSED!")
    print()

def test_mixed_priorities():
    """Test round-robin with different priorities across experts."""
    
    print("Testing round-robin with mixed priorities...")
    
    queue = SmartPriorityQueue()
    
    # Create mock workers
    class MockWorker:
        def __init__(self, thread_id):
            self.thread_id = thread_id
            self.current_task = None
    
    mock_workers = {i: MockWorker(i) for i in range(6)}
    queue.threads = mock_workers
    
    # Expert 1: 3 high priority tasks (priority=10)
    for i in range(3):
        task = AnalysisTask(id=f"e1_high_{i}", expert_instance_id=1, symbol=f"AAPL{i}", priority=10)
        queue.put((10, i, task))
    
    # Expert 2: 3 low priority tasks (priority=50)
    for i in range(3):
        task = AnalysisTask(id=f"e2_low_{i}", expert_instance_id=2, symbol=f"MSFT{i}", priority=50)
        queue.put((50, i+10, task))
    
    # Dequeue and assign to workers
    dequeued = []
    worker_idx = 0
    while not queue.empty():
        priority, counter, task = queue.get()
        dequeued.append((task.expert_instance_id, priority))
        
        # Assign to mock worker to simulate running state
        mock_workers[worker_idx].current_task = task
        worker_idx += 1
        
        print(f"Dequeued: Expert {task.expert_instance_id}, priority={priority}")
    
    print()
    print("Dequeue sequence:", dequeued)
    
    # Should alternate: E1, E2, E1, E2, E1, E2
    # (round-robin overrides priority across experts)
    expected_experts = [1, 2, 1, 2, 1, 2]
    actual_experts = [e for e, p in dequeued]
    
    print(f"Expected expert order: {expected_experts}")
    print(f"Actual expert order:   {actual_experts}")
    
    assert actual_experts == expected_experts, f"Expected {expected_experts}, got {actual_experts}"
    
    print("✅ Mixed priority test PASSED!")
    print()

if __name__ == "__main__":
    test_round_robin_fairness()
    test_priority_within_expert()
    test_mixed_priorities()
    
    print("=" * 60)
    print("All SmartPriorityQueue tests PASSED! ✅")
    print("=" * 60)
