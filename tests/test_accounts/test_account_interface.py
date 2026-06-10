"""Tests for AccountInterface non-abstract methods via MockAccount."""
import pytest
from tests.conftest import MockAccount, MockExpert
from tests.factories import create_account_definition, create_expert_instance, create_transaction
from ba2_trade_platform.core.models import TradingOrder
from ba2_trade_platform.core.types import OrderStatus, OrderDirection, OrderType, TransactionStatus


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


class TestValidateExpertAvailableBalance:
    """Regression tests for AccountInterface._validate_expert_available_balance.

    Reproduces the false-positive "Order value exceeds expert's available
    balance" error (SmartRiskManagerJob #27, BRUN entry order): the new
    transaction created for the order under validation is persisted with
    status=WAITING *before* validation runs, so _calculate_used_balance counts
    it against the expert's available balance. The order_value vs
    available_balance check then double-counts that transaction's value.
    """

    def test_new_position_excludes_its_own_waiting_transaction(self, monkeypatch):
        acct_def = create_account_definition()
        expert_instance = create_expert_instance(
            account_id=acct_def.id, expert="MockExpert", virtual_equity_pct=100.0
        )
        account = MockAccount(acct_def.id)
        account._balance = 1000.0
        account._prices["AAPL"] = 100.0
        monkeypatch.setattr(
            account, "get_instrument_current_price",
            lambda symbols: {s: account._prices.get(s) for s in symbols} if isinstance(symbols, list)
            else account._prices.get(symbols),
        )
        expert = MockExpert(expert_instance.id)

        monkeypatch.setattr(
            "ba2_trade_platform.core.utils.get_expert_instance_from_id",
            lambda expert_instance_id, use_cache=True: expert,
        )
        monkeypatch.setattr(
            "ba2_trade_platform.core.utils.get_account_instance_from_id",
            lambda account_id, session=None, use_cache=True: account,
        )

        # Existing OPENED position: 5 AAPL @ $100 = $500 used balance
        create_transaction(
            symbol="AAPL", quantity=5.0, side=OrderDirection.BUY,
            status=TransactionStatus.OPENED, open_price=100.0,
            expert_id=expert_instance.id,
        )

        # The new transaction created for the order under validation,
        # status=WAITING (committed before order validation runs): 1 MSFT @ $400
        new_transaction = create_transaction(
            symbol="MSFT", quantity=1.0, side=OrderDirection.BUY,
            status=TransactionStatus.WAITING, open_price=400.0,
            expert_id=expert_instance.id,
        )

        trading_order = TradingOrder(
            account_id=acct_def.id, symbol="MSFT", quantity=1.0,
            side=OrderDirection.BUY, order_type=OrderType.MARKET,
            status=OrderStatus.PENDING, transaction_id=new_transaction.id,
        )

        # virtual_balance = $1000 (100% of $1000 account balance)
        # true available balance (excluding the order's own transaction) = $1000 - $500 = $500
        # order_value = $400, well within $500 -> should NOT be rejected
        errors = account._validate_expert_available_balance(
            trading_order, new_transaction, expert_instance, current_price=400.0
        )

        assert errors == []


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
