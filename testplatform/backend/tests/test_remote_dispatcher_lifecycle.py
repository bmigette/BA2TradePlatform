"""Remote dispatcher lifecycle: one live set of dispatchers per worker, always.

THE 2026-08-19 INCIDENT. remote150 answered /health with ``capacity: 12, busy: 61`` -- 61
in-flight jobs against a 12-slot pool, while the master believed it was dispatching 6. The
master was the source: every re-admission spawned a FULL fresh set of dispatcher threads
without retiring the old set, and only the ONE thread that noticed the failure returned. Over
418 re-admissions that compounded into thousands of threads all claiming at once, which is what
actually drove the box to 1.3% free -- not per-trial footprint, and not the slot cap.

These tests pin the three invariants that make that impossible:
  * a worker has exactly ONE generation of dispatchers alive (epoch fencing),
  * marking it down retires ALL of them, not just the noticing thread,
  * backpressure (busy / low memory) is NOT a worker failure and must not kill the worker.
"""
from __future__ import annotations

import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.distributed_eval import DistributedEvaluator  # noqa: E402
from app.services.strategy_optimization_handler import MemoryGovernor  # noqa: E402


def _ev(cap=6):
    """A DistributedEvaluator with no network, no broker traffic, no threads started."""
    ev = DistributedEvaluator.__new__(DistributedEvaluator)
    ev.log = lambda *_a, **_k: None
    ev._stop = threading.Event()
    ev._threads = []
    ev._active_workers = []
    ev._down_workers = []
    ev._worker_lock = threading.Lock()
    ev._remote_govs = {}
    ev._remote_gov_lock = threading.Lock()
    ev._worker_epochs = {}
    ev.max_remote_slots_per_worker = cap
    ev.spawned = []
    ev._spawn = lambda target, name: ev.spawned.append(name)
    return ev


W = {"name": "remote150", "capacity": 12, "password": "x"}


# ------------------------------------------------------------------ epoch fencing / no duplicates

def test_readmission_does_not_stack_a_second_generation_of_dispatchers():
    """THE REGRESSION. Re-admitting a worker must REPLACE its dispatchers, never add to them."""
    ev = _ev()
    ev._spawn_remote_dispatchers(W)
    first = len(ev.spawned)
    ev._spawn_remote_dispatchers(W)          # re-admission
    ev._spawn_remote_dispatchers(W)          # and another
    assert first == 6
    # Each round spawns a fresh generation, but the PREVIOUS generation must be fenced off.
    assert ev._worker_epochs["remote150"] == 3


def test_each_readmission_bumps_the_epoch():
    ev = _ev()
    ev._spawn_remote_dispatchers(W)
    e1 = ev._worker_epochs["remote150"]
    ev._spawn_remote_dispatchers(W)
    assert ev._worker_epochs["remote150"] == e1 + 1


def test_a_stale_dispatcher_retires_itself_before_claiming():
    """A thread from generation N must exit the moment generation N+1 exists -- otherwise both
    generations claim concurrently, which is exactly how busy reached 61."""
    ev = _ev()
    ev._worker_epochs["remote150"] = 5
    claimed = []
    ev.broker = type("B", (), {"claim": lambda self, worker_id: claimed.append(1)})()
    ev._dispatch_remote(W, slot_idx=0, epoch=4)   # stale generation
    assert claimed == [], "a stale dispatcher must not claim any work"


def test_a_current_dispatcher_is_not_retired():
    ev = _ev()
    ev._worker_epochs["remote150"] = 4
    ev._stop.set()          # so the loop exits after its first guard check
    ev._dispatch_remote(W, slot_idx=0, epoch=4)   # must not raise; simply exits on _stop


# ------------------------------------------------------------------ marking a worker down

def test_marking_down_retires_every_dispatcher_not_just_the_noticing_one():
    ev = _ev()
    ev._spawn_remote_dispatchers(W)
    epoch_before = ev._worker_epochs["remote150"]
    ev._mark_worker_down(W, "dead")
    assert ev._worker_epochs["remote150"] != epoch_before, \
        "the epoch must move so the other 5 dispatchers retire themselves"


def test_a_worker_is_only_listed_down_once():
    """Several dispatchers give up at nearly the same instant. Appending per-thread put the same
    worker in _down_workers 6 times, and _recheck_down_workers then re-admitted it 6 times."""
    ev = _ev()
    ev._active_workers.append(W)
    for _ in range(6):
        ev._mark_worker_down(W, "dead")
    assert ev._down_workers.count(W) == 1
    assert W not in ev._active_workers


def test_marking_down_is_idempotent_for_an_already_down_worker():
    ev = _ev()
    ev._mark_worker_down(W, "dead")
    ev._mark_worker_down(W, "dead")
    assert len(ev._down_workers) == 1


# ------------------------------------------------------------------ governor survives re-admission

def test_shed_level_survives_a_readmission():
    """The governor walked 6 -> 4 because the box was starving. Re-admission rebuilt it at 6 and
    the ratchet reset -- 418 times in one run, which is why the log shows '6 -> 5' forever and
    never converges."""
    ev = _ev()
    ev._spawn_remote_dispatchers(W)
    gov = ev._remote_govs["remote150"]
    gov.current = 4
    ev._spawn_remote_dispatchers(W)
    assert ev._remote_govs["remote150"].current == 4, "a re-admission must not undo shedding"


def test_shed_level_is_clamped_to_a_smaller_new_cap():
    ev = _ev()
    ev._spawn_remote_dispatchers(W)
    ev._remote_govs["remote150"].current = 6
    ev.max_remote_slots_per_worker = 3
    ev._spawn_remote_dispatchers(W)
    assert ev._remote_govs["remote150"].current <= 3


def test_a_job_boundary_still_restores_full_concurrency():
    """Preserving the shed level across re-admission must NOT make it permanent -- restore() at
    the next job is what keeps one bad job from shrinking the fleet forever."""
    ev = _ev()
    ev._spawn_remote_dispatchers(W)
    gov = ev._remote_govs["remote150"]
    gov.current = 2
    gov.restore("new job")
    assert gov.current == gov.full


# ------------------------------------------------------------------ backpressure is not a failure

def test_backpressure_result_is_recognised_as_such():
    """A worker refusing work because it is full or low on memory is behaving CORRECTLY. Counting
    that as a failure is what marked a healthy-but-busy box dead after 3 refusals and started the
    re-admission storm."""
    from app.services.distributed_eval import _is_backpressure
    assert _is_backpressure({"retryable": True, "backpressure": True})
    assert not _is_backpressure({"retryable": True})
    assert not _is_backpressure({"ok": True})
    assert not _is_backpressure(None)
