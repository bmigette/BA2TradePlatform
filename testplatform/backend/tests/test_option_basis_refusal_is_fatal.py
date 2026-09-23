"""Plan Part E review: a split-basis refusal must ABORT a GA, never score as a 0-fitness trial.

``SplitBasisRefused`` is deterministic (every trial refuses identically); an
``OptionSpotBasisMismatch`` is data-driven (a genome that never reads the bad symbol's chain
survives), so scored as an ordinary failure the GA would silently select AWAY from the broken
symbol. Both must come back ``fatal`` so ``handle_strategy_optimization`` raises
``_FatalTrialError`` on the first one.
"""
import pytest

import app.services.strategy_optimization_handler as H


def _trial_raising(monkeypatch, exc):
    import app.services.backtest.daily_backtest_handler as dbh

    def boom(config, progress_cb=None):
        raise exc

    monkeypatch.setattr(dbh, "run_daily_backtest", boom)
    return H._trial_worker({"symbols": ["NFLX"]}, "calmar")


def _refusals():
    from ba2_common.core.split_basis import SplitBasisRefused
    from app.services.backtest.option_basis_guard import OptionSpotBasisMismatch
    return [SplitBasisRefused("Refusing the options run: no cached split calendar for NFLX"),
            OptionSpotBasisMismatch("NFLX 2024-05-01: put-call-parity spot / as-traded spot = 10.0")]


@pytest.mark.parametrize("i", [0, 1])
def test_a_basis_refusal_trial_is_fatal(monkeypatch, i):
    exc = _refusals()[i]
    out = _trial_raising(monkeypatch, exc)
    assert out["fatal"] is True and out["ok"] is False
    assert str(exc) in out["error"]


def test_the_batch_loop_aborts_on_the_first_fatal_trial():
    """The master's rule, read from the source it runs: a fatal trial raises at once."""
    import inspect
    src = inspect.getsource(H)
    assert 'if out.get("fatal") and fatal["msg"] is None:' in src
    assert 'raise _FatalTrialError(out["error"])' in src
