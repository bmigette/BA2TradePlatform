"""Preserve caller quantities at Alpaca's wire boundary; broker calls are mocked.

Fractional simple market/limit/stop/stop-limit orders require DAY. Complex-order
support is a separate broker constraint: rejection must never become a smaller
successful order or a canceled sub-share exit.
"""
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from alpaca.common.exceptions import APIError
from alpaca.trading.enums import TimeInForce, OrderClass

from ba2_trade_platform.core.db import add_instance, get_instance
from ba2_trade_platform.core.models import TradingOrder
from ba2_trade_platform.core.types import OrderDirection, OrderStatus, OrderType
from ba2_trade_platform.modules.accounts.AlpacaAccount import AlpacaAccount


def _alpaca_response(request):
    return SimpleNamespace(
        id="brk-1", symbol=request.symbol, qty=str(request.qty), side=request.side,
        type=request.type, status="new", time_in_force=request.time_in_force,
        order_class=None, legs=None, filled_qty="0", filled_avg_price=None,
        created_at=None, limit_price=None, stop_price=None)


def _bare_account():
    acct = object.__new__(AlpacaAccount)
    acct.id = 1
    acct.client = MagicMock()
    acct.client.submit_order.side_effect = _alpaca_response
    acct._margin_info_cache = {}
    acct._balance_cache_lock = threading.Lock()
    acct._balance_cache_time = 0.0
    return acct


def _saved_order(**kwargs):
    defaults = dict(account_id=1, symbol="AAPL", quantity=1.5,
                    side=OrderDirection.BUY, order_type=OrderType.MARKET,
                    status=OrderStatus.PENDING, good_for=None)
    defaults.update(kwargs)
    return get_instance(TradingOrder, add_instance(TradingOrder(**defaults)))


def _submitted_request(acct):
    return acct.client.submit_order.call_args[0][0]


# Test both sub-share holdings and quantities beyond two decimal places.
@pytest.mark.parametrize("quantity", [0.4018, 4.2537])
@pytest.mark.parametrize("order_type,side,prices", [
    (OrderType.MARKET, OrderDirection.BUY, {}),
    (OrderType.MARKET, OrderDirection.SELL, {}),
    (OrderType.BUY_LIMIT, OrderDirection.BUY, {"limit_price": 100.0}),
    (OrderType.SELL_LIMIT, OrderDirection.SELL, {"limit_price": 120.0}),
    (OrderType.BUY_STOP, OrderDirection.BUY, {"stop_price": 110.0}),
    (OrderType.SELL_STOP, OrderDirection.SELL, {"stop_price": 90.0}),
    (OrderType.BUY_STOP_LIMIT, OrderDirection.BUY,
     {"stop_price": 110.0, "limit_price": 111.0}),
    (OrderType.SELL_STOP_LIMIT, OrderDirection.SELL,
     {"stop_price": 90.0, "limit_price": 89.5}),
    (OrderType.OCO, OrderDirection.SELL,
     {"limit_price": 120.0, "stop_price": 90.0}),
])
def test_fractional_entry_protection_and_close_preserve_exact_quantity(
        quantity, order_type, side, prices):
    acct = _bare_account()
    order = _saved_order(quantity=quantity, order_type=order_type, side=side,
                         good_for="gtc", **prices)
    result = acct._submit_order_impl(
        order, is_closing_order=(order_type == OrderType.MARKET and side == OrderDirection.SELL))
    request = _submitted_request(acct)
    assert acct.client.submit_order.call_count == 1
    assert float(request.qty) == quantity
    assert request.time_in_force == TimeInForce.DAY
    assert getattr(request, "notional", None) is None
    assert order.quantity == quantity
    assert result is not None
    stored = get_instance(TradingOrder, order.id)
    assert stored.quantity == quantity
    assert stored.good_for == "day"
    if order_type == OrderType.OCO:
        assert request.order_class == OrderClass.OCO
        assert request.take_profit.limit_price == prices["limit_price"]
        assert request.stop_loss.stop_price == prices["stop_price"]


def test_fractional_market_with_unset_duration_uses_day():
    acct = _bare_account()
    acct._submit_order_impl(_saved_order(quantity=0.25))
    assert _submitted_request(acct).time_in_force == TimeInForce.DAY


@pytest.mark.parametrize("order_type,prices", [
    (OrderType.MARKET, {}), (OrderType.BUY_LIMIT, {"limit_price": 100.0}),
    (OrderType.SELL_STOP, {"stop_price": 90.0}),
])
@pytest.mark.parametrize("duration,expected", [(None, TimeInForce.GTC), ("day", TimeInForce.DAY)])
def test_whole_share_orders_keep_existing_duration(order_type, prices, duration, expected):
    acct = _bare_account()
    acct._submit_order_impl(_saved_order(quantity=3.0, order_type=order_type,
                                         good_for=duration, **prices))
    assert _submitted_request(acct).qty == 3.0
    assert _submitted_request(acct).time_in_force == expected


@pytest.mark.parametrize("quantity", [0.4018, 4.2537])
def test_wash_trade_complex_request_does_not_resize(quantity):
    acct = _bare_account()
    acct._submit_order_impl(_saved_order(quantity=quantity), tp_price=120.0,
                            sl_price=90.0, use_complex_order=True)
    request = _submitted_request(acct)
    assert request.qty == quantity
    assert request.order_class == OrderClass.BRACKET
    assert request.time_in_force == TimeInForce.DAY


@pytest.mark.parametrize("quantity", [0.4018, 4.2537])
def test_broker_rejection_is_an_error_never_a_smaller_order_or_silent_skip(quantity):
    acct = _bare_account()
    acct.client.submit_order.side_effect = APIError(
        '{"code":42210000,"message":"fractional orders must be simple orders"}')
    order = _saved_order(quantity=quantity, side=OrderDirection.SELL,
                         order_type=OrderType.OCO, limit_price=120.0, stop_price=90.0)
    result = acct._submit_order_impl(order)
    assert result is None
    assert acct.client.submit_order.call_count == 1
    assert _submitted_request(acct).qty == quantity
    stored = get_instance(TradingOrder, order.id)
    assert stored.quantity == quantity
    assert stored.status == OrderStatus.ERROR
    assert "fractional orders must be simple" in stored.comment


@pytest.mark.parametrize("quantity", [0.4018, 4.2537])
def test_replacement_preserves_fraction_and_day_duration(quantity):
    acct = _bare_account()
    order = _saved_order(quantity=quantity, side=OrderDirection.SELL,
                         order_type=OrderType.SELL_STOP, stop_price=90.0, good_for="gtc")
    acct.client.get_order_by_id.return_value = SimpleNamespace(qty=str(quantity))
    acct.client.replace_order_by_id.side_effect = lambda **kw: _alpaca_response(
        SimpleNamespace(symbol="AAPL", qty=quantity,
                        side="sell", type="stop", time_in_force=kw['order_data'].time_in_force))
    result = acct.modify_order("old-broker-id", order)
    assert result is not None
    request = acct.client.replace_order_by_id.call_args.kwargs['order_data']
    assert request.qty is None
    assert request.time_in_force == TimeInForce.DAY
    assert result.quantity == quantity


@pytest.mark.parametrize("quantity", [0.4018, 4.2537])
def test_fractional_resize_is_not_silently_rounded(quantity):
    acct = _bare_account()
    acct.client.replace_order_by_id.side_effect = lambda **kw: _alpaca_response(
        SimpleNamespace(symbol="AAPL", qty=quantity, side="sell", type="stop",
                        time_in_force=kw['order_data'].time_in_force))
    order = _saved_order(quantity=quantity, side=OrderDirection.SELL,
                         order_type=OrderType.SELL_STOP, stop_price=90.0)

    # A fractional replacement carries no qty field. This keeps the broker's exact
    # existing quantity; the request cannot invent a rounded replacement quantity.
    result = acct.modify_order("old-broker-id", order)
    assert result is not None
    request = acct.client.replace_order_by_id.call_args.kwargs['order_data']
    assert request.qty is None
    assert result.quantity == quantity
