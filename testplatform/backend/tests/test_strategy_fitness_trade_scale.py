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


def test_scale_caps_high_frequency_at_default_100():
    # 216 trades/yr clamped to the default cap 100 -> factor 1.0 -> 2.0 (NOT 4.32): the GA gets
    # no reward for over-trading (no scalper incentive).
    r = _r(avg_trades_per_year=216.0, fitness_trade_scale=True)
    assert compute_fitness("calmar_ratio", r) == pytest.approx(2.0)


def test_scale_cap_setting_allows_upweight_up_to_cap():
    # cap=200 -> 216/yr clamps to 200 -> factor 2.0 -> 2.0 * 2.0 = 4.0; a 150/yr book -> 1.5 -> 3.0.
    r = _r(avg_trades_per_year=216.0, fitness_trade_scale=True, fitness_trade_scale_cap=200.0)
    assert compute_fitness("calmar_ratio", r) == pytest.approx(4.0)
    r2 = _r(avg_trades_per_year=150.0, fitness_trade_scale=True, fitness_trade_scale_cap=200.0)
    assert compute_fitness("calmar_ratio", r2) == pytest.approx(3.0)


def test_scale_breakeven_at_100_per_year():
    r = _r(avg_trades_per_year=100.0, fitness_trade_scale=True)
    assert compute_fitness("calmar_ratio", r) == pytest.approx(2.0)


def test_scale_below_cap_unaffected_by_cap():
    # 50/yr is below the cap -> factor 0.5 regardless of cap value.
    r = _r(avg_trades_per_year=50.0, fitness_trade_scale=True, fitness_trade_scale_cap=100.0)
    assert compute_fitness("calmar_ratio", r) == pytest.approx(1.0)


def test_scale_leaves_negative_fitness_unchanged():
    # a losing (negative-calmar) config must NOT be nudged toward 0 by a <1 factor.
    r = _r(calmar_ratio=-1.5, avg_trades_per_year=5.0, fitness_trade_scale=True)
    assert compute_fitness("calmar_ratio", r) == pytest.approx(-1.5)


def test_scale_keeps_zero_trade_sentinel():
    r = _r(total_trades=0, avg_trades_per_year=0.0, fitness_trade_scale=True)
    assert compute_fitness("calmar_ratio", r) == ZERO_TRADE_SENTINEL
