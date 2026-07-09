"""Tests for the broker-agnostic order-error taxonomy (BrokerOrderErrorReason) and the
centralized retry/comment-recording logic in AccountInterface._handle_order_submit_error.

Root incident: a live sell_stop was rejected by Alpaca because the stop price had already
been breached by the market ("stop price must be less than current price"); the order was
marked ERROR and the position was left unprotected, and separately the Alpaca error message
never reached the order's `comment` (a stale in-memory TradingOrder overwrote it downstream).
These tests cover the fix: _classify_order_error maps a broker's native error onto a shared
enum, and _handle_order_submit_error uses that classification to retry a breached stop as a
MARKET order (broker-agnostic — the retry lives once in AccountInterface, not per broker) or
else record the typed reason + message in `comment` without ever silently losing it.
"""
import pytest

from tests.conftest import MockAccount
from tests.factories import create_account_definition
from ba2_trade_platform.core.db import add_instance, get_instance
from ba2_trade_platform.core.interfaces.AccountInterface import AccountInterface
from ba2_trade_platform.core.models import TradingOrder
from ba2_trade_platform.core.types import (
    BrokerOrderErrorReason, OrderDirection, OrderStatus, OrderType,
)


class _RaisingAccount(MockAccount):
    """A MockAccount that goes through the REAL AccountInterface.submit_order template
    (unlike MockAccount, which overrides submit_order directly and bypasses it), so
    _submit_order_impl's raised exception reaches _handle_order_submit_error for real."""

    submit_order = AccountInterface.submit_order  # un-shadow MockAccount's override

    def __init__(self, id_or_definition):
        super().__init__(id_or_definition)
        self._raise_on_submit: Exception | None = None
        self._classify_result = BrokerOrderErrorReason.UNKNOWN
        self._market_retry_succeeds = True

    def _classify_order_error(self, exc):
        return self._classify_result

    def _submit_order_impl(self, trading_order, tp_price=None, sl_price=None, is_closing_order=False):
        if self._raise_on_submit is not None and trading_order.order_type != OrderType.MARKET:
            raise self._raise_on_submit
        if trading_order.order_type == OrderType.MARKET and not self._market_retry_succeeds:
            raise RuntimeError("market retry also broken")
        trading_order.status = OrderStatus.FILLED
        trading_order.broker_order_id = "bo-1"
        return trading_order


def _make_stop_order(account_id: int) -> TradingOrder:
    order = TradingOrder(
        account_id=account_id, symbol="COHU", quantity=1.0,
        side=OrderDirection.SELL, order_type=OrderType.SELL_STOP,
        stop_price=52.88, status=OrderStatus.WAITING_TRIGGER,
        comment="20260708163141-SL-[ACC:1/TR:115/PORD:407] (chained on cancel of 411)",
    )
    order_id = add_instance(order)
    return get_instance(TradingOrder, order_id)


class TestDefaultClassification:
    def test_default_classify_is_unknown(self):
        acct_def = create_account_definition()
        account = MockAccount(acct_def.id)  # base default, not overridden
        assert account._classify_order_error(RuntimeError("boom")) == BrokerOrderErrorReason.UNKNOWN


class TestHandleOrderSubmitErrorUnknownReason:
    def test_marks_error_and_appends_reason_to_comment_without_losing_original(self):
        acct_def = create_account_definition()
        account = _RaisingAccount(acct_def.id)
        order = _make_stop_order(acct_def.id)
        original_comment = order.comment

        account._classify_result = BrokerOrderErrorReason.UNKNOWN
        result = account._handle_order_submit_error(order, RuntimeError("weird broker fault"))

        assert result is None
        updated = get_instance(TradingOrder, order.id)
        assert updated.status == OrderStatus.ERROR
        assert original_comment in updated.comment, "original chaining comment must survive"
        assert "[unknown]" in updated.comment
        assert "weird broker fault" in updated.comment


class TestHandleOrderSubmitErrorStopThroughMarket:
    def test_retries_as_market_and_succeeds(self):
        acct_def = create_account_definition()
        account = _RaisingAccount(acct_def.id)
        order = _make_stop_order(acct_def.id)

        account._classify_result = BrokerOrderErrorReason.STOP_THROUGH_MARKET
        account._raise_on_submit = RuntimeError(
            '{"code":42210000,"message":"stop price must be less than current price"}'
        )
        result = account._handle_order_submit_error(order, account._raise_on_submit)

        assert result is not None
        assert result.status == OrderStatus.FILLED
        updated = get_instance(TradingOrder, order.id)
        assert updated.order_type == OrderType.MARKET
        assert updated.stop_price is None
        assert "auto-converted to MARKET" in updated.comment
        assert updated.status != OrderStatus.ERROR  # the retry succeeded

    def test_market_retry_failure_still_marks_error_with_both_messages(self):
        acct_def = create_account_definition()
        account = _RaisingAccount(acct_def.id)
        order = _make_stop_order(acct_def.id)

        account._classify_result = BrokerOrderErrorReason.STOP_THROUGH_MARKET
        account._market_retry_succeeds = False
        account._raise_on_submit = RuntimeError("stop already breached")
        result = account._handle_order_submit_error(order, account._raise_on_submit)

        assert result is None
        updated = get_instance(TradingOrder, order.id)
        assert updated.status == OrderStatus.ERROR
        assert "market retry also failed" in updated.comment

    def test_non_stop_order_type_does_not_trigger_market_retry(self):
        """STOP_THROUGH_MARKET only makes sense for stop-type orders; a limit order
        misclassified this way must not be silently converted to MARKET."""
        acct_def = create_account_definition()
        account = _RaisingAccount(acct_def.id)
        order = TradingOrder(
            account_id=acct_def.id, symbol="COHU", quantity=1.0,
            side=OrderDirection.SELL, order_type=OrderType.SELL_LIMIT,
            limit_price=52.88, status=OrderStatus.PENDING,
        )
        order_id = add_instance(order)
        order = get_instance(TradingOrder, order_id)

        account._classify_result = BrokerOrderErrorReason.STOP_THROUGH_MARKET
        result = account._handle_order_submit_error(order, RuntimeError("n/a"))

        assert result is None
        updated = get_instance(TradingOrder, order.id)
        assert updated.status == OrderStatus.ERROR
        assert updated.order_type == OrderType.SELL_LIMIT  # unchanged, no market conversion


class TestAlpacaClassifyOrderError:
    def _api_error(self, code: int, message: str):
        from alpaca.common.exceptions import APIError
        import json
        return APIError(json.dumps({"code": code, "message": message}))

    def _account(self):
        from ba2_trade_platform.modules.accounts.AlpacaAccount import AlpacaAccount
        acct_def = create_account_definition()
        return AlpacaAccount.__new__(AlpacaAccount), acct_def  # bypass __init__ (no broker conn needed)

    def test_stop_through_market(self):
        account, _ = self._account()
        exc = self._api_error(42210000, "stop price must be less than current price")
        assert account._classify_order_error(exc) == BrokerOrderErrorReason.STOP_THROUGH_MARKET

    def test_invalid_symbol_same_code_different_message(self):
        """Regression guard: 42210000 is NOT a unique key — this message must NOT be
        misclassified as a breached stop just because the code matches."""
        account, _ = self._account()
        exc = self._api_error(42210000, "invalid underlying symbols: BRK-B")
        assert account._classify_order_error(exc) == BrokerOrderErrorReason.INVALID_SYMBOL

    def test_insufficient_buying_power(self):
        account, _ = self._account()
        exc = self._api_error(40310000, "insufficient buying power")
        assert account._classify_order_error(exc) == BrokerOrderErrorReason.INSUFFICIENT_FUNDS

    def test_wash_trade_same_code_different_message(self):
        """Regression guard: 40310000 also is NOT unique — must not collide with
        insufficient-buying-power."""
        account, _ = self._account()
        exc = self._api_error(40310000, "potential wash trade detected")
        assert account._classify_order_error(exc) == BrokerOrderErrorReason.WASH_TRADE

    def test_unmapped_code_is_unknown(self):
        account, _ = self._account()
        exc = self._api_error(99999999, "some new alpaca error nobody mapped yet")
        assert account._classify_order_error(exc) == BrokerOrderErrorReason.UNKNOWN

    def test_non_api_error_is_unknown(self):
        account, _ = self._account()
        assert account._classify_order_error(ConnectionError("network down")) == BrokerOrderErrorReason.UNKNOWN
