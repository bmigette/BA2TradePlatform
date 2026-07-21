from ba2_common.core import trade_store as ts
from ba2_trade_platform.core.interfaces.OptionsAccountInterface import OptionsAccountInterface as OAI
from ba2_trade_platform.core.db import add_instance
from ba2_trade_platform.core.models import TradingOrder
from ba2_trade_platform.core.types import AssetClass, OrderStatus, OrderDirection, OrderType


def test_reserve_required_csp():
    assert OAI.option_reserve_required("cash_secured_put", 2, strike=150.0) == 30000.0
    assert OAI.option_reserve_required("cash_secured_put", 0, strike=150.0) == 0.0


def test_reserve_required_credit_spread():
    # width 5, credit 1.5 -> max loss 3.5 * 100 * 1 = 350
    assert OAI.option_reserve_required("bear_call_spread", 1, spread_width=5.0, net_credit=1.5) == 350.0


def test_reserve_required_long_strategies_zero():
    assert OAI.option_reserve_required("long_call", 5, strike=150.0) == 0.0


def test_available_and_check(mock_account):
    # balance 100000; seed one open CSP order reserving 30000
    add_instance(TradingOrder(account_id=mock_account.id, symbol="AAPL", quantity=2,
        side=OrderDirection.SELL, order_type=OrderType.SELL_LIMIT, status=OrderStatus.FILLED,
        asset_class=AssetClass.OPTION, option_strategy="cash_secured_put",
        data={"option_reserve": 30000.0}))
    assert mock_account.reserved_option_buying_power() == 30000.0
    assert mock_account.available_option_buying_power() == 70000.0
    assert mock_account.check_option_buying_power(50000.0) is True
    assert mock_account.check_option_buying_power(80000.0) is False
    # a terminal (closed) order's reserve is released
    add_instance(TradingOrder(account_id=mock_account.id, symbol="AAPL", quantity=1,
        side=OrderDirection.SELL, order_type=OrderType.SELL_LIMIT, status=OrderStatus.CLOSED,
        asset_class=AssetClass.OPTION, option_strategy="cash_secured_put",
        data={"option_reserve": 99999.0}))
    assert mock_account.reserved_option_buying_power() == 30000.0  # closed one excluded


def test_reserved_option_buying_power_sees_inmem_orders(mock_account):
    """Regression (platform_wide_review_2026-07-18.md P1): reserved_option_buying_power() used a
    RAW select(TradingOrder) that silently saw an empty table whenever the SQL-less in-memory
    "dict trades" backtest store is active — every check_option_buying_power() call then passed
    as if no other option order held a reserve, letting a backtest over-commit buying power
    across concurrent option positions. Fixed by routing through orders_where() (the dual-path
    helper), matching the rest of the account-layer queries."""
    with ts.inmem_trades():
        ts.store_add(TradingOrder(
            account_id=mock_account.id, symbol="AAPL", quantity=2,
            side=OrderDirection.SELL, order_type=OrderType.SELL_LIMIT, status=OrderStatus.FILLED,
            asset_class=AssetClass.OPTION, option_strategy="cash_secured_put",
            data={"option_reserve": 30000.0},
        ))
        # before the fix this was 0.0 (the raw SQL query never sees in-mem orders)
        assert mock_account.reserved_option_buying_power() == 30000.0
        assert mock_account.available_option_buying_power() == 70000.0
        assert mock_account.check_option_buying_power(80000.0) is False
