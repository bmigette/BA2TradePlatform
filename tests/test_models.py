"""Tests for model methods on Transaction, TradingOrder, MarketAnalysis."""
import pytest
from ba2_trade_platform.core.models import (
    Transaction, TradingOrder, MarketAnalysis,
)
from ba2_trade_platform.core.types import (
    OrderDirection, OrderType, OrderStatus, TransactionStatus,
    MarketAnalysisStatus, AnalysisUseCase,
)
from ba2_trade_platform.core.db import add_instance
from tests.factories import (
    create_account_definition, create_transaction, create_trading_order,
    create_expert_instance,
)


class TestTransactionAsString:
    def test_as_string_contains_key_fields(self):
        txn = Transaction(
            id=1, symbol="AAPL", quantity=10.0,
            status=TransactionStatus.OPENED,
            side=OrderDirection.BUY,
            open_price=150.0,
        )
        s = txn.as_string()
        assert "AAPL" in s
        assert "10.0" in s
        assert "OPENED" in s
        assert "150.0" in s

    def test_repr_equals_as_string(self):
        txn = Transaction(
            id=1, symbol="MSFT", quantity=5.0,
            status=TransactionStatus.OPENED,
            side=OrderDirection.BUY,
        )
        assert repr(txn) == txn.as_string()

    def test_str_equals_as_string(self):
        txn = Transaction(
            id=1, symbol="MSFT", quantity=5.0,
            status=TransactionStatus.OPENED,
            side=OrderDirection.BUY,
        )
        assert str(txn) == txn.as_string()


class TestTransactionGetCurrentOpenQty:
    def test_no_orders_returns_zero(self):
        acct_def = create_account_definition()
        txn = create_transaction(symbol="AAPL", quantity=10.0)
        assert txn.get_current_open_qty() == 0.0

    def test_with_filled_buy_order(self):
        acct_def = create_account_definition()
        txn = create_transaction(symbol="AAPL", quantity=10.0)
        create_trading_order(
            account_id=acct_def.id, symbol="AAPL", quantity=10.0,
            side=OrderDirection.BUY, status=OrderStatus.FILLED,
            transaction_id=txn.id, filled_qty=10.0,
        )
        assert txn.get_current_open_qty() == 10.0

    def test_with_filled_sell_order_subtracts(self):
        acct_def = create_account_definition()
        txn = create_transaction(symbol="AAPL", quantity=10.0)
        create_trading_order(
            account_id=acct_def.id, symbol="AAPL", quantity=10.0,
            side=OrderDirection.BUY, status=OrderStatus.FILLED,
            transaction_id=txn.id, filled_qty=10.0,
        )
        create_trading_order(
            account_id=acct_def.id, symbol="AAPL", quantity=5.0,
            side=OrderDirection.SELL, status=OrderStatus.FILLED,
            transaction_id=txn.id, filled_qty=5.0,
        )
        assert txn.get_current_open_qty() == 5.0

    def test_unfilled_orders_not_counted(self):
        acct_def = create_account_definition()
        txn = create_transaction(symbol="AAPL", quantity=10.0)
        create_trading_order(
            account_id=acct_def.id, symbol="AAPL", quantity=10.0,
            side=OrderDirection.BUY, status=OrderStatus.PENDING,
            transaction_id=txn.id, filled_qty=None,
        )
        assert txn.get_current_open_qty() == 0.0


class TestTradingOrderAsString:
    def test_as_string_contains_key_fields(self):
        order = TradingOrder(
            id=1, account_id=1, symbol="AAPL", quantity=10.0,
            side=OrderDirection.BUY, order_type=OrderType.MARKET,
            status=OrderStatus.PENDING,
        )
        s = order.as_string()
        assert "AAPL" in s
        assert "BUY" in s
        assert "PENDING" in s

    def test_repr_equals_as_string(self):
        order = TradingOrder(
            id=1, account_id=1, symbol="AAPL", quantity=10.0,
            side=OrderDirection.BUY, order_type=OrderType.MARKET,
            status=OrderStatus.PENDING,
        )
        assert repr(order) == order.as_string()


class TestMarketAnalysisStateDefault:
    def test_state_defaults_to_empty_dict_when_none(self):
        ma = MarketAnalysis(
            symbol="AAPL",
            expert_instance_id=1,
            status=MarketAnalysisStatus.PENDING,
            state=None,
        )
        assert ma.state == {}

    def test_state_preserves_value(self):
        ma = MarketAnalysis(
            symbol="AAPL",
            expert_instance_id=1,
            status=MarketAnalysisStatus.PENDING,
            state={"key": "value"},
        )
        assert ma.state == {"key": "value"}

    def test_state_defaults_without_explicit_none(self):
        ma = MarketAnalysis(
            symbol="AAPL",
            expert_instance_id=1,
            status=MarketAnalysisStatus.PENDING,
        )
        assert ma.state == {}
