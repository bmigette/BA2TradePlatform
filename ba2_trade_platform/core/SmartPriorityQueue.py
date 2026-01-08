"""
Smart Priority Queue for Expert-Fair Task Distribution

This module provides a custom queue implementation that ensures fair distribution
of tasks across multiple experts, preventing any single expert from monopolizing
all worker threads.

Based on the SmartPriorityQueue pattern from autoreco project.
"""

from queue import Queue
import time
from typing import Optional, Dict, Tuple, Any
from ..logger import logger


class SmartPriorityQueue(Queue):
    """
    Smart Priority Queue with expert-based round-robin fairness.
    
    This queue extends the standard Queue class to implement custom dequeue logic
    that ensures tasks are distributed fairly across experts. When multiple experts
    have pending tasks, the queue alternates between them to prevent starvation.
    
    Key Features:
    - Round-robin selection across experts
    - Priority ordering within each expert's tasks
    - Prevents single expert from monopolizing workers
    - Thread-safe operation
    """
    
    def __init__(self, maxsize=0):
        """Initialize the smart priority queue."""
        super().__init__(maxsize)
        self.threads = None  # Will be set by WorkerQueue to track active workers
        
    def _init(self, maxsize):
        """Initialize the underlying data structure (list of items)."""
        self.queue = []
        self._expert_last_picked: Dict[int, float] = {}  # expert_id -> last pick timestamp
        
    def _qsize(self):
        """Return the size of the queue."""
        return len(self.queue)
        
    def _put(self, item):
        """
        Add an item to the queue.
        
        Args:
            item: Tuple of (priority, counter, task) where task has expert_instance_id
        """
        self.queue.append(item)
        
    def _get_expert_id(self, task) -> Optional[int]:
        """
        Extract expert_instance_id from a task object.
        
        Args:
            task: Task object (AnalysisTask, SmartRiskManagerTask, or InstrumentExpansionTask)
            
        Returns:
            Expert instance ID or None if not available
        """
        if hasattr(task, 'expert_instance_id'):
            return task.expert_instance_id
        return None
        
    def _get_currently_running_experts(self) -> Dict[int, int]:
        """
        Get count of tasks currently running per expert.
        
        Returns:
            Dict mapping expert_id to count of running tasks
        """
        expert_counts = {}
        
        if not self.threads:
            return expert_counts
            
        for thread_id, worker_thread in self.threads.items():
            if not hasattr(worker_thread, 'current_task'):
                continue
                
            current_task = worker_thread.current_task
            if current_task is None:
                continue
                
            expert_id = self._get_expert_id(current_task)
            if expert_id is not None:
                expert_counts[expert_id] = expert_counts.get(expert_id, 0) + 1
                
        return expert_counts
        
    def _get_best_item(self) -> Any:
        """
        Select the best item from the queue using expert-based round-robin.
        
        Algorithm:
        1. Group pending tasks by expert_id
        2. Count currently running tasks per expert
        3. Find expert with FEWEST running tasks (fair distribution)
        4. Use timestamp as tiebreaker if multiple experts have same running count
        5. From chosen expert, select highest priority task
        
        Returns:
            Best task tuple (priority, counter, task) or None if queue empty
        """
        if not self.queue:
            return None
            
        # Group tasks by expert
        expert_tasks: Dict[Optional[int], list] = {}
        for idx, item in enumerate(self.queue):
            priority, counter, task = item
            expert_id = self._get_expert_id(task)
            
            if expert_id not in expert_tasks:
                expert_tasks[expert_id] = []
            expert_tasks[expert_id].append((idx, priority, counter, task))
            
        # Get currently running expert counts
        running_experts = self._get_currently_running_experts()
        
        # Find expert with FEWEST running tasks (fair distribution priority)
        # Sort by: running_count (ascending), then timestamp (oldest first), then expert_id
        expert_scores = []
        for expert_id in expert_tasks.keys():
            running_count = running_experts.get(expert_id, 0)
            pick_time = self._expert_last_picked.get(expert_id, 0.0)
            expert_scores.append((running_count, pick_time, expert_id))
        
        # Sort by running count (fewest first), then timestamp (oldest), then ID
        expert_scores.sort(key=lambda x: (x[0], x[1], x[2]))
        chosen_running_count, chosen_pick_time, chosen_expert_id = expert_scores[0]
                
        # Select highest priority task from the chosen expert
        expert_task_list = expert_tasks[chosen_expert_id]
        # Sort by priority (lower is better), then counter for FIFO tiebreak
        expert_task_list.sort(key=lambda x: (x[1], x[2]))
        
        best_idx, best_priority, best_counter, best_task = expert_task_list[0]
        best_item = (best_priority, best_counter, best_task)
        
        # Update pick timestamp for tiebreaking
        self._expert_last_picked[chosen_expert_id] = time.time()
        
        # Logging for debugging
        pending_count = len(expert_task_list)
        time_since_pick = time.time() - chosen_pick_time if chosen_pick_time > 0 else float('inf')
        logger.debug(
            f"Round-robin selected expert {chosen_expert_id}: "
            f"{chosen_running_count} running, {pending_count} pending, "
            f"priority={best_priority}, last_picked={time_since_pick:.3f}s ago"
        )
            
        # Remove the selected item from queue
        self.queue.pop(best_idx)
        return best_item
        
    def _get(self):
        """
        Get the next item from the queue (called by Queue.get()).
        
        Returns:
            Next task tuple using round-robin selection
        """
        return self._get_best_item()
