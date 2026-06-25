"""Tests for ReadOnlyAccountInterface.refresh_transactions and canceled partial fills.

A cancel-and-replace (TP/SL rebase) can race a live fill: the broker executes
part of the order before honoring the cancel, leaving the order CANCELED with
filled_qty > 0. refresh_transactions() recalculates each open transaction's
quantity from its orders on every call (it runs on every TradeManager cycle,
not just on a detected status change), so if it drops those genuinely-filled
shares it permanently re-inflates the transaction back to the pre-fill
quantity - even overwriting a manual correction made via the Overview UI.
This is the NNE "Quantity Mismatch: broker +8 / transactions +12" incident
that kept reappearing no matter how many times it was manually fixed
(2026-06-24).
"""
from tests.conftest import MockAccount
from tests.factories import create_account_definition, create_transaction, create_trading_order
from ba2_trade_platform.core.db import get_instance
from ba2_trade_platform.core.models import Transaction
from ba2_trade_platform.core.types import OrderDirection, OrderStatus, OrderType


class TestRefreshTransactionsCanceledPartialFill:
    def test_canceled_partial_fill_counted_in_recalculated_quantity(self):
        """A SELL order that partially filled (4/6) before being CANCELED must
        still reduce the recalculated transaction quantity - the 4 shares
        really traded at the broker."""
        acct_def = create_account_definition()
        account = MockAccount(acct_def.id)
        txn = create_transaction(symbol="NNE", quantity=6.0, side=OrderDirection.BUY)
        create_trading_order(
            account_id=acct_def.id, symbol="NNE", quantity=6.0, side=OrderDirection.BUY,
            order_type=OrderType.MARKET, status=OrderStatus.FILLED, filled_qty=6.0,
            transaction_id=txn.id,
        )
        create_trading_order(
            account_id=acct_def.id, symbol="NNE", quantity=6.0, side=OrderDirection.SELL,
            order_type=OrderType.SELL_STOP_LIMIT, status=OrderStatus.CANCELED, filled_qty=4.0,
            transaction_id=txn.id,
        )

        account.refresh_transactions()

        fresh = get_instance(Transaction, txn.id)
        assert fresh.quantity == 2.0

    def test_canceled_zero_fill_does_not_count(self):
        """A CANCELED order that never filled (filled_qty falsy) must not
        contribute - this keeps the never-filled / rejected-leg case
        behaving exactly as before."""
        acct_def = create_account_definition()
        account = MockAccount(acct_def.id)
        txn = create_transaction(symbol="NNE", quantity=6.0, side=OrderDirection.BUY)
        create_trading_order(
            account_id=acct_def.id, symbol="NNE", quantity=6.0, side=OrderDirection.BUY,
            order_type=OrderType.MARKET, status=OrderStatus.FILLED, filled_qty=6.0,
            transaction_id=txn.id,
        )
        create_trading_order(
            account_id=acct_def.id, symbol="NNE", quantity=6.0, side=OrderDirection.SELL,
            order_type=OrderType.SELL_STOP_LIMIT, status=OrderStatus.CANCELED, filled_qty=0.0,
            transaction_id=txn.id,
        )

        account.refresh_transactions()

        fresh = get_instance(Transaction, txn.id)
        assert fresh.quantity == 6.0
