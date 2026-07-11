"""Optional win-rate fitness factor: fitness *= 2 * (win_rate / 100) when enabled.

50% win rate is break-even (1.0x), 100% doubles the fitness, 0% zeroes it. Unlike
fitness_trade_scale, this factor also applies to consistent_annual_return (win rate
isn't part of that metric's own formula).
"""
import pytest

from app.services.strategy_fitness import compute_fitness, ZERO_TRADE_SENTINEL


def _r(**kw):
    base = {"total_trades": 100, "calmar_ratio": 2.0, "win_rate": 50.0}
    base.update(kw)
    return base


def test_factor_off_is_unchanged():
    assert compute_fitness("calmar_ratio", _r()) == pytest.approx(2.0)


def test_factor_breakeven_at_50pct_win_rate():
    r = _r(win_rate=50.0, fitness_win_rate_factor=True)
    assert compute_fitness("calmar_ratio", r) == pytest.approx(2.0)


def test_factor_doubles_at_100pct_win_rate():
    r = _r(win_rate=100.0, fitness_win_rate_factor=True)
    assert compute_fitness("calmar_ratio", r) == pytest.approx(4.0)


def test_factor_zeroes_at_0pct_win_rate():
    r = _r(win_rate=0.0, fitness_win_rate_factor=True)
    assert compute_fitness("calmar_ratio", r) == pytest.approx(0.0)


def test_factor_scales_proportionally():
    # 65% win -> factor 1.3
    r = _r(win_rate=65.0, fitness_win_rate_factor=True)
    assert compute_fitness("calmar_ratio", r) == pytest.approx(2.6)


def test_factor_leaves_negative_fitness_unchanged():
    # a losing (negative-calmar) config must NOT be nudged toward 0 by a low win rate factor.
    r = _r(calmar_ratio=-1.5, win_rate=10.0, fitness_win_rate_factor=True)
    assert compute_fitness("calmar_ratio", r) == pytest.approx(-1.5)


def test_factor_keeps_zero_trade_sentinel():
    r = _r(total_trades=0, win_rate=0.0, fitness_win_rate_factor=True)
    assert compute_fitness("calmar_ratio", r) == ZERO_TRADE_SENTINEL


def test_factor_applies_to_consistent_annual_return():
    # CAR's own early-return path must still route through the win-rate factor (unlike
    # fitness_trade_scale, which is a structural no-op for CAR).
    r = {
        "total_trades": 100,
        "annualized_return": 30.0,
        "avg_trades_per_year": 30.0,
        "max_drawdown": -10.0,
        "win_rate": 100.0,
        "fitness_win_rate_factor": True,
    }
    car_off = compute_fitness("consistent_annual_return", {**r, "fitness_win_rate_factor": False})
    car_on = compute_fitness("consistent_annual_return", r)
    assert car_on == pytest.approx(car_off * 2.0)


def test_factor_does_not_apply_to_max_drawdown():
    # Negated distance metric, not a return -- excluded like fitness_trade_scale.
    r = {"total_trades": 100, "max_drawdown": -10.0, "win_rate": 100.0,
         "fitness_win_rate_factor": True}
    assert compute_fitness("max_drawdown", r) == pytest.approx(10.0)


def test_factor_missing_win_rate_is_noop():
    r = {"total_trades": 100, "calmar_ratio": 2.0, "fitness_win_rate_factor": True}
    assert compute_fitness("calmar_ratio", r) == pytest.approx(2.0)
