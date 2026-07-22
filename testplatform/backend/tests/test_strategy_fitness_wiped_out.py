"""A wiped-out account (results["account_wiped_out"] -- set when net_liquidating_value hit
<= 0 and the sim stopped early, see BacktestAccount.snapshot_equity / DailyBacktestEngine.run)
must score WORSE than a zero-trade config, regardless of what the (already-clamped, otherwise
nonsensical-past-that-point) metrics say.
"""
import pytest

from app.services.strategy_fitness import (
    compute_fitness, WIPED_OUT_SENTINEL, ZERO_TRADE_SENTINEL,
)


def _r(**kw):
    base = {"total_trades": 50, "calmar_ratio": 5.0, "win_rate": 60.0}
    base.update(kw)
    return base


def test_wiped_out_returns_the_wiped_out_sentinel():
    r = _r(account_wiped_out=True)
    assert compute_fitness("calmar_ratio", r) == WIPED_OUT_SENTINEL


def test_wiped_out_ranks_worse_than_zero_trade():
    assert WIPED_OUT_SENTINEL < ZERO_TRADE_SENTINEL


def test_wiped_out_overrides_an_otherwise_great_calmar():
    """Even a huge calmar_ratio (the kind a still-nonsensical post-wipeout equity curve could
    otherwise report) must not leak through -- the wipeout check runs BEFORE any metric read."""
    r = _r(calmar_ratio=999.0, account_wiped_out=True)
    assert compute_fitness("calmar_ratio", r) == WIPED_OUT_SENTINEL


def test_wiped_out_checked_for_every_metric():
    for metric in ("calmar_ratio", "total_return", "sharpe", "consistent_annual_return", "max_drawdown"):
        r = _r(account_wiped_out=True)
        assert compute_fitness(metric, r) == WIPED_OUT_SENTINEL


def test_not_wiped_out_is_unaffected():
    r = _r(account_wiped_out=False)
    assert compute_fitness("calmar_ratio", r) == pytest.approx(5.0)


def test_missing_flag_defaults_to_not_wiped_out():
    r = _r()
    assert "account_wiped_out" not in r
    assert compute_fitness("calmar_ratio", r) == pytest.approx(5.0)
