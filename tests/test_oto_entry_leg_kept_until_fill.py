"""An entry sent as an OTO (the wash-trade escape) keeps its broker leg until it fills.

2026-10-29 RAPP (dev account 1): entry 1438 went out as an OTO with a stop leg. The TP/SL step
cancelled that leg a second later to stage its own OCO; cancelling an OTO leg cancels the
unfilled entry at Alpaca, which had filled 34 of 35 shares, and the 34 were left untracked.
2026-10-22 DY / APLD: an exit that fired on the entry's fill was refused (40310000) because the
entry's legs still held the shares.
"""
import pytest

from ba2_trade_platform.core.TradeManager import TradeManager
from ba2_trade_platform.core.db import get_db, get_instance, update_instance
from ba2_trade_platform.core.models import TradingOrder, Transaction
from ba2_trade_platform.core.types import OrderDirection, OrderStatus, OrderType, TransactionStatus
from tests.conftest import MockAccount
from tests.factories import create_account_definition, create_trading_order, create_transaction


pytestmark = pytest.mark.usefixtures("reset_test_db")


def _oto_entry(entry_status):
    account = create_account_definition()
    txn = create_transaction(symbol="RAPP", side=OrderDirection.BUY, quantity=35.0, open_price=23.36,
                             stop_loss=19.85, take_profit=26.86, status=TransactionStatus.WAITING)
    entry = create_trading_order(account_id=account.id, symbol="RAPP", quantity=35.0,
                                 side=OrderDirection.BUY, order_type=OrderType.MARKET,
                                 status=entry_status, transaction_id=txn.id, broker_order_id="brk-entry",
                                 open_price=23.30 if entry_status == OrderStatus.FILLED else None)
    leg = create_trading_order(account_id=account.id, symbol="RAPP", quantity=35.0,
                               side=OrderDirection.SELL, order_type=OrderType.SELL_STOP,
                               status=OrderStatus.NEW, transaction_id=txn.id, stop_price=19.85,
                               broker_order_id="brk-leg", parent_order_id=entry.id)
    return account, txn, entry, leg


def test_unfilled_entry_keeps_its_own_oto_leg_and_stages_the_exit(monkeypatch):
    from ba2_trade_platform.modules.accounts.AlpacaAccount import AlpacaAccount
    definition, txn, entry, leg = _oto_entry(OrderStatus.NEW)
    account = object.__new__(AlpacaAccount)
    account.id = definition.id
    cancelled = []
    monkeypatch.setattr(account, "cancel_order", lambda order_id: cancelled.append(order_id) or True)
    monkeypatch.setattr(account, "get_instrument_current_price", lambda *a, **k: 23.36)

    with get_db() as session:
        t = session.get(Transaction, txn.id)
        e = session.get(TradingOrder, entry.id)
        orders = [session.get(TradingOrder, leg.id)]
        assert account._handle_unfilled_entry_exit(
            session, t, e, (OrderType.OCO, 26.86, 19.85, "TPSL"), orders)

    assert cancelled == [], "cancelling the OTO leg cancels the unfilled entry at Alpaca"
    assert get_instance(TradingOrder, leg.id).status == OrderStatus.NEW
    with get_db() as session:
        from sqlmodel import select
        staged = session.exec(select(TradingOrder).where(
            TradingOrder.depends_on_order == entry.id,
            TradingOrder.status == OrderStatus.WAITING_TRIGGER)).all()
    assert [(o.order_type, o.depends_order_status_trigger) for o in staged] == [
        (OrderType.OCO, OrderStatus.FILLED)]


def test_exit_firing_on_the_fill_waits_for_the_legs_cancel_then_submits(monkeypatch):
    import ba2_trade_platform.modules.accounts as accounts
    definition, txn, entry, leg = _oto_entry(OrderStatus.FILLED)
    exit_order = create_trading_order(
        account_id=definition.id, symbol="RAPP", quantity=35.0, side=OrderDirection.SELL,
        order_type=OrderType.OCO, status=OrderStatus.WAITING_TRIGGER, transaction_id=txn.id,
        limit_price=26.86, stop_price=19.85, depends_on_order=entry.id,
        depends_order_status_trigger=OrderStatus.FILLED, data={})
    cancelled, submitted = [], []

    class Account(MockAccount):
        def cancel_order(self, order_id):
            cancelled.append(order_id)
            return True

        def get_available_position_quantity(self, symbol):
            return 0.0 if get_instance(TradingOrder, leg.id).status == OrderStatus.NEW else 35.0

        def submit_order(self, order, is_closing_order=False):
            submitted.append(order.id)
            return super().submit_order(order, is_closing_order)

    monkeypatch.setattr(accounts, "get_account_class", lambda provider: Account)

    TradeManager()._check_all_waiting_trigger_orders()
    waiting = get_instance(TradingOrder, exit_order.id)
    assert cancelled == [leg.id] and submitted == []
    assert waiting.status == OrderStatus.WAITING_TRIGGER
    assert (waiting.depends_on_order, waiting.depends_order_status_trigger) == (leg.id, OrderStatus.CANCELED)

    released = get_instance(TradingOrder, leg.id)
    released.status = OrderStatus.CANCELED
    update_instance(released)
    TradeManager()._check_all_waiting_trigger_orders()
    assert submitted == [exit_order.id]


def test_exit_without_entry_legs_submits_on_the_fill_as_before(monkeypatch):
    import ba2_trade_platform.modules.accounts as accounts
    definition = create_account_definition()
    entry = create_trading_order(account_id=definition.id, symbol="RAPP", quantity=10.0,
                                 order_type=OrderType.MARKET, status=OrderStatus.FILLED, open_price=23.0)
    exit_order = create_trading_order(
        account_id=definition.id, symbol="RAPP", quantity=10.0, side=OrderDirection.SELL,
        order_type=OrderType.SELL_STOP, status=OrderStatus.WAITING_TRIGGER, stop_price=20.0,
        depends_on_order=entry.id, depends_order_status_trigger=OrderStatus.FILLED, data={})
    submitted = []

    class Account(MockAccount):
        def cancel_order(self, order_id):
            raise AssertionError("nothing to cancel")

        def submit_order(self, order, is_closing_order=False):
            submitted.append(order.id)
            return super().submit_order(order, is_closing_order)

    monkeypatch.setattr(accounts, "get_account_class", lambda provider: Account)
    TradeManager()._check_all_waiting_trigger_orders()
    assert submitted == [exit_order.id]
