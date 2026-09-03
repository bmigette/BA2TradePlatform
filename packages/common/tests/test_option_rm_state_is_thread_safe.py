"""The option RM's per-sleeve process state survives concurrent writes and resets.

THE DEFECT (review finding, 2026-09-01)
---------------------------------------
``OptionRiskManagement`` files three process-wide dicts by ``(thread id | None, expert id)``
-- the breaker latch, the in-flight charges and the decision journal -- and
``reset_thread_state`` cleared one thread's keys out of them with

    for key in [k for k in store if k[0] == me]:

That comprehension ITERATES a dict that sibling GA trial threads are writing to. Under
``--parallel > 1`` a sibling's insert lands while the comprehension is running and CPython
raises ``RuntimeError: dictionary changed size during iteration``.

The raise is not confined to the risk manager. ``reset_thread_state`` is the FIRST statement
of ``backtest_trading_db``'s ``finally``, so a raise there skipped
``common_db.clear_threadlocal_db()`` and the finishing trial left its thread-local DB
override installed: every later piece of work on that worker thread -- live code paths
included -- then read the dead run's database. A dict-iteration race that mis-routes the DB
of a whole worker thread.

WHAT THIS FILE PINS
-------------------
The shape of the reviewer's probe: writer threads hammering sleeve keys while resetter
threads clear their own, repeated hard enough to have caught the original. Two properties,
because a lock that stopped the crash by losing writes would be worse than the bug:

* no writer and no resetter raises (``test_concurrent_writes_and_resets_never_raise``);
* a reset still takes exactly its own thread's keys, and every sibling's key survives
  (``test_a_reset_takes_this_threads_keys_and_leaves_every_siblings_alone``).

MUTATION KILL (executed, not asserted): drop ``with _STATE_LOCK`` from
``_clear_this_threads_keys`` (iterate the live dict) and
``test_concurrent_writes_and_resets_never_raise`` fails with
``RuntimeError: dictionary changed size during iteration``.

Run from ``packages/common``:
    python -m pytest tests/test_option_rm_state_is_thread_safe.py -q
"""
from __future__ import annotations

import threading
from typing import Any, Dict, List, Tuple

import pytest

import ba2_common.core.OptionRiskManagement as rm
from ba2_common.core.option_book import BreakerState, CandidateStructure
from ba2_common.core.trade_store import inmem_trades

#: Enough keys that the clear's key scan is long enough for a sibling's insert to land
#: inside it. With the pre-seed the unlocked version fails on essentially every run; without
#: it the window is narrow enough to make the probe useless as a regression test.
PRESEED_KEYS = 4_000

#: Per-thread work. Deliberately not a token amount: the defect is a timing window, and a
#: probe that does ten iterations proves nothing about a GA that does millions.
WRITES_PER_WRITER = 3_000
RESETS_PER_RESETTER = 600
WRITERS = 4
RESETTERS = 4


def _candidate(tag: int) -> CandidateStructure:
    return CandidateStructure(underlying=f"SYM{tag}", strategy="bull_put_spread",
                              max_loss=100.0, notional=1_000.0, short_put_assignment=0.0)


@pytest.fixture(autouse=True)
def _clean():
    rm.reset_state()
    yield
    rm.reset_state()


def _preseed() -> None:
    """Fill the three stores with keys no live thread owns, so the clear has work to do."""
    for i in range(PRESEED_KEYS):
        key = (900_000 + i, i)
        rm._BREAKER_STATE[key] = BreakerState()
        rm._PENDING[key] = []
        rm._JOURNAL[key] = []


def _write_all(expert_id: int) -> None:
    """One write into each of the three stores, through the PRODUCTION writers.

    Not raw ``dict[key] = value``: the property is that every writer the module exposes takes
    the same lock, and a probe that bypassed them would pass against a lock protecting
    nothing.
    """
    rm.set_breaker_state(expert_id, BreakerState(peak_equity=100.0))
    rm.record_submitted(expert_id, None, _candidate(expert_id))
    rm._journal_entry(expert_id, rm.OptionEntryVerdict(True, "", "ok", "detail"),
                      "AAPL", "bull_put_spread", 1)


def _run_threads(writer_body, resetter_body) -> List[BaseException]:
    errors: List[BaseException] = []
    lock = threading.Lock()
    start = threading.Barrier(WRITERS + RESETTERS)

    def guarded(body, index):
        def run():
            try:
                start.wait()
                body(index)
            except BaseException as e:            # noqa: BLE001 -- the whole point
                with lock:
                    errors.append(e)
        return run

    threads = [threading.Thread(target=guarded(writer_body, i), daemon=True)
               for i in range(WRITERS)]
    threads += [threading.Thread(target=guarded(resetter_body, i), daemon=True)
                for i in range(RESETTERS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=180)
    assert not any(t.is_alive() for t in threads), "a worker never finished"
    return errors


def test_concurrent_writes_and_resets_never_raise():
    """THE regression. Writers insert new sleeve keys while resetters clear their own.

    Every thread runs inside ``inmem_trades()`` because that is what makes ``_sleeve_key``
    answer ``(thread id, expert)`` -- the backtest shape, and the only one in which a
    resetter has keys of its own to delete.

    MUTATION KILL: remove ``with _STATE_LOCK`` from ``_clear_this_threads_keys`` and this
    raises ``RuntimeError: dictionary changed size during iteration``.
    """
    _preseed()

    def writer(n: int) -> None:
        with inmem_trades():
            for i in range(WRITES_PER_WRITER):
                _write_all(n * WRITES_PER_WRITER + i)

    def resetter(n: int) -> None:
        with inmem_trades():
            for i in range(RESETS_PER_RESETTER):
                _write_all(500_000 + n * RESETS_PER_RESETTER + i)
                rm.reset_thread_state()

    errors = _run_threads(writer, resetter)
    assert not errors, f"{len(errors)} thread(s) raised: {errors[:3]}"


def test_a_reset_takes_this_threads_keys_and_leaves_every_siblings_alone():
    """The lock must not have been bought by losing writes.

    A reset that ran while a sibling was writing must still have taken exactly its OWN keys,
    and every key the siblings filed must still be there afterwards. Serialising the stores
    is only correct if it preserves the per-thread scoping the keys exist for.
    """
    _preseed()
    survived: Dict[int, List[Tuple[Any, ...]]] = {}
    lock = threading.Lock()

    def writer(n: int) -> None:
        with inmem_trades():
            mine = []
            for i in range(WRITES_PER_WRITER):
                expert_id = n * WRITES_PER_WRITER + i
                _write_all(expert_id)
                mine.append((threading.get_ident(), expert_id))
            with lock:
                survived[n] = mine

    def resetter(n: int) -> None:
        with inmem_trades():
            for i in range(RESETS_PER_RESETTER):
                _write_all(500_000 + n * RESETS_PER_RESETTER + i)
                rm.reset_thread_state()
            # the LAST act is a reset, so this thread must own nothing when it exits
            rm.reset_thread_state()
            me = threading.get_ident()
            for store in (rm._BREAKER_STATE, rm._PENDING, rm._JOURNAL):
                assert not [k for k in list(store) if k[0] == me], \
                    "a reset left this thread's own keys behind"

    errors = _run_threads(writer, resetter)
    assert not errors, f"{len(errors)} thread(s) raised: {errors[:3]}"

    assert len(survived) == WRITERS
    for keys in survived.values():
        missing = [k for k in keys if k not in rm._BREAKER_STATE]
        assert not missing, f"{len(missing)} writer key(s) were clobbered, e.g. {missing[:3]}"
    # ...and the pre-seeded keys, owned by no live thread, are untouched.
    assert len(rm._PENDING) >= PRESEED_KEYS
