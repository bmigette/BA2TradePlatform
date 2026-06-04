"""Regression: position-size validation must not lazy-load transaction.trading_orders
on a detached Transaction (raised 'not bound to a Session' and silently skipped)."""
from tests.conftest import MockAccount
from tests.factories import (
    create_account_definition, create_transaction, create_trading_order,
)
from ba2_trade_platform.core.types import OrderDirection, OrderStatus, OrderType


def _acct():
    return MockAccount(create_account_definition().id)


class TestTransactionEntryOrderHelper:
    def test_returns_first_order_and_side_readable_after_session(self):
        acct = _acct()
        txn = create_transaction(symbol="AAPL", side=OrderDirection.BUY)
        first = create_trading_order(account_id=acct.id, symbol="AAPL",
                                     side=OrderDirection.BUY, transaction_id=txn.id,
                                     order_type=OrderType.MARKET, status=OrderStatus.FILLED)
        create_trading_order(account_id=acct.id, symbol="AAPL",
                             side=OrderDirection.SELL, transaction_id=txn.id,
                             order_type=OrderType.SELL_LIMIT, status=OrderStatus.NEW)

        entry = acct._get_transaction_entry_order(txn.id)
        assert entry is not None
        assert entry.id == first.id
        # Reading a column attribute after the session closed must not raise.
        assert entry.side == OrderDirection.BUY

    def test_returns_none_when_no_orders(self):
        acct = _acct()
        txn = create_transaction(symbol="AAPL", side=OrderDirection.BUY)
        assert acct._get_transaction_entry_order(txn.id) is None

    def test_returns_none_for_none_id(self):
        assert _acct()._get_transaction_entry_order(None) is None
