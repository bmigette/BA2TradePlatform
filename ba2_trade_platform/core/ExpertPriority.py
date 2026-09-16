"""Non-blocking ordering of scheduled expert runs sharing an account and fire time.

Analysis tasks count as outstanding work, including expansion producers. Completed
analyses remain parked here (not on worker threads) until higher priorities finish
their order processing. Callbacks never execute while holding the state lock.
"""
from dataclasses import dataclass, field
from threading import RLock
from typing import Callable

from ..logger import logger


@dataclass
class ExpertRun:
    batch_id: str
    cohort: str
    expert_id: int
    account_id: int
    priority: int
    self_trading: bool
    submitting: bool = True
    tasks: set = field(default_factory=set)
    all_tasks: set = field(default_factory=set)
    ready: dict = field(default_factory=dict)
    held: dict = field(default_factory=dict)
    processing: bool = False
    finalizing: bool = False


class ExpertPriority:
    def __init__(self, on_complete: Callable, before_complete: Callable):
        self._lock = RLock()
        self._runs = {}
        self._draining = set()
        self._on_complete = on_complete
        self._before_complete = before_complete

    def register(self, runs):
        """Register the ENTIRE cohort before submitting its first task."""
        with self._lock:
            for run in runs:
                if run.batch_id in self._runs:
                    raise ValueError(f"Already registered expert batch {run.batch_id}")
            self._runs.update((run.batch_id, run) for run in runs)

    def contains(self, batch_id):
        with self._lock:
            return batch_id in self._runs

    def waiting_batches(self):
        with self._lock:
            return {run.batch_id for run in self._runs.values()
                    if (run.ready or run.held) and self._blocked(run)}

    def _blocked(self, run):
        return any(other.cohort == run.cohort and other.account_id == run.account_id
                   and other.priority > run.priority for other in self._runs.values())

    def submit(self, batch_id, task, enqueue, *, wait_for_turn=False):
        with self._lock:
            if task.completed_at is not None:
                return  # cancelled between task creation and queue registration
            run = self._runs.get(batch_id)
            if run is not None:
                task.expert_priority = run.priority
                task.priority_group = (run.cohort, run.account_id)
                run.tasks.add(task.id)
                run.all_tasks.add(task.id)
                if (run.self_trading or wait_for_turn) and self._blocked(run):
                    run.held[task.id] = enqueue
                    return
        enqueue()

    def seal(self, batch_ids):
        with self._lock:
            for batch_id in batch_ids:
                self._runs[batch_id].submitting = False
        self._drain()

    def ready(self, batch_id, use_case, callback):
        with self._lock:
            run = self._runs[batch_id]
            run.ready[use_case] = callback
            if self._blocked(run):
                logger.info("Expert %s batch %s: analysis ready; waiting for higher-priority "
                            "experts on account %s", run.expert_id, batch_id, run.account_id)
        self._drain()

    def finished(self, batch_id, task_id):
        with self._lock:
            run = self._runs.get(batch_id)
            if run is not None:
                run.tasks.discard(task_id)
                run.held.pop(task_id, None)
        self._drain()

    def _next_action(self, group):
        runs = (run for run in self._runs.values() if (run.cohort, run.account_id) == group)
        for run in sorted(runs, key=lambda r: -r.priority):
            if run.submitting or run.processing or self._blocked(run):
                continue
            if run.held:
                _, callback = run.held.popitem()
                return callback
            if run.tasks:
                continue
            if run.ready or not run.finalizing:
                # Entry-before-manage, matching the existing live/backtest ordering.
                if run.ready:
                    use_case = min(run.ready, key=lambda case: str(case))
                    callback = run.ready.pop(use_case)
                else:
                    run.finalizing = True
                    callback = lambda run=run: self._before_complete(run)
                run.processing = True

                def process(run=run, callback=callback):
                    try:
                        callback()
                    finally:
                        with self._lock:
                            run.processing = False
                return process
            del self._runs[run.batch_id]
            return lambda run=run: self._on_complete(run)
        return None

    def _drain(self):
        with self._lock:
            groups = list(dict.fromkeys((run.cohort, run.account_id) for run in self._runs.values()))
        for group in groups:
            with self._lock:
                if group in self._draining:
                    continue
                self._draining.add(group)
            while True:
                with self._lock:
                    action = self._next_action(group)
                    if action is None:
                        self._draining.remove(group)
                        break
                try:
                    action()
                except Exception:
                    logger.exception("Scheduled expert priority callback failed")


def validate_expert_priority(value):
    """Used at UI/import boundaries; SQLModel table assignment is not validated."""
    if isinstance(value, bool):
        raise ValueError("Expert priority must be a positive integer")
    number = int(value)
    if number < 1 or float(value) != number:
        raise ValueError("Expert priority must be a positive integer")
    return number


def priority_from_settings(settings, *, current=1):
    """Legacy exports omit priority (or carry null); preserve an existing value."""
    if "priority" not in settings or settings["priority"] is None:
        return current
    return validate_expert_priority(settings["priority"])
