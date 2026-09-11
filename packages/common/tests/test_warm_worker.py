"""The low-priority warm queue (spec section 6, lifecycle steps 3 and 4).

Pure mechanism: the per-thread setup and the per-item context are injected, so what
is pinned here is the QUEUE's behaviour. The FMP-specific half (freeze flag, sentinel
scope, purpose tag) is pinned in ``packages/providers/tests/test_warm_seams.py``.

The queue runs INSIDE the live trading process, which is what most of this is about:

* it must never hold a trading lock ("Background warmup must not hold an account
  submission lock or delay scheduled trading to finish");
* a pause must be VISIBLE and RECOVERABLE -- an exhausted allowance that silently
  never lifts leaves a queue that looks alive and downloads nothing forever;
* repeating an unchanged warm must download nothing at all, and two workers asking
  for the same payload must fetch it once.
"""
import threading

import pytest

from ba2_common.core.warm import worker as warm_worker
from ba2_common.core.warm.budget import WarmBudget


class _Requirement:
    """The only thing the queue needs of a requirement: a key."""

    def __init__(self, key):
        self.key = key


class _Entry:
    def __init__(self, requirement, action=warm_worker.ACTION_FETCH, estimated_bytes=None):
        self.requirement = requirement
        self.action = action
        self.estimated_bytes = estimated_bytes


class _Plan:
    def __init__(self, entries):
        self._entries = entries

    def pending(self):
        return list(self._entries)


class _Meter:
    def __init__(self, value=0):
        self.value = value

    def __call__(self):
        return self.value


def _budget(allowance=10_000_000, unknown=1000, meter=None):
    return WarmBudget(allowance_bytes=allowance, unknown_reserve_bytes=unknown,
                      meter=meter or _Meter(), gate=lambda: 0.0)


class _Recorder:
    def __init__(self, delay=0.0):
        self.calls = []
        self.contexts = []
        self.lock = threading.Lock()
        self._delay = delay

    def __call__(self, requirement):
        if self._delay:
            import time
            time.sleep(self._delay)
        with self.lock:
            self.calls.append(requirement.key)


@pytest.fixture
def warm_warnings():
    """Collect the shared logger's WARNINGs.

    ``caplog`` cannot see them: ``ba2_common.logger`` sets ``propagate = False``
    (logger.py:19), so records never reach the root handler pytest installs.
    """
    import logging

    from ba2_common.logger import logger

    class _Collect(logging.Handler):
        def __init__(self):
            super().__init__(level=logging.WARNING)
            self.messages = []

        def emit(self, record):
            self.messages.append(record.getMessage())

    handler = _Collect()
    logger.addHandler(handler)
    try:
        yield handler.messages
    finally:
        logger.removeHandler(handler)


@pytest.fixture
def queue_factory():
    started = []

    def make(fetcher, workers=2, budget=None, **kwargs):
        q = warm_worker.WarmQueue(workers=workers, budget=budget or _budget(),
                                  fetcher=fetcher, **kwargs)
        q.start()
        started.append(q)
        return q

    try:
        yield make
    finally:
        for q in started:
            q.stop(timeout=5.0)


# --------------------------------------------------------------------------- #
# Thread context
# --------------------------------------------------------------------------- #
def test_the_injected_setup_runs_once_per_thread_and_the_context_wraps_each_item(
        queue_factory):
    import contextlib

    inits = []
    entered = []

    @contextlib.contextmanager
    def _context():
        entered.append(threading.current_thread().name)
        yield

    rec = _Recorder()
    q = queue_factory(rec, workers=1, worker_init=lambda: inits.append(1),
                      fetch_context=_context)
    q.submit(_Requirement("a"))
    q.submit(_Requirement("b"))
    assert q.join(timeout=5.0)

    assert len(inits) == 1, "per-thread setup runs once, not per item"
    assert len(entered) == 2, "every fetch runs inside the injected context"


def test_the_worker_never_touches_an_account_submission_lock(queue_factory):
    from ba2_common.core.interfaces.AccountInterface import AccountInterface

    before = dict(AccountInterface._submit_locks)
    q = queue_factory(_Recorder())

    q.submit(_Requirement("a"))
    assert q.join(timeout=5.0)

    assert dict(AccountInterface._submit_locks) == before


# --------------------------------------------------------------------------- #
# Dedupe and repeat runs
# --------------------------------------------------------------------------- #
def test_a_duplicated_requirement_is_fetched_once(queue_factory):
    rec = _Recorder(delay=0.05)
    q = queue_factory(rec, workers=2)

    first = q.submit(_Requirement("a"))
    second = q.submit(_Requirement("a"))
    assert q.join(timeout=5.0)

    assert first is True and second is False
    assert rec.calls == ["a"], "two workers must share one fetch, not race for it"


def test_two_different_requirements_both_run(queue_factory):
    rec = _Recorder()
    q = queue_factory(rec, workers=2)

    q.submit(_Requirement("a"))
    q.submit(_Requirement("b"))
    assert q.join(timeout=5.0)

    assert sorted(rec.calls) == ["a", "b"]


def test_a_plan_with_nothing_pending_enqueues_nothing(queue_factory):
    rec = _Recorder()
    q = queue_factory(rec)

    assert q.submit_plan(_Plan([])) == []
    assert rec.calls == []


def test_an_entry_that_is_not_fetch_or_refresh_is_never_enqueued(queue_factory):
    rec = _Recorder()
    q = queue_factory(rec)

    assert q.submit_plan(_Plan([_Entry(_Requirement("a"), action="report")])) == []


def test_resubmitting_a_plan_the_queue_already_completed_enqueues_nothing(queue_factory):
    rec = _Recorder()
    q = queue_factory(rec)
    plan = _Plan([_Entry(_Requirement("a"))])

    assert q.submit_plan(plan) == ["a"]
    assert q.join(timeout=5.0)
    assert q.submit_plan(plan) == []

    assert rec.calls == ["a"]


# --------------------------------------------------------------------------- #
# Jobs (the enqueue-only batch hook)
# --------------------------------------------------------------------------- #
def test_a_job_runs_on_a_warm_thread_and_can_enqueue_work(queue_factory):
    rec = _Recorder()
    q = queue_factory(rec, workers=1)
    ran_on = []

    def _plan_it():
        ran_on.append(threading.current_thread().name)
        q.submit(_Requirement("from-the-job"))

    assert q.submit_job("plan:batch-1", _plan_it) is True
    assert q.join(timeout=5.0)

    assert ran_on and ran_on[0].startswith("WarmWorker")
    assert rec.calls == ["from-the-job"]


def test_a_job_costs_no_allowance(queue_factory):
    b = _budget(allowance=1000)
    q = queue_factory(_Recorder(), workers=1, budget=b)

    q.submit_job("plan:batch-1", lambda: None)
    assert q.join(timeout=5.0)

    assert b.remaining_bytes() == 1000


def test_the_same_batch_can_be_planned_again_later(queue_factory):
    """A job is repeatable by construction; it must not stay in the dedupe set."""
    runs = []
    q = queue_factory(_Recorder(), workers=1)

    q.submit_job("plan:batch-1", lambda: runs.append(1))
    assert q.join(timeout=5.0)
    assert q.submit_job("plan:batch-1", lambda: runs.append(2)) is True
    assert q.join(timeout=5.0)

    assert runs == [1, 2]


def test_a_failing_job_is_counted_and_does_not_stop_the_queue(queue_factory):
    def _boom():
        raise RuntimeError("cannot plan")

    q = queue_factory(_Recorder(), workers=1)
    q.submit_job("plan:batch-1", _boom)
    assert q.join(timeout=5.0)

    assert q.stats()["failed"] == 1
    assert q.is_running()


# --------------------------------------------------------------------------- #
# Pausing: visible, counted and recoverable
# --------------------------------------------------------------------------- #
def test_running_out_of_allowance_pauses_and_the_rest_is_counted_as_dropped(queue_factory):
    meter = _Meter()
    rec = _Recorder()

    def _spend(requirement):
        rec(requirement)
        meter.value += 400

    q = queue_factory(_spend, workers=1, budget=_budget(allowance=500, unknown=400,
                                                        meter=meter))
    q.submit_plan(_Plan([_Entry(_Requirement(k), estimated_bytes=400)
                         for k in ("a", "b", "c")]))
    assert q.join(timeout=5.0)

    stats = q.stats()
    assert rec.calls == ["a"]
    assert stats["paused"] is True
    assert stats["paused_dropped"] == 2, "what was NOT downloaded has to be countable"
    assert stats["gap"]["shortfall_bytes"] > 0


def test_a_pause_logs_once_per_episode_not_once_per_dropped_item(queue_factory,
                                                                 warm_warnings):
    meter = _Meter()

    def _spend(requirement):
        meter.value += 400

    q = queue_factory(_spend, workers=1, budget=_budget(allowance=500, unknown=400,
                                                        meter=meter))
    q.submit_plan(_Plan([_Entry(_Requirement(k), estimated_bytes=400)
                         for k in ("a", "b", "c", "d", "e")]))
    assert q.join(timeout=5.0)

    pauses = [m for m in warm_warnings if "Warm paused" in m]
    assert len(pauses) == 1, (
        "one line per dropped item buries the report that says how much did not run")


def test_the_pause_lifts_when_the_allowance_recovers(queue_factory):
    """The counters reset at the UTC day change; tomorrow's batch must not be dead."""
    meter = _Meter()
    rec = _Recorder()

    def _spend(requirement):
        rec(requirement)
        meter.value += 400

    q = queue_factory(_spend, workers=1, budget=_budget(allowance=500, unknown=400,
                                                        meter=meter))
    q.submit_plan(_Plan([_Entry(_Requirement(k), estimated_bytes=400) for k in ("a", "b")]))
    assert q.join(timeout=5.0)
    assert q.is_paused()

    meter.value = 0              # a new UTC day
    enqueued = q.submit_plan(_Plan([_Entry(_Requirement("c"), estimated_bytes=400)]))
    assert q.join(timeout=5.0)

    assert not q.is_paused()
    assert enqueued == ["c"] and rec.calls == ["a", "c"]


def test_a_pause_that_has_not_recovered_still_drops(queue_factory):
    meter = _Meter()
    rec = _Recorder()
    q = queue_factory(rec, workers=1, budget=_budget(allowance=500, unknown=400,
                                                     meter=meter))
    meter.value = 10_000        # something else spent the whole allowance

    q.submit_plan(_Plan([_Entry(_Requirement("a"), estimated_bytes=400)]))
    assert q.join(timeout=5.0)
    q.submit_plan(_Plan([_Entry(_Requirement("b"), estimated_bytes=400)]))
    assert q.join(timeout=5.0)

    assert rec.calls == []
    assert q.is_paused() and q.stats()["paused_dropped"] == 2


# --------------------------------------------------------------------------- #
# Failures
# --------------------------------------------------------------------------- #
def test_a_failing_fetch_is_counted_named_and_does_not_stop_the_queue(queue_factory):
    calls = []

    def _boom(requirement):
        calls.append(requirement.key)
        if requirement.key == "a":
            raise RuntimeError("one bad symbol")

    q = queue_factory(_boom, workers=1)
    q.submit(_Requirement("a"))
    q.submit(_Requirement("b"))
    assert q.join(timeout=5.0)

    stats = q.stats()
    assert len(calls) == 2
    assert stats["failed"] == 1 and stats["fetched"] == 1
    assert stats["failed_keys"] == ["a"]


def test_a_failed_requirement_can_be_retried_by_a_later_plan(queue_factory):
    """A transient provider error must not make a key unwarmable for the process's life."""
    attempts = []

    def _flaky(requirement):
        attempts.append(requirement.key)
        if len(attempts) == 1:
            raise RuntimeError("transient")

    q = queue_factory(_flaky, workers=1)
    q.submit(_Requirement("a"))
    assert q.join(timeout=5.0)

    assert q.submit(_Requirement("a")) is True
    assert q.join(timeout=5.0)

    assert attempts == ["a", "a"]
    assert q.stats()["failed_keys"] == [], "a key that later succeeded is no longer failing"


def test_a_failed_item_releases_its_reservation(queue_factory):
    b = _budget(allowance=1000)

    def _boom(requirement):
        raise RuntimeError("nope")

    q = queue_factory(_boom, workers=1, budget=b)
    q.submit(_Requirement("a"), estimated_bytes=900)
    assert q.join(timeout=5.0)

    assert b.reserved_bytes() == 0, "a failure must not strand the allowance"
