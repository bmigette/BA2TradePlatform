"""A PARTIAL closing fill must not close a whole equity transaction.

``refresh_transactions``' ``tp_sl_filled`` arm reads::

    filled_closing_orders = [o for o in dependent_orders if o.status == FILLED]
    ...
    elif filled_closing_orders and transaction.status == OPENED and not option_contracts_still_open:
        close_transaction_with_logging(..., close_reason="tp_sl_filled")

``filled_closing_orders`` is "SOME dependent order on this transaction is FILLED". It
carries no quantity at all. So ANY filled dependent leg closes the entire row, however
little of it that leg actually sold, and the undelivered remainder becomes an untracked
position: the ledger says flat, the broker says 200 shares.

The concrete producer is a partial assignment. A covered call written on part of a larger
multi-lot equity holding is assigned, the platform books a closing fill for the delivered
100 shares against a 300-share transaction, and the whole 300-share row goes CLOSED.

DIFFERENT DEFECT FROM THE ONE `de4f0f0f` FIXED. That one is about a multi-LEG OPTION
transaction being closed by one leg settling, and its guard (``option_contracts_still_open``)
keys on OPTION CONTRACT nets. An EQUITY transaction has no option contract rows, so its net
is empty, ``every_option_contract_is_flat`` reads that as "nothing open" and the guard is
inert. Same branch, different axis.

The equity analogue of that per-contract net already exists three lines up and is already
computed on every pass: ``position_balanced = abs(total_filled_buy - total_filled_sell) <
0.0001``, over the same order rows and with the same CANCELED-with-a-partial-fill
compensation. No third definition of "is this position flat" is needed.
"""
import pytest

from ba2_trade_platform.core.db import get_instance
from ba2_trade_platform.core.models import Transaction
from ba2_trade_platform.core.types import (
    AssetClass, OrderDirection, OrderStatus, OrderType, TransactionStatus,
)
from tests.conftest import MockAccount
from tests.factories import (
    create_account_definition, create_trading_order, create_transaction,
)


@pytest.fixture
def account():
    acct_def = create_account_definition()
    return MockAccount(acct_def.id)


def _lot(account, *, held=300.0, delivered=100.0, symbol="AAPL"):
    """A 300-share equity transaction, of which ``delivered`` shares are sold by a filled
    DEPENDENT order — the shape a partial assignment produces.

    The split is the point of the fixture. With ``delivered == held`` every assertion below
    would hold for the wrong reason, which is how this class of test usually fails to bite.
    """
    txn = create_transaction(symbol=symbol, quantity=held, side=OrderDirection.BUY,
                             status=TransactionStatus.OPENED, open_price=150.0)
    entry = create_trading_order(
        account_id=account.id, symbol=symbol, quantity=held, side=OrderDirection.BUY,
        order_type=OrderType.MARKET, status=OrderStatus.FILLED, filled_qty=held,
        transaction_id=txn.id, open_price=150.0, asset_class=AssetClass.EQUITY)
    create_trading_order(
        account_id=account.id, symbol=symbol, quantity=delivered, side=OrderDirection.SELL,
        order_type=OrderType.MARKET, status=OrderStatus.FILLED, filled_qty=delivered,
        transaction_id=txn.id, open_price=160.0, asset_class=AssetClass.EQUITY,
        depends_on_order=entry.id)
    return txn


def _fresh(txn_id):
    return get_instance(Transaction, txn_id)


# ---------------------------------------------------------------------------

def test_a_partial_delivery_does_not_close_the_whole_transaction(account):
    """100 of 300 shares delivered. 200 are still held and still ours to manage."""
    txn = _lot(account, held=300.0, delivered=100.0)

    account.refresh_transactions()

    fresh = _fresh(txn.id)
    assert fresh.status == TransactionStatus.OPENED, (
        f"a 100-share closing fill closed a 300-share transaction as "
        f"{fresh.close_reason!r}; the 200 undelivered shares are now an untracked position")


def test_the_remainder_is_still_recorded_on_the_transaction(account):
    """Not merely 'still OPENED' — still OPENED for the RIGHT quantity."""
    txn = _lot(account, held=300.0, delivered=100.0)
    account.refresh_transactions()
    assert _fresh(txn.id).quantity == 200.0


def test_a_FULL_delivery_still_closes_the_transaction(account):
    """The control that stops the fix becoming 'never close on a dependent fill'.

    This is the everyday TP/SL close and it must be untouched.
    """
    txn = _lot(account, held=300.0, delivered=300.0)
    account.refresh_transactions()

    fresh = _fresh(txn.id)
    assert fresh.status == TransactionStatus.CLOSED
    assert fresh.close_reason == "tp_sl_filled"


def test_a_delivery_completed_across_two_fills_closes_the_transaction(account):
    """The remainder arriving later must close it — the arm is about the POSITION, not
    about how many orders it took."""
    txn = _lot(account, held=300.0, delivered=100.0)
    account.refresh_transactions()
    assert _fresh(txn.id).status == TransactionStatus.OPENED

    from sqlmodel import Session, select
    from ba2_trade_platform.core.db import get_db
    from ba2_trade_platform.core.models import TradingOrder
    with Session(get_db().bind) as s:
        entry = [o for o in s.exec(select(TradingOrder)).all()
                 if o.side == OrderDirection.BUY][0]
    create_trading_order(
        account_id=account.id, symbol="AAPL", quantity=200.0, side=OrderDirection.SELL,
        order_type=OrderType.MARKET, status=OrderStatus.FILLED, filled_qty=200.0,
        transaction_id=txn.id, open_price=161.0, asset_class=AssetClass.EQUITY,
        depends_on_order=entry.id)

    account.refresh_transactions()
    assert _fresh(txn.id).status == TransactionStatus.CLOSED


def test_an_over_delivery_still_closes_the_transaction(account):
    """More sold than the ledger recorded is a mismatch, not a reason to stay open.

    Holding it OPEN would strand a row that is certainly flat (or short) at the broker.
    """
    txn = _lot(account, held=300.0, delivered=400.0)
    account.refresh_transactions()
    assert _fresh(txn.id).status == TransactionStatus.CLOSED


def test_a_short_transaction_partially_covered_also_stays_open(account):
    """The mirror image: a SHORT covered in part is still short in part."""
    txn = create_transaction(symbol="AAPL", quantity=300.0, side=OrderDirection.SELL,
                             status=TransactionStatus.OPENED, open_price=150.0)
    entry = create_trading_order(
        account_id=account.id, symbol="AAPL", quantity=300.0, side=OrderDirection.SELL,
        order_type=OrderType.MARKET, status=OrderStatus.FILLED, filled_qty=300.0,
        transaction_id=txn.id, open_price=150.0, asset_class=AssetClass.EQUITY)
    create_trading_order(
        account_id=account.id, symbol="AAPL", quantity=100.0, side=OrderDirection.BUY,
        order_type=OrderType.MARKET, status=OrderStatus.FILLED, filled_qty=100.0,
        transaction_id=txn.id, open_price=140.0, asset_class=AssetClass.EQUITY,
        depends_on_order=entry.id)

    account.refresh_transactions()
    assert _fresh(txn.id).status == TransactionStatus.OPENED
    assert _fresh(txn.id).quantity == 200.0


def test_a_partially_filled_then_canceled_TP_leaves_the_remainder_open(account):
    """The cancel-that-raced-a-fill shape, at this branch.

    60 of 300 really traded; 240 are still held. The row must not close, and the shares
    that DID trade must still come off the quantity.
    """
    txn = create_transaction(symbol="AAPL", quantity=300.0, side=OrderDirection.BUY,
                             status=TransactionStatus.OPENED, open_price=150.0)
    entry = create_trading_order(
        account_id=account.id, symbol="AAPL", quantity=300.0, side=OrderDirection.BUY,
        order_type=OrderType.MARKET, status=OrderStatus.FILLED, filled_qty=300.0,
        transaction_id=txn.id, open_price=150.0, asset_class=AssetClass.EQUITY)
    create_trading_order(
        account_id=account.id, symbol="AAPL", quantity=300.0, side=OrderDirection.SELL,
        order_type=OrderType.SELL_STOP_LIMIT, status=OrderStatus.CANCELED, filled_qty=60.0,
        transaction_id=txn.id, open_price=145.0, asset_class=AssetClass.EQUITY,
        depends_on_order=entry.id)

    account.refresh_transactions()
    fresh = _fresh(txn.id)
    assert fresh.status == TransactionStatus.OPENED
    assert fresh.quantity == 240.0
