"""Batch "Adjust TP %": the per-transaction decision is ONE side-aware, current-price-checked helper."""
from pathlib import Path
from types import SimpleNamespace

import pytest

from ba2_trade_platform.core.types import OrderDirection
from ba2_trade_platform.services import trades
from ba2_trade_platform.services.trades import plan_batch_tp

ROOT = Path(__file__).resolve().parents[1]


class _Account:
    def __init__(self, price=None, exc=None):
        self.price, self.exc = price, exc

    def get_instrument_current_price(self, symbol):
        if self.exc:
            raise self.exc
        return self.price


def _txn(side, open_price=100.0):
    return SimpleNamespace(id=7, symbol="ABC", side=side, open_price=open_price)


def test_long_ok():
    plan = plan_batch_tp(_Account(101.0), _txn(OrderDirection.BUY), 5.0)
    assert plan.reason is None
    assert plan.price == pytest.approx(105.0)


def test_long_market_already_above_target_refused():
    plan = plan_batch_tp(_Account(110.0), _txn(OrderDirection.BUY), 5.0)
    assert plan.price is None
    assert "fill immediately" in plan.reason


def test_short_target_below_entry():
    plan = plan_batch_tp(_Account(99.0), _txn(OrderDirection.SELL), 5.0)
    assert plan.reason is None
    assert plan.price == pytest.approx(95.0)


def test_short_market_already_below_target_refused():
    plan = plan_batch_tp(_Account(90.0), _txn(OrderDirection.SELL), 5.0)
    assert plan.price is None
    assert plan.reason


@pytest.mark.parametrize("price,exc", [(None, None), (0, None), (float("nan"), None),
                                       (None, ValueError("x")), (None, ConnectionError("x"))])
def test_price_unavailable_refused(price, exc):
    plan = plan_batch_tp(_Account(price, exc), _txn(OrderDirection.BUY), 5.0)
    assert plan.price is None
    assert "current price" in plan.reason


def test_invalid_open_price_refused():
    plan = plan_batch_tp(_Account(101.0), _txn(OrderDirection.BUY, open_price=None), 5.0)
    assert plan.price is None and "open price" in plan.reason


@pytest.mark.parametrize("rel", ["ba2_trade_platform/ui/pages/live_trades.py",
                                 "ba2_trade_platform/ui/pages/overview.py"])
def test_both_pages_use_the_helper(rel):
    src = (ROOT / rel).read_text(encoding="utf-8")
    assert "plan_batch_tp" in src
    assert "txn.open_price * (1 + tp_percent / 100)" not in src
    assert hasattr(trades, "plan_batch_tp")
