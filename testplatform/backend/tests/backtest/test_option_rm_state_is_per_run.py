"""The option risk manager's per-sleeve PROCESS state must not survive a run boundary.

``OptionRiskManagement`` keeps three things between evaluations, because none of them can be
derived from the ledger: the drawdown breaker's peak and latch, the charges for structures
submitted this cycle that the book cannot see yet, and the decision journal. Live that is
exactly right — the peak IS history. In the GA it would be a cross-trial dependency, and two
separate mechanisms keep it from being one:

* the sleeve KEY carries the thread id while a backtest trade store is active, so concurrent
  trials (and live state, which is keyed without one) cannot see each other. Pinned in
  ``packages/common/tests/test_option_risk_manager_wiring.py``;
* ``backtest_trading_db`` clears the state when a run ENDS, because sequential trials on one
  worker thread share a key. A trial that opens nothing because an EARLIER genome drew its
  sleeve down is not reproducible, and that is what this file pins.
"""
from __future__ import annotations

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
