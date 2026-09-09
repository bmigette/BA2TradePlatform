"""Live entry/retry stops follow the existing backtest tighter-wins policy."""
import pytest

from ba2_trade_platform.core.TradeManager import TradeManager
from ba2_trade_platform.core.db import get_instance, update_instance
from ba2_trade_platform.core.models import TradingOrder, Transaction
from ba2_trade_platform.core.types import OrderDirection, OrderStatus, OrderType, TransactionStatus
from tests.conftest import MockAccount
from tests.factories import create_account_definition, create_trading_order, create_transaction


pytestmark = pytest.mark.usefixtures("reset_test_db")


def _entry(side=OrderDirection.BUY, ruleset_sl=96.0, safeguard=90.0,
           status=OrderStatus.PENDING):
    account = create_account_definition()
    txn = create_transaction(side=side, stop_loss=ruleset_sl, open_price=100.0,
                             status=TransactionStatus.WAITING)
    order = create_trading_order(account_id=account.id, side=side, quantity=14.0,
                                 transaction_id=txn.id, order_type=OrderType.MARKET,
                                 status=status, stop_price=safeguard)
    return account, txn, order


@pytest.mark.parametrize("side,ruleset,safeguard,expected", [
    (OrderDirection.BUY, 96.0, 90.0, 96.0),
    (OrderDirection.SELL, 104.0, 110.0, 104.0),
    (OrderDirection.BUY, 82.0, 90.0, 90.0),
    (OrderDirection.SELL, 118.0, 110.0, 110.0),
    (OrderDirection.BUY, None, 90.0, 90.0),  # ED: safeguard only at entry.
    (OrderDirection.BUY, 96.0, None, None),  # Existing ruleset leg remains attached.
    (OrderDirection.BUY, None, None, None),
    (OrderDirection.BUY, 19.1424, 17.95, 19.1424),  # ENOV production discrepancy.
])
def test_funded_entry_keeps_the_same_stop_as_backtest(side, ruleset, safeguard, expected):
    from unittest.mock import Mock
    _, _, order = _entry(side, ruleset, safeguard)
    account = Mock()
    account.submit_order.return_value = order
    TradeManager()._submit_funded_entry_with_retry(account, order, sl_price=safeguard)
    account.submit_order.assert_called_once_with(order, sl_price=expected)
    assert order.quantity == 14.0
    assert order.stop_price == safeguard  # RM sizing input is not rewritten.


def test_db_lock_retry_reads_the_latest_ruleset_stop(monkeypatch):
    _, txn, order = _entry()
    stops = []

    class Account:
        def submit_order(self, order, sl_price=None):
            stops.append(sl_price)
            if len(stops) == 1:
                current = get_instance(Transaction, txn.id)
                current.stop_loss = 98.0
                update_instance(current)
                raise RuntimeError("database is locked")
            return order

    monkeypatch.setattr(TradeManager, "_ENTRY_SUBMIT_BACKOFF_S", 0)
    assert TradeManager()._submit_funded_entry_with_retry(Account(), order, 90.0)
    assert stops == [96.0, 98.0]


@pytest.mark.parametrize("closing,order_type,expected", [
    (False, OrderType.MARKET, 96.0),
    (True, OrderType.MARKET, None),
    (False, OrderType.BUY_STOP, None),  # A stop-entry trigger is not an exit stop.
])
def test_washtrade_retry_reconciles_only_market_entries(monkeypatch, closing, order_type, expected):
    import ba2_trade_platform.modules.accounts as accounts
    _, txn, order = _entry(status=OrderStatus.WASHTRADE_LOCKED)
    order.order_type = order_type
    if closing:
        order.side = OrderDirection.SELL
    update_instance(order)
    calls = []

    class Account(MockAccount):
        def submit_order(self, submitted, tp_price=None, sl_price=None, is_closing_order=False):
            calls.append((sl_price, is_closing_order))
            return submitted

    monkeypatch.setattr(accounts, "get_account_class", lambda provider: Account)
    TradeManager()._check_all_washtrade_locked_orders()
    assert calls == [(expected, closing)]


def test_real_alpaca_bracket_keeps_ruleset_stop_through_submit_and_fill(monkeypatch):
    """Real DB, Alpaca bracket maintenance and fill rebase; only broker IO is faked."""
    import ba2_trade_platform.modules.accounts as accounts
    from ba2_trade_platform.modules.accounts.AlpacaAccount import AlpacaAccount
    from ba2_common.core.trade_store import orders_where

    definition, txn, order = _entry()
    # No fill yet: establish a pre-fill reference, as the evaluator does.
    order.open_price = None
    update_instance(order)
    account = object.__new__(AlpacaAccount)
    account.id = definition.id
    monkeypatch.setattr(account, "get_instrument_current_price", lambda *a, **k: 100.0)
    monkeypatch.setattr(account, "_margin_enabled", lambda: False)
    submitted_stops = []

    def fake_broker(entry, **kwargs):
        submitted_stops.append(kwargs["sl_price"])
        entry.status = OrderStatus.NEW
        update_instance(entry)
        return get_instance(TradingOrder, entry.id)

    monkeypatch.setattr(account, "_submit_order_impl", fake_broker)
    assert account.adjust_tp_sl(txn, 120.0, 96.0, source="ruleset")
    TradeManager()._submit_funded_entry_with_retry(account, order, 90.0)
    assert submitted_stops == [96.0]
    assert get_instance(Transaction, txn.id).stop_loss == 96.0
    legs = orders_where(transaction_id=txn.id, depends_on_order=order.id)
    assert len(legs) == 1
    assert legs[0].stop_price == 96.0
    assert legs[0].limit_price == 120.0

    filled = get_instance(TradingOrder, order.id)
    filled.status = OrderStatus.FILLED
    filled.open_price = 101.0
    filled.filled_qty = filled.quantity
    update_instance(filled)
    monkeypatch.setattr(accounts, "get_account_class", lambda provider: MockAccount)
    TradeManager()._check_all_waiting_trigger_orders()
    leg = get_instance(TradingOrder, legs[0].id)
    assert leg.stop_price == pytest.approx(96.96)  # Still 4% from the actual fill.
    assert leg.quantity == 14.0
