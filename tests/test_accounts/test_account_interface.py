"""Tests for AccountInterface non-abstract methods via MockAccount."""
import pytest
from tests.conftest import MockAccount
from tests.factories import create_account_definition
from ba2_trade_platform.core.models import TradingOrder
from ba2_trade_platform.core.types import OrderStatus, OrderDirection, OrderType


class TestMockAccountBasics:
    def test_get_balance(self):
        acct_def = create_account_definition()
        account = MockAccount(acct_def.id)
        assert account.get_balance() == 100_000.0

    def test_get_positions_empty(self):
        acct_def = create_account_definition()
        account = MockAccount(acct_def.id)
        assert account.get_positions() == []

    def test_get_orders_empty(self):
        acct_def = create_account_definition()
        account = MockAccount(acct_def.id)
        assert account.get_orders() == []

    def test_get_account_info(self):
        acct_def = create_account_definition()
        account = MockAccount(acct_def.id)
        info = account.get_account_info()
        assert "balance" in info
        assert info["balance"] == 100_000.0


class TestSubmitOrder:
    def test_submit_order_fills(self):
        acct_def = create_account_definition()
        account = MockAccount(acct_def.id)
        order = TradingOrder(
            account_id=acct_def.id, symbol="AAPL", quantity=10.0,
            side=OrderDirection.BUY, order_type=OrderType.MARKET,
            status=OrderStatus.PENDING,
        )
        result = account.submit_order(order)
        assert result is not None
        assert result.status == OrderStatus.FILLED

    def test_submit_order_fails_when_disabled(self):
        acct_def = create_account_definition()
        account = MockAccount(acct_def.id)
        account._submit_order_result = False
        order = TradingOrder(
            account_id=acct_def.id, symbol="AAPL", quantity=10.0,
            side=OrderDirection.BUY, order_type=OrderType.MARKET,
            status=OrderStatus.PENDING,
        )
        result = account.submit_order(order)
        assert result is None


class TestCancelOrder:
    def test_cancel_order_sets_canceled(self):
        acct_def = create_account_definition()
        account = MockAccount(acct_def.id)
        order = TradingOrder(
            account_id=acct_def.id, symbol="AAPL", quantity=10.0,
            side=OrderDirection.BUY, order_type=OrderType.MARKET,
            status=OrderStatus.OPEN,
        )
        result = account.cancel_order(order)
        assert result.status == OrderStatus.CANCELED


class TestSymbolsExist:
    def test_all_symbols_exist(self):
        acct_def = create_account_definition()
        account = MockAccount(acct_def.id)
        result = account.symbols_exist(["AAPL", "MSFT"])
        assert result == {"AAPL": True, "MSFT": True}


class TestGetInstrumentPrice:
    def test_known_symbol_returns_price(self):
        acct_def = create_account_definition()
        account = MockAccount(acct_def.id)
        price = account._get_instrument_current_price_impl("AAPL")
        assert price == 150.0

    def test_unknown_symbol_returns_none(self):
        acct_def = create_account_definition()
        account = MockAccount(acct_def.id)
        price = account._get_instrument_current_price_impl("UNKNOWN")
        assert price is None

    def test_bulk_price_returns_dict(self):
        acct_def = create_account_definition()
        account = MockAccount(acct_def.id)
        prices = account._get_instrument_current_price_impl(["AAPL", "MSFT", "UNKNOWN"])
        assert prices["AAPL"] == 150.0
        assert prices["MSFT"] == 400.0
        assert prices["UNKNOWN"] is None
