"""Tests for TransactionHelper.reconcile_canceled_partial_fill.

A cancel-and-replace (TP/SL rebase) can race a live fill: the broker executes
part of the order before honoring the cancel, leaving the order CANCELED with
filled_qty > 0. Those shares really traded, so the transaction must shrink by
the filled amount — otherwise the book over-counts the position (the NNE
"Quantity Mismatch: broker +2 / transactions +6" incident of 2026-06-12).
"""
import pytest

from ba2_trade_platform.core.TransactionHelper import TransactionHelper
from ba2_trade_platform.core.db import get_instance
from ba2_trade_platform.core.models import Transaction, TradingOrder
from ba2_trade_platform.core.types import (
    OrderDirection, OrderStatus, OrderType, TransactionStatus,
)
from tests.factories import (
    create_account_definition,
    create_transaction,
    create_trading_order,
)


def _setup(filled_qty=4.0, txn_qty=6.0, order_status=OrderStatus.CANCELED,
           order_side=OrderDirection.SELL, txn_status=TransactionStatus.OPENED,
           order_data=None, with_dependent=True):
    """A long 6-share position whose OCO SL sold `filled_qty` then got canceled."""
    acct = create_account_definition()
    txn = create_transaction(symbol="NNE", quantity=txn_qty, side=OrderDirection.BUY,
                             status=txn_status, open_price=23.68)
    entry = create_trading_order(
        account_id=acct.id, symbol="NNE", quantity=txn_qty, side=OrderDirection.BUY,
        order_type=OrderType.MARKET, status=OrderStatus.FILLED,
        filled_qty=txn_qty, transaction_id=txn.id,
    )
    canceled_sl = create_trading_order(
        account_id=acct.id, symbol="NNE", quantity=txn_qty, side=order_side,
        order_type=OrderType.SELL_STOP, status=order_status,
        filled_qty=filled_qty, open_price=21.85, transaction_id=txn.id,
        data=order_data,
    )
    dependent = None
    if with_dependent:
        dependent = create_trading_order(
            account_id=acct.id, symbol="NNE", quantity=txn_qty, side=OrderDirection.SELL,
            order_type=OrderType.SELL_STOP, status=OrderStatus.WAITING_TRIGGER,
            transaction_id=txn.id, depends_on_order=entry.id,
            depends_order_status_trigger=OrderStatus.FILLED,
        )
    return txn, entry, canceled_sl, dependent


class TestReconcileCanceledPartialFill:
    def test_reduces_transaction_by_filled_qty(self):
        txn, _, sl, dep = _setup()
        assert TransactionHelper.reconcile_canceled_partial_fill(sl) is True
        fresh = get_instance(Transaction, txn.id)
        assert fresh.quantity == pytest.approx(2.0)
        assert fresh.status == TransactionStatus.OPENED
        # Replacement TP/SL resized so it can't oversell the remainder
        assert get_instance(TradingOrder, dep.id).quantity == pytest.approx(2.0)

    def test_records_audit_note(self):
        txn, _, sl, _ = _setup()
        TransactionHelper.reconcile_canceled_partial_fill(sl)
        fresh = get_instance(Transaction, txn.id)
        notes = (fresh.meta_data or {}).get("partial_fill_reconciliations")
        assert notes and notes[0]["filled_qty"] == 4.0
        assert notes[0]["qty_before"] == 6.0 and notes[0]["qty_after"] == 2.0

    def test_idempotent_second_call_is_noop(self):
        txn, _, sl, _ = _setup()
        assert TransactionHelper.reconcile_canceled_partial_fill(sl) is True
        sl_fresh = get_instance(TradingOrder, sl.id)
        assert TransactionHelper.reconcile_canceled_partial_fill(sl_fresh) is False
        assert get_instance(Transaction, txn.id).quantity == pytest.approx(2.0)

    def test_full_fill_before_cancel_closes_transaction(self):
        txn, _, sl, _ = _setup(filled_qty=6.0)
        assert TransactionHelper.reconcile_canceled_partial_fill(sl) is True
        fresh = get_instance(Transaction, txn.id)
        assert fresh.quantity == 0.0
        assert fresh.status == TransactionStatus.CLOSED
        assert fresh.close_price == pytest.approx(21.85)

    def test_noop_when_no_fill(self):
        txn, _, sl, _ = _setup(filled_qty=0.0)
        assert TransactionHelper.reconcile_canceled_partial_fill(sl) is False
        assert get_instance(Transaction, txn.id).quantity == pytest.approx(6.0)

    def test_noop_for_non_canceled_order(self):
        txn, _, sl, _ = _setup(order_status=OrderStatus.FILLED, filled_qty=6.0)
        assert TransactionHelper.reconcile_canceled_partial_fill(sl) is False

    def test_noop_for_entry_side_order(self):
        # Partially-filled canceled BUY (entry side) is a different case - untouched.
        txn, _, sl, _ = _setup(order_side=OrderDirection.BUY)
        assert TransactionHelper.reconcile_canceled_partial_fill(sl) is False
        assert get_instance(Transaction, txn.id).quantity == pytest.approx(6.0)

    def test_noop_for_closed_transaction(self):
        txn, _, sl, _ = _setup(txn_status=TransactionStatus.CLOSED)
        assert TransactionHelper.reconcile_canceled_partial_fill(sl) is False

    def test_fixed_quantity_dependent_not_resized(self):
        acct_txn = _setup(with_dependent=False)
        txn, entry, sl, _ = acct_txn
        partial_exit = create_trading_order(
            account_id=entry.account_id, symbol="NNE", quantity=3.0,
            side=OrderDirection.SELL, order_type=OrderType.SELL_LIMIT,
            status=OrderStatus.WAITING_TRIGGER, transaction_id=txn.id,
            depends_on_order=entry.id,
            depends_order_status_trigger=OrderStatus.FILLED,
            data={"fixed_quantity": True},
        )
        TransactionHelper.reconcile_canceled_partial_fill(sl)
        assert get_instance(TradingOrder, partial_exit.id).quantity == pytest.approx(3.0)
