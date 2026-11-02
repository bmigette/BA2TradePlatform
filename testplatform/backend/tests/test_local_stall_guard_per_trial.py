"""P6 (2026-10-24): the local stall guard is PER TRIAL, retries, and never poisons the memo.

BEFORE. ``_local_execute_jobs`` waited ``BT_LOCAL_STALL_TIMEOUT_S`` (5400 s) for ANY completion
and, if none came, wrote EVERY pending trial off at ``STALLED_SENTINEL`` -- including ones that
had started seconds earlier -- and the handler then MEMOIZED the sentinel, so the same genome was
never evaluated again in a later generation of the job.

NOW. Each trial has its own clock (time since it was submitted to its slot). A trial past the limit
is abandoned (slot respawned) and RE-QUEUED up to ``max_retries`` times; only then is it scored as
stalled, for that generation only (no memo entry), and the whole thing is logged at ERROR.
"""
import importlib
import threading
from concurrent.futures import Future

import pytest

mod = importlib.import_module("app.services.strategy_optimization_handler")

SRC = (__import__("pathlib").Path(__file__).resolve().parents[1]
       / "app" / "services" / "strategy_optimization_handler.py").read_text(encoding="utf-8")


class FakePool:
    """Same surface as _SlotPools. cfg = {"id", "wedge": n attempts that never finish,
    "secs": how long a healthy attempt takes}."""

    def __init__(self, n):
        self.busy = [None] * n
        self.attempts = {}
        self.abandoned = []
        self.submit_times = []

    def idle_slots(self):
        return [i for i, f in enumerate(self.busy) if f is None]

    def submit(self, slot, fn, cfg, metric):
        fut = Future()
        a = self.attempts[cfg["id"]] = self.attempts.get(cfg["id"], 0) + 1
        self.busy[slot] = fut
        if a > cfg.get("wedge", 0):
            t = threading.Timer(cfg.get("secs", 0.0), fut.set_result,
                                [{"ok": True, "fitness": 1.0, "id": cfg["id"]}])
            t.daemon = True
            t.start()
        return fut

    def mark_done(self, fut):
        for i, f in enumerate(self.busy):
            if f is fut:
                self.busy[i] = None

    def abandon_slot(self, fut):
        for i, f in enumerate(self.busy):
            if f is fut:
                self.busy[i] = None
                self.abandoned.append(i)
                return i
        return -1


def _jobs(*cfgs):
    return [(i, {"g": i}, f"k{i}", c) for i, c in enumerate(cfgs)]


def _run(pool, jobs, stall_s=0.3, retries=2):
    return {i: out for i, _f, _k, out in
            mod._dispatch_with_stall_guard(jobs, pool, lambda *a: None, "m", stall_s, retries)}


def test_a_trial_that_wedges_once_is_requeued_and_succeeds():
    pool = FakePool(1)
    res = _run(pool, _jobs({"id": "a", "wedge": 1}))
    assert res[0]["ok"] is True and not res[0].get("stalled")
    assert pool.attempts["a"] == 2 and len(pool.abandoned) == 1


def test_a_trial_that_always_wedges_is_scored_stalled_after_the_bounded_retries():
    pool = FakePool(1)
    res = _run(pool, _jobs({"id": "a", "wedge": 99}), retries=2)
    assert res[0]["ok"] is False and res[0]["stalled"] is True
    assert res[0]["attempts"] == 3 and pool.attempts["a"] == 3 and len(pool.abandoned) == 3
    assert "stalled" in res[0]["error"]


def test_the_guard_is_per_trial_not_per_batch():
    """W wedges from t=0; H starts at ~0.2 and runs 0.25 s, so it is still in flight when W's
    limit (0.3 s) passes. Only W may be written off; H is healthy and completes."""
    pool = FakePool(2)
    jobs = _jobs({"id": "w", "wedge": 99}, {"id": "q", "secs": 0.2}, {"id": "h", "secs": 0.25})
    res = _run(pool, jobs, stall_s=0.3, retries=0)
    assert res[0]["stalled"] is True
    assert res[1]["ok"] is True and res[2]["ok"] is True, res


def test_a_stall_logs_loudly(caplog):
    import logging
    caplog.set_level(logging.ERROR)
    _run(FakePool(1), _jobs({"id": "a", "wedge": 99}), retries=1)
    text = "\n".join(r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR)
    assert "LOCAL POOL STALLED" in text and "requeue" in text.lower() and "attempt 2/2" in text


def test_the_stalled_score_is_never_memoized():
    a = SRC.index('if out.get("stalled"):')
    b = SRC.index("fit = ZERO_TRADE_SENTINEL", a)
    branch = SRC[a:b]
    assert "memo.put" not in branch, "a stalled genome must be re-evaluable in a later generation"
    assert "_report_trial_result" not in branch, (
        "a stall is not a measurement: it must not be checkpointed as an evaluated individual")
    assert '"status": "stalled"' in branch
