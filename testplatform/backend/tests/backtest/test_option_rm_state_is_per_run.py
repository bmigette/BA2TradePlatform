"""The option risk manager's per-sleeve PROCESS state must not survive a run boundary.

``OptionRiskManagement`` keeps three things between evaluations, because none of them can be
derived from the ledger: the drawdown breaker's peak and latch, the charges for structures
submitted this cycle that the book cannot see yet, and the decision journal. Live that is
exactly right — the peak IS history. In the GA it would be a cross-trial dependency, and two
separate mechanisms keep it from being one:

* the sleeve KEY carries the thread id while a backtest trade store is active, so concurrent
  trials (and live state, which is keyed without one) cannot see each other. Pinned in
  ``packages/common/tests/test_option_risk_manager_wiring.py``;
* ``backtest_trading_db`` clears the state at BOTH ends of a run, because sequential trials
  on one worker thread share a key. A trial that opens nothing because an EARLIER genome
  drew its sleeve down is not reproducible, and that is what this file pins.

That clear is ``reset_thread_state`` -- scoped to the keys the finishing thread itself
filed. It was a process-wide ``reset_state()`` until 2026-09-01, which made the per-thread
key pointless the moment ``--parallel > 1``: the first trial to finish wiped its still-running
siblings. The last two tests here are the concurrent case that a one-trial-at-a-time test
cannot see.
"""
from __future__ import annotations

import threading

from ba2_common.core import OptionRiskManagement as option_rm
from ba2_common.core.option_book import BreakerState, CandidateStructure

from app.services.backtest.backtest_db import backtest_trading_db


def _candidate() -> CandidateStructure:
    return CandidateStructure(underlying="XYZ", strategy="bull_put_spread",
                              max_loss=500.0, notional=9_500.0,
                              short_put_assignment=9_500.0)


def test_a_run_leaves_no_sleeve_state_behind():
    """Whatever the run put in the store is gone when it exits, so nothing it decided can
    reach live code paths (or the next trial) on this thread.

    Deliberately says nothing about whether the run can SEE state set outside it: that
    depends on the sleeve key, which depends on whether the in-memory trade store is
    enabled in this environment, and it is pinned directly (with the store mode forced) in
    packages/common/tests/test_option_risk_manager_wiring.py."""
    with backtest_trading_db("rm-state-run-a"):
        option_rm.set_breaker_state(1, BreakerState(peak_equity=5_000.0, halted=True))
        option_rm.record_submitted(1, 99, _candidate())
        assert option_rm.get_breaker_state(1).halted is True     # the premise

    assert option_rm.get_breaker_state(1) == BreakerState()
    assert option_rm.pending_charges(1) == ()


def test_the_next_run_does_not_inherit_the_previous_sleeve():
    """The GA reuses one worker thread for trial after trial, and those trials share a
    sleeve key. A halted sleeve carried into the next genome would silently score it as
    "opened nothing"."""
    with backtest_trading_db("rm-state-run-b"):
        option_rm.set_breaker_state(1, BreakerState(peak_equity=5_000.0, halted=True))
        option_rm.record_submitted(1, 99, _candidate())
        assert option_rm.pending_charges(1)          # the premise: state really was set

    with backtest_trading_db("rm-state-run-c"):
        assert option_rm.get_breaker_state(1).halted is False
        assert option_rm.pending_charges(1) == ()
        assert option_rm.journal(1) == ()


# ==========================================================================
# THE CONCURRENT case. The two tests above run one trial at a time, and a
# run-boundary reset that clears the WHOLE process passes both of them --
# which is what let a bare ``.clear()`` ship. Under ``--parallel > 1`` the
# GA runs trials in worker threads simultaneously, so the reset has to be
# scoped to the finishing trial's OWN keys or the first trial to exit wipes
# every sibling's breaker latch, in-flight charges and journal, and those
# siblings trade on against a sleeve the rails believe is empty.
# ==========================================================================
def test_a_finishing_trial_does_not_wipe_a_CONCURRENT_trials_sleeve():
    """Two trials, two threads, overlapping lifetimes. B starts and finishes INSIDE A's
    run; A's latch and in-flight charge must still be there afterwards.

    MUTATION KILL: swap ``reset_thread_state`` back to ``reset_state`` in
    ``backtest_trading_db`` (or drop the key-shape filter in ``_clear_this_threads_keys``)
    and A comes back un-halted with no charges."""
    b_may_run = threading.Event()
    b_done = threading.Event()
    failures: list = []

    def trial_b():
        try:
            b_may_run.wait(timeout=10)
            with backtest_trading_db("rm-state-concurrent-b"):
                option_rm.set_breaker_state(1, BreakerState(peak_equity=1_000.0,
                                                            halted=True))
                option_rm.record_submitted(1, 202, _candidate())
        except Exception as e:                              # noqa: BLE001 - reported below
            failures.append(e)
        finally:
            b_done.set()

    worker = threading.Thread(target=trial_b, name="rm-trial-b")
    worker.start()
    try:
        with backtest_trading_db("rm-state-concurrent-a"):
            option_rm.set_breaker_state(1, BreakerState(peak_equity=5_000.0, halted=True))
            option_rm.record_submitted(1, 101, _candidate())
            assert option_rm.get_breaker_state(1).halted is True     # the premise
            b_may_run.set()
            assert b_done.wait(timeout=30), "the concurrent trial never finished"
            assert not failures, failures
            # B has now ENDED -- its run-boundary reset has fired. A is still running.
            assert option_rm.get_breaker_state(1) == BreakerState(peak_equity=5_000.0,
                                                                  halted=True)
            assert len(option_rm.pending_charges(1)) == 1
            assert option_rm.journal(1) is not None
    finally:
        b_may_run.set()
        worker.join(timeout=30)


def test_a_backtest_run_boundary_never_clears_the_LIVE_sleeve_keys():
    """The live keys are ``(None, expert)`` and a backtest thread must not touch them.

    It would be easy to: the reset fires from ``backtest_trading_db``'s ``finally``, AFTER
    the in-memory trade store has been exited, so ``_sleeve_key`` answers ``(None, ...)``
    there. The scope is read off the key SHAPE for exactly this reason. A live sleeve whose
    breaker has stood the book down must not be re-armed by a backtest finishing on the
    same thread."""
    live = BreakerState(peak_equity=250_000.0, halted=True)
    option_rm.set_breaker_state(4242, live)                 # no store active -> a LIVE key
    try:
        with backtest_trading_db("rm-state-live-keys"):
            option_rm.set_breaker_state(4242, BreakerState(peak_equity=9.0, halted=True))
        assert option_rm.get_breaker_state(4242) == live
    finally:
        option_rm.reset_state()
