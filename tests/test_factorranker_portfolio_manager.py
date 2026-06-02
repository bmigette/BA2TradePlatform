from unittest.mock import patch, MagicMock

from ba2_trade_platform.core.types import OrderDirection, OrderStatus
from ba2_trade_platform.modules.experts.FactorRanker.portfolio import FactorPortfolioManager
from tests.factories import (
    create_account_definition, create_expert_instance,
    create_transaction, create_trading_order,
)


class _CapturingAccount:
    """Account double that records submit_order calls and fills/persists the order."""

    def __init__(self, prices):
        self.submitted = []
        self._prices = prices

    def get_instrument_current_price(self, symbol):
        return self._prices.get(symbol)

    def submit_order(self, order, is_closing_order=False):
        from ba2_trade_platform.core.db import add_instance
        self.submitted.append((order, is_closing_order))
        order.status = OrderStatus.FILLED
        order.filled_qty = order.quantity
        add_instance(order, expunge_after_flush=True)
        return order


def _hold(account_id, expert_id, symbol, shares):
    """Create an OPENED transaction with a filled BUY so get_current_open_qty == shares."""
    trans = create_transaction(symbol=symbol, quantity=shares, side=OrderDirection.BUY, expert_id=expert_id)
    create_trading_order(
        account_id=account_id, symbol=symbol, quantity=shares, side=OrderDirection.BUY,
        status=OrderStatus.FILLED, transaction_id=trans.id, filled_qty=shares,
    )
    return trans


def test_rebalance_buys_new_and_sells_dropped():
    acct = create_account_definition()
    inst = create_expert_instance(account_id=acct.id, expert="FactorRanker")
    _hold(acct.id, inst.id, "A", 10)   # currently hold A (10 sh) and C (20 sh)
    _hold(acct.id, inst.id, "C", 20)

    account = _CapturingAccount({"A": 10.0, "B": 5.0, "C": 4.0})
    expert_stub = MagicMock()
    expert_stub.get_virtual_balance.return_value = 1000.0

    with patch("ba2_trade_platform.core.utils.get_account_instance_from_id", return_value=account), \
         patch("ba2_trade_platform.core.utils.get_expert_instance_from_id", return_value=expert_stub):
        mgr = FactorPortfolioManager(inst.id)
        mgr.rebalance({"A": 0.5, "B": 0.5})

    by_symbol = {o.symbol: (o.side, o.quantity, closing) for (o, closing) in account.submitted}
    # A: target $500/$10 = 50 sh, hold 10 -> BUY 40 (entry)
    assert by_symbol["A"] == (OrderDirection.BUY, 40, False)
    # B: target $500/$5 = 100 sh, hold 0 -> BUY 100 (entry)
    assert by_symbol["B"] == (OrderDirection.BUY, 100, False)
    # C: dropped from target -> SELL all 20 (closing)
    assert by_symbol["C"] == (OrderDirection.SELL, 20, True)


def test_new_buy_creates_expert_attributed_transaction():
    """A brand-new buy must produce a Transaction with expert_id set (attribution path)."""
    acct = create_account_definition()
    inst = create_expert_instance(account_id=acct.id, expert="FactorRanker")

    account = _CapturingAccount({"B": 5.0})
    expert_stub = MagicMock()
    expert_stub.get_virtual_balance.return_value = 1000.0

    with patch("ba2_trade_platform.core.utils.get_account_instance_from_id", return_value=account), \
         patch("ba2_trade_platform.core.utils.get_expert_instance_from_id", return_value=expert_stub):
        mgr = FactorPortfolioManager(inst.id)
        mgr.rebalance({"B": 1.0})

    order, closing = account.submitted[0]
    assert order.symbol == "B" and order.side == OrderDirection.BUY
    assert order.transaction_id is not None
    from ba2_trade_platform.core.db import get_instance
    from ba2_trade_platform.core.models import Transaction
    trans = get_instance(Transaction, order.transaction_id)
    assert trans.expert_id == inst.id     # attribution via transaction.expert_id
