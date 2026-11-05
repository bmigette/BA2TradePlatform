"""The fixed world the Overview golden tests run on: two accounts, three experts, closed/open trades spread over the
last 70 days (relative to ``NOW``), orders in every status, and labelled instruments. ``NOW`` is frozen so the
period windows are reproducible. Used by ``capture_stats.py`` (run once on the pre-extraction code) and by
``test_stats_golden.py``."""
from datetime import datetime, timedelta

from ba2_trade_platform.core.db import add_instance
from ba2_trade_platform.core.models import Instrument
from ba2_trade_platform.core.types import (InstrumentType, OrderDirection, OrderStatus, OrderType,
                                           TransactionStatus)
from tests.factories import (create_account_definition, create_expert_instance, create_trading_order,
                             create_transaction)

NOW = datetime(2026, 11, 4, 12, 0, 0)


def _closed(symbol, days_ago, open_price, close_price, *, expert_id, account_id, side=OrderDirection.BUY, qty=10.0):
    tx = create_transaction(symbol=symbol, quantity=qty, side=side, status=TransactionStatus.CLOSED,
                            open_price=open_price, close_price=close_price, expert_id=expert_id,
                            close_date=NOW - timedelta(days=days_ago))
    create_trading_order(account_id, symbol=symbol, transaction_id=tx.id, status=OrderStatus.FILLED)
    return tx


def seed_world():
    """Returns ``{"acct1", "acct2", "e1", "e2", "e3"}`` ids."""
    a1 = create_account_definition(name="Alpaca Paper")
    a2 = create_account_definition(name="Manual TT")
    e1 = create_expert_instance(account_id=a1.id, alias="Alpha")
    e2 = create_expert_instance(account_id=a1.id, alias=None)
    e3 = create_expert_instance(account_id=a1.id, alias="Gamma", enabled=False)

    _closed("AAPL", 2, 100.0, 110.0, expert_id=e1.id, account_id=a1.id)           # +100, inside 7d
    _closed("MSFT", 5, 200.0, 190.0, expert_id=e1.id, account_id=a1.id)           # -100, inside 7d
    _closed("NVDA", 10, 50.0, 56.0, expert_id=e2.id, account_id=a1.id)            # +60, 7-14d
    _closed("TSLA", 12, 300.0, 280.0, expert_id=e2.id, account_id=a1.id, side=OrderDirection.SELL)  # short +200, 7-14d
    _closed("AMD", 20, 80.0, 70.0, expert_id=e1.id, account_id=a1.id)             # -100, 14-30d
    _closed("META", 25, 400.0, 404.0, expert_id=e3.id, account_id=a1.id, qty=5.0)  # +20, 14-30d
    _closed("INTC", 45, 30.0, 33.0, expert_id=e1.id, account_id=a1.id)            # +30, 30-60d
    _closed("KO", 50, 60.0, 59.0, expert_id=e2.id, account_id=a1.id)              # -10, 30-60d
    _closed("PEP", 65, 150.0, 160.0, expert_id=e1.id, account_id=a1.id)           # outside every window
    _closed("SPY", 3, 500.0, 505.0, expert_id=None, account_id=a2.id, qty=2.0)    # manual account, no expert
    # closed, but P&L cannot be computed (no close price): counted nowhere, summed as nothing
    tx = create_transaction(symbol="IBM", quantity=1.0, status=TransactionStatus.CLOSED, open_price=100.0,
                            close_price=None, expert_id=e1.id, close_date=NOW - timedelta(days=1))
    create_trading_order(a1.id, symbol="IBM", transaction_id=tx.id, status=OrderStatus.FILLED)

    for sym, expert in (("GOOG", e1), ("AMZN", e2)):
        tx = create_transaction(symbol=sym, quantity=3.0, status=TransactionStatus.OPENED, open_price=100.0,
                                expert_id=expert.id)
        create_trading_order(a1.id, symbol=sym, transaction_id=tx.id, status=OrderStatus.FILLED)
    tx = create_transaction(symbol="QQQ", quantity=1.0, status=TransactionStatus.OPENED, open_price=400.0)
    create_trading_order(a2.id, symbol="QQQ", transaction_id=tx.id, status=OrderStatus.FILLED)

    # orders in every status, MARKET and LIMIT (only MARKET is counted)
    for status in (OrderStatus.FILLED, OrderStatus.NEW, OrderStatus.OPEN, OrderStatus.ACCEPTED,
                   OrderStatus.PENDING, OrderStatus.WAITING_TRIGGER, OrderStatus.ERROR, OrderStatus.CANCELED):
        create_trading_order(a1.id, symbol="ZZZ", status=status, order_type=OrderType.MARKET)
    create_trading_order(a1.id, symbol="ZZZ", status=OrderStatus.OPEN, order_type=OrderType.BUY_LIMIT)
    create_trading_order(a2.id, symbol="ZZZ", status=OrderStatus.ERROR, order_type=OrderType.MARKET)

    for name, itype, cats, labels in (
            ("AAPL", InstrumentType.STOCK, ["Tech"], ["Growth", "Core"]),
            ("MSFT", InstrumentType.STOCK, ["Tech"], []),
            ("XLE", InstrumentType.ETF, [], ["Energy"]),
            ("GLD", InstrumentType.ETF, [], []),
            ("BND", None, [], [])):
        add_instance(Instrument(name=name, instrument_type=itype, categories=cats, labels=labels),
                     expunge_after_flush=True)
    return {"acct1": a1.id, "acct2": a2.id, "e1": e1.id, "e2": e2.id, "e3": e3.id}


POSITIONS = [
    {"symbol": "AAPL", "market_value": "1000.5"}, {"symbol": "MSFT", "market_value": 500.0},
    {"symbol": "XLE", "market_value": "-250.25"}, {"symbol": "GLD", "market_value": 100.0},
    {"symbol": "BND", "market_value": "40"}, {"symbol": "NOPE", "market_value": 75.0},
    {"symbol": "", "market_value": 5.0}, {"symbol": "XLE", "market_value": "garbage"},
]
