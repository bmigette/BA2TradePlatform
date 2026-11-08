"""A PARTIALLY_FILLED protective OCO is still working at the broker: a TP/SL adjust cancels it.

Dev 8080, 2026-11-05/06 (CELH, transaction 72). The take-profit leg of a 52-share OCO filled
29; Alpaca cancelled the stop leg and kept the OCO working for the other 23 (status
partially_filled, holding them). Every later TP/SL adjust skipped that OCO when collecting
the exits to replace -- it filtered on ``get_executed_statuses()``, which includes
PARTIALLY_FILLED -- so it never cancelled it and submitted a fresh 23-share OCO that Alpaca
rejected with 40310000 insufficient qty. The 23 shares stayed with a take-profit and NO stop.

The OCO must be treated as live protection: cancelled, with the replacement (sized on the
23 still held) chained on that cancel. Same rule in the shared ``protective_legs`` copy.
"""
from datetime import datetime, timedelta, timezone

from ba2_trade_platform.core.db import get_instance, update_instance
from ba2_trade_platform.core.models import TradingOrder, Transaction
from ba2_trade_platform.core.types import OrderDirection, OrderStatus, OrderType, TransactionStatus
from tests.factories import create_account_definition, create_trading_order, create_transaction

T0 = datetime(2026, 9, 17, 13, 40, tzinfo=timezone.utc)
TP, SL = 32.52, 24.95


def _account(monkeypatch, definition):
    from ba2_trade_platform.modules.accounts.AlpacaAccount import AlpacaAccount

    acct = object.__new__(AlpacaAccount)
    acct.id = definition.id
    cancels, submits = [], []

    def fake_cancel(order_id):
        cancels.append(int(order_id))
        o = get_instance(TradingOrder, int(order_id))
        o.status = OrderStatus.PENDING_CANCEL
        update_instance(o)
        return True

    def fake_submit(order, **kwargs):
        submits.append(order.id)
        return order

    monkeypatch.setattr(acct, "cancel_order", fake_cancel)
    monkeypatch.setattr(acct, "submit_order", fake_submit)
    monkeypatch.setattr(acct, "get_instrument_current_price", lambda *a, **k: 28.0)
    monkeypatch.setattr(acct, "_margin_enabled", lambda: False)
    return acct, cancels, submits


def _celh_rows(definition):
    from ba2_common.core.TransactionHelper import TransactionHelper

    txn = create_transaction(symbol="CELH", side=OrderDirection.BUY, quantity=23.0,
                             open_price=28.35, take_profit=TP, stop_loss=SL,
                             status=TransactionStatus.OPENED)
    entry = create_trading_order(account_id=definition.id, transaction_id=txn.id, symbol="CELH",
                                 side=OrderDirection.BUY, quantity=52.0,
                                 order_type=OrderType.MARKET, status=OrderStatus.FILLED,
                                 filled_qty=52.0, open_price=28.35, broker_order_id="b-entry",
                                 created_at=T0)
    oco = create_trading_order(account_id=definition.id, transaction_id=txn.id, symbol="CELH",
                               side=OrderDirection.SELL, quantity=52.0, order_type=OrderType.OCO,
                               status=OrderStatus.PARTIALLY_FILLED, filled_qty=29.0,
                               limit_price=TP, stop_price=SL, broker_order_id="b-oco",
                               comment=TransactionHelper.tpsl_comment(
                                   "TPSL", definition.id, txn.id, entry.id),
                               created_at=T0 + timedelta(days=11))
    return txn, oco


def _working_exits(txn_id):
    from ba2_common.core.trade_store import orders_where

    return sorted(
        (o.order_type, o.quantity, o.status, o.depends_on_order, o.depends_order_status_trigger)
        for o in orders_where(transaction_id=txn_id)
        if o.side == OrderDirection.SELL
        and o.status not in OrderStatus.get_terminal_statuses())


def test_a_partly_filled_oco_is_cancelled_and_the_remainder_restaged(monkeypatch):
    definition = create_account_definition()
    txn, oco = _celh_rows(definition)
    acct, cancels, submits = _account(monkeypatch, definition)

    assert acct.adjust_tp_sl(get_instance(Transaction, txn.id), TP, SL, source="ruleset") is True

    # The working OCO is cancelled; no inline submit that Alpaca would reject while the OCO
    # still holds the 23 shares.
    assert cancels == [oco.id]
    assert submits == []
    # The replacement covers the 23 still held (52 bought - 29 sold), chained on the cancel.
    assert _working_exits(txn.id) == sorted([
        (OrderType.OCO, 52.0, OrderStatus.PENDING_CANCEL, None, None),
        (OrderType.OCO, 23.0, OrderStatus.WAITING_TRIGGER, oco.id, OrderStatus.CANCELED),
    ])
