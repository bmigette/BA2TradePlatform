"""
Tests for the ``fixed_quantity`` flag on TradingOrder.

A PennyMomentum stepped (partial) exit deliberately sells less than the whole
position. ``TransactionHelper.adjust_qty`` normally force-syncs a WAITING_TRIGGER
dependent order's quantity to the whole transaction quantity once the entry fills,
which would turn a partial exit into a full exit. A dependent order flagged
``data={"fixed_quantity": True}`` must be left untouched.
"""
import pytest

from ba2_trade_platform.core.TransactionHelper import TransactionHelper
from ba2_trade_platform.core.db import get_instance
from ba2_trade_platform.core.models import TradingOrder
from ba2_trade_platform.core.types import OrderDirection, OrderStatus, OrderType
from tests.factories import (
    create_account_definition,
    create_transaction,
    create_trading_order,
)


def _setup_partial_exit(fixed: bool):
    """Build a transaction with a filled entry BUY and a WAITING_TRIGGER partial SELL."""
    acct = create_account_definition()
    txn = create_transaction(symbol="BBAI", quantity=100.0, open_price=5.0)
    entry = create_trading_order(
        account_id=acct.id, symbol="BBAI", quantity=100.0,
        side=OrderDirection.BUY, order_type=OrderType.MARKET,
        status=OrderStatus.FILLED, filled_qty=100.0, transaction_id=txn.id,
    )
    sell = create_trading_order(
        account_id=acct.id, symbol="BBAI", quantity=50.0,
        side=OrderDirection.SELL, order_type=OrderType.MARKET,
        status=OrderStatus.WAITING_TRIGGER, transaction_id=txn.id,
        depends_on_order=entry.id,
        depends_order_status_trigger=OrderStatus.FILLED,
        data={"fixed_quantity": True} if fixed else None,
    )
    return txn, entry, sell


def test_adjust_qty_leaves_fixed_quantity_dependent_untouched():
    txn, entry, sell = _setup_partial_exit(fixed=True)

    TransactionHelper.adjust_qty(txn, new_quantity=80.0)

    # Entry is still resized to the transaction quantity...
    assert get_instance(TradingOrder, entry.id).quantity == 80.0
    # ...but the flagged partial-exit SELL keeps its deliberate 50 shares.
    assert get_instance(TradingOrder, sell.id).quantity == 50.0


def test_adjust_qty_resizes_unflagged_dependent():
    """Control: without the flag the dependent is still force-synced (existing behavior)."""
    txn, entry, sell = _setup_partial_exit(fixed=False)

    TransactionHelper.adjust_qty(txn, new_quantity=80.0)

    assert get_instance(TradingOrder, sell.id).quantity == 80.0
