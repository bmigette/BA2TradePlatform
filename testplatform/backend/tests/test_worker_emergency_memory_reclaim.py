"""Emergency memory reclaim: the worker's OWN last resort when free memory is critically low
AND trials are running, so the ordinary "wait for drain" watchdog path is losing the race.

WHY THIS EXISTS. _memory_watchdog's existing busy branch only ever waits and refuses new
submits -- by design, because rebuilding mid-trial discards live work. That is correct when a
trial's own memory footprint is roughly stable, but FMPEarningsDrift-S3's individual trials grow
their OWN RSS over their runtime (some ran 1900+s with a still-rising bar/memo count), so
waiting can lose to trials that keep growing faster than any one of them finishes -- observed
live 2026-08-31/09-01: remote227 went from 68GB free to under 1GB free in minutes while every
trial was still "busy", and the box stayed there until a human SSH'd in and SIGKILL'd the top
RSS consumers by hand. This automates exactly that manual recovery.

Any single killed child already breaks the WHOLE ProcessPoolExecutor (Python fails every
pending future on the pool, not just the dead child's own -- see test_worker_pool_resize.py's
docstring on why _rebuild_pool exists at all), so there is no partial-kill path that preserves
unaffected trials: a full pool rebuild is the correct outcome either way, and the master's
existing BrokenProcessPool-is-retryable handling requeues every lost trial cleanly (proven live
by the manual SSH recovery this automates).
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import psutil
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import worker_server as ws  # noqa: E402


def _kid(pid, rss_mb):
    k = MagicMock()
    k.pid = pid
    k.memory_info.return_value = MagicMock(rss=rss_mb * 1048576)
    return k


@pytest.fixture
def worker(monkeypatch):
    rebuilt = []
    monkeypatch.setattr(ws, "_CAPACITY", 12, raising=False)
    monkeypatch.setattr(ws, "_CAPACITY_MAX", 12, raising=False)
    monkeypatch.setattr(ws, "_rebuild_pool", lambda exc: rebuilt.append(exc), raising=False)
    ws._rebuilt = rebuilt
    return ws


def _with_children(monkeypatch, kids, server_rss_mb=500):
    """psutil.Process(os.getpid()).children(recursive=True) -> kids; total system memory fixed
    at 256 GB (matches remote227) so the peak-RSS budget math is easy to reason about by hand."""
    proc = MagicMock()
    proc.children.return_value = kids
    proc.memory_info.return_value = MagicMock(rss=server_rss_mb * 1048576)
    monkeypatch.setattr(psutil, "Process", lambda pid=None: proc)
    vm = MagicMock(total=256 * 1024 * 1048576)  # 256 GB in bytes
    monkeypatch.setattr(psutil, "virtual_memory", lambda: vm)
    return proc


# --------------------------------------------------------------------------- #
# Case A: more children are actually running than the current target -- a prior
# graceful resize trimmed _CAPACITY but never the live process count (the
# cancel_futures limitation _rebuild_pool's own docstring calls out).
# --------------------------------------------------------------------------- #
def test_excess_above_target_kills_down_to_the_current_capacity(worker, monkeypatch):
    kids = [_kid(100 + i, 12000 - i * 100) for i in range(15)]  # 15 running, target is 12
    _with_children(monkeypatch, kids)

    ws._emergency_reclaim(3.0)

    assert len(worker._rebuilt) == 1, "must always end in one pool rebuild"
    killed_pids = {k.pid for k in kids if k.kill.called}
    assert len(killed_pids) == 3, "trims exactly the 3 processes above the 12-slot target"
    # the 3 LARGEST are the ones that go, not an arbitrary 3
    assert killed_pids == {100, 101, 102}
    assert worker._CAPACITY == 12, "the target itself was fine -- only the excess is trimmed"


def test_excess_above_target_kills_the_biggest_first_not_pid_order(worker, monkeypatch):
    kids = [_kid(200, 3000), _kid(201, 9000), _kid(202, 1000),
            _kid(203, 7000), _kid(204, 2000)]  # 5 running, target 12 -- wait, need > target
    monkeypatch.setattr(ws, "_CAPACITY", 3, raising=False)
    _with_children(monkeypatch, kids)

    ws._emergency_reclaim(2.0)

    killed_pids = {k.pid for k in kids if k.kill.called}
    assert killed_pids == {201, 203}, "the two biggest (9000, 7000 MB), regardless of PID order"


# --------------------------------------------------------------------------- #
# Case B: actual running children <= the current target -- nothing "extra" to trim,
# so the target itself is too big for this box right now. Lower it (same peak-RSS
# budget formula the master's own governor uses) and kill down to the new number.
# --------------------------------------------------------------------------- #
def test_no_excess_lowers_the_target_and_kills_down_to_it(worker, monkeypatch):
    # 10 children at peak 12000 MB each. Budget = 256*1024*0.85 - 500 (server) ~= 222390 MB.
    # target = 222390 // 12000 = 18 ... that's bigger than current capacity (12), so with
    # peak this high relative to a 12-slot target, nothing should shrink. Use a much smaller
    # box budget by forcing a high peak instead: peak 30000 MB -> target = 222390//30000 = 7.
    kids = [_kid(300 + i, 30000 - i * 50) for i in range(10)]
    monkeypatch.setattr(ws, "_CAPACITY", 10, raising=False)
    _with_children(monkeypatch, kids)

    ws._emergency_reclaim(2.5)

    assert len(worker._rebuilt) == 1
    assert worker._CAPACITY == 7, "lowered via the same peak-RSS budget formula the master uses"
    killed_pids = {k.pid for k in kids if k.kill.called}
    assert killed_pids == {300, 301, 302}, "the 3 BIGGEST go, down to the new 7-slot target"
    survivors = {k.pid for k in kids if not k.kill.called}
    assert survivors == {303, 304, 305, 306, 307, 308, 309}


def test_master_sees_the_lowered_capacity_on_its_next_health_poll(worker, monkeypatch):
    """No push needed: /health already reads the module-global _CAPACITY, so a lowered
    target here is exactly what the master reads on its ordinary periodic poll."""
    kids = [_kid(400 + i, 30000) for i in range(10)]
    monkeypatch.setattr(ws, "_CAPACITY", 10, raising=False)
    monkeypatch.setattr(ws, "_verify", lambda a, r: None, raising=False)
    monkeypatch.setattr(ws, "_JOBS", {}, raising=False)
    _with_children(monkeypatch, kids)

    ws._emergency_reclaim(2.5)

    assert ws.health(None, authorization="x")["capacity"] == worker._CAPACITY


def test_no_excess_and_no_peak_data_shrinks_by_one_as_a_safe_default(worker, monkeypatch):
    """An empty/unreadable children list still must not raise or grow the target -- shrink by
    one and let the next poll refine it once real data exists."""
    _with_children(monkeypatch, [])

    ws._emergency_reclaim(1.0)

    assert worker._CAPACITY == 11
    assert len(worker._rebuilt) == 1


# --------------------------------------------------------------------------- #
# Wiring into the existing watchdog loop's busy branch.
# --------------------------------------------------------------------------- #
def test_watchdog_calls_emergency_reclaim_only_below_the_emergency_floor_while_busy(
        worker, monkeypatch):
    calls = []
    monkeypatch.setattr(ws, "_emergency_reclaim", lambda pct: calls.append(pct), raising=False)
    monkeypatch.setattr(ws, "_busy_job_count", lambda: 3, raising=False)
    monkeypatch.setattr(ws, "_free_mem_pct", lambda: 4.0, raising=False)  # < emergency (5)

    ws._maybe_reclaim_under_pressure(4.0)

    assert calls == [4.0]


def test_watchdog_does_not_escalate_between_the_floor_and_the_emergency_line(worker, monkeypatch):
    """Busy AND under the 10% floor but still above the 5% emergency line: the existing
    wait-and-refuse behaviour stays exactly as it was -- no killing."""
    calls = []
    monkeypatch.setattr(ws, "_emergency_reclaim", lambda pct: calls.append(pct), raising=False)
    monkeypatch.setattr(ws, "_busy_job_count", lambda: 3, raising=False)

    ws._maybe_reclaim_under_pressure(7.0)

    assert calls == []
    assert worker._rebuilt == []


def test_a_busy_tick_below_the_emergency_line_escalates(worker, monkeypatch):
    calls = []
    monkeypatch.setattr(ws, "_emergency_reclaim", lambda pct: calls.append(pct), raising=False)
    monkeypatch.setattr(ws, "_free_mem_pct", lambda: 3.0, raising=False)
    monkeypatch.setattr(ws, "_busy_job_count", lambda: 2, raising=False)

    ws._memory_watchdog_tick()

    assert calls == [3.0]
    assert worker._rebuilt == [], "escalation goes through _emergency_reclaim, not a bare rebuild"


def test_an_idle_tick_below_the_emergency_line_recycles_normally_and_never_escalates(
        worker, monkeypatch):
    """Idle already has its own full-recycle path (a plain _rebuild_pool call); the emergency
    kill path exists only for the busy case that path cannot reach."""
    calls = []
    monkeypatch.setattr(ws, "_emergency_reclaim", lambda pct: calls.append(pct), raising=False)
    monkeypatch.setattr(ws, "_free_mem_pct", lambda: 3.0, raising=False)
    monkeypatch.setattr(ws, "_busy_job_count", lambda: 0, raising=False)

    ws._memory_watchdog_tick()

    assert calls == []
    assert len(worker._rebuilt) == 1


def test_a_tick_at_or_above_the_floor_does_nothing_at_all(worker, monkeypatch):
    calls = []
    monkeypatch.setattr(ws, "_emergency_reclaim", lambda pct: calls.append(pct), raising=False)
    monkeypatch.setattr(ws, "_free_mem_pct", lambda: ws._MEM_FLOOR_PCT, raising=False)
    monkeypatch.setattr(ws, "_busy_job_count", lambda: 5, raising=False)

    ws._memory_watchdog_tick()

    assert calls == []
    assert worker._rebuilt == []
