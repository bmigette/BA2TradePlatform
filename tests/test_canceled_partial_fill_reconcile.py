"""Tests for TransactionHelper.reconcile_canceled_partial_fill.

A cancel-and-replace (TP/SL rebase) can race a live fill: the broker executes
part of the order before honoring the cancel, leaving the order CANCELED with
filled_qty > 0. Those shares really traded, so the transaction must shrink by
the filled amount — otherwise the book over-counts the position (the NNE
"Quantity Mismatch: broker +2 / transactions +6" incident of 2026-06-12).
"""
import pytest

import ba2_common.core.TransactionHelper as transaction_helper_module
from ba2_common.core.utils import calculate_transaction_pnl
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
           order_data=None, with_dependent=True, txn_side=OrderDirection.BUY,
           fill_price=21.85, txn_kwargs=None):
    """A long 6-share position whose OCO SL sold `filled_qty` then got canceled.

    ``txn_side=SELL`` (with ``order_side=BUY``) builds the mirrored short.
    ``fill_price`` is the canceled order's average fill price (``open_price``).
    """
    close_side = OrderDirection.SELL if txn_side == OrderDirection.BUY else OrderDirection.BUY
    stop_type = OrderType.SELL_STOP if close_side == OrderDirection.SELL else OrderType.BUY_STOP
    acct = create_account_definition()
    txn = create_transaction(symbol="NNE", quantity=txn_qty, side=txn_side,
                             status=txn_status, open_price=23.68, **(txn_kwargs or {}))
    entry = create_trading_order(
        account_id=acct.id, symbol="NNE", quantity=txn_qty, side=txn_side,
        order_type=OrderType.MARKET, status=OrderStatus.FILLED,
        filled_qty=txn_qty, transaction_id=txn.id,
    )
    canceled_sl = create_trading_order(
        account_id=acct.id, symbol="NNE", quantity=txn_qty, side=order_side,
        order_type=stop_type, status=order_status,
        filled_qty=filled_qty, open_price=fill_price, stop_price=21.50,
        transaction_id=txn.id, data=order_data,
    )
    dependent = None
    if with_dependent:
        dependent = create_trading_order(
            account_id=acct.id, symbol="NNE", quantity=txn_qty, side=close_side,
            order_type=stop_type, status=OrderStatus.WAITING_TRIGGER,
            transaction_id=txn.id, depends_on_order=entry.id,
            depends_order_status_trigger=OrderStatus.FILLED,
        )
    return txn, entry, canceled_sl, dependent


class _LoggerSpy:
    """Stands in for the module logger: ba2 loggers have propagate=False (no caplog)."""

    def __init__(self):
        self.records = []

    def _record(self, level):
        def log(msg, *args, **kwargs):
            self.records.append((level, str(msg)))
        return log

    def __getattr__(self, name):
        if name in ("debug", "info", "warning", "error", "critical", "exception"):
            return self._record(name)
        raise AttributeError(name)

    def messages(self, level):
        return [m for lvl, m in self.records if lvl == level]


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
        assert fresh.status == TransactionStatus.CLOSED
        assert fresh.close_price == pytest.approx(21.85)
        # The consumed size is KEPT (as every other close keeps it), so the realised
        # P&L is measurable: zeroing it booked every such close at P&L 0.0.
        assert fresh.quantity == pytest.approx(6.0)
        assert calculate_transaction_pnl(fresh) == pytest.approx((21.85 - 23.68) * 6.0)
        notes = fresh.meta_data["partial_fill_reconciliations"]
        assert notes[0]["qty_before"] == 6.0 and notes[0]["qty_after"] == 0.0
        assert get_instance(TradingOrder, sl.id).data["partial_fill_reconciled"] is True

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


class TestFullyConsumedPositionCloses:
    """A canceled closing order whose fill covers the whole remaining position."""

    def test_short_close_books_sign_correct_pnl(self):
        txn, _, buy_back, _ = _setup(filled_qty=6.0, txn_side=OrderDirection.SELL,
                                     order_side=OrderDirection.BUY, fill_price=25.00)
        assert TransactionHelper.reconcile_canceled_partial_fill(buy_back) is True
        fresh = get_instance(Transaction, txn.id)
        assert fresh.status == TransactionStatus.CLOSED
        assert fresh.close_price == pytest.approx(25.00)
        assert fresh.quantity == pytest.approx(6.0)
        assert calculate_transaction_pnl(fresh) == pytest.approx((23.68 - 25.00) * 6.0)

    def test_pnl_uses_the_contract_multiplier(self):
        txn, _, sl, _ = _setup(filled_qty=6.0, txn_kwargs={"multiplier": 100})
        assert TransactionHelper.reconcile_canceled_partial_fill(sl) is True
        fresh = get_instance(Transaction, txn.id)
        assert calculate_transaction_pnl(fresh) == pytest.approx((21.85 - 23.68) * 6.0 * 100)

    def test_overfill_books_only_the_remaining_position(self):
        # Prior reconciliations already took the row down to 2; this fill reports 6.
        txn, _, sl, _ = _setup(filled_qty=6.0, txn_qty=2.0)
        assert TransactionHelper.reconcile_canceled_partial_fill(sl) is True
        fresh = get_instance(Transaction, txn.id)
        assert fresh.status == TransactionStatus.CLOSED
        assert fresh.quantity == pytest.approx(2.0)
        assert calculate_transaction_pnl(fresh) == pytest.approx((21.85 - 23.68) * 2.0)

    def test_close_goes_through_close_transaction_with_logging(self, monkeypatch):
        import ba2_common.core.db as db_module
        activities = []
        monkeypatch.setattr(db_module, "log_activity",
                            lambda **kwargs: activities.append(kwargs))
        txn, _, sl, _ = _setup(filled_qty=6.0)
        assert TransactionHelper.reconcile_canceled_partial_fill(sl) is True
        closed = [a for a in activities if a["data"]["transaction_id"] == txn.id]
        assert len(closed) == 1
        assert closed[0]["data"]["profit_loss"] == pytest.approx((21.85 - 23.68) * 6.0)
        assert closed[0]["data"]["close_price"] == pytest.approx(21.85)
        assert closed[0]["source_account_id"] == sl.account_id
        assert get_instance(Transaction, txn.id).close_reason == closed[0]["data"]["close_reason"]

    def test_inactive_dependents_are_closed_not_resized_to_zero(self):
        txn, entry, sl, dep = _setup(filled_qty=6.0)
        fixed_exit = create_trading_order(
            account_id=entry.account_id, symbol="NNE", quantity=3.0,
            side=OrderDirection.SELL, order_type=OrderType.SELL_LIMIT,
            status=OrderStatus.PENDING, transaction_id=txn.id,
            depends_on_order=entry.id, depends_order_status_trigger=OrderStatus.FILLED,
            data={"fixed_quantity": True},
        )
        assert TransactionHelper.reconcile_canceled_partial_fill(sl) is True
        # Same terminal status AccountInterface.close_transaction gives unsent orders.
        for order_id, qty in ((dep.id, 6.0), (fixed_exit.id, 3.0)):
            row = get_instance(TradingOrder, order_id)
            assert row.status == OrderStatus.CLOSED
            assert row.quantity == pytest.approx(qty)

    def test_unknown_fill_price_does_not_close_at_an_invented_price(self, monkeypatch):
        spy = _LoggerSpy()
        monkeypatch.setattr(transaction_helper_module, "logger", spy)
        # The order carries a stop price but no average fill price.
        txn, _, sl, dep = _setup(filled_qty=6.0, fill_price=None)
        assert TransactionHelper.reconcile_canceled_partial_fill(sl) is False

        fresh = get_instance(Transaction, txn.id)
        assert fresh.status == TransactionStatus.OPENED
        assert fresh.close_price is None
        assert fresh.quantity == pytest.approx(6.0)
        assert not (fresh.meta_data or {}).get("partial_fill_reconciliations")
        assert not (get_instance(TradingOrder, sl.id).data or {}).get("partial_fill_reconciled")
        dep_row = get_instance(TradingOrder, dep.id)
        assert dep_row.status == OrderStatus.WAITING_TRIGGER
        assert dep_row.quantity == pytest.approx(6.0)
        errors = spy.messages("error")
        assert errors and any(f"order {sl.id}" in m for m in errors)

    def test_later_refresh_with_a_real_fill_price_completes_it(self, monkeypatch):
        monkeypatch.setattr(transaction_helper_module, "logger", _LoggerSpy())
        txn, _, sl, _ = _setup(filled_qty=6.0, fill_price=None)
        assert TransactionHelper.reconcile_canceled_partial_fill(sl) is False

        from ba2_trade_platform.core.db import update_instance
        row = get_instance(TradingOrder, sl.id)
        row.open_price = 21.85
        update_instance(row)

        assert TransactionHelper.reconcile_canceled_partial_fill(row) is True
        fresh = get_instance(Transaction, txn.id)
        assert fresh.status == TransactionStatus.CLOSED
        assert calculate_transaction_pnl(fresh) == pytest.approx((21.85 - 23.68) * 6.0)
