"""Tests for TradeManager order processing logic."""
import pytest
from unittest.mock import MagicMock, patch
from ba2_trade_platform.core.TradeManager import TradeManager
from ba2_trade_platform.core.models import TradingOrder
from ba2_trade_platform.core.types import OrderStatus, OrderDirection, OrderType
from tests.conftest import MockAccount
from tests.factories import create_account_definition, create_trading_order


class TestTriggerAndPlaceOrder:
    def _make_order(self, account_id):
        return TradingOrder(
            account_id=account_id, symbol="AAPL", quantity=10.0,
            side=OrderDirection.BUY, order_type=OrderType.MARKET,
            status=OrderStatus.PENDING,
        )

    def test_places_order_when_status_matches(self):
        acct_def = create_account_definition()
        account = MockAccount(acct_def.id)
        order = self._make_order(acct_def.id)
        tm = TradeManager()

        result = tm.trigger_and_place_order(
            account, order,
            parent_status=OrderStatus.FILLED,
            trigger_status=OrderStatus.FILLED,
        )
        assert result is not None
        assert order.status == OrderStatus.OPEN

    def test_does_not_place_when_status_mismatch(self):
        acct_def = create_account_definition()
        account = MockAccount(acct_def.id)
        order = self._make_order(acct_def.id)
        tm = TradeManager()

        result = tm.trigger_and_place_order(
            account, order,
            parent_status=OrderStatus.PENDING,
            trigger_status=OrderStatus.FILLED,
        )
        assert result is None

    def test_returns_none_when_submit_fails(self):
        acct_def = create_account_definition()
        account = MockAccount(acct_def.id)
        account._submit_order_result = False
        order = self._make_order(acct_def.id)
        tm = TradeManager()

        result = tm.trigger_and_place_order(
            account, order,
            parent_status=OrderStatus.FILLED,
            trigger_status=OrderStatus.FILLED,
        )
        assert result is None
