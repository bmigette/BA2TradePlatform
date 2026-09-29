from datetime import datetime, timedelta
from types import SimpleNamespace

from ba2_common.analytics.performance import (
    calculate_profit_factor, calculate_sharpe_ratio, expert_performance, max_drawdown_from_pnl,
    monthly_pnl,
)
from ba2_common.core.types import OrderDirection

T0 = datetime(2026, 1, 5)


def _t(i, side, o, c, qty=1, mult=None):
    return SimpleNamespace(side=side, open_price=o, close_price=c, quantity=qty, multiplier=mult,
                           open_date=T0 + timedelta(days=i), close_date=T0 + timedelta(days=i + 1))


def test_zero_close_is_a_measured_loss_and_option_multiplier_applies():
    p = expert_performance([_t(0, OrderDirection.BUY, 2.0, 0.0, mult=100)])
    assert p["total_pnl"] == -200.0 and p["losses"] == 1


def test_short_side_accepts_enum_or_string():
    a = expert_performance([_t(0, OrderDirection.SELL, 10.0, 8.0, qty=5)])
    b = expert_performance([_t(0, "SELL", 10.0, 8.0, qty=5)])
    assert a["total_pnl"] == b["total_pnl"] == 10.0


def test_sharpe_needs_thirty_returns():
    assert calculate_sharpe_ratio([0.01] * 29) is None
    assert expert_performance([_t(i, "BUY", 10, 11) for i in range(29)])["sharpe_ratio"] is None


def test_drawdown_walks_close_order():
    txns = [_t(1, "BUY", 10, 5, qty=10), _t(0, "BUY", 10, 20, qty=10)]  # +100 first by close
    p = expert_performance(txns)
    assert (p["max_drawdown"], p["max_drawdown_pct"]) == max_drawdown_from_pnl([100.0, -50.0])


def test_profit_factor_edge_cases():
    assert calculate_profit_factor([], []) is None
    assert calculate_profit_factor([5.0], []) == float("inf")


def test_monthly_pnl_buckets_by_close_month():
    txns = [_t(0, "BUY", 10, 11), _t(40, "BUY", 10, 9)]
    assert monthly_pnl(txns) == {"2026-01": {"pnl": 1.0, "count": 1},
                                 "2026-02": {"pnl": -1.0, "count": 1}}
