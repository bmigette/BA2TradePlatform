"""The shared account path passes quantities through; it is not a sizing grid.

Exercise the real AccountInterface template and SQLite ledger, with only the
broker implementation doubled. No TP/SL or transaction policy is changed.
"""
import pytest

from tests.conftest import MockAccount
from tests.factories import create_account_definition, create_transaction, create_trading_order
from ba2_common.core.interfaces.AccountInterface import AccountInterface
from ba2_common.core.db import get_instance, update_instance
from ba2_common.core.models import TradingOrder, Transaction
from ba2_common.core.types import OrderDirection, OrderStatus, OrderType


class QuantityAccount(MockAccount):
    submit_order = AccountInterface.submit_order

    def __init__(self, account_id):
        super().__init__(account_id)
        self.sent = []

    def _submit_order_impl(self, order, **kwargs):
        self.sent.append((order.quantity, kwargs['is_closing_order']))
        order.broker_order_id = f"test-{order.id}"
        order.status = OrderStatus.NEW
        update_instance(order)
        return order


@pytest.fixture
def account():
    return QuantityAccount(create_account_definition().id)


@pytest.mark.parametrize("quantity", [0.4018, 4.2537])
def test_open_records_the_entry_quantity_without_rounding(account, quantity):
    order = TradingOrder(account_id=account.id, symbol="AAPL", quantity=quantity,
                         side=OrderDirection.BUY, order_type=OrderType.MARKET,
                         status=OrderStatus.PENDING)
    result = account.submit_order(order)
    assert result is not None
    assert account.sent == [(quantity, False)]
    assert get_instance(TradingOrder, result.id).quantity == quantity
    assert get_instance(Transaction, result.transaction_id).quantity == quantity


@pytest.mark.parametrize("quantity", [0.4018, 4.2537])
@pytest.mark.parametrize("order_type,prices", [
    (OrderType.SELL_LIMIT, {"limit_price": 170.0}),
    (OrderType.SELL_STOP, {"stop_price": 130.0}),
    (OrderType.SELL_STOP_LIMIT, {"stop_price": 130.0, "limit_price": 129.0}),
    (OrderType.OCO, {"limit_price": 170.0, "stop_price": 130.0}),
])
def test_tp_sl_and_oco_pass_supplied_fraction_unchanged(account, quantity, order_type, prices):
    txn = create_transaction(quantity=quantity)
    entry = create_trading_order(account_id=account.id, transaction_id=txn.id,
                                 quantity=quantity, filled_qty=quantity, status=OrderStatus.FILLED)
    order = TradingOrder(account_id=account.id, symbol="AAPL", quantity=quantity,
                         side=OrderDirection.SELL, order_type=order_type,
                         status=OrderStatus.PENDING, transaction_id=txn.id,
                         depends_on_order=entry.id,
                         depends_order_status_trigger=OrderStatus.FILLED, **prices)
    result = account.submit_order(order)
    assert result is not None
    assert account.sent == [(quantity, False)]
    assert get_instance(TradingOrder, result.id).quantity == quantity
    assert get_instance(Transaction, txn.id).quantity == quantity


def test_partial_close_keeps_requested_fraction_not_full_transaction(account):
    txn = create_transaction(quantity=4.2537)
    create_trading_order(account_id=account.id, transaction_id=txn.id,
                         quantity=4.2537, filled_qty=4.2537, status=OrderStatus.FILLED)
    order = TradingOrder(account_id=account.id, symbol="AAPL", quantity=0.4018,
                         side=OrderDirection.SELL, order_type=OrderType.MARKET,
                         status=OrderStatus.PENDING, transaction_id=txn.id)
    result = account.submit_order(order, is_closing_order=True)
    assert result is not None
    assert account.sent == [(0.4018, True)]
    assert get_instance(TradingOrder, result.id).quantity == 0.4018


@pytest.mark.parametrize("deferred", [False, True])
def test_full_close_uses_exact_remaining_filled_quantity(account, deferred):
    txn = create_transaction(quantity=4.2537)
    create_trading_order(account_id=account.id, transaction_id=txn.id,
                         quantity=4.2537, filled_qty=4.2537, status=OrderStatus.FILLED)
    partial = create_trading_order(account_id=account.id, transaction_id=txn.id,
                                   quantity=1.1254, filled_qty=1.1254,
                                   side=OrderDirection.SELL, status=OrderStatus.FILLED)
    remaining = abs(txn.get_current_open_qty())
    assert remaining == pytest.approx(3.1283)
    result = account.submit_close_order_for_transaction(
        txn, last_broker_canceled_order_id=partial.id if deferred else None)
    assert result['success'], result
    close = get_instance(TradingOrder, result['close_order_id'])
    assert close.quantity == remaining
    assert account.sent == ([] if deferred else [(remaining, True)])
