"""Tests for AccountInterface non-abstract methods via MockAccount."""
import pytest
from tests.conftest import MockAccount, MockExpert
from tests.factories import (
    create_account_definition, create_expert_instance, create_trading_order,
    create_transaction,
)
from ba2_common.core.interfaces.AccountInterface import PLEDGED_COVER_REFUSAL
from ba2_trade_platform.core.models import ExpertSetting, TradingOrder, Transaction
from ba2_trade_platform.core.types import (
    AssetClass, OrderStatus, OrderDirection, OrderType, TransactionStatus,
)
from ba2_trade_platform.core.db import add_instance, get_instance


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


def _expert_with_cap(account_id, max_position_pct, virtual_equity_pct=100.0):
    """An ExpertInstance with a per-instrument cap setting (100% sleeve by default)."""
    expert_instance = create_expert_instance(
        account_id=account_id, expert="MockExpert", virtual_equity_pct=virtual_equity_pct
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
    """Only the one method ``_validate_expert_available_balance`` calls.

    Records the ``exclude_transaction_id`` it was called with: which branch excludes
    the order's own WAITING transaction is a real money decision, not a detail.
    """

    def __init__(self, available_balance):
        self._available_balance = available_balance
        self.exclude_calls = []

    def get_available_balance(self, exclude_transaction_id=None):
        self.exclude_calls.append(exclude_transaction_id)
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


# ---------------------------------------------------------------------------
# "Unknown reads as zero / unknown reads as PASS" in the account money paths.
#
# Three sites in this module answered a money question they could not measure:
#
#   * ``_validate_expert_available_balance`` returned an EMPTY error list -- which
#     every caller reads as "validation passed" -- whenever the expert's available
#     balance came back ``None``. ``TastyTradeAccount.get_balance()`` returns None on
#     ANY exception, so one broker hiccup silently opened the balance gate.
#   * ``_validate_position_size_limits`` did the same when the quote failed: no price
#     meant BOTH risk gates (per-instrument cap AND expert balance) were skipped, on a
#     MARKET order that already carries a transaction_id.
#   * ``submit_close_order_for_transaction`` sized the close off
#     ``abs(get_current_open_qty()) or transaction.quantity`` -- a net of zero fell
#     through to the ORDERED quantity, i.e. a close acting on a number it never
#     measured.
#
# The equity branch of ``_validate_position_size_limits`` already had the right shape
# (``errors.append(...)``, never a bare ``return errors``); these tests hold the other
# three to it. Each defect is paired with its inverse: a LEGITIMATE zero (a measured
# $0.00 available balance, a genuinely flat book) must still be honoured as an answer.
# ---------------------------------------------------------------------------

def _capture_errors(monkeypatch):
    """Collect ``logger.error`` text emitted by AccountInterface.

    NOT caplog: ``ba2_trade_platform/logger.py`` sets ``propagate = False`` and other
    test modules swap the logger module wholesale, so caplog's root handler can see
    nothing. Patching the module-under-test's own ``logger`` is immune to both.
    """
    import sys
    import ba2_common.core.interfaces.AccountInterface  # noqa: F401  (ensure imported)
    # NOT ``import ... as _ai``: ``ba2_common.core.interfaces.__init__`` re-exports the
    # CLASS under the same name, so the ``as`` form binds the class, not the module.
    module = sys.modules["ba2_common.core.interfaces.AccountInterface"]
    messages = []
    monkeypatch.setattr(module.logger, "error", lambda msg, *a, **k: messages.append(str(msg)))
    return messages


class TestUnreadableBalanceIsNotAPass:
    """``available_balance is None`` must be a refusal, not an empty error list."""

    def _order_and_txn(self, acct_def, expert_instance, symbol="MSFT", qty=1.0,
                       price=400.0, side=OrderDirection.BUY):
        transaction = create_transaction(
            symbol=symbol, quantity=0.0, side=side,
            status=TransactionStatus.WAITING, open_price=price,
            expert_id=expert_instance.id,
        )
        order = TradingOrder(
            account_id=acct_def.id, symbol=symbol, quantity=qty,
            side=side, order_type=OrderType.MARKET,
            status=OrderStatus.PENDING, transaction_id=transaction.id,
        )
        return order, transaction

    def test_new_position_unreadable_balance_refuses(self, monkeypatch):
        acct_def = create_account_definition()
        account = MockAccount(acct_def.id)
        expert_instance = _expert_with_cap(acct_def.id, max_position_pct=100.0)
        monkeypatch.setattr(
            "ba2_common.core.instance_resolver._resolver",
            _StubExpertResolver(_StubExpertInterface(available_balance=None)),
        )
        errors_logged = _capture_errors(monkeypatch)
        order, transaction = self._order_and_txn(acct_def, expert_instance)

        errors = account._validate_expert_available_balance(
            order, transaction, expert_instance, current_price=400.0
        )

        assert errors, "an unreadable available balance must not report 'validation passed'"
        assert any("available balance" in e.lower() for e in errors), errors
        assert errors_logged, "refusing to validate must be logged, not silent"

    def test_adding_to_position_unreadable_balance_refuses(self, monkeypatch):
        """The add-to-position branch has its own ``get_available_balance()`` call and
        its own bare ``return errors``; fixing only the new-position branch leaves the
        gate open for every top-up."""
        acct_def = create_account_definition()
        account = MockAccount(acct_def.id)
        expert_instance = _expert_with_cap(acct_def.id, max_position_pct=100.0)
        monkeypatch.setattr(
            "ba2_common.core.instance_resolver._resolver",
            _StubExpertResolver(_StubExpertInterface(available_balance=None)),
        )
        order, transaction = self._order_and_txn(acct_def, expert_instance)
        # An existing SAME-SIDE entry order makes this an "adding to position" order.
        create_trading_order(
            account_id=acct_def.id, symbol="MSFT", quantity=1.0,
            side=OrderDirection.BUY, status=OrderStatus.FILLED,
            transaction_id=transaction.id, filled_qty=1.0,
        )

        errors = account._validate_expert_available_balance(
            order, transaction, expert_instance, current_price=400.0
        )

        assert errors, "the add-to-position branch must refuse an unreadable balance too"
        # Specifically the GUARD's message. `assert errors` alone was satisfied by
        # deleting the guard entirely: None then reached `additional_value > None`,
        # the outer except caught the TypeError and appended its own error. A crash
        # is not the check running.
        assert any("before adding to" in e for e in errors), errors
        assert not any("could not be completed" in e for e in errors), (
            "the guard must refuse, not crash into the catch-all", errors)

    def test_a_measured_zero_balance_blocks_adding_to_a_position(self, monkeypatch):
        """THE INVERSE for the add-to-position branch: $0.00 available is measured, so
        the top-up is rejected for exceeding it with the ordinary message."""
        acct_def = create_account_definition()
        account = MockAccount(acct_def.id)
        expert_instance = _expert_with_cap(acct_def.id, max_position_pct=100.0)
        monkeypatch.setattr(
            "ba2_common.core.instance_resolver._resolver",
            _StubExpertResolver(_StubExpertInterface(available_balance=0.0)),
        )
        order, transaction = self._order_and_txn(acct_def, expert_instance)
        create_trading_order(
            account_id=acct_def.id, symbol="MSFT", quantity=1.0,
            side=OrderDirection.BUY, status=OrderStatus.FILLED,
            transaction_id=transaction.id, filled_qty=1.0,
        )

        errors = account._validate_expert_available_balance(
            order, transaction, expert_instance, current_price=400.0
        )

        assert any("Adding $400.00 exceeds expert's available balance $0.00" in e
                   for e in errors), errors
        assert not any("could not" in e.lower() for e in errors), errors

    def test_a_readable_balance_permits_adding_to_a_position(self, monkeypatch):
        """...and a top-up the sleeve can afford is still allowed."""
        acct_def = create_account_definition()
        account = MockAccount(acct_def.id)
        expert_instance = _expert_with_cap(acct_def.id, max_position_pct=100.0)
        monkeypatch.setattr(
            "ba2_common.core.instance_resolver._resolver",
            _StubExpertResolver(_StubExpertInterface(available_balance=10_000.0)),
        )
        order, transaction = self._order_and_txn(acct_def, expert_instance)
        create_trading_order(
            account_id=acct_def.id, symbol="MSFT", quantity=1.0,
            side=OrderDirection.BUY, status=OrderStatus.FILLED,
            transaction_id=transaction.id, filled_qty=1.0,
        )

        assert account._validate_expert_available_balance(
            order, transaction, expert_instance, current_price=400.0
        ) == []

    def test_a_measured_zero_balance_is_still_an_answer(self, monkeypatch):
        """THE INVERSE. $0.00 available is a MEASURED number, not an unknown: the order
        must be rejected for exceeding it, with the ordinary 'exceeds' message -- never
        with the 'could not run' one."""
        acct_def = create_account_definition()
        account = MockAccount(acct_def.id)
        expert_instance = _expert_with_cap(acct_def.id, max_position_pct=100.0)
        monkeypatch.setattr(
            "ba2_common.core.instance_resolver._resolver",
            _StubExpertResolver(_StubExpertInterface(available_balance=0.0)),
        )
        order, transaction = self._order_and_txn(acct_def, expert_instance)

        errors = account._validate_expert_available_balance(
            order, transaction, expert_instance, current_price=400.0
        )

        assert any("exceeds expert's available balance $0.00" in e for e in errors), errors
        assert not any("could not" in e.lower() for e in errors), (
            "a measured zero must not be reported as an unrun check", errors)

    def test_a_readable_balance_that_covers_the_order_still_passes(self, monkeypatch):
        """And the gate must not start rejecting everything."""
        acct_def = create_account_definition()
        account = MockAccount(acct_def.id)
        expert_instance = _expert_with_cap(acct_def.id, max_position_pct=100.0)
        monkeypatch.setattr(
            "ba2_common.core.instance_resolver._resolver",
            _StubExpertResolver(_StubExpertInterface(available_balance=10_000.0)),
        )
        order, transaction = self._order_and_txn(acct_def, expert_instance)

        assert account._validate_expert_available_balance(
            order, transaction, expert_instance, current_price=400.0
        ) == []


class TestUnreadablePriceIsNotAPass:
    """No quote means NEITHER risk gate ran. That is a refusal, not a pass."""

    def test_missing_price_refuses_to_validate(self, monkeypatch):
        acct_def = create_account_definition()
        account = MockAccount(acct_def.id)
        expert_instance = _expert_with_cap(acct_def.id, max_position_pct=10.0)
        monkeypatch.setattr(
            "ba2_common.core.instance_resolver._resolver",
            _StubExpertResolver(_StubExpertInterface(available_balance=1_000_000.0)),
        )
        # A dead quote feed: the symbol resolves to no price at all.
        monkeypatch.setattr(account, "get_instrument_current_price", lambda *_a, **_k: None)
        errors_logged = _capture_errors(monkeypatch)

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

        assert errors, "no price means the cap was never checked -- that is not a pass"
        assert any("no price" in e.lower() for e in errors), errors
        assert errors_logged, "an unrun risk gate must be logged"

    def test_a_readable_price_still_validates_normally(self, monkeypatch):
        """THE INVERSE: with a real quote the gate behaves exactly as before."""
        acct_def = create_account_definition()
        account = MockAccount(acct_def.id)
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
        # 1 share x $150 against a $10k cap -> comfortably inside; no errors.
        small = TradingOrder(
            account_id=acct_def.id, symbol="AAPL", quantity=1.0,
            side=OrderDirection.BUY, order_type=OrderType.MARKET,
            status=OrderStatus.PENDING, transaction_id=transaction.id,
        )
        assert account._validate_position_size_limits(small) == []


class TestAZeroQuoteIsNotAPrice:
    """GAP 1: ``if current_price is None`` was too narrow.

    A quote of exactly ``0.0`` is not a price -- it is a broker or feed that answered
    with nothing usable. It sails through the ``is None`` check, and then EVERY number
    downstream of it is zero:

        position_value  = current_price * trading_order.quantity  -> 0.0
        order_value     = current_price * trading_order.quantity  -> 0.0

    so ``0.0 > max_position_value`` and ``0.0 > available_balance`` are both False and
    BOTH risk gates report "no problems" for an order of any size, on a MARKET order
    that already carries a transaction_id. The cap is not merely wrong here, it is
    off.

    The guard is on the PRICE and on nothing else. A zero quantity, a zero position
    value, a zero cap and a zero equity are all legitimate measured zeros and are
    pinned below -- widening the guard to any of them is the inverse defect, which is
    quieter and therefore worse.
    """

    def _fixture(self, monkeypatch, price, *, max_position_pct=10.0, quantity=400.0):
        acct_def = create_account_definition()
        account = MockAccount(acct_def.id)
        expert_instance = _expert_with_cap(acct_def.id, max_position_pct=max_position_pct)
        monkeypatch.setattr(
            "ba2_common.core.instance_resolver._resolver",
            _StubExpertResolver(_StubExpertInterface(available_balance=1_000_000.0)),
        )
        # Patch the SEAM, not ``_prices``: get_instrument_current_price caches any
        # non-None price in a CLASS-level dict keyed by account id, so a canned 0.0
        # would outlive the test.
        monkeypatch.setattr(account, "get_instrument_current_price",
                            lambda *_a, **_k: price)
        transaction = create_transaction(
            symbol="AAPL", quantity=0.0, side=OrderDirection.BUY,
            status=TransactionStatus.WAITING, open_price=150.0,
            expert_id=expert_instance.id,
        )
        order = TradingOrder(
            account_id=acct_def.id, symbol="AAPL", quantity=quantity,
            side=OrderDirection.BUY, order_type=OrderType.MARKET,
            status=OrderStatus.PENDING, transaction_id=transaction.id,
        )
        return account, order

    def test_a_zero_price_refuses_to_validate(self, monkeypatch):
        """THE DEFECT: $0.00 x 400 shares = $0.00, which is inside every limit."""
        errors_logged = _capture_errors(monkeypatch)
        account, order = self._fixture(monkeypatch, price=0.0)

        errors = account._validate_position_size_limits(order)

        assert errors, "a $0.00 quote is not a price -- neither gate could be priced"
        assert any("no price" in e.lower() for e in errors), errors
        # The refusal must NAME the offending value, or an operator reading it cannot
        # tell a dead feed from a missing symbol.
        assert any("0.0" in e for e in errors), errors
        assert errors_logged, "an unrun risk gate must be logged, not silent"

    def test_the_zero_price_refusal_comes_from_the_GUARD_not_the_catch_all(self, monkeypatch):
        """``assert errors`` alone is satisfied by a crash. A risk control that blew up
        is not a risk control that ran, and the two must stay distinguishable."""
        account, order = self._fixture(monkeypatch, price=0.0)

        errors = account._validate_position_size_limits(order)

        assert not any("could not be completed" in e for e in errors), (
            "the guard must refuse, not fall into the outer except", errors)

    def test_a_zero_price_is_refused_even_when_the_cap_is_generous(self, monkeypatch):
        """Pins that the refusal is the PRICE guard and not the cap firing by luck: a
        100% cap and a million-dollar sleeve can reject nothing on their own."""
        account, order = self._fixture(monkeypatch, price=0.0, max_position_pct=100.0)

        errors = account._validate_position_size_limits(order)

        assert errors, "a generous cap must not make an unpriceable order acceptable"
        assert not any("exceeds expert's max allowed" in e for e in errors), errors

    def test_a_negative_price_is_refused_too(self, monkeypatch):
        """Worse than zero: a negative quote makes ``position_value`` NEGATIVE, so the
        cap comparison is not merely satisfied, it is satisfied by an ever-larger
        order. Equities and options have no negative quote; this is a broken feed."""
        errors_logged = _capture_errors(monkeypatch)
        account, order = self._fixture(monkeypatch, price=-3.0)

        errors = account._validate_position_size_limits(order)

        assert errors, "a negative quote is not a price"
        assert any("no price" in e.lower() for e in errors), errors
        assert any("-3.0" in e for e in errors), errors
        assert errors_logged

    # --- the inverses: legitimate zeros that must NOT be swept up ----------

    @pytest.mark.parametrize("price", [0.01, 0.0001, 1e-6])
    def test_a_sub_penny_price_is_a_real_quote(self, monkeypatch, price):
        """THE INVERSE #1: sub-penny tickers exist and this platform trades pennies.
        The guard must test for zero and NOTHING WIDER -- a ``< 0.01`` guard would
        refuse to validate every legitimate sub-penny order, and mutation showed that
        one pinned magnitude only defends the thresholds above it, so the smallest
        case here is three orders of magnitude below the smallest real US tick."""
        account, order = self._fixture(monkeypatch, price=price, quantity=1.0)

        assert account._validate_position_size_limits(order) == []

    def test_a_zero_QUANTITY_order_is_not_the_price_guards_business(self, monkeypatch):
        """THE INVERSE #2: it is the zero PRICE that is unmeasurable. A zero-quantity
        order has a perfectly measured $0.00 position value, and guarding
        ``position_value <= 0`` instead would refuse it with a price complaint."""
        account, order = self._fixture(monkeypatch, price=150.0, quantity=0.0)

        assert account._validate_position_size_limits(order) == []

    def test_a_measured_zero_EQUITY_is_still_an_answer(self, monkeypatch):
        """THE INVERSE #3: an account measured at $0.00 equity has a $0.00 sleeve and
        therefore a $0.00 cap -- every order is rejected BY THE CAP, with the ordinary
        message. Widening the equity guard from ``is None`` to falsy would report a
        measured empty account as an unrun check."""
        class _EmptyAccount(MockAccount):
            def get_account_info(self):
                return {"balance": 0.0, "equity": 0.0}

        acct_def = create_account_definition()
        account = _EmptyAccount(acct_def.id)
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
            account_id=acct_def.id, symbol="AAPL", quantity=1.0,
            side=OrderDirection.BUY, order_type=OrderType.MARKET,
            status=OrderStatus.PENDING, transaction_id=transaction.id,
        )

        errors = account._validate_position_size_limits(order)

        assert any("exceeds expert's max allowed $0.00" in e for e in errors), errors
        assert not any("could not" in e.lower() for e in errors), (
            "a measured $0 account is an answer, not an unrun check", errors)

    def test_the_refusal_STOPS_before_pricing_either_gate(self, monkeypatch):
        """The guard bare-``return``s for a reason: with no usable price there is
        nothing for the two gates to compute, and running them anyway means the
        per-instrument cap divides by the quote (``int(max_additional_value /
        current_price)``) and the balance gate compares a fabricated $0.00 order
        value. Dropping the early return leaves the refusal in the list, so nothing
        below it fails -- this is what notices."""
        account, order = self._fixture(monkeypatch, price=0.0)
        reached = []
        monkeypatch.setattr(account, "_validate_single_position_size",
                            lambda *a, **k: reached.append("cap") or [])
        monkeypatch.setattr(account, "_validate_expert_available_balance",
                            lambda *a, **k: reached.append("balance") or [])

        errors = account._validate_position_size_limits(order)

        assert len(errors) == 1, ("the refusal must be the only thing reported", errors)
        assert reached == [], (
            "neither gate can be priced off a $0.00 quote, so neither must be run",
            reached)


class TestTheGatesActuallyStopTheOrder:
    """The errors these gates append are only worth appending if the caller obeys.

    ``AccountInterface.submit_order`` calls ``_validate_trading_order``, which folds
    the position-size errors into its own list, and raises ``ValueError`` when the
    result is not valid. Every gate test above asserts on the RETURNED LIST; nothing
    pinned that the list is honoured, so a caller that dropped it -- or a
    ``_validate_trading_order`` that stopped extending ``errors`` with it -- would
    leave every one of them green while orders went to the broker regardless.
    """

    class _RealSubmitAccount(MockAccount):
        """MockAccount WITHOUT its ``submit_order`` override, so the base class's
        validate-then-refuse path is the one under test."""
        from ba2_common.core.interfaces.AccountInterface import AccountInterface as _AI
        submit_order = _AI.submit_order
        del _AI

        def __init__(self, id_or_definition):
            super().__init__(id_or_definition)
            self.impl_calls = []

        def _submit_order_impl(self, trading_order, tp_price=None, sl_price=None,
                               is_closing_order=False, use_complex_order=False):
            self.impl_calls.append(trading_order)
            return super()._submit_order_impl(trading_order, tp_price, sl_price,
                                              is_closing_order, use_complex_order)

    def _capped_setup(self, monkeypatch, *, price, ordered_qty, fills, add_qty,
                      max_position_pct=10.0):
        acct_def = create_account_definition()
        account = self._RealSubmitAccount(acct_def.id)
        expert_instance = _expert_with_cap(acct_def.id, max_position_pct=max_position_pct)
        monkeypatch.setattr(
            "ba2_common.core.instance_resolver._resolver",
            _StubExpertResolver(_StubExpertInterface(available_balance=1_000_000.0)),
        )
        monkeypatch.setattr(account, "get_instrument_current_price",
                            lambda *_a, **_k: price)
        transaction = create_transaction(
            symbol="AAPL", quantity=ordered_qty, side=OrderDirection.BUY,
            status=TransactionStatus.OPENED, open_price=150.0,
            expert_id=expert_instance.id,
        )
        for side, filled in fills:
            create_trading_order(
                account_id=acct_def.id, symbol="AAPL", quantity=filled,
                side=side, status=OrderStatus.FILLED,
                transaction_id=transaction.id, filled_qty=filled,
            )
        order = TradingOrder(
            account_id=acct_def.id, symbol="AAPL", quantity=add_qty,
            side=OrderDirection.BUY, order_type=OrderType.MARKET,
            status=OrderStatus.PENDING, transaction_id=transaction.id,
        )
        return account, order

    def test_a_zero_priced_order_never_reaches_the_broker(self, monkeypatch):
        account, order = self._capped_setup(
            monkeypatch, price=0.0, ordered_qty=10.0,
            fills=[(OrderDirection.BUY, 10.0)], add_qty=20.0,
        )

        with pytest.raises(ValueError, match="no price"):
            account.submit_order(order)

        assert account.impl_calls == [], "nothing may be sent on an unpriceable order"

    def test_an_over_cap_add_never_reaches_the_broker(self, monkeypatch):
        """The measured holding (60) puts this add over the cap; the ordered
        quantity (10) would not have."""
        account, order = self._capped_setup(
            monkeypatch, price=150.0, ordered_qty=10.0,
            fills=[(OrderDirection.BUY, 10.0), (OrderDirection.BUY, 50.0)],
            add_qty=20.0,
        )

        with pytest.raises(ValueError, match="exceeding expert's max allowed"):
            account.submit_order(order)

        assert account.impl_calls == []

    def test_an_order_inside_every_limit_IS_submitted(self, monkeypatch):
        """THE INVERSE: the gates must not have become a blanket refusal."""
        account, order = self._capped_setup(
            monkeypatch, price=150.0, ordered_qty=10.0,
            fills=[(OrderDirection.BUY, 10.0)], add_qty=20.0,
        )

        result = account.submit_order(order)

        assert result is not None
        assert len(account.impl_calls) == 1


class TestCloseNeverFallsBackToTheOrderedQuantity:
    """``submit_close_order_for_transaction`` must size off what it MEASURED."""

    def _txn_with_orders(self, acct_def, *, ordered_qty, fills):
        """``fills`` is a list of (side, filled_qty) FILLED orders."""
        transaction = create_transaction(
            symbol="AAPL", quantity=ordered_qty, side=OrderDirection.BUY,
            status=TransactionStatus.OPENED, open_price=150.0,
        )
        for side, filled in fills:
            create_trading_order(
                account_id=acct_def.id, symbol="AAPL", quantity=filled,
                side=side, status=OrderStatus.FILLED,
                transaction_id=transaction.id, filled_qty=filled,
            )
        return transaction

    def test_flat_book_does_not_close_the_stale_ordered_quantity(self, monkeypatch):
        """THE DEFECT: 100 bought, 100 already sold -> net 0. The old
        ``abs(net) or transaction.quantity`` fell through to the transaction's stale
        ordered quantity and submitted a 100-share SELL against a position that no
        longer exists (Alpaca 40310000 on a cash-only account)."""
        acct_def = create_account_definition()
        account = MockAccount(acct_def.id)
        submitted = []
        monkeypatch.setattr(account, "submit_order",
                            lambda o, **k: submitted.append(o) or o)
        transaction = self._txn_with_orders(
            acct_def, ordered_qty=100.0,
            fills=[(OrderDirection.BUY, 100.0), (OrderDirection.SELL, 100.0)],
        )

        result = account.submit_close_order_for_transaction(transaction)

        assert submitted == [], "an already-flat transaction must not submit a close order"
        assert result["close_order_id"] is None
        assert "flat" in result["message"].lower(), result["message"]
        # Already-flat is the state the caller ASKED for, so it is a success: reporting
        # failure would make close_transaction retry a close it must never send.
        assert result["success"] is True

    def test_flat_book_does_not_queue_a_deferred_close_either(self, monkeypatch):
        """The deferred (depends_on_order) branch writes the order straight to the DB
        without going through submit_order, so it needs its own proof."""
        acct_def = create_account_definition()
        account = MockAccount(acct_def.id)
        transaction = self._txn_with_orders(
            acct_def, ordered_qty=100.0,
            fills=[(OrderDirection.BUY, 100.0), (OrderDirection.SELL, 100.0)],
        )
        blocker = create_trading_order(
            account_id=acct_def.id, symbol="AAPL", quantity=100.0,
            side=OrderDirection.SELL, status=OrderStatus.CANCELED,
            transaction_id=transaction.id,
        )

        result = account.submit_close_order_for_transaction(
            transaction, last_broker_canceled_order_id=blocker.id
        )

        assert result["close_order_id"] is None
        assert "flat" in result["message"].lower(), result["message"]
        assert result["success"] is True

    def test_partial_exit_closes_the_remainder_not_the_ordered_quantity(self, monkeypatch):
        """THE INVERSE #1: a real, measured net must still be closed -- and it is the
        REMAINDER (60), never the ordered 100. The ACTIVITY LOG has its own copy of
        that number and must not drift from the order."""
        import ba2_common.core.utils as _utils
        acct_def = create_account_definition()
        account = MockAccount(acct_def.id)
        submitted = []
        logged = []
        monkeypatch.setattr(account, "submit_order",
                            lambda o, **k: submitted.append(o) or o)
        monkeypatch.setattr(_utils, "log_close_order_activity",
                            lambda **kw: logged.append(kw))
        transaction = self._txn_with_orders(
            acct_def, ordered_qty=100.0,
            fills=[(OrderDirection.BUY, 100.0), (OrderDirection.SELL, 40.0)],
        )

        result = account.submit_close_order_for_transaction(transaction)

        assert result["success"] is True
        assert len(submitted) == 1
        assert submitted[0].quantity == 60.0
        assert submitted[0].side == OrderDirection.SELL
        assert [k["quantity"] for k in logged] == [60.0]

    def test_ordinary_full_position_still_closes(self, monkeypatch):
        """THE INVERSE #2: the everyday path is untouched."""
        acct_def = create_account_definition()
        account = MockAccount(acct_def.id)
        submitted = []
        monkeypatch.setattr(account, "submit_order",
                            lambda o, **k: submitted.append(o) or o)
        transaction = self._txn_with_orders(
            acct_def, ordered_qty=100.0, fills=[(OrderDirection.BUY, 100.0)],
        )

        result = account.submit_close_order_for_transaction(transaction)

        assert result["success"] is True
        assert len(submitted) == 1
        assert submitted[0].quantity == 100.0

    def test_a_fractional_remainder_is_still_a_position(self, monkeypatch):
        """THE INVERSE #3: Alpaca trades FRACTIONAL shares, so the flat guard must
        test for zero and nothing wider. 0.25 of a share is a real, measured holding
        that a `<= 0.5` guard would silently abandon with its stops still armed."""
        acct_def = create_account_definition()
        account = MockAccount(acct_def.id)
        submitted = []
        monkeypatch.setattr(account, "submit_order",
                            lambda o, **k: submitted.append(o) or o)
        transaction = self._txn_with_orders(
            acct_def, ordered_qty=1.5,
            fills=[(OrderDirection.BUY, 1.5), (OrderDirection.SELL, 1.25)],
        )

        result = account.submit_close_order_for_transaction(transaction)

        assert result["success"] is True
        assert len(submitted) == 1
        assert submitted[0].quantity == pytest.approx(0.25)

    def test_short_position_closes_the_absolute_net(self, monkeypatch):
        """A short's net is negative; the close is a BUY of its magnitude."""
        acct_def = create_account_definition()
        account = MockAccount(acct_def.id)
        submitted = []
        monkeypatch.setattr(account, "submit_order",
                            lambda o, **k: submitted.append(o) or o)
        transaction = create_transaction(
            symbol="AAPL", quantity=100.0, side=OrderDirection.SELL,
            status=TransactionStatus.OPENED, open_price=150.0,
        )
        create_trading_order(
            account_id=acct_def.id, symbol="AAPL", quantity=100.0,
            side=OrderDirection.SELL, status=OrderStatus.FILLED,
            transaction_id=transaction.id, filled_qty=100.0,
        )

        result = account.submit_close_order_for_transaction(transaction)

        assert result["success"] is True
        assert len(submitted) == 1
        assert submitted[0].quantity == 100.0
        assert submitted[0].side == OrderDirection.BUY


class TestPerInstrumentCapHonoursItsOwnNumbers:
    """Gaps a 212-mutation run found in the cap arithmetic these fixes sit on."""

    def test_a_zero_percent_per_instrument_cap_blocks_everything(self, monkeypatch):
        """The same "0% means 0%" question as the virtual-equity sleeve, one level
        down. ``if max_position_pct is None`` must NOT decay into ``if not
        max_position_pct``: an operator who caps a symbol at 0% of the sleeve has
        said 'never hold this', and truthiness would read it as 'no cap at all'."""
        acct_def = create_account_definition()
        account = MockAccount(acct_def.id)
        account._prices["AAPL"] = 150.0
        expert_instance = _expert_with_cap(acct_def.id, max_position_pct=0.0)
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
            account_id=acct_def.id, symbol="AAPL", quantity=1.0,
            side=OrderDirection.BUY, order_type=OrderType.MARKET,
            status=OrderStatus.PENDING, transaction_id=transaction.id,
        )

        errors = account._validate_position_size_limits(order)

        assert any("exceeds expert's max allowed $0.00" in e for e in errors), errors

    def test_adding_to_a_position_is_capped_on_the_TOTAL_and_on_the_SLEEVE(self, monkeypatch):
        """Three separate mutations survived here: the add-to-position comparison
        could be inverted, the EXISTING holding could be dropped from the new total,
        and the sleeve percentage could be ignored so the cap was measured against
        the whole account. One case pins all three."""
        acct_def = create_account_definition()
        account = MockAccount(acct_def.id)
        account._prices["AAPL"] = 150.0
        # $100k account x 50% sleeve = $50k virtual; 10% cap = $5,000.
        expert_instance = _expert_with_cap(acct_def.id, max_position_pct=10.0,
                                           virtual_equity_pct=50.0)
        monkeypatch.setattr(
            "ba2_common.core.instance_resolver._resolver",
            _StubExpertResolver(_StubExpertInterface(available_balance=1_000_000.0)),
        )
        # Already holding 30 shares = $4,500 -- inside the cap on its own.
        transaction = create_transaction(
            symbol="AAPL", quantity=30.0, side=OrderDirection.BUY,
            status=TransactionStatus.OPENED, open_price=150.0,
            expert_id=expert_instance.id,
        )
        create_trading_order(
            account_id=acct_def.id, symbol="AAPL", quantity=30.0,
            side=OrderDirection.BUY, status=OrderStatus.FILLED,
            transaction_id=transaction.id, filled_qty=30.0,
        )
        # Adding 10 more ($1,500, also inside the cap alone) takes the TOTAL to
        # $6,000 -- over the $5,000 cap.
        order = TradingOrder(
            account_id=acct_def.id, symbol="AAPL", quantity=10.0,
            side=OrderDirection.BUY, order_type=OrderType.MARKET,
            status=OrderStatus.PENDING, transaction_id=transaction.id,
        )

        errors = account._validate_position_size_limits(order)

        assert any("exceeding expert's max allowed $5000.00" in e for e in errors), errors

    def test_the_add_to_position_balance_check_counts_the_whole_position(self, monkeypatch):
        """``exclude_transaction_id`` belongs ONLY to the new-position branch, where
        the order's own WAITING transaction would otherwise be double-counted against
        itself. On a top-up the existing holding is REAL exposure and must stay in the
        used balance -- excluding it would silently raise the ceiling."""
        acct_def = create_account_definition()
        account = MockAccount(acct_def.id)
        expert_instance = _expert_with_cap(acct_def.id, max_position_pct=100.0)
        stub = _StubExpertInterface(available_balance=10_000.0)
        monkeypatch.setattr("ba2_common.core.instance_resolver._resolver",
                            _StubExpertResolver(stub))
        transaction = create_transaction(
            symbol="MSFT", quantity=1.0, side=OrderDirection.BUY,
            status=TransactionStatus.OPENED, open_price=400.0,
            expert_id=expert_instance.id,
        )
        create_trading_order(
            account_id=acct_def.id, symbol="MSFT", quantity=1.0,
            side=OrderDirection.BUY, status=OrderStatus.FILLED,
            transaction_id=transaction.id, filled_qty=1.0,
        )
        order = TradingOrder(
            account_id=acct_def.id, symbol="MSFT", quantity=1.0,
            side=OrderDirection.BUY, order_type=OrderType.MARKET,
            status=OrderStatus.PENDING, transaction_id=transaction.id,
        )

        account._validate_expert_available_balance(
            order, transaction, expert_instance, current_price=400.0)

        assert stub.exclude_calls == [None], (
            "a top-up must not exclude its own (real) position from used balance",
            stub.exclude_calls)

    def test_a_new_position_DOES_exclude_its_own_waiting_transaction(self, monkeypatch):
        """The inverse of the above, on the branch where the exclusion is correct."""
        acct_def = create_account_definition()
        account = MockAccount(acct_def.id)
        expert_instance = _expert_with_cap(acct_def.id, max_position_pct=100.0)
        stub = _StubExpertInterface(available_balance=10_000.0)
        monkeypatch.setattr("ba2_common.core.instance_resolver._resolver",
                            _StubExpertResolver(stub))
        transaction = create_transaction(
            symbol="MSFT", quantity=0.0, side=OrderDirection.BUY,
            status=TransactionStatus.WAITING, open_price=400.0,
            expert_id=expert_instance.id,
        )
        order = TradingOrder(
            account_id=acct_def.id, symbol="MSFT", quantity=1.0,
            side=OrderDirection.BUY, order_type=OrderType.MARKET,
            status=OrderStatus.PENDING, transaction_id=transaction.id,
        )

        account._validate_expert_available_balance(
            order, transaction, expert_instance, current_price=400.0)

        assert stub.exclude_calls == [transaction.id]


class TestThePerInstrumentCapSizesTheHoldingOffWhatWasMEASURED:
    """GAP 3: ``_validate_single_position_size`` sized the existing holding off
    ``transaction.quantity`` -- what was ORDERED -- rather than the measured net.

    Same staleness as the close bug above (``submit_close_order_for_transaction``),
    applied one gate over. ``transaction.quantity`` is the amount the transaction was
    opened FOR; after a partial fill, a partial exit, an external close or an
    assignment it no longer describes anything the account holds. Which way it is
    wrong depends only on which direction reality drifted:

      * a partially CLOSED position is counted at its full original size, so the cap
        BLOCKS a top-up that is comfortably inside it; and
      * a position GROWN past its opening order is counted at the smaller original
        size, so the cap ADMITS a top-up that takes the real holding over it.

    The second is the money-losing direction and is the one nothing caught.

    ``transaction.get_current_open_qty()`` is the measured net -- exactly what the
    close path now uses. Note it returns a ``float``, deliberately not an
    ``Optional[float]`` (that would push ``None`` into arithmetic at ~10 call sites);
    when a fill is UNMEASURABLE it excludes it and says so loudly, and that log must
    still reach the operator from here.
    """

    def _setup(self, monkeypatch, *, ordered_qty, fills, add_qty,
               max_position_pct=10.0, price=150.0):
        """A capped expert holding ``fills``, asked to add ``add_qty`` more.

        ``ordered_qty`` is what ``transaction.quantity`` says -- the stale number.
        """
        acct_def = create_account_definition()
        account = MockAccount(acct_def.id)
        account._prices["AAPL"] = price
        # $100k account x 100% sleeve; a 10% cap is $10,000 = 66.67 shares at $150.
        expert_instance = _expert_with_cap(acct_def.id, max_position_pct=max_position_pct)
        monkeypatch.setattr(
            "ba2_common.core.instance_resolver._resolver",
            _StubExpertResolver(_StubExpertInterface(available_balance=1_000_000.0)),
        )
        transaction = create_transaction(
            symbol="AAPL", quantity=ordered_qty, side=OrderDirection.BUY,
            status=TransactionStatus.OPENED, open_price=price,
            expert_id=expert_instance.id,
        )
        for side, filled in fills:
            create_trading_order(
                account_id=acct_def.id, symbol="AAPL", quantity=filled,
                side=side, status=OrderStatus.FILLED,
                transaction_id=transaction.id, filled_qty=filled,
            )
        order = TradingOrder(
            account_id=acct_def.id, symbol="AAPL", quantity=add_qty,
            side=OrderDirection.BUY, order_type=OrderType.MARKET,
            status=OrderStatus.PENDING, transaction_id=transaction.id,
        )
        return account, order, transaction

    def test_a_partially_closed_position_is_sized_at_WHAT_REMAINS(self, monkeypatch):
        """THE DEFECT, over-strict direction: 100 bought, 60 already sold -> 40 held
        ($6,000). Adding 20 ($3,000) makes $9,000, inside the $10,000 cap. Counting
        the ORDERED 100 makes it look like $18,000 and refuses a legitimate add."""
        account, order, _txn = self._setup(
            monkeypatch, ordered_qty=100.0,
            fills=[(OrderDirection.BUY, 100.0), (OrderDirection.SELL, 60.0)],
            add_qty=20.0,
        )

        assert account._validate_position_size_limits(order) == []

    def test_a_position_grown_past_its_opening_order_is_capped_on_the_MEASURED_total(self, monkeypatch):
        """THE DEFECT, money-losing direction: the transaction was opened for 10 but
        has since been topped up to 60 held ($9,000) -- ``transaction.quantity`` still
        says 10. Adding 20 more takes the real holding to 80 ($12,000), over the
        $10,000 cap; sized off the stale 10 the cap sees $4,500 and waves it through."""
        account, order, _txn = self._setup(
            monkeypatch, ordered_qty=10.0,
            fills=[(OrderDirection.BUY, 10.0), (OrderDirection.BUY, 50.0)],
            add_qty=20.0,
        )

        errors = account._validate_position_size_limits(order)

        assert errors, "the cap must be measured against what is actually held"
        assert any("exceeding expert's max allowed $10000.00" in e for e in errors), errors

    def test_the_refusal_reports_the_MEASURED_holding_not_the_ordered_one(self, monkeypatch):
        """The message is the operator's only view of why the order was refused, and
        it carries its own copy of the number. 'Current position: 10 shares' when 60
        are held is a lie about money, and it would survive a fix that changed only
        the arithmetic."""
        account, order, _txn = self._setup(
            monkeypatch, ordered_qty=10.0,
            fills=[(OrderDirection.BUY, 10.0), (OrderDirection.BUY, 50.0)],
            add_qty=20.0,
        )

        errors = account._validate_position_size_limits(order)

        assert any("Current position: 60.0 shares ($9000.00)" in e for e in errors), errors
        # ...and the remaining headroom quoted with it: $10,000 - $9,000 = $1,000 -> 6
        # shares at $150, not the 33 that the stale 10-share reading implies.
        assert any("Can add up to 6 more shares" in e for e in errors), errors

    # --- the inverses ------------------------------------------------------

    def test_an_ordinary_untouched_position_is_unchanged(self, monkeypatch):
        """THE INVERSE #1: when nothing has drifted, ordered == measured and the gate
        behaves exactly as before. 30 held ($4,500) + 10 ($1,500) = $6,000 < $10,000."""
        account, order, _txn = self._setup(
            monkeypatch, ordered_qty=30.0, fills=[(OrderDirection.BUY, 30.0)],
            add_qty=10.0,
        )

        assert account._validate_position_size_limits(order) == []

    def test_an_ordinary_untouched_position_is_still_capped(self, monkeypatch):
        """THE INVERSE #2: and it still REFUSES when it should. 60 held ($9,000) + 20
        ($3,000) = $12,000 > $10,000."""
        account, order, _txn = self._setup(
            monkeypatch, ordered_qty=60.0, fills=[(OrderDirection.BUY, 60.0)],
            add_qty=20.0,
        )

        errors = account._validate_position_size_limits(order)

        assert any("exceeding expert's max allowed $10000.00" in e for e in errors), errors

    def test_a_fully_closed_transaction_holds_nothing(self, monkeypatch):
        """THE INVERSE #3: a measured net of ZERO is an ANSWER -- the book is flat --
        and re-opening into that transaction is capped on the new order alone. The
        ORDERED 100 would have refused it outright."""
        account, order, _txn = self._setup(
            monkeypatch, ordered_qty=100.0,
            fills=[(OrderDirection.BUY, 100.0), (OrderDirection.SELL, 100.0)],
            add_qty=20.0,
        )

        assert account._validate_position_size_limits(order) == []

    def test_a_new_position_branch_is_untouched(self, monkeypatch):
        """THE INVERSE #4: the else-branch (no matching entry order) never read the
        transaction quantity at all and must keep sizing on the order alone."""
        acct_def = create_account_definition()
        account = MockAccount(acct_def.id)
        account._prices["AAPL"] = 150.0
        expert_instance = _expert_with_cap(acct_def.id, max_position_pct=10.0)
        monkeypatch.setattr(
            "ba2_common.core.instance_resolver._resolver",
            _StubExpertResolver(_StubExpertInterface(available_balance=1_000_000.0)),
        )
        transaction = create_transaction(
            symbol="AAPL", quantity=999.0, side=OrderDirection.BUY,
            status=TransactionStatus.WAITING, open_price=150.0,
            expert_id=expert_instance.id,
        )
        order = TradingOrder(
            account_id=acct_def.id, symbol="AAPL", quantity=10.0,
            side=OrderDirection.BUY, order_type=OrderType.MARKET,
            status=OrderStatus.PENDING, transaction_id=transaction.id,
        )

        assert account._validate_position_size_limits(order) == []

    def test_a_fractional_holding_is_measured_as_such(self, monkeypatch):
        """THE INVERSE #5: Alpaca trades fractional shares. 0.5 held must not be
        rounded or truncated into 0 (or into the ordered 1)."""
        account, order, _txn = self._setup(
            monkeypatch, ordered_qty=1.0,
            fills=[(OrderDirection.BUY, 66.5), (OrderDirection.SELL, 0.25)],
            add_qty=0.5, price=150.0,
        )
        # 66.25 held = $9,937.50; + 0.5 ($75) = $10,012.50, just over the $10,000 cap.
        errors = account._validate_position_size_limits(order)

        assert any("exceeding expert's max allowed $10000.00" in e for e in errors), errors

    def test_an_OPPOSITE_side_order_is_not_an_add(self, monkeypatch):
        """Found by mutation: dropping ``entry_order.side == trading_order.side``
        survived the WHOLE root suite and packages/common -- nothing anywhere pinned
        the side test. Without it a SELL against a 60-share long is scored as though
        it GREW the position to 80 ($12,000 over a $10,000 cap) and is refused. That
        is a risk-REDUCING order blocked by a risk control; ``is_closing_order=True``
        only covers the paths that remember to set it."""
        acct_def = create_account_definition()
        account = MockAccount(acct_def.id)
        account._prices["AAPL"] = 150.0
        expert_instance = _expert_with_cap(acct_def.id, max_position_pct=10.0)
        monkeypatch.setattr(
            "ba2_common.core.instance_resolver._resolver",
            _StubExpertResolver(_StubExpertInterface(available_balance=1_000_000.0)),
        )
        transaction = create_transaction(
            symbol="AAPL", quantity=60.0, side=OrderDirection.BUY,
            status=TransactionStatus.OPENED, open_price=150.0,
            expert_id=expert_instance.id,
        )
        create_trading_order(
            account_id=acct_def.id, symbol="AAPL", quantity=60.0,
            side=OrderDirection.BUY, status=OrderStatus.FILLED,
            transaction_id=transaction.id, filled_qty=60.0,
        )
        reducing = TradingOrder(
            account_id=acct_def.id, symbol="AAPL", quantity=20.0,
            side=OrderDirection.SELL, order_type=OrderType.MARKET,
            status=OrderStatus.PENDING, transaction_id=transaction.id,
        )

        assert account._validate_position_size_limits(reducing) == []

    def test_a_zero_percent_SLEEVE_gives_this_gate_a_zero_cap(self, monkeypatch):
        """Found by mutation: ``expert_instance.virtual_equity_pct or 100.0`` here
        survived this file entirely. It is the fifth clone of the coercion that
        test_virtual_equity_zero_pct.py killed in four other places, and it lives in
        the cap's DENOMINATOR: a sleeve allocated 0% would be told it may put the
        whole account into one instrument."""
        acct_def = create_account_definition()
        account = MockAccount(acct_def.id)
        account._prices["AAPL"] = 150.0
        expert_instance = _expert_with_cap(acct_def.id, max_position_pct=100.0,
                                           virtual_equity_pct=0.0)
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
            account_id=acct_def.id, symbol="AAPL", quantity=1.0,
            side=OrderDirection.BUY, order_type=OrderType.MARKET,
            status=OrderStatus.PENDING, transaction_id=transaction.id,
        )

        errors = account._validate_position_size_limits(order)

        assert any("exceeds expert's max allowed $0.00" in e for e in errors), errors

    def test_only_EXECUTED_orders_count_as_a_holding(self, monkeypatch):
        """Found by mutation: nothing in this file distinguished a filled order from
        a queued one at this gate. A PENDING order is an intention, not a holding --
        counting it would make the cap refuse a top-up on the strength of shares
        nobody owns yet (and double-count the order under validation itself).

        The status filter must also run BEFORE the unmeasurable-fill check: a queued
        order has no ``filled_qty`` because nothing has filled, which is not the same
        as a broker that filled and would not say how much. Reporting every open
        order as UNMEASURABLE would bury the real alarm.
        """
        errors_logged = _capture_errors(monkeypatch)
        acct_def = create_account_definition()
        account = MockAccount(acct_def.id)
        account._prices["AAPL"] = 150.0
        expert_instance = _expert_with_cap(acct_def.id, max_position_pct=10.0)
        monkeypatch.setattr(
            "ba2_common.core.instance_resolver._resolver",
            _StubExpertResolver(_StubExpertInterface(available_balance=1_000_000.0)),
        )
        transaction = create_transaction(
            symbol="AAPL", quantity=60.0, side=OrderDirection.BUY,
            status=TransactionStatus.OPENED, open_price=150.0,
            expert_id=expert_instance.id,
        )
        create_trading_order(
            account_id=acct_def.id, symbol="AAPL", quantity=30.0,
            side=OrderDirection.BUY, status=OrderStatus.FILLED,
            transaction_id=transaction.id, filled_qty=30.0,
        )
        # Queued, not owned: 30 more shares the broker has not filled.
        create_trading_order(
            account_id=acct_def.id, symbol="AAPL", quantity=30.0,
            side=OrderDirection.BUY, status=OrderStatus.OPEN,
            transaction_id=transaction.id,
        )
        # 30 held ($4,500) + 20 ($3,000) = $7,500, inside the $10,000 cap.
        order = TradingOrder(
            account_id=acct_def.id, symbol="AAPL", quantity=20.0,
            side=OrderDirection.BUY, order_type=OrderType.MARKET,
            status=OrderStatus.PENDING, transaction_id=transaction.id,
        )

        assert account._validate_position_size_limits(order) == []
        assert not any("UNMEASURABLE" in m for m in errors_logged), (
            "a queued order is not an unmeasurable fill", errors_logged)

    def test_an_add_that_lands_EXACTLY_on_the_cap_is_allowed(self, monkeypatch):
        """Found by mutation (``>`` -> ``>=`` survived): the boundary is a real
        decision and nothing pinned it. A cap is a MAXIMUM -- an add that brings the
        holding to exactly $10,000.00 of a $10,000.00 cap is inside it, and the
        new-position branch three lines down already reads ``>``. 60 held ($9,000) +
        6.666666666666667 ($1,000) = $10,000.00 exactly."""
        account, order, _txn = self._setup(
            monkeypatch, ordered_qty=60.0, fills=[(OrderDirection.BUY, 60.0)],
            add_qty=1_000.0 / 150.0,
        )

        assert account._validate_position_size_limits(order) == []

    def test_an_add_ONE_CENT_over_the_cap_is_refused(self, monkeypatch):
        """Found by mutation: the only over-cap add pinned here was $2,000 over, so
        loosening the comparison to ``max_position_value + 1`` survived. A cap that
        is quietly a dollar wider than it says is a cap nobody can audit."""
        account, order, _txn = self._setup(
            monkeypatch, ordered_qty=60.0, fills=[(OrderDirection.BUY, 60.0)],
            add_qty=1_000.01 / 150.0,
        )

        errors = account._validate_position_size_limits(order)

        assert any("exceeding expert's max allowed $10000.00" in e for e in errors), errors

    def test_the_headroom_quoted_by_an_over_cap_refusal_is_never_negative(self, monkeypatch):
        """Found by mutation (``if max_additional_value > 0 else 0`` -> truthiness
        survived): when the holding is ALREADY over the cap -- the price ran up, or
        the cap was lowered under a live position -- the headroom is negative, and
        ``int(-1500/150)`` prints "Can add up to -10 more shares". A refusal that
        tells the operator to buy a negative number of shares is not an instruction
        anyone can act on. 80 held = $12,000 against a $10,000 cap."""
        account, order, _txn = self._setup(
            monkeypatch, ordered_qty=80.0, fills=[(OrderDirection.BUY, 80.0)],
            add_qty=10.0,
        )

        errors = account._validate_position_size_limits(order)

        assert any("Can add up to 0 more shares" in e for e in errors), errors
        assert not any("-" in e.split("Can add up to")[1] for e in errors
                       if "Can add up to" in e), errors

    def test_a_SHORT_holding_is_capped_on_its_magnitude(self, monkeypatch):
        """THE INVERSE #6: ``get_current_open_qty()`` returns a SIGNED net -- negative
        for a short. Without ``abs()`` a 60-share short reads as -60, so adding 20 more
        short 'reduces' the total to -40 = -$6,000, which is inside every cap however
        large the position gets. The exposure is the magnitude."""
        acct_def = create_account_definition()
        account = MockAccount(acct_def.id)
        account._prices["AAPL"] = 150.0
        expert_instance = _expert_with_cap(acct_def.id, max_position_pct=10.0)
        monkeypatch.setattr(
            "ba2_common.core.instance_resolver._resolver",
            _StubExpertResolver(_StubExpertInterface(available_balance=1_000_000.0)),
        )
        transaction = create_transaction(
            symbol="AAPL", quantity=60.0, side=OrderDirection.SELL,
            status=TransactionStatus.OPENED, open_price=150.0,
            expert_id=expert_instance.id,
        )
        create_trading_order(
            account_id=acct_def.id, symbol="AAPL", quantity=60.0,
            side=OrderDirection.SELL, status=OrderStatus.FILLED,
            transaction_id=transaction.id, filled_qty=60.0,
        )
        order = TradingOrder(
            account_id=acct_def.id, symbol="AAPL", quantity=20.0,
            side=OrderDirection.SELL, order_type=OrderType.MARKET,
            status=OrderStatus.PENDING, transaction_id=transaction.id,
        )

        errors = account._validate_position_size_limits(order)

        assert any("exceeding expert's max allowed $10000.00" in e for e in errors), errors
        assert any("Current position: 60.0 shares" in e for e in errors), errors

    def test_the_measured_net_is_SIGNED(self, monkeypatch):
        """The contract this fix now depends on, pinned at the source.

        Found by mutation: ``Transaction.get_current_open_qty`` documents "positive
        for longs, negative for shorts", and wrapping its return in ``abs()`` survived
        the ENTIRE root suite and packages/common -- every consumer today takes the
        magnitude, so nothing defends the sign. A caller that starts trusting the
        docstring (to decide a close's direction, say) would silently turn every short
        into a long."""
        from ba2_trade_platform.core.db import get_instance
        from ba2_trade_platform.core.models import Transaction

        acct_def = create_account_definition()
        short = create_transaction(
            symbol="AAPL", quantity=60.0, side=OrderDirection.SELL,
            status=TransactionStatus.OPENED, open_price=150.0,
        )
        create_trading_order(
            account_id=acct_def.id, symbol="AAPL", quantity=60.0,
            side=OrderDirection.SELL, status=OrderStatus.FILLED,
            transaction_id=short.id, filled_qty=60.0,
        )
        long = create_transaction(
            symbol="MSFT", quantity=60.0, side=OrderDirection.BUY,
            status=TransactionStatus.OPENED, open_price=400.0,
        )
        create_trading_order(
            account_id=acct_def.id, symbol="MSFT", quantity=60.0,
            side=OrderDirection.BUY, status=OrderStatus.FILLED,
            transaction_id=long.id, filled_qty=60.0,
        )

        assert get_instance(Transaction, short.id).get_current_open_qty() == -60.0
        assert get_instance(Transaction, long.id).get_current_open_qty() == 60.0

    # --- the loud log must not be swallowed here ---------------------------

    def test_an_unmeasurable_fill_is_still_reported_loudly_from_this_gate(self, monkeypatch):
        """``get_current_open_qty`` cannot express "unmeasurable" in its float return
        (an ``Optional[float]`` would push ``None`` into arithmetic at ~10 call
        sites), so it EXCLUDES the order and logs at error instead. Reading it from
        inside a risk control wrapped in a broad ``except Exception`` is exactly where
        such a log gets lost -- pin that it still reaches the operator, and that the
        gate does not crash into its catch-all."""
        errors_logged = _capture_errors(monkeypatch)
        acct_def = create_account_definition()
        account = MockAccount(acct_def.id)
        account._prices["AAPL"] = 150.0
        expert_instance = _expert_with_cap(acct_def.id, max_position_pct=10.0)
        monkeypatch.setattr(
            "ba2_common.core.instance_resolver._resolver",
            _StubExpertResolver(_StubExpertInterface(available_balance=1_000_000.0)),
        )
        transaction = create_transaction(
            symbol="AAPL", quantity=60.0, side=OrderDirection.BUY,
            status=TransactionStatus.OPENED, open_price=150.0,
            expert_id=expert_instance.id,
        )
        # FILLED, but the broker never said how much: UNMEASURABLE, not zero.
        create_trading_order(
            account_id=acct_def.id, symbol="AAPL", quantity=60.0,
            side=OrderDirection.BUY, status=OrderStatus.FILLED,
            transaction_id=transaction.id, filled_qty=None,
        )
        order = TradingOrder(
            account_id=acct_def.id, symbol="AAPL", quantity=20.0,
            side=OrderDirection.BUY, order_type=OrderType.MARKET,
            status=OrderStatus.PENDING, transaction_id=transaction.id,
        )

        errors = account._validate_position_size_limits(order)

        assert any("UNMEASURABLE" in m for m in errors_logged), (
            "the unmeasurable-fill log must not be swallowed by the risk control",
            errors_logged)
        assert not any("could not be completed" in e for e in errors), (
            "reading the measured net must not crash into the catch-all", errors)


class TestTheLimitsAreMaximaNotExclusiveBounds:
    """Found by mutation: every ``>`` in these two gates could be flipped to ``>=``
    and nothing noticed. "Max allowed $10,000" either admits an order landing on
    exactly $10,000 or it does not; both are defensible, only one is what the code
    does, and an unpinned boundary is one somebody re-decides by accident.

    All four comparisons agree: the limit is a MAXIMUM, so exactly-at-limit passes.
    """

    def _setup(self, monkeypatch, *, available_balance, max_position_pct, fills=()):
        acct_def = create_account_definition()
        account = MockAccount(acct_def.id)
        account._prices["AAPL"] = 150.0
        expert_instance = _expert_with_cap(acct_def.id, max_position_pct=max_position_pct)
        monkeypatch.setattr(
            "ba2_common.core.instance_resolver._resolver",
            _StubExpertResolver(_StubExpertInterface(available_balance=available_balance)),
        )
        transaction = create_transaction(
            symbol="AAPL", quantity=0.0, side=OrderDirection.BUY,
            status=TransactionStatus.WAITING if not fills else TransactionStatus.OPENED,
            open_price=150.0, expert_id=expert_instance.id,
        )
        for side, filled in fills:
            create_trading_order(
                account_id=acct_def.id, symbol="AAPL", quantity=filled,
                side=side, status=OrderStatus.FILLED,
                transaction_id=transaction.id, filled_qty=filled,
            )
        return account, acct_def, transaction

    def _order(self, acct_def, transaction, qty):
        return TradingOrder(
            account_id=acct_def.id, symbol="AAPL", quantity=qty,
            side=OrderDirection.BUY, order_type=OrderType.MARKET,
            status=OrderStatus.PENDING, transaction_id=transaction.id,
        )

    def test_a_new_position_landing_exactly_on_the_cap_passes(self, monkeypatch):
        """$10,000 cap, 66.666... shares at $150 = $10,000.00 exactly."""
        account, acct_def, txn = self._setup(
            monkeypatch, available_balance=1_000_000.0, max_position_pct=10.0)

        order = self._order(acct_def, txn, 10_000.0 / 150.0)

        assert account._validate_position_size_limits(order) == []

    def test_a_new_position_one_cent_over_the_cap_is_refused(self, monkeypatch):
        """The other side of the same boundary, so 'passes' cannot mean 'the gate
        stopped working'."""
        account, acct_def, txn = self._setup(
            monkeypatch, available_balance=1_000_000.0, max_position_pct=10.0)

        order = self._order(acct_def, txn, 10_000.01 / 150.0)

        assert any("exceeds expert's max allowed" in e
                   for e in account._validate_position_size_limits(order))

    def test_an_order_spending_exactly_the_available_balance_passes(self, monkeypatch):
        """A sleeve with $1,500.00 free may spend $1,500.00 of it."""
        account, acct_def, txn = self._setup(
            monkeypatch, available_balance=1_500.0, max_position_pct=100.0)

        order = self._order(acct_def, txn, 10.0)   # 10 x $150 = $1,500.00

        assert account._validate_position_size_limits(order) == []

    def test_a_top_up_spending_exactly_the_available_balance_passes(self, monkeypatch):
        """Same boundary on the add-to-position branch, which has its own comparison."""
        account, acct_def, txn = self._setup(
            monkeypatch, available_balance=1_500.0, max_position_pct=100.0,
            fills=[(OrderDirection.BUY, 1.0)])

        order = self._order(acct_def, txn, 10.0)

        assert account._validate_position_size_limits(order) == []

    def test_a_top_up_one_cent_over_the_available_balance_is_refused(self, monkeypatch):
        account, acct_def, txn = self._setup(
            monkeypatch, available_balance=1_499.99, max_position_pct=100.0,
            fills=[(OrderDirection.BUY, 1.0)])

        order = self._order(acct_def, txn, 10.0)

        assert any("exceeds expert's available balance" in e
                   for e in account._validate_position_size_limits(order))

    def test_a_new_position_one_cent_over_the_available_balance_is_refused(self, monkeypatch):
        """Found by mutation: the top-up branch had a MODEST overshoot pinned, the
        new-position branch only a huge one (10x the balance). Widening its ceiling to
        ``available_balance * 2`` therefore survived -- a doubled sleeve is precisely
        the kind of off-by-a-factor nobody spots in a message that still reads
        plausibly."""
        account, acct_def, txn = self._setup(
            monkeypatch, available_balance=1_499.99, max_position_pct=100.0)

        order = self._order(acct_def, txn, 10.0)   # $1,500.00

        assert any("exceeds expert's available balance" in e
                   for e in account._validate_position_size_limits(order))


class TestDeferredCloseAuditTrail:
    def test_the_deferred_close_records_the_MEASURED_quantity(self, monkeypatch):
        """The deferred branch writes the close order straight to the DB and logs the
        activity itself. Both must carry the measured remainder: an audit trail that
        says 100 when 60 shares were sold is a lie about money, and nothing pinned
        the activity log's copy of it."""
        import ba2_common.core.utils as _utils
        acct_def = create_account_definition()
        account = MockAccount(acct_def.id)
        logged = []
        monkeypatch.setattr(_utils, "log_close_order_activity",
                            lambda **kw: logged.append(kw))

        transaction = create_transaction(
            symbol="AAPL", quantity=100.0, side=OrderDirection.BUY,
            status=TransactionStatus.OPENED, open_price=150.0,
        )
        for side, filled in [(OrderDirection.BUY, 100.0), (OrderDirection.SELL, 40.0)]:
            create_trading_order(
                account_id=acct_def.id, symbol="AAPL", quantity=filled,
                side=side, status=OrderStatus.FILLED,
                transaction_id=transaction.id, filled_qty=filled,
            )
        blocker = create_trading_order(
            account_id=acct_def.id, symbol="AAPL", quantity=40.0,
            side=OrderDirection.SELL, status=OrderStatus.CANCELED,
            transaction_id=transaction.id,
        )

        result = account.submit_close_order_for_transaction(
            transaction, last_broker_canceled_order_id=blocker.id)

        from ba2_trade_platform.core.db import get_instance
        from ba2_trade_platform.core.models import TradingOrder as _TO
        created = get_instance(_TO, result["close_order_id"])
        assert created.quantity == 60.0
        assert created.depends_on_order == blocker.id
        assert [k["quantity"] for k in logged] == [60.0]


class TestTheEquityCloseSeamRefusesOptions:
    """An option Transaction's symbol is the UNDERLYING and its quantity is CONTRACTS.
    Building an equity order from those two fields submits N shares for N contracts."""

    def _option_txn(self, acct_def, *, contracts=2.0):
        """An OPTION transaction holding one FILLED option leg on the underlying AAPL."""
        txn = create_transaction(symbol="AAPL", quantity=contracts,
                                 asset_class=AssetClass.OPTION)
        create_trading_order(account_id=acct_def.id, symbol="AAPL", quantity=contracts,
                             transaction_id=txn.id, status=OrderStatus.FILLED,
                             filled_qty=contracts, asset_class=AssetClass.OPTION,
                             contract_symbol="AAPL260116C00250000")
        return txn

    def test_an_option_transaction_is_refused(self):
        acct_def = create_account_definition()
        account = MockAccount(acct_def.id)
        txn = self._option_txn(acct_def)
        with pytest.raises(ValueError) as exc:
            account.submit_close_order_for_transaction(txn)
        assert "close_option" in str(exc.value)
        # The refusal has to say the position was left alone, not merely that it refused.
        assert "No close order was created" in str(exc.value)

    def test_an_equity_transaction_is_still_closed(self):
        acct_def = create_account_definition()
        account = MockAccount(acct_def.id)
        txn = create_transaction(symbol="AAPL", quantity=10.0)
        create_trading_order(account_id=acct_def.id, symbol="AAPL", quantity=10.0,
                             transaction_id=txn.id, status=OrderStatus.FILLED,
                             filled_qty=10.0)
        result = account.submit_close_order_for_transaction(txn)
        assert result["success"] is True

    def test_NO_ORDER_IS_WRITTEN_when_an_option_is_refused(self):
        """Refusing must not leave a half-created equity order behind."""
        from ba2_common.core.trade_store import orders_where
        acct_def = create_account_definition()
        account = MockAccount(acct_def.id)
        txn = self._option_txn(acct_def)
        before = len(orders_where(account_id=acct_def.id))
        with pytest.raises(ValueError):
            account.submit_close_order_for_transaction(txn)
        assert len(orders_where(account_id=acct_def.id)) == before


class TestCloseTransactionRefusesOptions:
    """``close_transaction`` cancels working orders at the broker and rewrites order rows
    on its way to building the close, so refusing at the bottom would already have stripped
    an option position of its protective legs. The refusal must land BEFORE any of that."""

    def _option_txn_with_working_orders(self, acct_def):
        """An OPTION transaction carrying the two order shapes ``close_transaction`` acts on.

        ``at_broker`` is NEW — in ``get_unfilled_statuses()`` but NOT in
        ``get_unsent_statuses()`` — and carries a broker id, so the unguarded path reaches
        ``self.cancel_order(broker_order_id)``. ``unsent`` is PENDING, which IS in the unsent
        set, so the unguarded path rewrites its DB status to CLOSED. One of each is what
        makes both halves of "nothing happened" discriminating rather than vacuous.
        """
        txn = create_transaction(symbol="AAPL", quantity=1.0,
                                 asset_class=AssetClass.OPTION)
        at_broker = create_trading_order(
            account_id=acct_def.id, symbol="AAPL", quantity=1.0,
            transaction_id=txn.id, status=OrderStatus.NEW,
            broker_order_id="BRK-OPT-1",
            asset_class=AssetClass.OPTION, contract_symbol="AAPL260116C00250000")
        unsent = create_trading_order(
            account_id=acct_def.id, symbol="AAPL", quantity=1.0,
            transaction_id=txn.id, status=OrderStatus.PENDING,
            asset_class=AssetClass.OPTION, contract_symbol="AAPL260116C00250000")
        return txn, at_broker, unsent

    def _spy_on_cancel(self, monkeypatch, account):
        """Record every broker id handed to ``cancel_order``; return the list.

        A spy, not the real ``MockAccount.cancel_order``: production calls
        ``cancel_order(order.broker_order_id)`` with a STRING while the mock does
        ``order.status = ...``, so a genuine call raises AttributeError, is swallowed by
        the surrounding ``except``, and leaves ``canceled_count`` at 0 whether the guard
        fired or not. Recording the argument makes "nothing was canceled" an observation.
        """
        canceled: list = []
        monkeypatch.setattr(account, "cancel_order", canceled.append)
        return canceled

    def test_close_transaction_refuses_an_option(self):
        acct_def = create_account_definition()
        account = MockAccount(acct_def.id)
        txn = create_transaction(symbol="AAPL", quantity=1.0,
                                 asset_class=AssetClass.OPTION)
        result = account.close_transaction(txn.id)
        assert result["success"] is False
        assert "close_option" in result["message"]
        # The message must name THIS seam's hazard. "would submit shares of AAPL" is
        # submit_close_order_for_transaction's, and that path now raises before submitting
        # anything; what this caller actually risks is losing the protective legs and
        # stranding the transaction in CLOSING.
        assert "protective" in result["message"] and "CLOSING" in result["message"]
        assert "shares" not in result["message"]

    def test_close_transaction_still_closes_an_equity(self):
        acct_def = create_account_definition()
        account = MockAccount(acct_def.id)
        txn = create_transaction(symbol="AAPL", quantity=10.0)
        create_trading_order(account_id=acct_def.id, symbol="AAPL", quantity=10.0,
                             transaction_id=txn.id, status=OrderStatus.FILLED,
                             filled_qty=10.0)
        result = account.close_transaction(txn.id)
        assert result["success"] is True

    def test_an_option_close_CANCELS_NOTHING(self, monkeypatch):
        """The headline property: no broker cancellation, no order-status rewrite, and the
        transaction is not even moved to CLOSING."""
        acct_def = create_account_definition()
        account = MockAccount(acct_def.id)
        txn, at_broker, unsent = self._option_txn_with_working_orders(acct_def)
        canceled = self._spy_on_cancel(monkeypatch, account)

        result = account.close_transaction(txn.id)

        assert canceled == []            # nothing reached the broker
        assert result["success"] is False
        assert result["canceled_count"] == 0
        assert result["deleted_count"] == 0
        assert get_instance(TradingOrder, at_broker.id).status == OrderStatus.NEW
        assert get_instance(TradingOrder, unsent.id).status == OrderStatus.PENDING
        assert get_instance(Transaction, txn.id).status == TransactionStatus.OPENED

    def test_a_LOCKED_DATABASE_is_not_read_as_NOT_AN_OPTION(self, monkeypatch):
        """The guard's lookup absorbs InstanceNotFound and NOTHING else.

        A bare ``except Exception: _txn = None`` reinstates the bug the guard exists to
        prevent: a transient "database is locked" reads as "not an option, carry on", the
        re-read inside the body then succeeds, and the equity path runs over the OPTION
        anyway. ``absorb_if_benign(e, InstanceNotFound)`` propagates a non-benign exception
        under the default ``enforce`` mode, so the close fails loudly instead of quietly
        doing the damage. The failure is what we assert: no cancel, no status change.
        """
        import importlib
        from sqlalchemy.exc import OperationalError

        acct_def = create_account_definition()
        account = MockAccount(acct_def.id)
        txn, at_broker, unsent = self._option_txn_with_working_orders(acct_def)
        canceled = self._spy_on_cancel(monkeypatch, account)

        # The package module, not the class the interfaces package re-exports under the
        # same name; the in-tree shim aliases to this very object.
        ai_mod = importlib.import_module("ba2_common.core.interfaces.AccountInterface")
        real_get_instance = ai_mod.get_instance
        calls = {"n": 0}

        def flaky_get_instance(model_class, instance_id, *args, **kwargs):
            # TRANSIENT, like real lock contention: only the guard's own read fails, so a
            # swallowed error genuinely would fall through to a successful re-read.
            calls["n"] += 1
            if calls["n"] == 1:
                raise OperationalError("SELECT 1", {}, Exception("database is locked"))
            return real_get_instance(model_class, instance_id, *args, **kwargs)

        monkeypatch.setattr(ai_mod, "get_instance", flaky_get_instance)

        with pytest.raises(OperationalError):
            account.close_transaction(txn.id)

        assert canceled == []
        assert get_instance(TradingOrder, at_broker.id).status == OrderStatus.NEW
        assert get_instance(TradingOrder, unsent.id).status == OrderStatus.PENDING
        assert get_instance(Transaction, txn.id).status == TransactionStatus.OPENED

    def test_an_unresolvable_transaction_id_still_returns_a_result_dict(self):
        """The other side of the narrowing: InstanceNotFound IS named as benign.

        The guard reads the transaction up front and get_instance RAISES InstanceNotFound
        for a missing row (it never returns None). That one condition must keep falling
        through to the existing handling and come back as a result dict, not an exception."""
        acct_def = create_account_definition()
        account = MockAccount(acct_def.id)
        result = account.close_transaction(999999)
        assert result["success"] is False
        assert isinstance(result["message"], str) and result["message"]


# ===========================================================================
# OPT-L1, THE EXIT HALF — the equity close refuses to sell pledged cover
#
# THE HOLE THIS CLOSES, and it is the half that matters most. Seam 1 stopped
# the wrong-INSTRUMENT order: an option transaction can no longer be closed or
# adjusted through the equity path, and options no longer enter the allocation
# plan. It did NOT stop the dangerous half. A "set AAPL to 0%" allocation run
# walks the EQUITY transactions for AAPL and closes each one; every one of
# those is a perfectly valid equity close, so nothing above refuses it — and
# the 100 shares collateralising an open short call are sold, leaving that call
# NAKED with every guard reporting success and the option's own legs untouched.
#
# Task 5 made "are these shares spoken for?" answerable. This is the first
# refusal on the EXIT side to consume the answer.
#
# BOTH ACCESSORS ARE TRI-STATE AND ``None`` IS A REFUSAL. An unreadable option
# book is not an empty one, and ``get_positions()`` returning ``None`` is a
# FETCH FAILURE, not a flat account — the conflation that force-closed 8 real
# transactions on 2026-07-03. Selling on either unknown is precisely how a
# covered call goes naked during an outage, so both are pinned here.
# ===========================================================================
def _equity_only_account_class():
    """``MockAccount``'s behaviour WITHOUT the ``OptionsAccountInterface`` base.

    An IBKR/TastyTrade-shaped account: it can trade, it cannot hold options.
    Built from ``MockAccount``'s own namespace rather than hand-written so the
    two doubles cannot drift — the ONLY intended difference is the missing
    mixin, and re-implementing thirty broker methods by hand would make that
    claim unverifiable.

    Note what this double does NOT get: ``shares_pledged_to_short_calls`` and
    ``held_shares_for_cover`` come from the mixin, so they are simply absent
    here. A guard that asked the question without first checking the capability
    would raise AttributeError rather than quietly pass — the failure is loud.
    """
    from ba2_common.core.interfaces.AccountInterface import (
        AccountInterface as _AI,
    )
    skip = {"__dict__", "__weakref__", "__abstractmethods__", "_abc_impl",
            "__module__", "__qualname__", "__doc__"}
    body = {k: v for k, v in vars(MockAccount).items() if k not in skip}
    return type("EquityOnlyAccount", (_AI,), body)


class TestTheExitGuardRefusesToSellPledgedCover:

    # -- scaffolding -------------------------------------------------------
    OCC = "AAPL260116C00150000"

    def _long_equity_txn(self, acct_def, *, shares=100.0):
        """An ordinary long AAPL equity transaction, fully filled."""
        txn = create_transaction(symbol="AAPL", quantity=shares,
                                 side=OrderDirection.BUY,
                                 status=TransactionStatus.OPENED, open_price=150.0)
        create_trading_order(account_id=acct_def.id, symbol="AAPL", quantity=shares,
                             side=OrderDirection.BUY, status=OrderStatus.FILLED,
                             transaction_id=txn.id, filled_qty=shares)
        return txn

    def _short_equity_txn(self, acct_def, *, shares=100.0):
        """A SHORT AAPL equity transaction — its close is a BUY."""
        txn = create_transaction(symbol="AAPL", quantity=shares,
                                 side=OrderDirection.SELL,
                                 status=TransactionStatus.OPENED, open_price=150.0)
        create_trading_order(account_id=acct_def.id, symbol="AAPL", quantity=shares,
                             side=OrderDirection.SELL, status=OrderStatus.FILLED,
                             transaction_id=txn.id, filled_qty=shares)
        return txn

    def _open_short_call(self, acct_def, *, contracts=1.0, multiplier=100,
                         underlying="AAPL"):
        """One open, filled SHORT CALL on ``underlying`` — the thing that pledges."""
        from ba2_trade_platform.core.types import OptionRight
        txn = create_transaction(symbol=underlying, quantity=contracts,
                                 side=OrderDirection.SELL,
                                 asset_class=AssetClass.OPTION)
        return create_trading_order(
            account_id=acct_def.id, symbol=self.OCC, quantity=contracts,
            side=OrderDirection.SELL, status=OrderStatus.FILLED,
            transaction_id=txn.id, filled_qty=contracts,
            asset_class=AssetClass.OPTION, multiplier=multiplier,
            contract_symbol=self.OCC, underlying_symbol=underlying,
            option_type=OptionRight.CALL, strike=150.0)

    def _holding(self, account, shares, symbol="AAPL"):
        """What ``get_positions()`` hands back for a long equity lot."""
        from types import SimpleNamespace
        account._positions = [SimpleNamespace(
            symbol=symbol, qty=shares, qty_available=shares,
            side=OrderDirection.BUY, asset_class="us_equity")]

    def _recording_account(self, acct_def, monkeypatch, *, cls=MockAccount):
        """An account whose broker submissions are recorded, not performed."""
        account = cls(acct_def.id)
        submitted = []
        monkeypatch.setattr(account, "submit_order",
                            lambda o, **k: submitted.append(o) or o)
        return account, submitted

    # -- the refusal -------------------------------------------------------
    def test_selling_shares_pledged_to_a_short_call_is_refused(self, monkeypatch):
        """THE HEADLINE. 100 shares held, one short call written against them:
        selling the lot leaves that call naked, and nothing used to ask."""
        acct_def = create_account_definition()
        account, submitted = self._recording_account(acct_def, monkeypatch)
        self._holding(account, 100.0)
        self._open_short_call(acct_def)
        txn = self._long_equity_txn(acct_def, shares=100.0)

        result = account.submit_close_order_for_transaction(txn)

        assert result["success"] is False
        message = result["message"]
        assert PLEDGED_COVER_REFUSAL in message, message
        assert "100" in message, "the HELD and PLEDGED counts must be named: " + message
        assert "pledged" in message, message
        assert "short by 100" in message, "the SHORTFALL must be named: " + message
        assert result["close_order_id"] is None
        assert submitted == [], "the broker was asked to sell the cover"

    def test_the_refusal_names_the_remedy_that_actually_works(self, monkeypatch):
        """A cover refusal is fixed by releasing the cover, never by funding the
        account — the opposite of both money refusals, and the reason it carries
        its own marker rather than the entry seam's."""
        acct_def = create_account_definition()
        account, _ = self._recording_account(acct_def, monkeypatch)
        self._holding(account, 100.0)
        self._open_short_call(acct_def)
        txn = self._long_equity_txn(acct_def, shares=100.0)

        message = account.submit_close_order_for_transaction(txn)["message"]

        assert "short call" in message.lower(), message
        assert "NAKED" in message, message

    def test_selling_UNPLEDGED_shares_is_allowed(self, monkeypatch):
        """THE INVERSE. No short call, so no claim: the everyday close is
        completely unaffected. A guard that refuses everything is not a guard."""
        acct_def = create_account_definition()
        account, submitted = self._recording_account(acct_def, monkeypatch)
        self._holding(account, 100.0)
        txn = self._long_equity_txn(acct_def, shares=100.0)

        result = account.submit_close_order_for_transaction(txn)

        assert result["success"] is True, result["message"]
        assert len(submitted) == 1
        assert submitted[0].quantity == 100.0
        assert submitted[0].side == OrderDirection.SELL

    def test_selling_only_the_UNPLEDGED_EXCESS_is_allowed(self, monkeypatch):
        """150 held, 100 pledged, sell 50: the call keeps its cover and the free
        shares stay sellable. Refusing this would freeze an account's stock the
        moment one contract is written against any part of it."""
        acct_def = create_account_definition()
        account, submitted = self._recording_account(acct_def, monkeypatch)
        self._holding(account, 150.0)
        self._open_short_call(acct_def)
        txn = self._long_equity_txn(acct_def, shares=50.0)

        result = account.submit_close_order_for_transaction(txn)

        assert result["success"] is True, result["message"]
        assert len(submitted) == 1 and submitted[0].quantity == 50.0

    def test_the_boundary_is_exact(self, monkeypatch):
        """Leaving EXACTLY the pledged quantity behind is admitted; one share
        fewer is not. A cap whose boundary nobody pinned drifts by one."""
        for sell, expect_ok in ((50.0, True), (51.0, False)):
            acct_def = create_account_definition()
            account, submitted = self._recording_account(acct_def, monkeypatch)
            self._holding(account, 150.0)
            self._open_short_call(acct_def)
            txn = self._long_equity_txn(acct_def, shares=sell)

            result = account.submit_close_order_for_transaction(txn)

            assert result["success"] is expect_ok, f"selling {sell}: {result['message']}"
            assert bool(submitted) is expect_ok

    def test_the_multiplier_is_honoured_so_an_adjusted_contract_pledges_its_own_size(
            self, monkeypatch):
        """OPT-L7 reaches this seam through the accessor: a contract delivering
        130 shares pledges 130, and 100 shares no longer cover it."""
        acct_def = create_account_definition()
        account, submitted = self._recording_account(acct_def, monkeypatch)
        self._holding(account, 100.0)
        self._open_short_call(acct_def, multiplier=130)
        txn = self._long_equity_txn(acct_def, shares=100.0)

        result = account.submit_close_order_for_transaction(txn)

        assert result["success"] is False
        assert "130" in result["message"], result["message"]
        assert submitted == []

    def test_a_short_call_on_ANOTHER_underlying_does_not_block_this_one(self, monkeypatch):
        """Cover is per ticker. An MSFT call has no claim on AAPL shares."""
        acct_def = create_account_definition()
        account, submitted = self._recording_account(acct_def, monkeypatch)
        self._holding(account, 100.0)
        self._open_short_call(acct_def, underlying="MSFT")
        txn = self._long_equity_txn(acct_def, shares=100.0)

        result = account.submit_close_order_for_transaction(txn)

        assert result["success"] is True, result["message"]
        assert len(submitted) == 1

    # -- UNKNOWN is a refusal, and the two unknowns read differently -------
    def test_an_UNMEASURABLE_PLEDGE_is_refused_and_names_the_option_book(
            self, monkeypatch):
        """A book that cannot be read is not an empty book. The shares may
        already be spoken for and there is no way to find out."""
        from sqlalchemy.exc import OperationalError

        acct_def = create_account_definition()
        account, submitted = self._recording_account(acct_def, monkeypatch)
        self._holding(account, 100.0)
        txn = self._long_equity_txn(acct_def, shares=100.0)
        # A LOCKED DATABASE -- benign, so the accessor absorbs it into UNKNOWN
        # rather than propagating. That is the case the tri-state exists for.
        monkeypatch.setattr(account, "open_option_orders_book_wide", lambda: (_ for _ in ()).throw(
            OperationalError("SELECT 1", {}, Exception("database is locked"))))

        result = account.submit_close_order_for_transaction(txn)

        assert result["success"] is False
        message = result["message"]
        assert PLEDGED_COVER_REFUSAL in message, message
        assert "OPTION BOOK" in message, "send the operator to the option book: " + message
        assert "position feed" not in message.lower(), (
            "the position feed was readable — blaming it sends the operator to the "
            "wrong system: " + message)
        assert submitted == []

    def test_an_UNMEASURABLE_HOLDING_with_a_pledge_open_is_refused_and_names_the_feed(
            self, monkeypatch):
        """``get_positions()`` returning None is a FETCH FAILURE, not a flat
        account. With a call already written, selling on that is the outage case
        that strips its cover."""
        acct_def = create_account_definition()
        account, submitted = self._recording_account(acct_def, monkeypatch)
        self._open_short_call(acct_def)
        txn = self._long_equity_txn(acct_def, shares=100.0)
        monkeypatch.setattr(account, "get_positions", lambda: None)

        result = account.submit_close_order_for_transaction(txn)

        assert result["success"] is False
        message = result["message"]
        assert PLEDGED_COVER_REFUSAL in message, message
        assert "POSITION FEED" in message, "send the operator to the feed: " + message
        assert "option book" not in message.lower(), (
            "the option book was readable — blaming it sends the operator to the "
            "wrong system: " + message)
        assert submitted == []

    def test_an_unmeasurable_holding_with_NOTHING_pledged_is_never_even_asked(
            self, monkeypatch):
        """The position feed is consulted ONLY once a pledge is known to exist.

        Not an optimisation detail: ``held_shares_for_cover`` calls
        ``get_positions()``, which on a live broker is a network round trip, and
        an account with no short calls would otherwise pay for one on every
        single equity close. It is also the difference between "a broker blip
        blocks nothing" and "a broker blip blocks every exit on the platform".
        """
        acct_def = create_account_definition()
        account, submitted = self._recording_account(acct_def, monkeypatch)
        txn = self._long_equity_txn(acct_def, shares=100.0)
        calls = []

        def _explode():
            calls.append(1)
            return None

        monkeypatch.setattr(account, "get_positions", _explode)

        result = account.submit_close_order_for_transaction(txn)

        assert calls == [], "the position feed was fetched with nothing pledged"
        assert result["success"] is True, result["message"]
        assert len(submitted) == 1

    # -- what the guard must NOT touch ------------------------------------
    def test_a_BUY_side_close_is_unaffected(self, monkeypatch):
        """Closing a SHORT equity position BUYS shares back, which can only ADD
        cover. Refusing it would strand a short nobody can flatten — strictly
        worse than the naked call it does not prevent. Both feeds are broken
        here on purpose: a BUY must not consult them at all."""
        acct_def = create_account_definition()
        account, submitted = self._recording_account(acct_def, monkeypatch)
        self._open_short_call(acct_def)
        txn = self._short_equity_txn(acct_def, shares=100.0)
        monkeypatch.setattr(account, "get_positions", lambda: None)
        monkeypatch.setattr(account, "open_option_orders_book_wide",
                            lambda: (_ for _ in ()).throw(AssertionError(
                                "a BUY-side close must not read the option book")))

        result = account.submit_close_order_for_transaction(txn)

        assert result["success"] is True, result["message"]
        assert len(submitted) == 1 and submitted[0].side == OrderDirection.BUY

    def test_a_NON_OPTIONS_account_is_completely_unaffected(self, monkeypatch):
        """An account that cannot hold options has nothing pledged, by
        construction, and must not pay to be told so.

        The double has no cover accessors at all (they live on the mixin), so a
        guard that asked without checking the capability would raise
        AttributeError here rather than quietly pass.
        """
        acct_def = create_account_definition()
        account, submitted = self._recording_account(
            acct_def, monkeypatch, cls=_equity_only_account_class())
        assert not hasattr(account, "shares_pledged_to_short_calls")
        self._holding(account, 100.0)
        # A short call IS in the book. An options account would refuse on it;
        # this one cannot own it, so the row is simply not its business.
        self._open_short_call(acct_def)
        txn = self._long_equity_txn(acct_def, shares=100.0)

        result = account.submit_close_order_for_transaction(txn)

        assert result["success"] is True, result["message"]
        assert len(submitted) == 1 and submitted[0].quantity == 100.0

    def test_an_already_flat_transaction_still_reports_flat_not_pledged(self, monkeypatch):
        """The guard sits AFTER the flat check, so a transaction with nothing
        left to sell keeps its own (successful) answer. Refusing a no-op close
        would make ``close_transaction`` retry a close it must never send."""
        acct_def = create_account_definition()
        account, submitted = self._recording_account(acct_def, monkeypatch)
        self._holding(account, 100.0)
        self._open_short_call(acct_def)
        txn = create_transaction(symbol="AAPL", quantity=100.0,
                                 side=OrderDirection.BUY,
                                 status=TransactionStatus.OPENED, open_price=150.0)
        for side in (OrderDirection.BUY, OrderDirection.SELL):
            create_trading_order(account_id=acct_def.id, symbol="AAPL", quantity=100.0,
                                 side=side, status=OrderStatus.FILLED,
                                 transaction_id=txn.id, filled_qty=100.0)

        result = account.submit_close_order_for_transaction(txn)

        assert result["success"] is True
        assert "flat" in result["message"].lower(), result["message"]
        assert submitted == []

    # -- nothing is written --------------------------------------------------
    def test_a_REFUSAL_WRITES_NO_ORDER(self, monkeypatch):
        """Counted before and after, on both the immediate and the DEFERRED
        branch. A refusal that leaves a close order behind is worse than the
        naked call it prevented: the book then claims an exit the broker has
        never heard of, and the deferred branch writes straight to the DB
        without going through ``submit_order`` at all.
        """
        from ba2_common.core.trade_store import orders_where

        acct_def = create_account_definition()
        account, submitted = self._recording_account(acct_def, monkeypatch)
        self._holding(account, 100.0)
        self._open_short_call(acct_def)
        txn = self._long_equity_txn(acct_def, shares=100.0)
        blocker = create_trading_order(
            account_id=acct_def.id, symbol="AAPL", quantity=100.0,
            side=OrderDirection.SELL, status=OrderStatus.CANCELED,
            transaction_id=txn.id)
        before = len(orders_where(account_id=acct_def.id))

        immediate = account.submit_close_order_for_transaction(txn)
        deferred = account.submit_close_order_for_transaction(
            txn, last_broker_canceled_order_id=blocker.id)

        assert immediate["success"] is False and deferred["success"] is False
        assert len(orders_where(account_id=acct_def.id)) == before, \
            "a refused close left an order row behind"
        assert submitted == []

    def test_the_refusal_reaches_close_transaction_as_a_failed_result(self, monkeypatch):
        """The convention this guard chose, end to end.

        ``close_transaction`` copies ``success``/``message`` straight through, so
        a dict refusal arrives at the allocation runner as a failed row carrying
        the reason — where a raise would be re-wrapped by that method's outer
        handler as ``Error: <str>`` with a stack trace, presenting a routine,
        self-clearing refusal as a crash.
        """
        acct_def = create_account_definition()
        account, submitted = self._recording_account(acct_def, monkeypatch)
        self._holding(account, 100.0)
        self._open_short_call(acct_def)
        txn = self._long_equity_txn(acct_def, shares=100.0)

        result = account.close_transaction(txn.id)

        assert result["success"] is False
        assert PLEDGED_COVER_REFUSAL in result["message"], result["message"]
        assert "Error:" not in result["message"], (
            "a raise would have been re-wrapped by the outer handler: "
            + result["message"])
        assert submitted == []


# ===========================================================================
# THE SECOND LOT — the guard must subtract a close it has already authorised
#
# The guard as first written compared THIS transaction's size against a live,
# account-wide ``held``, with no account of the closes the same run had already
# created. ``_close_symbol`` walks EVERY transaction for a symbol, and the
# codebase explicitly models multi-lot holdings (``split_delta_fifo``: "30 shares
# held as 20 + 10"). 200 AAPL held as two 100-share lots with 100 pledged to one
# short call therefore admitted BOTH closes — the guard's own scenario, defeated
# by holding the shares in two rows instead of one.
#
# ON THE DEFERRED BRANCH THAT IS DETERMINISTIC, NOT A RACE. Whenever
# ``close_transaction`` cancelled a TP/SL the close order is written PENDING and
# nothing goes to the broker, so ``get_positions()`` CANNOT decrement between
# iterations however slowly the loop runs. Both branches are pinned below; the
# deferred one is the one that must never regress.
# ===========================================================================
class TestTheExitGuardNetsSalesAlreadyInFlight:

    OCC = TestTheExitGuardRefusesToSellPledgedCover.OCC
    _long_equity_txn = TestTheExitGuardRefusesToSellPledgedCover._long_equity_txn
    _open_short_call = TestTheExitGuardRefusesToSellPledgedCover._open_short_call
    _holding = TestTheExitGuardRefusesToSellPledgedCover._holding

    def _accepting_account(self, acct_def, monkeypatch):
        """A broker that ACCEPTS a market SELL and has not printed it yet.

        The honest state of the world between submission and fill, and the only
        one in which the immediate branch's hazard is visible: the order row is
        persisted and working, ``get_positions()`` still reports every share.
        A double that filled instantly AND decremented the holding would hide the
        window; one that filled instantly and did NOT decrement would invent it.
        """
        from ba2_trade_platform.core.db import add_instance

        account = MockAccount(acct_def.id)
        sent = []

        def _submit(order, **kwargs):
            sent.append(order)
            order.status = OrderStatus.NEW
            order.account_id = account.id
            order.broker_order_id = f"bkr-{len(sent)}"
            if not order.id:
                add_instance(order, expunge_after_flush=True)
            return order

        monkeypatch.setattr(account, "submit_order", _submit)
        return account, sent

    def _two_lots(self, acct_def, account, *, held=200.0, lot=100.0, contracts=1.0):
        """``held`` shares of AAPL carried as TWO transactions, one short call."""
        self._holding(account, held)
        self._open_short_call(acct_def, contracts=contracts)
        return (self._long_equity_txn(acct_def, shares=lot),
                self._long_equity_txn(acct_def, shares=lot))

    # -- the two branches --------------------------------------------------
    def test_the_DEFERRED_close_of_a_second_lot_sees_the_first(self, monkeypatch):
        """THE DETERMINISTIC ONE. Both closes are written PENDING behind a
        cancelled TP/SL, so nothing reaches the broker and no fill can decrement
        the holding between them. Before the netting both rows were written and
        200 of 200 shares were queued to sell against a 100-share pledge."""
        from ba2_common.core.trade_store import orders_where

        acct_def = create_account_definition()
        account, sent = self._accepting_account(acct_def, monkeypatch)
        first, second = self._two_lots(acct_def, account)
        blocker = create_trading_order(
            account_id=acct_def.id, symbol="AAPL", quantity=100.0,
            side=OrderDirection.SELL, status=OrderStatus.CANCELED)

        one = account.submit_close_order_for_transaction(
            first, last_broker_canceled_order_id=blocker.id)
        two = account.submit_close_order_for_transaction(
            second, last_broker_canceled_order_id=blocker.id)

        assert one["success"] is True, one["message"]
        assert two["success"] is False, "the second lot did not see the first"
        assert PLEDGED_COVER_REFUSAL in two["message"], two["message"]
        assert "already committed to be sold" in two["message"], two["message"]
        queued = [o for o in orders_where(account_id=acct_def.id)
                  if o.side == OrderDirection.SELL
                  and o.order_type == OrderType.MARKET
                  and o.status == OrderStatus.PENDING]
        assert [o.quantity for o in queued] == [100.0], \
            "200 of the 200 held shares were queued to sell against a 100-share pledge"
        assert sent == [], "the deferred branch must not reach the broker at all"

    def test_the_IMMEDIATE_close_of_a_second_lot_sees_the_first(self, monkeypatch):
        """The same hole with the order actually sent. The broker has accepted
        the first market SELL and not yet printed it, so the position feed still
        reports 200 — which is exactly when the second close must not be sized
        against it."""
        acct_def = create_account_definition()
        account, sent = self._accepting_account(acct_def, monkeypatch)
        first, second = self._two_lots(acct_def, account)

        one = account.submit_close_order_for_transaction(first)
        two = account.submit_close_order_for_transaction(second)

        assert one["success"] is True, one["message"]
        assert two["success"] is False, "the second lot did not see the first"
        assert PLEDGED_COVER_REFUSAL in two["message"], two["message"]
        assert [(o.symbol, o.side, o.quantity) for o in sent] == \
            [("AAPL", OrderDirection.SELL, 100.0)], \
            "both lots reached the broker and the short call was left naked"

    def test_a_run_that_can_afford_BOTH_lots_still_closes_both(self, monkeypatch):
        """THE INVERSE, and the reason netting is not just 'refuse more'. 300
        held, 100 pledged: two 100-share lots can both go and the call keeps its
        cover. A guard that counted in-flight sales without doing the arithmetic
        would stop the second one anyway."""
        acct_def = create_account_definition()
        account, sent = self._accepting_account(acct_def, monkeypatch)
        first, second = self._two_lots(acct_def, account, held=300.0)

        one = account.submit_close_order_for_transaction(first)
        two = account.submit_close_order_for_transaction(second)

        assert one["success"] is True and two["success"] is True, two["message"]
        assert [o.quantity for o in sent] == [100.0, 100.0]

    # -- what must NOT be netted ------------------------------------------
    def test_a_transactions_OWN_working_close_is_not_counted_against_itself(
            self, monkeypatch):
        """A lot's own working SELL and the close being proposed dispose of the
        SAME shares, and both exit seams cancel the lot's legs on the way. Count
        them together and every retry of a protected close refuses forever, with
        nothing an operator could do to clear it."""
        acct_def = create_account_definition()
        account, sent = self._accepting_account(acct_def, monkeypatch)
        self._holding(account, 200.0)
        self._open_short_call(acct_def)
        txn = self._long_equity_txn(acct_def, shares=100.0)
        create_trading_order(
            account_id=acct_def.id, symbol="AAPL", quantity=100.0,
            side=OrderDirection.SELL, order_type=OrderType.MARKET,
            status=OrderStatus.PENDING, transaction_id=txn.id,
            comment="Closing position for transaction")

        result = account.submit_close_order_for_transaction(txn)

        assert result["success"] is True, result["message"]
        assert len(sent) == 1

    def test_a_FILLED_close_is_not_subtracted_a_second_time(self, monkeypatch):
        """A close that FILLED has already left the position and
        ``get_positions()`` reports the smaller holding. Subtracting it again
        would refuse a sale the account can plainly make.

        Recorded as belt-and-braces rather than as a single-mutation
        discriminator: TWO independent things keep a filled close out of the
        total — the ACTIVE status filter, and ``ordered − filled`` being zero —
        so widening either one alone changes nothing. Both must go at once for
        this to fail, which is exactly the property worth stating.
        """
        acct_def = create_account_definition()
        account, sent = self._accepting_account(acct_def, monkeypatch)
        self._holding(account, 200.0)          # the fill is already reflected here
        self._open_short_call(acct_def)
        other = self._long_equity_txn(acct_def, shares=100.0)
        create_trading_order(
            account_id=acct_def.id, symbol="AAPL", quantity=100.0,
            side=OrderDirection.SELL, order_type=OrderType.MARKET,
            status=OrderStatus.FILLED, filled_qty=100.0, transaction_id=other.id)
        txn = self._long_equity_txn(acct_def, shares=100.0)

        result = account.submit_close_order_for_transaction(txn)

        assert result["success"] is True, result["message"]
        assert len(sent) == 1

    def test_a_CANCELED_close_row_gives_its_shares_back(self, monkeypatch):
        """A terminal row will never sell anything. Leaving it counted would
        freeze the shares of every close that was ever abandoned."""
        acct_def = create_account_definition()
        account, sent = self._accepting_account(acct_def, monkeypatch)
        self._holding(account, 200.0)
        self._open_short_call(acct_def)
        other = self._long_equity_txn(acct_def, shares=100.0)
        create_trading_order(
            account_id=acct_def.id, symbol="AAPL", quantity=100.0,
            side=OrderDirection.SELL, order_type=OrderType.MARKET,
            status=OrderStatus.CANCELED, transaction_id=other.id)
        txn = self._long_equity_txn(acct_def, shares=100.0)

        result = account.submit_close_order_for_transaction(txn)

        assert result["success"] is True, result["message"]
        assert len(sent) == 1

    def test_a_RESTING_PROTECTIVE_LEG_is_not_netted__a_recorded_gap(self, monkeypatch):
        """DELIBERATE, and pinned so that changing it is a decision.

        A stop-loss on the covering shares really can strip a call, and counting
        it here was tried. It cannot stay: ``close_transaction`` cancels a
        transaction's legs AT THE BROKER without terminalising the DB rows (they
        clear on the next ``refresh_orders``), so every later exit on the ticker
        refused on the strength of orders that no longer existed — a guard that
        blocks every exit is its own incident. ``MARKET`` is therefore the test
        for "a sale already decided", which every close path on this platform
        satisfies and no protective leg ever does.

        Charging a stop against the cover it can strip belongs where the stop is
        WRITTEN. If that is ever built here instead, this test is the one to
        delete, on purpose, with the stale-cancel problem solved first.
        """
        acct_def = create_account_definition()
        account, sent = self._accepting_account(acct_def, monkeypatch)
        self._holding(account, 200.0)
        self._open_short_call(acct_def)
        other = self._long_equity_txn(acct_def, shares=100.0)
        create_trading_order(
            account_id=acct_def.id, symbol="AAPL", quantity=100.0,
            side=OrderDirection.SELL, order_type=OrderType.SELL_STOP,
            status=OrderStatus.NEW, stop_price=140.0, transaction_id=other.id,
            broker_order_id="bkr-stop")
        txn = self._long_equity_txn(acct_def, shares=100.0)

        result = account.submit_close_order_for_transaction(txn)

        assert result["success"] is True, result["message"]
        assert len(sent) == 1

    def test_a_working_close_on_ANOTHER_SYMBOL_is_not_netted(self, monkeypatch):
        """Cover is per ticker and so is the supply against it."""
        acct_def = create_account_definition()
        account, sent = self._accepting_account(acct_def, monkeypatch)
        self._holding(account, 200.0)
        self._open_short_call(acct_def)
        create_trading_order(
            account_id=acct_def.id, symbol="MSFT", quantity=100.0,
            side=OrderDirection.SELL, order_type=OrderType.MARKET,
            status=OrderStatus.PENDING)
        txn = self._long_equity_txn(acct_def, shares=100.0)

        result = account.submit_close_order_for_transaction(txn)

        assert result["success"] is True, result["message"]
        assert len(sent) == 1

    def test_an_OPTION_sell_order_is_not_counted_as_shares(self, monkeypatch):
        """One contract is not one share. A working option SELL carries the
        UNDERLYING in ``underlying_symbol`` and a contract count in ``quantity``;
        read as shares it would subtract 100 from a share supply of 200 and
        refuse a close the account can plainly make. Short PUTs so the pledge
        itself is untouched and the asset-class filter is the only thing on
        trial."""
        from ba2_trade_platform.core.types import OptionRight
        put = "AAPL260116P00150000"
        acct_def = create_account_definition()
        account, sent = self._accepting_account(acct_def, monkeypatch)
        self._holding(account, 200.0)
        self._open_short_call(acct_def)
        create_trading_order(
            account_id=acct_def.id, symbol="AAPL", quantity=100.0,
            side=OrderDirection.SELL, order_type=OrderType.MARKET,
            status=OrderStatus.PENDING, asset_class=AssetClass.OPTION,
            multiplier=100, contract_symbol=put, underlying_symbol="AAPL",
            option_type=OptionRight.PUT, strike=150.0)
        txn = self._long_equity_txn(acct_def, shares=100.0)

        result = account.submit_close_order_for_transaction(txn)

        assert result["success"] is True, result["message"]
        assert len(sent) == 1

    # -- partial fills and unknowns ---------------------------------------
    def test_only_the_UNFILLED_REMAINDER_of_a_working_close_is_netted(self, monkeypatch):
        """A close of 100 that has printed 60 has already taken 60 out of the
        holding the feed reports; only the 40 still working is still to come.
        Netting the whole 100 would double-count the filled part and refuse a
        legitimate close."""
        acct_def = create_account_definition()
        account, sent = self._accepting_account(acct_def, monkeypatch)
        self._holding(account, 240.0)          # 300 bought, 60 of the other lot gone
        self._open_short_call(acct_def)
        other = self._long_equity_txn(acct_def, shares=100.0)
        create_trading_order(
            account_id=acct_def.id, symbol="AAPL", quantity=100.0,
            side=OrderDirection.SELL, order_type=OrderType.MARKET,
            status=OrderStatus.PARTIALLY_FILLED, filled_qty=60.0,
            transaction_id=other.id)
        txn = self._long_equity_txn(acct_def, shares=100.0)

        # 240 held − 40 still working − 100 sold now = 100 left, exactly the pledge.
        result = account.submit_close_order_for_transaction(txn)

        assert result["success"] is True, result["message"]
        assert len(sent) == 1

        # ...and one share more is over the line.
        bigger = self._long_equity_txn(acct_def, shares=101.0)
        refused = account.submit_close_order_for_transaction(bigger)
        assert refused["success"] is False, refused["message"]

    def test_a_filled_qty_ABOVE_the_ordered_quantity_does_not_ADD_supply(
            self, monkeypatch):
        """A damaged row is not a NEGATIVE commitment.

        ``ordered − filled`` is −50 on a close of 100 that reports 150 filled, and
        without the floor that number is SUBTRACTED from the working total —
        inventing 50 shares of headroom out of a corrupted row, in the one
        direction that uncovers a call. The clean 100-share close beside it is
        what makes the sign visible: the total must be 100, not 50.

        (The floor is redundant in the two option views, whose ``> _ASSIGNMENT_EPS``
        test already drops a negative remainder. It is load-bearing here, where
        the remainder is summed unconditionally, which is why the case is pinned
        on this accessor.)
        """
        acct_def = create_account_definition()
        account, sent = self._accepting_account(acct_def, monkeypatch)
        self._holding(account, 250.0)
        self._open_short_call(acct_def)
        clean = self._long_equity_txn(acct_def, shares=100.0)
        damaged = self._long_equity_txn(acct_def, shares=100.0)
        create_trading_order(
            account_id=acct_def.id, symbol="AAPL", quantity=100.0,
            side=OrderDirection.SELL, order_type=OrderType.MARKET,
            status=OrderStatus.PENDING, transaction_id=clean.id)
        create_trading_order(
            account_id=acct_def.id, symbol="AAPL", quantity=100.0,
            side=OrderDirection.SELL, order_type=OrderType.MARKET,
            status=OrderStatus.PARTIALLY_FILLED, filled_qty=150.0,
            transaction_id=damaged.id)
        txn = self._long_equity_txn(acct_def, shares=100.0)

        # 250 held − 100 working − 100 sold now = 50 left against a 100 pledge.
        result = account.submit_close_order_for_transaction(txn)

        assert result["success"] is False, result["message"]
        assert sent == []

    def test_a_working_SELL_with_no_usable_SIZE_is_refused_and_names_the_order_book(
            self, monkeypatch):
        """UNKNOWN is not zero here either, and it is a THIRD system to repair.

        A working SELL for 0 shares is a damaged row, not an order that sells
        nothing — ``submit_order`` refuses to send one for exactly that reason
        ("a zero-quantity TP/SL is cancelled by the broker and leaves the
        position unprotected"). How many shares it will really take is unknown,
        and unknown must not resolve to the number that frees the cover.

        The message must not send the operator to the option book or the position
        feed: both answered.
        """
        acct_def = create_account_definition()
        account, sent = self._accepting_account(acct_def, monkeypatch)
        self._holding(account, 200.0)
        self._open_short_call(acct_def)
        other = self._long_equity_txn(acct_def, shares=100.0)
        create_trading_order(
            account_id=acct_def.id, symbol="AAPL", quantity=0.0,
            side=OrderDirection.SELL, order_type=OrderType.MARKET,
            status=OrderStatus.PENDING, transaction_id=other.id)
        txn = self._long_equity_txn(acct_def, shares=100.0)

        result = account.submit_close_order_for_transaction(txn)

        assert result["success"] is False
        message = result["message"]
        assert PLEDGED_COVER_REFUSAL in message, message
        assert "does not report a usable size" in message, message
        assert "option book" not in message.lower(), (
            "the option book answered — blaming it sends the operator to the "
            "wrong system: " + message)
        assert "position feed" not in message.lower(), (
            "the position feed answered — blaming it sends the operator to the "
            "wrong system: " + message)
        assert sent == []

    def test_a_LOCKED_DATABASE_refuses_rather_than_reading_an_empty_order_book(
            self, monkeypatch):
        """The tri-state's absorb half, on the third accessor.

        A locked database is the world being uncooperative, so it is absorbed
        into UNKNOWN rather than propagated — and UNKNOWN refuses. Reading it as
        "nothing is working" is how the netting silently stops existing during
        exactly the outage in which two runners are most likely to overlap.
        """
        from sqlalchemy.exc import OperationalError
        from ba2_common.core import trade_store

        acct_def = create_account_definition()
        account, sent = self._accepting_account(acct_def, monkeypatch)
        self._holding(account, 200.0)
        self._open_short_call(acct_def)
        txn = self._long_equity_txn(acct_def, shares=100.0)
        real = trade_store.orders_where

        def _locked(**kwargs):
            if kwargs.get("statuses") is not None:
                raise OperationalError("SELECT 1", {}, Exception("database is locked"))
            return real(**kwargs)

        monkeypatch.setattr(trade_store, "orders_where", _locked)

        result = account.submit_close_order_for_transaction(txn)

        assert result["success"] is False
        assert PLEDGED_COVER_REFUSAL in result["message"], result["message"]
        assert sent == []

    def test_a_DEFECT_in_the_order_book_read_still_propagates(self, monkeypatch):
        """The other half. Anything outside the benign set is a defect, and a
        defect that quietly answered "unmeasurable" forever would be a gate that
        has silently stopped working."""
        from ba2_common.core import trade_store

        acct_def = create_account_definition()
        account, sent = self._accepting_account(acct_def, monkeypatch)
        self._holding(account, 200.0)
        self._open_short_call(acct_def)
        txn = self._long_equity_txn(acct_def, shares=100.0)
        real = trade_store.orders_where

        def _broken(**kwargs):
            if kwargs.get("statuses") is not None:
                raise TypeError("orders_where() got an unexpected keyword argument")
            return real(**kwargs)

        monkeypatch.setattr(trade_store, "orders_where", _broken)

        with pytest.raises(TypeError):
            account.submit_close_order_for_transaction(txn)
        assert sent == []

    def test_the_working_order_book_is_not_read_when_NOTHING_is_pledged(
            self, monkeypatch):
        """Same discipline as the position feed: a third read, and on a live
        broker a third chance for an outage to block every exit on the platform.
        An account with no short calls must not pay for it."""
        acct_def = create_account_definition()
        account, sent = self._accepting_account(acct_def, monkeypatch)
        self._holding(account, 200.0)
        txn = self._long_equity_txn(acct_def, shares=100.0)
        calls = []
        monkeypatch.setattr(account, "equity_shares_working_to_sell",
                            lambda *a, **k: calls.append(1) or None)

        result = account.submit_close_order_for_transaction(txn)

        assert calls == [], "the working order book was read with nothing pledged"
        assert result["success"] is True, result["message"]
        assert len(sent) == 1
