"""BT/live option parity review (I2): deterministic run refusals must ABORT a GA, never score 0.

Each refusal below is raised identically by EVERY trial of the run -- the run config (spread
model, ``option_trade_records``) or the market calendar is wrong, or the backtest clock stepped
on a non-session date. Scored as an ordinary failed trial each becomes 0 fitness and the GA
"finishes" on nothing, so ``_trial_worker`` must return them ``fatal`` (the master then raises
``_FatalTrialError`` on the first one; see ``test_option_basis_refusal_is_fatal.py``).

Every exception here is produced by the REAL refusing code, not constructed by hand, so a
refusal that loses its named type fails this file. Each is still a ``ValueError`` (or, for the
calendar, the existing ``RuntimeError``) so existing catches keep seeing it.
"""
from datetime import date

import pytest

import app.services.strategy_optimization_handler as H


def _trial_raising(monkeypatch, exc):
    import app.services.backtest.daily_backtest_handler as dbh

    def boom(config, progress_cb=None):
        raise exc

    monkeypatch.setattr(dbh, "run_daily_backtest", boom)
    return H._trial_worker({"symbols": ["NFLX"]}, "calmar")


def _raised(fn):
    try:
        fn()
    except Exception as e:  # noqa: BLE001 -- capturing the real refusal is the point
        return e
    raise AssertionError(f"{fn} did not refuse")


def _spread_model_refusal():
    from app.services.backtest.backtest_account import BacktestAccount
    return _raised(lambda: BacktestAccount._resolve_spread_model({}))


def _trade_records_refusal():
    from app.services.backtest.results import require_option_trade_records
    return _raised(lambda: require_option_trade_records({}))


def _non_session_refusal():
    from ba2_common.core.market_calendar import backtest_decision_label
    return _raised(lambda: backtest_decision_label(date(2024, 6, 15)))    # a Saturday


def _calendar_unavailable():
    from ba2_common.core.market_calendar import MarketCalendarUnavailable
    return MarketCalendarUnavailable("pandas-market-calendars could not be built")


@pytest.mark.parametrize("make,name,base", [
    (_spread_model_refusal, "SpreadModelConfigError", ValueError),
    (_trade_records_refusal, "OptionTradeRecordsFlagMissing", ValueError),
    (_non_session_refusal, "NotARegularSession", ValueError),
    (_calendar_unavailable, "MarketCalendarUnavailable", RuntimeError),
], ids=["spread_model", "option_trade_records", "non_session_bar", "calendar_unavailable"])
def test_a_deterministic_refusal_is_fatal(monkeypatch, make, name, base):
    exc = make()
    assert type(exc).__name__ == name and isinstance(exc, base)
    out = _trial_raising(monkeypatch, exc)
    assert out["fatal"] is True and out["ok"] is False
    assert str(exc) in out["error"]


def test_the_backtest_accounts_label_refusal_keeps_its_name():
    """``BacktestAccount.decision_label`` re-wraps the calendar refusal with the simulated
    date; the NAME must survive the wrap or the fatal list stops matching it."""
    from types import SimpleNamespace
    from datetime import datetime

    from app.services.backtest.backtest_account import BacktestAccount

    acct = BacktestAccount.__new__(BacktestAccount)
    acct._price = SimpleNamespace(now=lambda: datetime(2024, 6, 15, 16, 0))    # a Saturday
    exc = _raised(acct.decision_label)
    assert type(exc).__name__ == "NotARegularSession"
    assert "2024-06-15" in str(exc)


def test_an_ordinary_bad_genome_is_still_not_fatal(monkeypatch):
    """The control: a plain ValueError from a trial is a bad trial, not a run defect."""
    out = _trial_raising(monkeypatch, ValueError("this genome is nonsense"))
    assert out["fatal"] is False
