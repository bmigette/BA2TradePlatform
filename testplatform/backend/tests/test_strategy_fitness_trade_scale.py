"""Optional trade-frequency fitness scale: fitness *= avg_trades_per_year / 100 when enabled."""
import pytest

from app.services.strategy_fitness import compute_fitness, ZERO_TRADE_SENTINEL


def _r(**kw):
    base = {"total_trades": 100, "calmar_ratio": 2.0, "avg_trades_per_year": 50.0}
    base.update(kw)
    return base


def test_scale_off_is_unchanged():
    assert compute_fitness("calmar_ratio", _r()) == pytest.approx(2.0)


def test_scale_downweights_thin_config():
    # 5 trades/yr (a 16-trade/3yr lottery) -> factor 0.05 -> 2.0 * 0.05 = 0.10
    r = _r(avg_trades_per_year=5.0, fitness_trade_scale=True)
    assert compute_fitness("calmar_ratio", r) == pytest.approx(0.10)


def test_scale_upweights_high_frequency():
    # 216 trades/yr (a 648-trade/3yr book) -> factor 2.16 -> 2.0 * 2.16 = 4.32
    r = _r(avg_trades_per_year=216.0, fitness_trade_scale=True)
    assert compute_fitness("calmar_ratio", r) == pytest.approx(4.32)


def test_scale_breakeven_at_100_per_year():
    r = _r(avg_trades_per_year=100.0, fitness_trade_scale=True)
    assert compute_fitness("calmar_ratio", r) == pytest.approx(2.0)


def test_scale_leaves_negative_fitness_unchanged():
    # a losing (negative-calmar) config must NOT be nudged toward 0 by a <1 factor.
    r = _r(calmar_ratio=-1.5, avg_trades_per_year=5.0, fitness_trade_scale=True)
    assert compute_fitness("calmar_ratio", r) == pytest.approx(-1.5)


def test_scale_keeps_zero_trade_sentinel():
    r = _r(total_trades=0, avg_trades_per_year=0.0, fitness_trade_scale=True)
    assert compute_fitness("calmar_ratio", r) == ZERO_TRADE_SENTINEL
