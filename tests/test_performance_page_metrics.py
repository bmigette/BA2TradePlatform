"""Characterization pin for PerformanceTab's per-expert metrics, written BEFORE the metric
code moved to ba2_common.analytics.performance (site plan P0a). Must stay green after."""
from datetime import datetime, timedelta
from types import SimpleNamespace

from ba2_trade_platform.core.types import OrderDirection
from ba2_trade_platform.ui.pages.performance import PerformanceTab

T0 = datetime(2026, 1, 5, 15, 0)


def _txn(i, side, open_p, close_p, qty=10, mult=None, days=2, expert_id=901):
    return SimpleNamespace(id=i, expert_id=expert_id, side=side, open_price=open_p,
                           close_price=close_p, quantity=qty, multiplier=mult,
                           open_date=T0 + timedelta(days=i),
                           close_date=T0 + timedelta(days=i + days))


TXNS = (
    [_txn(0, OrderDirection.BUY, 100.0, 110.0), _txn(1, OrderDirection.SELL, 50.0, 55.0),
     _txn(2, OrderDirection.BUY, 2.0, 0.0, qty=1, mult=100),     # expired worthless option
     _txn(3, OrderDirection.SELL, 1.5, 0.0, qty=2, mult=100)]    # short option kept premium
    + [_txn(10 + k, OrderDirection.BUY, 20.0, 20.0 + (k % 5) - 2) for k in range(30)]
)


def test_per_expert_metrics_are_pinned():
    m = PerformanceTab(None)._calculate_transaction_metrics(TXNS)["Expert-901"]
    assert m["total_transactions"] == 34
    assert round(m["total_pnl"], 6) == round(100 - 50 - 200 + 300 + sum(
        ((k % 5) - 2) * 10 for k in range(30)), 6)
    assert (m["wins"], m["losses"]) == (14, 14)
    assert round(m["sharpe_ratio"], 9) == -0.004913114
    assert round(m["avg_duration_days"], 6) == 2.0
    assert round(m["max_drawdown"], 9) == 250.0
    assert round(m["max_drawdown_pct"], 9) == 250.0
    assert round(m["profit_factor"], 9) == 1.348837209
    assert round(m["largest_win"], 9) == 300.0
    assert round(m["largest_loss"], 9) == -200.0
    assert round(m["win_rate"], 9) == 50.0
    assert round(m["avg_pnl"], 9) == 4.411764706
    assert list(m["transactions"]) == list(TXNS)
    assert len(m["returns"]) == 34
    assert [round(r, 9) for r in m["returns"][:4]] == [0.1, -0.1, -1.0, 1.0]
    assert round(sum(m["returns"]), 9) == 0.0
    # Key insertion order is part of the contract (order-sensitive live captures).
    assert list(m) == ['total_transactions', 'max_drawdown', 'max_drawdown_pct',
                       'avg_duration_days', 'total_pnl', 'avg_pnl', 'win_rate', 'wins',
                       'losses', 'profit_factor', 'largest_win', 'largest_loss',
                       'sharpe_ratio', 'transactions', 'returns']


def test_monthly_metrics_are_pinned():
    monthly = PerformanceTab(None)._calculate_monthly_metrics(TXNS)
    total = sum(v["Expert-901"]["pnl"] for v in monthly.values())
    count = sum(v["Expert-901"]["count"] for v in monthly.values())
    assert count == 34
    assert round(total, 6) == round(100 - 50 - 200 + 300 + sum(
        ((k % 5) - 2) * 10 for k in range(30)), 6)
    assert {k: {n: dict(v) for n, v in per.items()} for k, per in monthly.items()} == {
        "2026-01": {"Expert-901": {"pnl": 150.0, "count": 19}},
        "2026-02": {"Expert-901": {"pnl": 0.0, "count": 15}},
    }
    assert list(monthly) == ["2026-01", "2026-02"]
