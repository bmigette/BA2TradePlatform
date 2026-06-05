from datetime import date
from ba2_trade_platform.core.db import get_instance, add_instance
from ba2_trade_platform.core.models import TradingOrder
from ba2_trade_platform.core.types import (
    AssetClass, OptionRight, OrderDirection, OrderType, OrderStatus,
)


def test_equity_order_defaults_to_equity_asset_class(mock_account_def):
    oid = add_instance(TradingOrder(
        account_id=mock_account_def.id, symbol="AAPL", quantity=10,
        side=OrderDirection.BUY, order_type=OrderType.MARKET,
        status=OrderStatus.PENDING,
    ))
    o = get_instance(TradingOrder, oid)
    assert o.asset_class == AssetClass.EQUITY
    assert o.contract_symbol is None
    assert o.multiplier is None


def test_option_order_persists_contract_metadata(mock_account_def):
    oid = add_instance(TradingOrder(
        account_id=mock_account_def.id, symbol="AAPL", quantity=2,
        side=OrderDirection.BUY, order_type=OrderType.BUY_LIMIT,
        status=OrderStatus.PENDING, limit_price=5.2,
        asset_class=AssetClass.OPTION, contract_symbol="AAPL260116C00150000",
        option_type=OptionRight.CALL, strike=150.0, expiry=date(2026, 1, 16),
        underlying_symbol="AAPL", multiplier=100,
        position_intent="buy_to_open", option_strategy="long_call",
    ))
    o = get_instance(TradingOrder, oid)
    assert o.asset_class == AssetClass.OPTION
    assert o.contract_symbol == "AAPL260116C00150000"
    assert o.option_type == OptionRight.CALL
    assert o.strike == 150.0
    assert o.expiry == date(2026, 1, 16)
    assert o.underlying_symbol == "AAPL"
    assert o.multiplier == 100
    assert o.position_intent == "buy_to_open"
    assert o.option_strategy == "long_call"
