"""An order CANCELED after a partial fill still traded the shares it filled.

2026-10-29 RAPP (dev account 1): entry 1438 filled 34 of 35 shares and was then CANCELED. Three
readers treated "CANCELED" as "nothing filled":
  - refresh_transactions never opened transaction 432 and closed it as ``all_orders_terminal``;
  - the staged exit (OCO 1441, waiting for the entry's FILLED) was cancelled with it,
so the 34 shares were left with no transaction and no stop.
2026-10-28 PRG (dev account 1): stop 809 sold 14 of 15 shares and was then CANCELED by the
breached-stop close. ``get_current_open_qty`` skipped the CANCELED stop, so close order 1426 was
sized at 15 against the 1 share still held and Alpaca refused it (40310000).
"""
import pytest

from ba2_trade_platform.core.TradeManager import TradeManager, classify_waiting_trigger
from ba2_trade_platform.core.db import get_instance, update_instance
from ba2_trade_platform.core.models import TradingOrder, Transaction
from ba2_trade_platform.core.types import OrderDirection, OrderStatus, OrderType, TransactionStatus
from tests.conftest import MockAccount
from tests.factories import create_account_definition, create_trading_order, create_transaction


pytestmark = pytest.mark.usefixtures("reset_test_db")


def _partly_filled_entry(filled_qty, exit_status=OrderStatus.WAITING_TRIGGER):
    definition = create_account_definition()
    txn = create_transaction(symbol="RAPP", side=OrderDirection.BUY, quantity=35.0,
                             stop_loss=19.85, take_profit=26.86, status=TransactionStatus.WAITING)
    txn.open_date = None  # never opened: the factory stamps an open date and price
    txn.open_price = None
    update_instance(txn)
    entry =create_trading_order(account_id=definition.id, symbol="RAPP", quantity=35.0,
                                 side=OrderDirection.BUY, order_type=OrderType.MARKET,
                                 status=OrderStatus.CANCELED, filled_qty=filled_qty,
                                 open_price=23.3554 if filled_qty else None,
                                 transaction_id=txn.id, broker_order_id="brk-entry")
    exit_order = create_trading_order(
        account_id=definition.id, symbol="RAPP", quantity=35.0, side=OrderDirection.SELL,
        order_type=OrderType.OCO, status=exit_status, transaction_id=txn.id,
        limit_price=26.86, stop_price=19.85, depends_on_order=entry.id,
        depends_order_status_trigger=OrderStatus.FILLED, data={})
    return definition, txn, entry, exit_order


def test_classify_a_canceled_parent_that_filled_part_submits():
    assert classify_waiting_trigger(OrderStatus.CANCELED, OrderStatus.FILLED, 34.0) == "submit"
    assert classify_waiting_trigger(OrderStatus.EXPIRED, OrderStatus.FILLED, 34.0) == "submit"
    assert classify_waiting_trigger(OrderStatus.CANCELED, OrderStatus.FILLED, 0.0) == "cancel"
    assert classify_waiting_trigger(OrderStatus.CANCELED, OrderStatus.FILLED, None) == "cancel"
    # A replacement chained on a CANCELED trigger is unaffected by what the parent filled.
    assert classify_waiting_trigger(OrderStatus.CANCELED, OrderStatus.CANCELED, 14.0) == "submit"


def test_the_staged_exit_protects_the_filled_shares(monkeypatch):
    import ba2_trade_platform.modules.accounts as accounts
    definition, txn, entry, exit_order = _partly_filled_entry(34.0)
    submitted = []

    class Account(MockAccount):
        def get_available_position_quantity(self, symbol):
            return 34.0

        def submit_order(self, order, is_closing_order=False):
            submitted.append((order.id, order.quantity))
            return super().submit_order(order, is_closing_order)

    monkeypatch.setattr(accounts, "get_account_class", lambda provider: Account)
    TradeManager()._check_all_waiting_trigger_orders()

    assert submitted == [(exit_order.id, 34.0)]


def test_the_staged_exit_of_an_entry_that_filled_nothing_is_still_cancelled(monkeypatch):
    import ba2_trade_platform.modules.accounts as accounts
    definition, txn, entry, exit_order = _partly_filled_entry(0.0)

    class Account(MockAccount):
        def submit_order(self, order, is_closing_order=False):
            raise AssertionError("nothing filled, nothing to protect")

    monkeypatch.setattr(accounts, "get_account_class", lambda provider: Account)
    TradeManager()._check_all_waiting_trigger_orders()

    assert get_instance(TradingOrder, exit_order.id).status == OrderStatus.CANCELED


def test_refresh_opens_the_transaction_at_the_filled_quantity():
    definition, txn, entry, exit_order = _partly_filled_entry(34.0, exit_status=OrderStatus.CANCELED)

    MockAccount(definition.id).refresh_transactions()

    fresh = get_instance(Transaction, txn.id)
    assert fresh.status == TransactionStatus.OPENED, (
        f"34 shares are held; closing the row ({fresh.close_reason!r}) leaves them untracked")
    assert fresh.quantity == 34.0
    assert fresh.open_price == 23.3554
    assert fresh.open_date is not None
    assert fresh.close_reason is None


def test_refresh_still_deletes_an_entry_that_filled_nothing():
    definition, txn, entry, exit_order = _partly_filled_entry(0.0, exit_status=OrderStatus.CANCELED)

    MockAccount(definition.id).refresh_transactions()

    from ba2_common.core.db import InstanceNotFound
    with pytest.raises(InstanceNotFound):
        get_instance(Transaction, txn.id)


def test_open_qty_counts_what_a_canceled_exit_sold():
    definition = create_account_definition()
    txn = create_transaction(symbol="PRG", side=OrderDirection.BUY, quantity=15.0,
                             status=TransactionStatus.OPENED)
    entry = create_trading_order(account_id=definition.id, symbol="PRG", quantity=15.0,
                                 side=OrderDirection.BUY, order_type=OrderType.MARKET,
                                 status=OrderStatus.FILLED, filled_qty=15.0, transaction_id=txn.id)
    create_trading_order(account_id=definition.id, symbol="PRG", quantity=15.0,
                         side=OrderDirection.SELL, order_type=OrderType.SELL_STOP,
                         status=OrderStatus.CANCELED, filled_qty=14.0, transaction_id=txn.id,
                         depends_on_order=entry.id)

    assert get_instance(Transaction, txn.id).get_current_open_qty() == 1.0
