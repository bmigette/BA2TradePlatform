"""Tests for SmartPriorityQueue round-robin and priority logic."""
import pytest
from ba2_trade_platform.core.SmartPriorityQueue import SmartPriorityQueue
from ba2_trade_platform.core.WorkerQueue import AnalysisTask


class MockWorker:
    def __init__(self, thread_id):
        self.thread_id = thread_id
        self.current_task = None


class TestBasicQueueOperations:
    def test_put_and_get(self):
        queue = SmartPriorityQueue()
        task = AnalysisTask(id="t1", expert_instance_id=1, symbol="AAPL", priority=10)
        queue.put((10, 0, task))
        assert not queue.empty()
        _, _, retrieved = queue.get()
        assert retrieved.id == "t1"

    def test_empty_queue(self):
        queue = SmartPriorityQueue()
        assert queue.empty()

    def test_qsize(self):
        queue = SmartPriorityQueue()
        for i in range(5):
            task = AnalysisTask(id=f"t{i}", expert_instance_id=1, symbol="AAPL", priority=10)
            queue.put((10, i, task))
        assert queue.qsize() == 5


class TestPriorityWithinExpert:
    def test_higher_priority_dequeued_first(self):
        queue = SmartPriorityQueue()
        tasks = [
            (50, 0, AnalysisTask(id="low", expert_instance_id=1, symbol="A", priority=50)),
            (10, 1, AnalysisTask(id="high", expert_instance_id=1, symbol="B", priority=10)),
            (30, 2, AnalysisTask(id="med", expert_instance_id=1, symbol="C", priority=30)),
        ]
        for t in tasks:
            queue.put(t)

        priorities = []
        while not queue.empty():
            p, _, _ = queue.get()
            priorities.append(p)

        assert priorities == [10, 30, 50]


class TestRoundRobinFairness:
    def test_multiple_experts_get_turns(self):
        queue = SmartPriorityQueue()
        queue.threads = {i: MockWorker(i) for i in range(5)}

        counter = 0
        for expert_id, count in [(1, 5), (2, 5)]:
            for i in range(count):
                task = AnalysisTask(
                    id=f"e{expert_id}_t{i}", expert_instance_id=expert_id,
                    symbol=f"SYM{i}", priority=10,
                )
                queue.put((10, counter, task))
                counter += 1

        # Dequeue first 4 and track expert distribution
        expert_counts = {1: 0, 2: 0}
        for i in range(4):
            _, _, task = queue.get()
            expert_counts[task.expert_instance_id] += 1
            queue.threads[i].current_task = task

        # Both experts should have been represented
        assert expert_counts[1] > 0
        assert expert_counts[2] > 0
