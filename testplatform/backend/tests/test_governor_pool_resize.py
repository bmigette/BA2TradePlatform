"""Under memory pressure the governor must shrink the POOL, not only stop feeding it.

THE 2026-08-20 SMALL-BAND FINDING. On the small cap band (1,323 screened symbols, 5.7 GB of bars
per trial) remote150 ran ~9 GB per pool child. The governor shed dispatch 6 -> 5 -> 4 -> 3 -> 2
exactly as designed and the box stayed at 5-6% free the whole way, because ProcessPoolExecutor
hands work to whichever child is free: with SIX children in the pool, all six eventually run a
trial and all six hold a working set, no matter how few run at once. Concurrency was never the
binding constraint on resident memory.

So the governor's only remote actuator was the wrong one. Shedding must also drive the pool size
down, which is the one operation that actually returns children to the OS.

A pool rebuild cancels in-flight trials. That is acceptable HERE and only here: the master
already requeues them (they say nothing about the genome), and the alternative measured above is
an hour of thrashing followed by a single-slot worker.
"""
from __future__ import annotations

import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services import distributed_eval as de  # noqa: E402
from app.services.strategy_optimization_handler import MemoryGovernor  # noqa: E402


W = {"name": "remote150", "capacity": 6, "capacity_max": 12, "password": "x"}


def _ev(cap=6):
    ev = de.DistributedEvaluator.__new__(de.DistributedEvaluator)
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
    ev._spawn = lambda target, name: None
    return ev


@pytest.fixture
def calls(monkeypatch):
    got = []
    monkeypatch.setattr(de.worker_client, "resize_pool",
                        lambda worker, n, **kw: got.append((n, kw)) or
                        {"ok": True, "capacity": n, "changed": True})
    return got


# --------------------------------------------------------------- shedding drives the pool down

def test_shedding_also_shrinks_the_pool(calls):
    """THE FIX. Six children at ~9 GB is 54 GB whatever the dispatch cap says."""
    ev = _ev()
    gov = MemoryGovernor(6, log=lambda *_: None)
    ev._remote_govs["remote150"] = gov
    w = dict(W)

    ev._shed_remote_slot("remote150", gov, "5.5% free < 10% floor", worker=w)

    assert gov.current == 5
    assert calls and calls[0][0] == 5, "the pool must be resized to match the new ceiling"


def test_pool_resize_is_forced_because_a_busy_worker_would_refuse(calls):
    """busy counts in-flight SUBMISSIONS, and under load there is essentially always one, so a
    polite resize would be refused forever and the box would never recover."""
    ev = _ev()
    gov = MemoryGovernor(6, log=lambda *_: None)
    ev._remote_govs["remote150"] = gov
    ev._shed_remote_slot("remote150", gov, "why", worker=dict(W))
    assert calls[0][1].get("force") is True


def test_the_masters_view_of_capacity_follows_the_resize(calls):
    ev = _ev()
    gov = MemoryGovernor(6, log=lambda *_: None)
    ev._remote_govs["remote150"] = gov
    w = dict(W)
    ev._shed_remote_slot("remote150", gov, "why", worker=w)
    assert w["capacity"] == 5


def test_repeated_pressure_walks_the_pool_down_too(calls):
    ev = _ev()
    gov = MemoryGovernor(6, log=lambda *_: None)
    ev._remote_govs["remote150"] = gov
    w = dict(W)
    for _ in range(3):
        ev._shed_remote_slot("remote150", gov, "why", worker=w)
    assert [c[0] for c in calls] == [5, 4, 3]
    assert gov.current == 3


def test_the_last_slot_is_never_resized_away(calls):
    """A worker throttled out of existence stalls the queue; slot 0 always survives."""
    ev = _ev()
    gov = MemoryGovernor(1, log=lambda *_: None)
    ev._remote_govs["remote150"] = gov
    for _ in range(4):
        ev._shed_remote_slot("remote150", gov, "why", worker=dict(W))
    assert gov.current == 1
    assert calls == [], "nothing to shed means nothing to resize"


# --------------------------------------------------------------- it must never break the run

def test_a_failed_resize_still_leaves_the_dispatch_shed_in_place(monkeypatch):
    """The concurrency cut is the part that always works; losing the resize must not lose that."""
    ev = _ev()
    monkeypatch.setattr(de.worker_client, "resize_pool",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("404")))
    gov = MemoryGovernor(6, log=lambda *_: None)
    ev._remote_govs["remote150"] = gov
    ev._shed_remote_slot("remote150", gov, "why", worker=dict(W))
    assert gov.current == 5, "an older worker without /pool/resize must still be throttled"


def test_shedding_without_a_worker_handle_still_works(calls):
    """assess_remote's trial-snapshot path has the name but not the worker dict; it must not
    crash, it just cannot resize."""
    ev = _ev()
    gov = MemoryGovernor(6, log=lambda *_: None)
    ev._remote_govs["remote150"] = gov
    ev._shed_remote_slot("remote150", gov, "why")
    assert gov.current == 5
    assert calls == []


def test_no_resize_when_nothing_was_shed(calls):
    ev = _ev()
    gov = MemoryGovernor(1, log=lambda *_: None)
    ev._remote_govs["remote150"] = gov
    assert ev._shed_remote_slot("remote150", gov, "why", worker=dict(W)) is False
    assert calls == []
