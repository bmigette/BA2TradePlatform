"""Per-job remote pool sizing: the worker resizes its pool to the slots the job will actually use.

THE POINT. The daemon's pool children are spawned at daemon start (``--workers N``) and stay
resident whatever the master dispatches, so capping master-side dispatch never freed a byte --
remote150 held 12 children (~41 GB) to serve 6 engaged slots. Sizing the POOL per job is what
turns BA2_MAX_REMOTE_SLOTS into a real memory lever, and it does not need a daemon restart:
_CAPACITY is a module global that _make_pool reads at call time, and _rebuild_pool is what
returns a child's working set to the OS.

Resizing is only safe when nothing is in flight -- rebuilding under a live trial would discard
it -- so a busy worker refuses and keeps its current size.
"""
from __future__ import annotations

import sys
from concurrent.futures import Future
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import worker_server as ws  # noqa: E402


@pytest.fixture
def worker(monkeypatch):
    rebuilt = []
    monkeypatch.setattr(ws, "_JOBS", {}, raising=False)
    monkeypatch.setattr(ws, "_JOBS_SUBMITTED_AT", {}, raising=False)
    monkeypatch.setattr(ws, "_CAPACITY", 12, raising=False)
    # The daemon was started with --workers 12; _CAPACITY_MAX is that ceiling and does not
    # move with per-job resizing.
    monkeypatch.setattr(ws, "_CAPACITY_MAX", 12, raising=False)
    monkeypatch.setattr(ws, "_verify", lambda a: None, raising=False)
    monkeypatch.setattr(ws, "_rebuild_pool", lambda exc: rebuilt.append(exc), raising=False)
    ws._rebuilt = rebuilt
    return ws


def _busy(mod, n):
    for i in range(n):
        mod._JOBS[f"j{i}"] = Future()
        mod._JOBS_SUBMITTED_AT[f"j{i}"] = 0.0


# ------------------------------------------------------------------ resizing

def test_resizing_down_shrinks_the_pool_and_rebuilds_it(worker):
    """Rebuilding is the ONLY thing that hands an idle child's working set back to the OS."""
    out = worker.pool_resize({"workers": 6}, authorization="x")
    assert out["ok"] is True and out["changed"] is True
    assert out["capacity"] == 6
    assert worker._CAPACITY == 6
    assert len(worker._rebuilt) == 1, "the pool must actually be rebuilt, not just renumbered"


def test_resizing_up_is_allowed_too(worker):
    worker.pool_resize({"workers": 4}, authorization="x")
    out = worker.pool_resize({"workers": 10}, authorization="x")
    assert out["capacity"] == 10 and worker._CAPACITY == 10


def test_resizing_to_the_same_size_is_a_noop(worker):
    """A per-job resize fires on every job; re-spawning an identical pool would throw away a
    warm pool for nothing."""
    out = worker.pool_resize({"workers": 12}, authorization="x")
    assert out["ok"] is True and out["changed"] is False
    assert worker._rebuilt == []


def test_a_busy_worker_refuses_to_resize(worker):
    """Two optimizations can share a worker (the grid runs --parallel 2). Rebuilding under a live
    trial would discard it, so the running size wins and the caller is told why."""
    _busy(worker, 3)
    out = worker.pool_resize({"workers": 4}, authorization="x")
    assert out["ok"] is False and out["changed"] is False
    assert out["capacity"] == 12, "the live pool keeps its size"
    assert "in flight" in out["reason"]
    assert worker._rebuilt == []


def test_completed_jobs_do_not_block_a_resize(worker):
    for i in range(4):
        f = Future()
        f.set_result({"ok": True})
        worker._JOBS[f"done{i}"] = f
        worker._JOBS_SUBMITTED_AT[f"done{i}"] = 0.0
    out = worker.pool_resize({"workers": 5}, authorization="x")
    assert out["changed"] is True and worker._CAPACITY == 5


@pytest.mark.parametrize("bad", [0, -4, None, "six"])
def test_a_nonsense_size_is_rejected_without_touching_the_pool(worker, bad):
    with pytest.raises(Exception):
        worker.pool_resize({"workers": bad}, authorization="x")
    assert worker._CAPACITY == 12
    assert worker._rebuilt == []


def test_resize_updates_what_health_advertises(worker):
    """The master reads capacity from /health to size its dispatchers; a resize that didn't move
    it would leave the master dispatching against the OLD number."""
    worker.pool_resize({"workers": 3}, authorization="x")
    assert worker.health(authorization="x")["capacity"] == 3


def test_capacity_gate_follows_the_new_size(worker, monkeypatch):
    """After shrinking to 2, a third concurrent submit must be refused as backpressure."""
    monkeypatch.setattr(worker, "_free_mem_pct", lambda: 80.0, raising=False)
    monkeypatch.setattr(worker, "_sweep_orphaned_jobs", lambda: None, raising=False)

    class _Pool:
        def __init__(self):
            self.submitted = []

        def submit(self, fn, *args):
            self.submitted.append(args)
            return Future()

    pool = _Pool()
    monkeypatch.setattr(worker, "_POOL", pool, raising=False)
    worker.pool_resize({"workers": 2}, authorization="x")
    _busy(worker, 2)

    job_id = worker._submit_job(lambda: None)
    assert pool.submitted == []
    assert worker._JOBS[job_id].result()["backpressure"] is True


# ------------------------------------------------------------------ the ratchet (grow-back)

def test_resize_is_clamped_to_the_daemons_own_ceiling(worker):
    """--workers is a hard maximum: a job asking for more must not spawn children the operator
    never sized the box for."""
    out = worker.pool_resize({"workers": 40}, authorization="x")
    assert out["capacity"] == 12


def test_health_reports_the_ceiling_alongside_the_current_size(worker):
    """The master needs BOTH: capacity to know the pool's size now, capacity_max to know how far
    it may grow it back."""
    worker.pool_resize({"workers": 4}, authorization="x")
    h = worker.health(authorization="x")
    assert h["capacity"] == 4 and h["capacity_max"] == 12


def test_a_shrunk_pool_can_be_grown_back(worker):
    """THE RATCHET. The mid band shrinks the pool to 6; the large band must get 12 back. Sizing
    the next job off the CURRENT pool size instead of the ceiling pinned it at 6 permanently."""
    worker.pool_resize({"workers": 6}, authorization="x")
    assert worker._CAPACITY == 6
    out = worker.pool_resize({"workers": 12}, authorization="x")
    assert out["changed"] is True and worker._CAPACITY == 12
