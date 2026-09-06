"""ENTRY BEFORE MANAGE: live must run an expert's OPEN_POSITIONS pass AFTER its ENTER_MARKET
pass when both fire at the same minute -- the backtest's order.

WHY THIS EXISTS. daily_engine._run_expert_bar runs the entry pass and THEN
_manage_open_positions on a bar where both are due. Live had no order at all: the two
scheduled jobs for one expert fire at the same minute as independent scheduler jobs and land
in a parallel WorkerQueue, so whichever analysis finished first processed its orders first.
That is not cosmetic -- entry sizing reads available balance, so whether exits had already
released cash changed Monday's position sizes from week to week. Found 2026-09-06 while
deploying five instances whose entry and exit schedules both landed on Monday 09:30.

These tests drive the two queue methods directly. WorkerQueue() does not start its threads
(start() is separate), so the instance is inert.
"""
import pytest

from ba2_trade_platform.core.WorkerQueue import (
    WorkerQueue, InstrumentExpansionTask, AnalysisTask, WorkerTaskStatus,
)
from ba2_trade_platform.core.types import AnalysisUseCase


EXPERT = 42
BATCH = "42_0930_20260907"


def _queue() -> WorkerQueue:
    return WorkerQueue()          # inert: threads only start on .start()


def _expansion(expert_id, subtype, status, expansion_type="SCREENER"):
    return InstrumentExpansionTask(
        id=f"exp-{expert_id}-{subtype}-{status.value}",
        expert_instance_id=expert_id, expansion_type=expansion_type,
        subtype=subtype, status=status, batch_id=BATCH)


def _submissions(q, monkeypatch):
    """Record every expansion submission instead of queuing it; return the log."""
    log = []

    def fake_submit(expert_instance_id, expansion_type, **kw):
        log.append((expert_instance_id, expansion_type, kw.get("subtype"), kw.get("batch_id")))
        return f"task-{len(log)}"
    monkeypatch.setattr(q, "submit_instrument_expansion_task", fake_submit)
    return log


def test_nothing_in_flight_means_no_parking():
    """The common weekday case: exits fire alone and must run immediately."""
    q = _queue()
    assert q.defer_open_positions_if_entry_in_flight(EXPERT, BATCH) is False
    assert EXPERT not in q._deferred_open_positions


def test_a_pending_entry_EXPANSION_parks_the_exit_pass():
    """At the moment both jobs fire, the SCREENER expansion is all that exists for the entry
    side -- its per-symbol analysis fan-out has not been created yet. It must count as
    in-flight, or the exit pass would slip past the entry pass every single Monday."""
    q = _queue()
    task = _expansion(EXPERT, AnalysisUseCase.ENTER_MARKET, WorkerTaskStatus.PENDING)
    q._tasks[task.id] = task
    assert q.defer_open_positions_if_entry_in_flight(EXPERT, BATCH) is True
    assert q._deferred_open_positions[EXPERT]["batch_id"] == BATCH


def test_a_running_entry_ANALYSIS_task_parks_the_exit_pass():
    q = _queue()
    task = AnalysisTask(id="a-1", expert_instance_id=EXPERT, symbol="AAPL",
                        subtype=AnalysisUseCase.ENTER_MARKET, status=WorkerTaskStatus.RUNNING)
    q._tasks[task.id] = task
    assert q.defer_open_positions_if_entry_in_flight(EXPERT, BATCH) is True


def test_the_entry_processing_lock_alone_parks_the_exit_pass():
    """Analysis may be finished while order processing is still running under the entry lock;
    that window is exactly when the exit pass must still wait."""
    q = _queue()
    q._processing_experts.add(f"expert_{EXPERT}_{AnalysisUseCase.ENTER_MARKET.value}")
    assert q.defer_open_positions_if_entry_in_flight(EXPERT, BATCH) is True


def test_an_exit_pass_never_parks_behind_ITSELF_or_another_expert():
    """Only ENTER_MARKET work for the SAME expert counts. An in-flight OPEN_POSITIONS task
    must not park the pass (it would never be released), nor may another expert's entry."""
    q = _queue()
    own_exit = _expansion(EXPERT, AnalysisUseCase.OPEN_POSITIONS, WorkerTaskStatus.RUNNING,
                          expansion_type="OPEN_POSITIONS")
    other = _expansion(EXPERT + 1, AnalysisUseCase.ENTER_MARKET, WorkerTaskStatus.PENDING)
    q._tasks[own_exit.id] = own_exit
    q._tasks[other.id] = other
    assert q.defer_open_positions_if_entry_in_flight(EXPERT, BATCH) is False


def test_release_submits_exactly_once_with_the_original_batch_and_is_idempotent(monkeypatch):
    """The entry hook and the safety timer can BOTH call release; only the first may submit.
    A double submission would run the exit ruleset twice on the same bar."""
    q = _queue()
    log = _submissions(q, monkeypatch)
    q._tasks["e"] = _expansion(EXPERT, AnalysisUseCase.ENTER_MARKET, WorkerTaskStatus.PENDING)
    assert q.defer_open_positions_if_entry_in_flight(EXPERT, BATCH) is True

    first = q.release_deferred_open_positions(EXPERT, "entry pass processed")
    second = q.release_deferred_open_positions(EXPERT, "safety timer")

    assert first == "task-1"
    assert second is None, "second release must be a no-op"
    assert log == [(EXPERT, "OPEN_POSITIONS", AnalysisUseCase.OPEN_POSITIONS, BATCH)]
    assert EXPERT not in q._deferred_open_positions


def test_release_with_nothing_parked_is_a_silent_no_op(monkeypatch):
    q = _queue()
    log = _submissions(q, monkeypatch)
    assert q.release_deferred_open_positions(EXPERT, "safety timer") is None
    assert log == []


def test_an_already_queued_exit_pass_does_not_raise_on_release(monkeypatch):
    """submit_instrument_expansion_task raises ValueError when an identical task is already
    pending; release must absorb that rather than propagate into the entry hook."""
    q = _queue()
    q._deferred_open_positions[EXPERT] = {"batch_id": BATCH, "registered_at": 0.0}

    def already(*a, **k):
        raise ValueError("already queued")
    monkeypatch.setattr(q, "submit_instrument_expansion_task", already)
    assert q.release_deferred_open_positions(EXPERT, "entry pass processed") is None
    assert EXPERT not in q._deferred_open_positions
