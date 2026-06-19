"""Phase 2 Task 2: BacktestAccount is concrete + ledger/read-only abstracts + time machine.

Run from the backend dir so the ``app.*`` import root resolves:
    ./venv/bin/python -m pytest tests/backtest/test_backtest_account_contract.py -v

These tests use an in-memory ``AsOfPriceSource`` (hand-built 5-bar AAPL series via
``load_bars``) — NO network, NO real provider — so they are hermetic.
"""
from __future__ import annotations

from datetime import datetime

import pytest

CFG = {
    "starting_cash": 100_000.0,
    "commission_per_trade": 1.0,
    "slippage_bps": 5.0,
    "fill_model": "next_bar_open",
}

# A hand-coded 5-bar AAPL daily series (date + OHLCV row dicts).
_AAPL_BARS = [
    {"Date": datetime(2024, 1, 2), "Open": 100, "High": 101, "Low": 99, "Close": 100, "Volume": 1000},
    {"Date": datetime(2024, 1, 3), "Open": 102, "High": 103, "Low": 101, "Close": 102, "Volume": 1100},
    {"Date": datetime(2024, 1, 4), "Open": 104, "High": 106, "Low": 103, "Close": 105, "Volume": 1200},
    {"Date": datetime(2024, 1, 5), "Open": 105, "High": 108, "Low": 104, "Close": 107, "Volume": 1300},
    {"Date": datetime(2024, 1, 8), "Open": 107, "High": 109, "Low": 106, "Close": 108, "Volume": 1400},
]


def _make_price_source():
    from app.services.backtest.price_source import AsOfPriceSource

    ps = AsOfPriceSource(ohlcv_provider=None)  # no provider; bars loaded directly
    ps.load_bars("AAPL", _AAPL_BARS)
    ps.set_clock(datetime(2024, 1, 2))
    return ps


def _acct():
    """Build a wired BacktestAccount against a fresh per-run backtest DB.

    Returns (account, db_context) — the caller MUST close the context.
    """
    from app.services.backtest.backtest_db import (
        backtest_trading_db,
        seed_account_definition,
    )
    from app.services.backtest.seam_wiring import wire_backtest_seams
    from app.services.backtest.backtest_account import BacktestAccount

    wire_backtest_seams()
    ctx = backtest_trading_db("contract")
    ctx.__enter__()
    seed_account_definition(1, CFG)
    ps = _make_price_source()
    acct = BacktestAccount(1, ps, CFG)
    wire_backtest_seams().register_account(1, acct)
    return acct, ctx, ps


def test_no_abstractmethod_left():
    """GATE item 1: every AccountInterface abstract is implemented -> instantiable."""
    from app.services.backtest.backtest_account import BacktestAccount

    assert getattr(BacktestAccount, "__abstractmethods__", frozenset()) == frozenset()


def test_backtest_account_is_concrete_and_ledger_reads():
    acct, ctx, _ps = _acct()
    try:
        assert acct.get_balance() == 100_000.0
        info = acct.get_account_info()
        assert info.equity == 100_000.0          # _AttrDict attribute access
        assert info["equity"] == 100_000.0       # and dict access
        assert acct.get_positions() == []
        assert acct.symbols_exist(["AAPL", "ZZZZ"]) == {"AAPL": True, "ZZZZ": False}
        assert acct.get_dividends() == []
        assert acct.get_filled_trades() == []
        assert acct.get_balance_history() == []
    finally:
        ctx.__exit__(None, None, None)


def test_time_machine_close_lookup():
    acct, ctx, ps = _acct()
    try:
        ps.set_clock(datetime(2024, 1, 2))
        assert acct.get_instrument_current_price("AAPL") == 100.0
        ps.set_clock(datetime(2024, 1, 4))
        assert acct.get_instrument_current_price("AAPL") == 105.0
        # bulk form returns a dict (None for unknown symbols)
        ps.set_clock(datetime(2024, 1, 8))
        bulk = acct.get_instrument_current_price(["AAPL", "ZZZZ"])
        assert bulk == {"AAPL": 108.0, "ZZZZ": None}
    finally:
        ctx.__exit__(None, None, None)


def test_price_cache_busted_across_bars():
    """GATE item 6 (account-level): consecutive bars return DIFFERENT prices.

    The inherited wall-clock TTL cache must NOT leak the day-1 price into day-2.
    """
    acct, ctx, ps = _acct()
    try:
        ps.set_clock(datetime(2024, 1, 2))
        assert acct.get_instrument_current_price("AAPL") == 100.0
        ps.set_clock(datetime(2024, 1, 3))
        assert acct.get_instrument_current_price("AAPL") == 102.0  # not stale 100.0
    finally:
        ctx.__exit__(None, None, None)


def test_missing_price_raises_loud():
    acct, ctx, ps = _acct()
    try:
        ps.set_clock(datetime(2024, 1, 2))
        with pytest.raises(ValueError):
            acct.get_instrument_current_price("ZZZZ")  # no bars -> loud, no fallback
    finally:
        ctx.__exit__(None, None, None)


def test_clock_must_be_set():
    from app.services.backtest.price_source import AsOfPriceSource

    ps = AsOfPriceSource(ohlcv_provider=None)
    ps.load_bars("AAPL", _AAPL_BARS)
    with pytest.raises(RuntimeError):
        ps.now()  # engine must set the clock before any lookup


def test_get_settings_definitions_shape():
    from app.services.backtest.backtest_account import BacktestAccount

    defs = BacktestAccount.get_settings_definitions()
    for key in ("starting_cash", "commission_per_trade", "slippage_bps", "fill_model"):
        assert key in defs
        assert defs[key]["required"] is True
        assert "default" not in defs[key]  # no-defaults rule (backend/CLAUDE.md)


def test_position_ledger_weighted_average_and_realized_pl():
    """Unit-test the ledger math directly (no DB): weighted avg on add, realised P&L on close."""
    from app.services.backtest.backtest_account import BacktestAccount
    from app.services.backtest.price_source import AsOfPriceSource

    ps = AsOfPriceSource(ohlcv_provider=None)
    ps.load_bars("AAPL", _AAPL_BARS)
    ps.set_clock(datetime(2024, 1, 2))
    acct = BacktestAccount(999, ps, CFG)  # no DB needed for pure-ledger math

    acct._update_position("AAPL", 10, 100.0)   # long 10 @ 100
    acct._update_position("AAPL", 10, 110.0)   # add 10 @ 110 -> avg 105
    pos = acct._positions["AAPL"]
    assert pos.qty == 20
    assert pos.avg_price == 105.0

    acct._update_position("AAPL", -20, 115.0)  # close 20 @ 115 -> realised (115-105)*20 = 200
    pos = acct._positions["AAPL"]
    assert pos.qty == 0
    assert pos.avg_price == 0.0
    assert pos.realized_pl == pytest.approx(200.0)


def test_snapshot_equity_builds_curve():
    acct, ctx, ps = _acct()
    try:
        ps.set_clock(datetime(2024, 1, 2))
        s1 = acct.snapshot_equity(datetime(2024, 1, 2))
        assert s1["net_liquidating_value"] == 100_000.0
        assert s1["cash_balance"] == 100_000.0
        assert s1["equity_value"] == 0.0
        ps.set_clock(datetime(2024, 1, 3))
        acct.snapshot_equity(datetime(2024, 1, 3))
        hist = acct.get_balance_history()
        assert len(hist) == 2
        assert [h["date"] for h in hist] == [datetime(2024, 1, 2), datetime(2024, 1, 3)]
    finally:
        ctx.__exit__(None, None, None)


def test_market_order_fills_and_updates_ledger():
    """End-to-end through the INHERITED submit_order -> _submit_order_impl -> refresh_orders.

    Proves the read-only ledger reflects a fill: a MARKET BUY on day 1 fills at day-2
    open (102) worsened by 5 bps slippage -> 102.051, plus $1 commission, so cash drops
    by 10*102.051 + 1 and a long position of 10 appears in get_positions().
    """
    acct, ctx, ps = _acct()
    try:
        from ba2_common.core.models import TradingOrder
        from ba2_common.core.types import OrderType, OrderDirection, OrderStatus

        # 5 bps slippage worsens a BUY: 102 * (1 + 5/10_000) = 102.051
        expected_fill = 102.0 * (1.0 + CFG["slippage_bps"] / 10_000.0)

        ps.set_clock(datetime(2024, 1, 2))
        order = TradingOrder(
            account_id=1,
            symbol="AAPL",
            quantity=10,
            side=OrderDirection.BUY,
            order_type=OrderType.MARKET,
            status=OrderStatus.NEW,
            comment="contract-test",
        )
        acct.submit_order(order)  # inherited validation/persistence + _submit_order_impl
        assert order.broker_order_id is not None

        ps.set_clock(datetime(2024, 1, 2))  # fill engine fills MARKET at NEXT bar (Jan 3) open
        acct.refresh_orders()

        filled = acct.get_order(order.broker_order_id)
        assert filled.status == OrderStatus.FILLED
        assert filled.open_price == pytest.approx(expected_fill)
        assert acct.get_balance() == pytest.approx(100_000.0 - 10 * expected_fill - 1.0)

        positions = acct.get_positions()
        assert len(positions) == 1
        assert positions[0]["symbol"] == "AAPL"
        assert positions[0]["qty"] == 10

        # filled-trade history now reflects the executed order
        trades = acct.get_filled_trades()
        assert len(trades) == 1
        assert trades[0]["symbol"] == "AAPL"
        assert trades[0]["qty"] == 10.0
        assert trades[0]["price"] == pytest.approx(expected_fill)
    finally:
        ctx.__exit__(None, None, None)
