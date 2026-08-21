"""Tests for AccountInterface non-abstract methods via MockAccount."""
import pytest
from tests.conftest import MockAccount, MockExpert
from tests.factories import create_account_definition, create_expert_instance, create_transaction
from ba2_trade_platform.core.models import ExpertSetting, TradingOrder
from ba2_trade_platform.core.types import OrderStatus, OrderDirection, OrderType, TransactionStatus
from ba2_trade_platform.core.db import add_instance


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


class _TastyShapedAccount(MockAccount):
    """MockAccount whose ``get_account_info()`` has the TastyTrade DICT shape.

    ``TastyTradeAccount.get_account_info()`` returns a dict keyed
    ``net_liquidating_value`` / ``margin_equity`` / ``cash_balance`` -- there is NO
    ``equity`` key. ``_validate_position_size_limits`` used to duck-type
    ``float(account_info.equity)``, which raises ``AttributeError`` on ANY dict-shaped
    broker; the broad ``except Exception`` then logged a warning and returned an EMPTY
    error list, i.e. "validation passed". Both the per-instrument cap and the expert
    virtual-balance cap were dead for TastyTrade.
    """

    def get_account_info(self):
        return {
            "account_number": "5WX00000",
            "account_type": "Individual",
            "buying_power": 50_000.0,
            "net_liquidating_value": 100_000.0,
            "cash_balance": 25_000.0,
            "equity_buying_power": 50_000.0,
            "margin_equity": 100_000.0,
            "supports_trading": True,
        }


def _expert_with_cap(account_id, max_position_pct):
    """An ExpertInstance at 100% virtual equity with a per-instrument cap setting."""
    expert_instance = create_expert_instance(
        account_id=account_id, expert="MockExpert", virtual_equity_pct=100.0
    )
    add_instance(
        ExpertSetting(
            instance_id=expert_instance.id,
            key="max_virtual_equity_per_instrument_percent",
            value_str=None,
            value_float=float(max_position_pct),
        ),
        expunge_after_flush=True,
    )
    return expert_instance


class _StubExpertResolver:
    """Instance resolver returning a canned expert interface."""

    def __init__(self, expert):
        self._expert = expert

    def get_expert_instance(self, expert_id):
        return self._expert

    def get_account_instance(self, account_id):
        raise NotImplementedError

    def get_account_instance_from_transaction(self, transaction):
        raise NotImplementedError


class _StubExpertInterface:
    """Only the one method ``_validate_expert_available_balance`` calls."""

    def __init__(self, available_balance):
        self._available_balance = available_balance

    def get_available_balance(self, exclude_transaction_id=None):
        return self._available_balance


class TestPositionSizeGuardsRunForDictShapedBrokers:
    """C1: re-parenting silently disabled BOTH position-size guards for TastyTrade.

    ``_validate_position_size_limits`` is SHARED code -- Alpaca (pydantic
    ``TradeAccount``, has ``.equity``) went through it fine, but every dict-shaped
    broker crashed on ``account_info.equity`` above the balance check, so neither
    guard ran. Equity is now read through the typed ``get_account_snapshot()`` seam.
    """

    def test_per_instrument_cap_rejects_an_oversized_order(self, monkeypatch):
        acct_def = create_account_definition()
        account = _TastyShapedAccount(acct_def.id)
        account._prices["AAPL"] = 150.0
        expert_instance = _expert_with_cap(acct_def.id, max_position_pct=10.0)

        # Expert balance is not the constraint here -- the per-instrument cap is.
        monkeypatch.setattr(
            "ba2_common.core.instance_resolver._resolver",
            _StubExpertResolver(_StubExpertInterface(available_balance=1_000_000.0)),
        )

        transaction = create_transaction(
            symbol="AAPL", quantity=0.0, side=OrderDirection.BUY,
            status=TransactionStatus.WAITING, open_price=150.0,
            expert_id=expert_instance.id,
        )
        order = TradingOrder(
            account_id=acct_def.id, symbol="AAPL", quantity=400.0,
            side=OrderDirection.BUY, order_type=OrderType.MARKET,
            status=OrderStatus.PENDING, transaction_id=transaction.id,
        )

        # $100k equity x 100% virtual = $100k; 10% cap = $10k. 400 x $150 = $60k.
        errors = account._validate_position_size_limits(order)

        assert any("exceeds expert's max allowed" in e for e in errors), errors

    def test_expert_available_balance_guard_also_runs(self, monkeypatch):
        """The equity crash sat ABOVE the balance check, so it never ran either."""
        acct_def = create_account_definition()
        account = _TastyShapedAccount(acct_def.id)
        account._prices["AAPL"] = 150.0
        # 100% cap -> the per-instrument limit cannot fire; only the balance check can.
        expert_instance = _expert_with_cap(acct_def.id, max_position_pct=100.0)

        monkeypatch.setattr(
            "ba2_common.core.instance_resolver._resolver",
            _StubExpertResolver(_StubExpertInterface(available_balance=1_000.0)),
        )

        transaction = create_transaction(
            symbol="AAPL", quantity=0.0, side=OrderDirection.BUY,
            status=TransactionStatus.WAITING, open_price=150.0,
            expert_id=expert_instance.id,
        )
        order = TradingOrder(
            account_id=acct_def.id, symbol="AAPL", quantity=100.0,
            side=OrderDirection.BUY, order_type=OrderType.MARKET,
            status=OrderStatus.PENDING, transaction_id=transaction.id,
        )

        errors = account._validate_position_size_limits(order)

        assert any("available balance" in e for e in errors), errors

    def test_alpaca_shaped_object_account_info_is_not_regressed(self, monkeypatch):
        """Alpaca returns a pydantic TradeAccount (attribute access). Reading equity
        through the snapshot seam must keep rejecting the same order."""
        class _AlpacaShapedAccount(MockAccount):
            def get_account_info(self):
                from types import SimpleNamespace
                return SimpleNamespace(equity="100000", buying_power="50000",
                                       cash="25000", multiplier="2")

        acct_def = create_account_definition()
        account = _AlpacaShapedAccount(acct_def.id)
        account._prices["AAPL"] = 150.0
        expert_instance = _expert_with_cap(acct_def.id, max_position_pct=10.0)

        monkeypatch.setattr(
            "ba2_common.core.instance_resolver._resolver",
            _StubExpertResolver(_StubExpertInterface(available_balance=1_000_000.0)),
        )

        transaction = create_transaction(
            symbol="AAPL", quantity=0.0, side=OrderDirection.BUY,
            status=TransactionStatus.WAITING, open_price=150.0,
            expert_id=expert_instance.id,
        )
        order = TradingOrder(
            account_id=acct_def.id, symbol="AAPL", quantity=400.0,
            side=OrderDirection.BUY, order_type=OrderType.MARKET,
            status=OrderStatus.PENDING, transaction_id=transaction.id,
        )

        errors = account._validate_position_size_limits(order)

        assert any("exceeds expert's max allowed" in e for e in errors), errors

    def test_unknown_equity_refuses_to_validate_instead_of_passing(self, monkeypatch):
        """An all-None snapshot means 'the broker told us nothing'. Silently passing
        an unrun risk check is what made C1 invisible."""
        class _MuteAccount(MockAccount):
            def get_account_info(self):
                return {"account_number": "5WX00000"}   # no balance figure at all

        acct_def = create_account_definition()
        account = _MuteAccount(acct_def.id)
        account._prices["AAPL"] = 150.0
        expert_instance = _expert_with_cap(acct_def.id, max_position_pct=10.0)

        transaction = create_transaction(
            symbol="AAPL", quantity=0.0, side=OrderDirection.BUY,
            status=TransactionStatus.WAITING, open_price=150.0,
            expert_id=expert_instance.id,
        )
        order = TradingOrder(
            account_id=acct_def.id, symbol="AAPL", quantity=400.0,
            side=OrderDirection.BUY, order_type=OrderType.MARKET,
            status=OrderStatus.PENDING, transaction_id=transaction.id,
        )

        errors = account._validate_position_size_limits(order)

        assert errors, "unknown equity must be a failure to validate, not a pass"
        assert any("equity" in e.lower() for e in errors), errors

    def test_a_crash_inside_the_risk_control_is_reported_as_a_failure(self, monkeypatch):
        """An exception inside a risk control must NOT be indistinguishable from
        'validation passed' -- that is precisely how C1 hid for a whole release."""
        acct_def = create_account_definition()
        account = _TastyShapedAccount(acct_def.id)
        account._prices["AAPL"] = 150.0
        expert_instance = _expert_with_cap(acct_def.id, max_position_pct=10.0)

        def _boom(*_a, **_k):
            raise AttributeError("'dict' object has no attribute 'equity'")

        monkeypatch.setattr(account, "_validate_single_position_size", _boom)

        transaction = create_transaction(
            symbol="AAPL", quantity=0.0, side=OrderDirection.BUY,
            status=TransactionStatus.WAITING, open_price=150.0,
            expert_id=expert_instance.id,
        )
        order = TradingOrder(
            account_id=acct_def.id, symbol="AAPL", quantity=400.0,
            side=OrderDirection.BUY, order_type=OrderType.MARKET,
            status=OrderStatus.PENDING, transaction_id=transaction.id,
        )

        errors = account._validate_position_size_limits(order)

        assert errors, "a crashed risk control must not report success"
        assert any("could not be completed" in e for e in errors), errors


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
