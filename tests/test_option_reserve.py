from ba2_common.core import trade_store as ts
from ba2_trade_platform.core.interfaces.OptionsAccountInterface import OptionsAccountInterface as OAI
from ba2_trade_platform.core.db import add_instance
from ba2_trade_platform.core.models import TradingOrder, Transaction
from ba2_trade_platform.core.types import (
    AssetClass, OrderStatus, OrderDirection, OrderType, TransactionStatus,
)


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


def test_reserve_is_released_when_the_position_closes(mock_account):
    """Regression: an option BP reserve must be released the moment its structure CLOSES.

    The reserve was summed over every order not in a TERMINAL status — but FILLED is not
    terminal and nothing ever cleared ``data["option_reserve"]`` or terminalised the entry
    order, so each credit/naked structure ever opened consumed buying power FOREVER (a
    one-way ratchet). On the options grid's $20k account that exhausted BP after 1-3
    structures: the credit groups (OS2/OS3, which reserve) capped out at 10-20 trades all
    clustered in the run's first weeks, while the debit groups (OS1/OS4, which reserve
    nothing) traded 43-214 times over the same window. The reserve belongs to the POSITION,
    so it is now counted only while the owning transaction is still open."""
    txn = Transaction(symbol="AAPL", quantity=1, open_price=1.0,
                      status=TransactionStatus.OPENED, side=OrderDirection.SELL)
    txn_id = add_instance(txn)
    add_instance(TradingOrder(
        account_id=mock_account.id, symbol="AAPL", quantity=1,
        side=OrderDirection.SELL, order_type=OrderType.SELL_LIMIT,
        status=OrderStatus.FILLED, asset_class=AssetClass.OPTION,
        option_strategy="cash_secured_put", transaction_id=txn_id,
        data={"option_reserve": 18000.0}))

    # While the structure is held the reserve ties up buying power.
    assert mock_account.reserved_option_buying_power() == 18000.0

    # Closing the structure must give the buying power back.
    from ba2_trade_platform.core.db import get_instance, update_instance
    stored = get_instance(Transaction, txn_id)
    stored.status = TransactionStatus.CLOSED
    update_instance(stored)
    assert mock_account.reserved_option_buying_power() == 0.0
    assert mock_account.available_option_buying_power() == 100000.0


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


def test_available_option_buying_power_uses_the_option_tradable_balance(mock_account, monkeypatch):
    """Base is get_option_tradable_balance(), not get_balance(): a levered option sleeve
    gets the levered figure; with margin off the two are equal and nothing changes."""
    # A hypothetical 2x option sleeve. No orders are seeded, so the reserve pool is
    # measurable and empty and the whole levered base is available.
    monkeypatch.setattr(mock_account, "get_option_tradable_balance",
                        lambda: 2 * mock_account.get_balance())
    assert mock_account.available_option_buying_power() == 2 * mock_account.get_balance()

    # The reserve still comes off the LEVERED base, not the plain balance.
    add_instance(TradingOrder(account_id=mock_account.id, symbol="AAPL", quantity=2,
        side=OrderDirection.SELL, order_type=OrderType.SELL_LIMIT, status=OrderStatus.FILLED,
        asset_class=AssetClass.OPTION, option_strategy="cash_secured_put",
        data={"option_reserve": 30000.0}))
    assert mock_account.available_option_buying_power() == 170000.0


def test_available_option_buying_power_is_none_when_the_option_tradable_balance_raises(
        mock_account, monkeypatch):
    """An unreadable tradable balance is UNKNOWN, not zero and not the plain balance:
    None (which every gate refuses on), plus a logged error so it is not silent."""
    def _boom():
        raise ValueError("balance unavailable")
    monkeypatch.setattr(mock_account, "get_option_tradable_balance", _boom)

    from ba2_common import logger as logger_module
    errors = []
    monkeypatch.setattr(logger_module.logger, "error",
                        lambda msg, *a, **k: errors.append(str(msg)))

    assert mock_account.available_option_buying_power() is None
    assert mock_account.check_option_buying_power(1.0) is False
    assert any("balance unavailable" in m for m in errors), errors
