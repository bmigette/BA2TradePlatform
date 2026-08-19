"""The worker refuses work when it is full, instead of queueing it behind the pool.

remote150 answered /health with ``capacity: 12, busy: 61`` on 2026-08-19. _submit_job gated only
on the memory floor, so every extra dispatch was accepted and queued inside the pool executor.
Each queued trial was work the master had ALREADY abandoned (no-bars timeout, 420s) and
recomputed elsewhere -- so it burned CPU for nobody and kept all 12 pool children resident, which
is what actually starved the box.

A refusal here is not a failure: it rides the existing ``retryable`` contract, plus a
``backpressure`` flag so the master requeues without counting it against the worker.
"""
from __future__ import annotations

import sys
from concurrent.futures import Future
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import worker_server as ws  # noqa: E402


@pytest.fixture
def clean_registry(monkeypatch):
    monkeypatch.setattr(ws, "_JOBS", {}, raising=False)
    monkeypatch.setattr(ws, "_JOBS_SUBMITTED_AT", {}, raising=False)
    monkeypatch.setattr(ws, "_JOBS_LAST_POLL_AT", {}, raising=False)
    monkeypatch.setattr(ws, "_JOB_CTL", {}, raising=False)
    monkeypatch.setattr(ws, "_sweep_orphaned_jobs", lambda: None, raising=False)
    # Plenty of memory, so only the capacity gate can refuse.
    monkeypatch.setattr(ws, "_free_mem_pct", lambda: 80.0, raising=False)
    return ws


class _Pool:
    """Stand-in trial pool that records submissions and never completes them."""

    def __init__(self):
        self.submitted = []

    def submit(self, fn, *args):
        self.submitted.append(args)
        return Future()          # deliberately never resolved -> counts as busy


def _fill(mod, n):
    """Register *n* unresolved jobs, i.e. n busy slots."""
    for i in range(n):
        mod._JOBS[f"job{i}"] = Future()
        mod._JOBS_SUBMITTED_AT[f"job{i}"] = 0.0


def _result_of(mod, job_id):
    return mod._JOBS[job_id].result()


# ------------------------------------------------------------------ the gate

def test_a_full_worker_refuses_instead_of_queueing(clean_registry, monkeypatch):
    """THE REGRESSION: submissions past capacity used to land in the pool's queue."""
    mod = clean_registry
    pool = _Pool()
    monkeypatch.setattr(mod, "_POOL", pool, raising=False)
    monkeypatch.setattr(mod, "_CAPACITY", 4, raising=False)
    _fill(mod, 4)

    job_id = mod._submit_job(lambda: None)

    assert pool.submitted == [], "nothing may reach the pool while every slot is busy"
    out = _result_of(mod, job_id)
    assert out["ok"] is False and out["retryable"] is True
    assert out["backpressure"] is True
    assert out["fatal"] is False, "being busy is never fatal"


def test_a_worker_with_a_free_slot_still_accepts(clean_registry, monkeypatch):
    mod = clean_registry
    pool = _Pool()
    monkeypatch.setattr(mod, "_POOL", pool, raising=False)
    monkeypatch.setattr(mod, "_CAPACITY", 4, raising=False)
    _fill(mod, 3)

    mod._submit_job(lambda: None)
    assert len(pool.submitted) == 1


def test_an_idle_worker_accepts(clean_registry, monkeypatch):
    mod = clean_registry
    pool = _Pool()
    monkeypatch.setattr(mod, "_POOL", pool, raising=False)
    monkeypatch.setattr(mod, "_CAPACITY", 4, raising=False)
    mod._submit_job(lambda: None)
    assert len(pool.submitted) == 1


def test_completed_jobs_do_not_count_against_capacity(clean_registry, monkeypatch):
    """busy counts UNRESOLVED futures; a finished job still in the registry must free its slot."""
    mod = clean_registry
    pool = _Pool()
    monkeypatch.setattr(mod, "_POOL", pool, raising=False)
    monkeypatch.setattr(mod, "_CAPACITY", 2, raising=False)
    for i in range(5):
        f = Future()
        f.set_result({"ok": True})
        mod._JOBS[f"done{i}"] = f
        mod._JOBS_SUBMITTED_AT[f"done{i}"] = 0.0

    mod._submit_job(lambda: None)
    assert len(pool.submitted) == 1


def test_the_refusal_is_registered_so_the_master_can_poll_it(clean_registry, monkeypatch):
    """The master submits then polls; a refusal must be fetchable as a normal 'done' status
    rather than a submit-time error it has no path for."""
    mod = clean_registry
    monkeypatch.setattr(mod, "_POOL", _Pool(), raising=False)
    monkeypatch.setattr(mod, "_CAPACITY", 1, raising=False)
    _fill(mod, 1)

    job_id = mod._submit_job(lambda: None)
    assert job_id in mod._JOBS
    assert mod._JOBS[job_id].done()
    assert job_id in mod._JOBS_SUBMITTED_AT, "must be sweepable like any other job"


# ------------------------------------------------------------------ ordering vs the memory gate

def test_the_memory_floor_still_refuses_first_and_is_flagged_backpressure(clean_registry, monkeypatch):
    """A starved-but-idle worker must still refuse, and that refusal must ALSO be backpressure --
    three of them previously counted as failures and declared the box dead."""
    mod = clean_registry
    pool = _Pool()
    monkeypatch.setattr(mod, "_POOL", pool, raising=False)
    monkeypatch.setattr(mod, "_CAPACITY", 8, raising=False)
    monkeypatch.setattr(mod, "_free_mem_pct", lambda: 1.0, raising=False)

    job_id = mod._submit_job(lambda: None)
    assert pool.submitted == []
    out = _result_of(mod, job_id)
    assert out["backpressure"] is True
    assert "memory floor" in out["error"]
