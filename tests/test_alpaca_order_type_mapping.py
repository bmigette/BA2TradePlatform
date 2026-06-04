"""AlpacaAccount._map_order_type: Alpaca's non-directional order type + side ->
our directional OrderType. Regression for limit/stop/stop_limit collapsing to MARKET."""
from types import SimpleNamespace
import pytest

from ba2_trade_platform.modules.accounts.AlpacaAccount import AlpacaAccount
from ba2_trade_platform.core.types import OrderType, OrderDirection


@pytest.mark.parametrize("atype,side,expected", [
    ("market", OrderDirection.BUY, OrderType.MARKET),
    ("market", OrderDirection.SELL, OrderType.MARKET),
    ("limit", OrderDirection.BUY, OrderType.BUY_LIMIT),
    ("limit", OrderDirection.SELL, OrderType.SELL_LIMIT),
    ("stop", OrderDirection.BUY, OrderType.BUY_STOP),
    ("stop", OrderDirection.SELL, OrderType.SELL_STOP),
    ("stop_limit", OrderDirection.BUY, OrderType.BUY_STOP_LIMIT),
    ("stop_limit", OrderDirection.SELL, OrderType.SELL_STOP_LIMIT),
    ("trailing_stop", OrderDirection.SELL, OrderType.TRAILING_STOP),
])
def test_map_order_type(atype, side, expected):
    assert AlpacaAccount._map_order_type(atype, side) == expected


def test_map_order_type_handles_enum_like_value():
    # Alpaca returns enum objects exposing .value
    assert AlpacaAccount._map_order_type(SimpleNamespace(value="limit"), OrderDirection.SELL) == OrderType.SELL_LIMIT


def test_map_order_type_unknown_defaults_market():
    assert AlpacaAccount._map_order_type("something_new", OrderDirection.BUY) == OrderType.MARKET
    assert AlpacaAccount._map_order_type(None, OrderDirection.BUY) == OrderType.MARKET
