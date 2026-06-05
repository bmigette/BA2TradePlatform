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


from ba2_trade_platform.core.models import Transaction
from ba2_trade_platform.core.types import TransactionStatus


def test_current_open_equity_applies_option_multiplier(mock_account_def):
    txn_id = add_instance(Transaction(
        symbol="AAPL", quantity=2, side=OrderDirection.BUY,
        status=TransactionStatus.OPENED, open_price=5.2,
    ))
    add_instance(TradingOrder(
        account_id=mock_account_def.id, symbol="AAPL", quantity=2,
        side=OrderDirection.BUY, order_type=OrderType.MARKET,
        status=OrderStatus.FILLED, filled_qty=2, open_price=5.2,
        transaction_id=txn_id, asset_class=AssetClass.OPTION, multiplier=100,
    ))
    txn = get_instance(Transaction, txn_id)
    # 2 contracts * $5.2 premium * 100 multiplier = $1040
    assert txn.get_current_open_equity() == 1040.0


def test_current_open_equity_equity_order_unchanged(mock_account_def):
    txn_id = add_instance(Transaction(
        symbol="AAPL", quantity=10, side=OrderDirection.BUY,
        status=TransactionStatus.OPENED, open_price=150.0,
    ))
    add_instance(TradingOrder(
        account_id=mock_account_def.id, symbol="AAPL", quantity=10,
        side=OrderDirection.BUY, order_type=OrderType.MARKET,
        status=OrderStatus.FILLED, filled_qty=10, open_price=150.0,
        transaction_id=txn_id,  # asset_class defaults to equity, multiplier None
    ))
    txn = get_instance(Transaction, txn_id)
    assert txn.get_current_open_equity() == 1500.0


def test_pending_open_equity_uses_option_premium(mock_account_def):
    # An UNFILLED option BUY_LIMIT entry: equity = contracts * premium * 100,
    # and must NOT require the underlying market price.
    txn_id = add_instance(Transaction(
        symbol="AAPL", quantity=2, side=OrderDirection.BUY,
        status=TransactionStatus.WAITING,
    ))
    add_instance(TradingOrder(
        account_id=mock_account_def.id, symbol="AAPL", quantity=2,
        side=OrderDirection.BUY, order_type=OrderType.BUY_LIMIT,
        status=OrderStatus.PENDING, limit_price=5.2, transaction_id=txn_id,
        asset_class=AssetClass.OPTION, multiplier=100,
    ))
    txn = get_instance(Transaction, txn_id)
    assert txn.get_pending_open_equity(account_interface=None) == 1040.0
