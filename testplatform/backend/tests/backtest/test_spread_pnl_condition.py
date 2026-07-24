"""B9 backend wiring: profit_loss_* conditions on a MULTI-LEG option parent must price
the STRUCTURE (net premium over child legs), not fall through to the underlying price.

The unit logic lives in tests/test_option_spread_pnl_condition.py (live suite); this
file proves the same condition works through the BACKTEST stack: BacktestAccount's
get_option_quote (via HistoricalOptionsProvider's point-in-time quotes) and the
ba2_common trade_store DB path inside backtest_trading_db.

Setup: bull call spread (BUY parent, NO contract_symbol, 2 structures, net debit
$3.75/structure) with FILLED legs 180C/185C. At D2 the premium closes are 5.55/1.05
-> structure net 4.50 -> +20.0%. A >50% TP must NOT fire; a >15% one must.
Run from the backend dir:
    ./venv/bin/python -m pytest tests/backtest/test_spread_pnl_condition.py -v
"""
from __future__ import annotations

from datetime import datetime

import pytest

CFG = {
    "starting_cash": 100_000.0,
    "commission_per_trade": 0.0,
    "slippage_bps": 0.0,
    "fill_model": "next_bar_open",
}

D1 = datetime(2024, 1, 2)
D2 = datetime(2024, 1, 3)

LONG_LEG = "AAPL240315C00180000"
SHORT_LEG = "AAPL240315C00185000"

_UNDERLYING = [
    {"Date": D1, "Open": 180, "High": 182, "Low": 178, "Close": 181, "Volume": 1000},
    {"Date": D2, "Open": 181, "High": 184, "Low": 180, "Close": 183, "Volume": 1100},
]


def _seed_two_leg_cache(db_path: str) -> None:
    """Chain (zero-spread snapshot -> PIT bid == ask == bar close) + D2 premium bars:
    180C closes 5.55, 185C closes 1.05."""
    from app.services.backtest.options_cache import OptionsHistoryCache

    cache = OptionsHistoryCache(db_path)
    cache.write_chain_rows(
        "AAPL",
        "2024-01-01",
        [
            {"occ_symbol": LONG_LEG, "option_type": "call", "strike": 180.0,
             "expiry": "2024-03-15", "bid": 3.0, "ask": 3.0, "last": 3.0, "iv": 0.25},
            {"occ_symbol": SHORT_LEG, "option_type": "call", "strike": 185.0,
             "expiry": "2024-03-15", "bid": 1.0, "ask": 1.0, "last": 1.0, "iv": 0.25},
        ],
    )
    cache.write_bar_rows(
        [
            {"occ_symbol": LONG_LEG, "date": "2024-01-03", "open": 5.4, "high": 5.7,
             "low": 5.3, "close": 5.55, "volume": 500, "underlying": "AAPL",
             "option_type": "call", "strike": 180.0, "expiry": "2024-03-15"},
            {"occ_symbol": SHORT_LEG, "date": "2024-01-03", "open": 1.0, "high": 1.1,
             "low": 0.9, "close": 1.05, "volume": 500, "underlying": "AAPL",
             "option_type": "call", "strike": 185.0, "expiry": "2024-03-15"},
        ]
    )


@pytest.fixture
def spread_account(tmp_path):
    """BacktestAccount holding a FILLED 2-lot bull call spread (net debit $3.75).
    Returns (acct, parent_order, ctx)."""
    from app.services.backtest.backtest_db import (
        backtest_trading_db,
        seed_account_definition,
    )
    from app.services.backtest.seam_wiring import wire_backtest_seams
    from app.services.backtest.backtest_account import BacktestAccount
    from app.services.backtest.price_source import AsOfPriceSource
    from app.services.backtest.options_provider import HistoricalOptionsProvider
    from ba2_common.core.db import add_instance
    from ba2_common.core.models import TradingOrder, Transaction
    from ba2_common.core.types import (
        AssetClass, OptionRight, OrderDirection, OrderStatus, OrderType,
        TransactionStatus,
    )

    cache_db = str(tmp_path / "spread_pnl_cache.sqlite")
    _seed_two_leg_cache(cache_db)
    provider = HistoricalOptionsProvider(cache_db)

    wire_backtest_seams()
    ctx = backtest_trading_db("spread-pnl")
    ctx.__enter__()
    seed_account_definition(1, CFG)
    ps = AsOfPriceSource(ohlcv_provider=None)
    ps.load_bars("AAPL", _UNDERLYING)
    ps.set_clock(D2)
    acct = BacktestAccount(1, ps, CFG, options_provider=provider)
    wire_backtest_seams().register_account(1, acct)

    txn = Transaction(
        symbol="AAPL", quantity=2, side=OrderDirection.BUY,
        status=TransactionStatus.OPENED, open_price=3.75, multiplier=100,
        account_id=acct.id,
    )
    add_instance(txn)
    parent = TradingOrder(
        account_id=acct.id, symbol="AAPL", quantity=2, side=OrderDirection.BUY,
        order_type=OrderType.MARKET, status=OrderStatus.FILLED, filled_qty=2,
        open_price=3.75, transaction_id=txn.id,
        asset_class=AssetClass.OPTION, contract_symbol=None,
        option_strategy="bull_call_spread", underlying_symbol="AAPL", multiplier=100,
    )
    add_instance(parent)
    for contract, side in ((LONG_LEG, OrderDirection.BUY), (SHORT_LEG, OrderDirection.SELL)):
        add_instance(TradingOrder(
            account_id=acct.id, symbol="AAPL", quantity=2, side=side,
            order_type=OrderType.MARKET, status=OrderStatus.FILLED, filled_qty=2,
            transaction_id=txn.id, parent_order_id=parent.id,
            asset_class=AssetClass.OPTION, contract_symbol=contract,
            option_type=OptionRight.CALL, strike=180.0 if side == OrderDirection.BUY else 185.0,
            expiry=datetime(2024, 3, 15).date(), underlying_symbol="AAPL", multiplier=100,
        ))
    yield acct, parent, ctx
    ctx.__exit__(None, None, None)


def test_spread_parent_pnl_prices_the_structure(spread_account):
    """Net 4.50 vs 3.75 debit = +20.0%: a >50% TP stays put, a >15% TP fires, and the
    amount scales by structures x multiplier ((4.50-3.75) x 2 x 100 = $150)."""
    from ba2_common.core.TradeConditions import (
        ProfitLossAmountCondition,
        ProfitLossPercentCondition,
    )

    acct, parent, _ctx = spread_account

    tp50 = ProfitLossPercentCondition(
        account=acct, instrument_name="AAPL", expert_recommendation=None,
        operator_str=">", value=50.0, existing_order=parent,
    )
    assert tp50.evaluate() is False
    assert tp50.calculated_value == pytest.approx(20.0, abs=0.01)

    tp15 = ProfitLossPercentCondition(
        account=acct, instrument_name="AAPL", expert_recommendation=None,
        operator_str=">", value=15.0, existing_order=parent,
    )
    assert tp15.evaluate() is True

    amt = ProfitLossAmountCondition(
        account=acct, instrument_name="AAPL", expert_recommendation=None,
        operator_str=">", value=100.0, existing_order=parent,
    )
    assert amt.evaluate() is True
    assert amt.calculated_value == pytest.approx(150.0, abs=0.01)
