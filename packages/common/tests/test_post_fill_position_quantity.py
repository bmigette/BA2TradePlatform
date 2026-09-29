"""``TransactionHelper.post_fill_position_quantity``: what a protective exit must cover.

Not ``transaction.quantity`` (the gross entry size: a trim never lowers it, an add raises it
before it fills) -- the net filled position plus every WORKING non-protection order's unfilled
remainder, in the position's direction. Review finding C1, 2026-09-29.
"""
import pytest

from ba2_common.core.TransactionHelper import TransactionHelper
from ba2_common.core.models import TradingOrder, Transaction
from ba2_common.core.types import OrderDirection, OrderStatus, OrderType

BUY, SELL = OrderDirection.BUY, OrderDirection.SELL


def _o(side, qty, status, order_type=OrderType.MARKET, filled=None, comment=None, oid=1):
    return TradingOrder(id=oid, account_id=1, symbol="AAA", side=side, quantity=qty,
                        order_type=order_type, status=status, filled_qty=filled, comment=comment)


def _qty(orders, side=BUY):
    return TransactionHelper.post_fill_position_quantity(Transaction(id=9), side, orders)


ENTRY = _o(BUY, 10, OrderStatus.FILLED, filled=10)
MARKED_SL = "20260929120000-SL-[ACC:1/TR:9/PORD:1]"


def test_a_working_trim_and_a_filled_trim_both_leave_the_remainder():
    assert _qty([ENTRY, _o(SELL, 6, OrderStatus.NEW)]) == 4
    assert _qty([ENTRY, _o(SELL, 6, OrderStatus.FILLED, filled=6)]) == 4


def test_a_working_add_counts_before_it_fills():
    assert _qty([ENTRY, _o(BUY, 5, OrderStatus.ACCEPTED)]) == 15
    assert _qty([ENTRY, _o(BUY, 5, OrderStatus.PARTIALLY_FILLED, filled=2)]) == 15


def test_resting_protection_does_not_move_the_position_until_it_fires():
    sl = _o(SELL, 10, OrderStatus.NEW, OrderType.SELL_STOP, comment=MARKED_SL)
    assert _qty([ENTRY, sl]) == 10
    fired = _o(SELL, 10, OrderStatus.FILLED, OrderType.SELL_STOP, filled=10, comment=MARKED_SL)
    assert _qty([ENTRY, fired]) == 0


def test_dead_orders_count_for_nothing():
    assert _qty([ENTRY, _o(SELL, 6, OrderStatus.CANCELED), _o(BUY, 5, OrderStatus.REJECTED)]) == 10


def test_a_short_is_sized_in_its_own_direction():
    entry = _o(SELL, 10, OrderStatus.FILLED, filled=10)
    assert _qty([entry, _o(BUY, 4, OrderStatus.NEW)], side=SELL) == 6


def test_an_executed_order_with_no_filled_qty_is_unmeasurable():
    with pytest.raises(ValueError, match="unmeasurable"):
        _qty([_o(BUY, 10, OrderStatus.FILLED, filled=None)])


def test_a_partially_filled_working_order_without_its_fill_is_unmeasurable():
    with pytest.raises(ValueError, match="unmeasurable"):
        _qty([ENTRY, _o(BUY, 5, OrderStatus.PARTIALLY_FILLED, filled=None)])
