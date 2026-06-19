"""Round-trip pairing in ``BacktestAccount.get_round_trip_trades`` (realised-P&L rows).

``get_round_trip_trades`` groups FILLED orders by ``transaction_id`` and emits ONE
round-trip row per transaction so trade-quality metrics (win_rate / profit_factor /
expectancy / exit_reason) have realised P&L to work with.

These tests prove the SIDE-based entry/exit classification against a real
``BacktestAccount`` over a fresh per-run backtest DB (no network, no provider). Orders are
filled directly via ``_apply_fill`` (which stamps the simulated fill bar into
``_fill_dates`` and updates the cash/position ledger), so we control side / fill time /
``depends_on_order`` exactly.

The classification under test:

  * the OPENING order is the EARLIEST-filled order in the transaction; its side is the
    ``opening_side``;
  * ENTRIES are same-side orders (open + rebalance ADDs), EXITS are opposite-side orders
    (plain rebalance/stop sells AND dependent TP/SL/OCO legs — both are closers);
  * a transaction closed by a PLAIN market sell (``depends_on_order is None``, opposite
    side) must produce a real round-trip (exit_reason ``"exit"``), NOT fall through to the
    ``open_at_end`` mark-to-market branch (the pre-fix bug: a plain sell was mis-read as the
    entry, leaving no exits).

Run from the backend dir:
    ./venv/bin/python -m pytest tests/backtest/test_round_trip_trades.py -v
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
D3 = datetime(2024, 1, 4)
D4 = datetime(2024, 1, 5)


def _bars(symbol, last_close):
    return [
        {"Date": d, "Open": 100, "High": 200, "Low": 50, "Close": c, "Volume": 1000}
        for (d, c) in [(D1, 100), (D2, 100), (D3, 100), (D4, last_close)]
    ]


def _acct(symbol="AAPL", last_close=100.0, account_id=1, cfg=CFG):
    """Build a wired BacktestAccount over a fresh per-run backtest DB. Caller closes ctx."""
    from app.services.backtest.backtest_db import (
        backtest_trading_db,
        seed_account_definition,
    )
    from app.services.backtest.seam_wiring import wire_backtest_seams
    from app.services.backtest.backtest_account import BacktestAccount
    from app.services.backtest.price_source import AsOfPriceSource

    wire_backtest_seams()
    ctx = backtest_trading_db(f"round-trip-{account_id}")
    ctx.__enter__()
    seed_account_definition(account_id, cfg)
    ps = AsOfPriceSource(ohlcv_provider=None)
    ps.load_bars(symbol, _bars(symbol, last_close))
    acct = BacktestAccount(account_id, ps, cfg)
    wire_backtest_seams().register_account(account_id, acct)
    ps.set_clock(D1)
    return acct, ctx, ps


def _open_entry(acct, symbol="AAPL", qty=10, side=None):
    """Submit a MARKET entry (auto-creates the transaction); return (broker_id, txn_id)."""
    from ba2_common.core.models import TradingOrder
    from ba2_common.core.types import OrderType, OrderStatus, OrderDirection

    side = side or OrderDirection.BUY
    o = TradingOrder(
        account_id=acct.id,
        symbol=symbol,
        quantity=qty,
        side=side,
        order_type=OrderType.MARKET,
        status=OrderStatus.NEW,
        comment="rt-entry",
    )
    acct.submit_order(o)  # auto-creates a WAITING transaction
    persisted = acct.get_order(o.broker_order_id)
    return o.broker_order_id, persisted.transaction_id


def _attach_order(acct, txn_id, side, qty=10, symbol="AAPL", depends_on=None,
                  order_type=None, limit=None, stop=None):
    """Persist a sibling order on an existing transaction; return its broker_order_id."""
    from ba2_common.core.models import TradingOrder
    from ba2_common.core.types import OrderType, OrderStatus
    from ba2_common.core.db import add_instance

    bid = acct._next_broker_id()
    o = TradingOrder(
        account_id=acct.id,
        symbol=symbol,
        quantity=qty,
        side=side,
        order_type=order_type or OrderType.MARKET,
        limit_price=limit,
        stop_price=stop,
        transaction_id=txn_id,
        status=OrderStatus.NEW,
        depends_on_order=depends_on,
        broker_order_id=bid,
        comment="rt-leg",
    )
    add_instance(o)
    return bid


def _fill(acct, broker_id, px, as_of):
    """Fill a working order at ``px`` on bar ``as_of`` (stamps _fill_dates + ledger)."""
    o = acct.get_order(broker_id)
    acct._apply_fill(o, px, as_of)


# ---------------------------------------------------------------------------
# Plain-sell closer (the FactorRanker rebalance/stop case) — the bug
# ---------------------------------------------------------------------------
def test_plain_market_sell_closes_as_round_trip_not_open_at_end():
    """A LONG closed by a PLAIN market sell (depends_on_order None, opposite side) yields
    ONE round-trip with entry=buy / exit=sell / exit_reason 'exit' — NOT 'open_at_end'."""
    from ba2_common.core.types import OrderDirection

    acct, ctx, ps = _acct(account_id=101)
    try:
        buy_bid, txn = _open_entry(acct, qty=10, side=OrderDirection.BUY)
        _fill(acct, buy_bid, 100.0, D2)  # entry buy fills @100

        sell_bid = _attach_order(acct, txn, OrderDirection.SELL, qty=10)  # plain closer
        _fill(acct, sell_bid, 120.0, D3)  # closing sell fills @120

        rts = acct.get_round_trip_trades()
        assert len(rts) == 1
        t = rts[0]
        assert t["exit_reason"] == "exit"
        assert t["exit_reason"] != "open_at_end"
        assert t["direction"] == "buy"
        assert t["entry_price"] == pytest.approx(100.0)  # the BUY, not the sell
        assert t["exit_price"] == pytest.approx(120.0)   # the plain SELL
        assert t["size"] == pytest.approx(10.0)
        assert t["pnl"] == pytest.approx(200.0)  # (120-100)*10 long, 0 commission
        assert t["pnl"] > 0
    finally:
        ctx.__exit__(None, None, None)


def test_plain_sell_at_loss_counts_as_losing_trade():
    """Bought then sold LOWER via a plain sell -> pnl < 0 so win_rate reflects the loss."""
    from ba2_common.core.types import OrderDirection

    acct, ctx, ps = _acct(account_id=102)
    try:
        buy_bid, txn = _open_entry(acct, qty=10, side=OrderDirection.BUY)
        _fill(acct, buy_bid, 100.0, D2)

        sell_bid = _attach_order(acct, txn, OrderDirection.SELL, qty=10)
        _fill(acct, sell_bid, 80.0, D3)  # sold at a loss

        rts = acct.get_round_trip_trades()
        assert len(rts) == 1
        t = rts[0]
        assert t["exit_reason"] == "exit"
        assert t["entry_price"] == pytest.approx(100.0)
        assert t["exit_price"] == pytest.approx(80.0)
        assert t["pnl"] == pytest.approx(-200.0)
        assert t["pnl"] < 0  # a losing trade
    finally:
        ctx.__exit__(None, None, None)


def test_two_buys_then_one_sell_weighted_avg_entry():
    """ADD case: two buys (10 @100, 10 @110) then a single sell of 20 -> entry_px is the
    qty-weighted average of the two buys (105); exit is the sell."""
    from ba2_common.core.types import OrderDirection

    acct, ctx, ps = _acct(account_id=103)
    try:
        buy1_bid, txn = _open_entry(acct, qty=10, side=OrderDirection.BUY)
        _fill(acct, buy1_bid, 100.0, D2)  # first buy @100

        buy2_bid = _attach_order(acct, txn, OrderDirection.BUY, qty=10)  # rebalance ADD
        _fill(acct, buy2_bid, 110.0, D3)  # second buy @110

        sell_bid = _attach_order(acct, txn, OrderDirection.SELL, qty=20)
        _fill(acct, sell_bid, 130.0, D4)  # close all 20 @130

        rts = acct.get_round_trip_trades()
        assert len(rts) == 1
        t = rts[0]
        assert t["direction"] == "buy"
        assert t["entry_price"] == pytest.approx(105.0)  # (10*100 + 10*110)/20
        assert t["exit_price"] == pytest.approx(130.0)
        assert t["size"] == pytest.approx(20.0)
        assert t["pnl"] == pytest.approx((130.0 - 105.0) * 20.0)  # +500
        assert t["exit_reason"] == "exit"
    finally:
        ctx.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# REGRESSION: dependent OCO/TP/SL legs must still pair + classify as before
# ---------------------------------------------------------------------------
def test_regression_tp_leg_closes_as_take_profit():
    """A dependent SELL_LIMIT (TP) leg still pairs as the exit with exit_reason
    'take_profit' and the same realised pnl as before the side-based rewrite."""
    from ba2_common.core.types import OrderDirection, OrderType

    acct, ctx, ps = _acct(account_id=104)
    try:
        buy_bid, txn = _open_entry(acct, qty=10, side=OrderDirection.BUY)
        _fill(acct, buy_bid, 100.0, D2)
        entry_db_id = acct.get_order(buy_bid).id

        tp_bid = _attach_order(
            acct, txn, OrderDirection.SELL, qty=10,
            depends_on=entry_db_id, order_type=OrderType.SELL_LIMIT, limit=130.0,
        )
        _fill(acct, tp_bid, 130.0, D3)  # TP fills @ limit

        rts = acct.get_round_trip_trades()
        assert len(rts) == 1
        t = rts[0]
        assert t["exit_reason"] == "take_profit"
        assert t["direction"] == "buy"
        assert t["entry_price"] == pytest.approx(100.0)
        assert t["exit_price"] == pytest.approx(130.0)
        assert t["pnl"] == pytest.approx(300.0)
    finally:
        ctx.__exit__(None, None, None)


def test_regression_sl_leg_closes_as_stop_loss():
    """A dependent SELL_STOP (SL) leg still classifies as 'stop_loss'."""
    from ba2_common.core.types import OrderDirection, OrderType

    acct, ctx, ps = _acct(account_id=105)
    try:
        buy_bid, txn = _open_entry(acct, qty=10, side=OrderDirection.BUY)
        _fill(acct, buy_bid, 100.0, D2)
        entry_db_id = acct.get_order(buy_bid).id

        sl_bid = _attach_order(
            acct, txn, OrderDirection.SELL, qty=10,
            depends_on=entry_db_id, order_type=OrderType.SELL_STOP, stop=90.0,
        )
        _fill(acct, sl_bid, 90.0, D3)

        rts = acct.get_round_trip_trades()
        assert len(rts) == 1
        t = rts[0]
        assert t["exit_reason"] == "stop_loss"
        assert t["entry_price"] == pytest.approx(100.0)
        assert t["exit_price"] == pytest.approx(90.0)
        assert t["pnl"] == pytest.approx(-100.0)
    finally:
        ctx.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# Still-open transaction must KEEP the open_at_end mark-to-market branch
# ---------------------------------------------------------------------------
def test_open_transaction_marks_to_market_open_at_end():
    """An entry with NO closing fill is marked-to-market at the last price
    (exit_reason 'open_at_end'), with entry = the buy."""
    from ba2_common.core.types import OrderDirection

    acct, ctx, ps = _acct(account_id=106, last_close=150.0)
    try:
        buy_bid, txn = _open_entry(acct, qty=10, side=OrderDirection.BUY)
        _fill(acct, buy_bid, 100.0, D2)
        ps.set_clock(D4)  # last bar -> close 150

        rts = acct.get_round_trip_trades()
        assert len(rts) == 1
        t = rts[0]
        assert t["exit_reason"] == "open_at_end"
        assert t["entry_price"] == pytest.approx(100.0)
        assert t["exit_price"] == pytest.approx(150.0)  # marked to last close
    finally:
        ctx.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# Task 10: OPTION round-trips must scale realised P&L by the contract multiplier
# ---------------------------------------------------------------------------
# The premium is quoted PER SHARE but a contract controls 100 shares, so a call
# bought @1.00 and closed @1.50 (1 contract) realises (1.50-1.00)*1*100 = $50 gross,
# NOT $0.50. The buy-to-open and sell-to-close ride the SAME transaction so the
# round-trip pairing groups them (a fresh transaction on the close would NOT pair).
_OPT_OCC = "AAPL240315C00180000"

# Underlying bars on three consecutive trading days so each submit has a "next bar":
#   D1 -> buy-to-open fills next_bar_open on D2; D2 -> sell-to-close fills on D3.
_OPT_UNDERLYING = [
    {"Date": D1, "Open": 180, "High": 182, "Low": 178, "Close": 181, "Volume": 1000},
    {"Date": D2, "Open": 181, "High": 184, "Low": 180, "Close": 183, "Volume": 1100},
    {"Date": D3, "Open": 183, "High": 186, "Low": 182, "Close": 185, "Volume": 1200},
]


def _seed_option_cache(db_path: str) -> None:
    """Seed the CALL chain + two premium bars: entry open 1.00 (D2), exit open 1.50 (D3)."""
    from app.services.backtest.options_cache import OptionsHistoryCache

    cache = OptionsHistoryCache(db_path)
    cache.write_chain_rows(
        "AAPL",
        "2024-01-01",
        [{"occ_symbol": _OPT_OCC, "option_type": "call", "strike": 180.0,
          "expiry": "2024-03-15", "bid": 0.95, "ask": 1.05, "last": 1.0, "iv": 0.25}],
    )
    cache.write_bar_rows(
        [
            {"occ_symbol": _OPT_OCC, "date": "2024-01-03", "open": 1.0, "high": 1.2,
             "low": 0.9, "close": 1.1, "volume": 500, "underlying": "AAPL",
             "option_type": "call", "strike": 180.0, "expiry": "2024-03-15"},
            {"occ_symbol": _OPT_OCC, "date": "2024-01-04", "open": 1.5, "high": 1.7,
             "low": 1.4, "close": 1.6, "volume": 400, "underlying": "AAPL",
             "option_type": "call", "strike": 180.0, "expiry": "2024-03-15"},
        ]
    )


@pytest.fixture
def option_round_trip_account(tmp_path):
    """BacktestAccount that has BOUGHT then CLOSED 1 call through the real fill engine.

    Buy-to-open fills @1.00 (D2 open premium); sell-to-close (on the SAME transaction)
    fills @1.50 (D3 open premium). Zero commission/slippage keeps gross == pnl.
    """
    from app.services.backtest.backtest_db import (
        backtest_trading_db,
        seed_account_definition,
    )
    from app.services.backtest.seam_wiring import wire_backtest_seams
    from app.services.backtest.backtest_account import BacktestAccount
    from app.services.backtest.price_source import AsOfPriceSource
    from app.services.backtest.options_provider import HistoricalOptionsProvider
    from ba2_common.core.option_types import OptionLeg
    from ba2_common.core.types import OrderDirection

    cache_db = str(tmp_path / "opt_rt_cache.sqlite")
    _seed_option_cache(cache_db)
    provider = HistoricalOptionsProvider(cache_db)

    wire_backtest_seams()
    ctx = backtest_trading_db("opt-round-trip")
    ctx.__enter__()
    seed_account_definition(1, CFG)
    ps = AsOfPriceSource(ohlcv_provider=None)
    ps.load_bars("AAPL", _OPT_UNDERLYING)
    ps.set_clock(D1)
    acct = BacktestAccount(1, ps, CFG, options_provider=provider)
    wire_backtest_seams().register_account(1, acct)

    try:
        # 1. Buy-to-open 1 call (market). Submitted on D1 -> auto-creates the txn.
        leg = OptionLeg(
            contract_symbol=_OPT_OCC, side=OrderDirection.BUY,
            position_intent="buy_to_open", underlying="AAPL",
        )
        parent = acct.submit_option_order(
            legs=[leg], quantity=1, order_type="market", option_strategy="long_call"
        )
        open_txn = acct.get_order(parent.id).transaction_id

        # 2. Fill the open at the D2 open premium (1.00).
        acct.refresh_orders()
        acct.refresh_transactions()
        assert acct.get_order(parent.id).open_price == pytest.approx(1.0)

        # 3. Sell-to-close 1 call (market) on the SAME transaction so the pairing groups them.
        close_leg = OptionLeg(
            contract_symbol=_OPT_OCC, side=OrderDirection.SELL,
            position_intent="sell_to_close", underlying="AAPL",
        )
        close_parent = acct.submit_option_order(
            legs=[close_leg], quantity=1, order_type="market",
            option_strategy="close", transaction_id=open_txn,
        )

        # 4. Step the clock to D2 so the close fills at the D3 open premium (1.50).
        ps.set_clock(D2)
        acct.refresh_orders()
        acct.refresh_transactions()
        assert acct.get_order(close_parent.id).open_price == pytest.approx(1.5)

        yield acct
    finally:
        ctx.__exit__(None, None, None)


def test_option_round_trip_pnl_uses_multiplier(option_round_trip_account):
    acct = option_round_trip_account
    rt = [t for t in acct.get_round_trip_trades() if t["exit_reason"] != "open_at_end"]
    # (1.50-1.00)*1*100 = 50 gross, minus commissions (0 here, so pnl == 50).
    assert any(abs(t["pnl"] - 50.0) <= 2.0 for t in rt)


# ---------------------------------------------------------------------------
# Task 10b: close_option_position must RIDE the open position's transaction
# ---------------------------------------------------------------------------
# Unlike the Task-10 fixture (which hand-passes transaction_id to
# submit_option_order), this drives ``close_option_position`` — the public close
# path — end-to-end. The close must reduce the ORIGINAL transaction to flat (net
# qty -> 0), NOT spawn a second OPENED transaction holding the opposite leg.
# Before the fix: get_option_positions() shows TWO positions (long + new short)
# and the close order carries a DIFFERENT transaction_id, so round-trips can't pair.
@pytest.fixture
def option_close_account(tmp_path):
    """BacktestAccount with 1 OPEN call, ready for ``close_option_position``.

    Buy-to-open fills @1.00 (D2 open premium). The position is left OPEN (qty 1);
    the test closes it via ``close_option_position`` and advances to fill @1.50.
    Returns (acct, ps, open_txn_id, open_order_id).
    """
    from app.services.backtest.backtest_db import (
        backtest_trading_db,
        seed_account_definition,
    )
    from app.services.backtest.seam_wiring import wire_backtest_seams
    from app.services.backtest.backtest_account import BacktestAccount
    from app.services.backtest.price_source import AsOfPriceSource
    from app.services.backtest.options_provider import HistoricalOptionsProvider
    from ba2_common.core.option_types import OptionLeg
    from ba2_common.core.types import OrderDirection

    cache_db = str(tmp_path / "opt_close_cache.sqlite")
    _seed_option_cache(cache_db)
    provider = HistoricalOptionsProvider(cache_db)

    wire_backtest_seams()
    ctx = backtest_trading_db("opt-close")
    ctx.__enter__()
    seed_account_definition(1, CFG)
    ps = AsOfPriceSource(ohlcv_provider=None)
    ps.load_bars("AAPL", _OPT_UNDERLYING)
    ps.set_clock(D1)
    acct = BacktestAccount(1, ps, CFG, options_provider=provider)
    wire_backtest_seams().register_account(1, acct)

    try:
        leg = OptionLeg(
            contract_symbol=_OPT_OCC, side=OrderDirection.BUY,
            position_intent="buy_to_open", underlying="AAPL",
        )
        parent = acct.submit_option_order(
            legs=[leg], quantity=1, order_type="market", option_strategy="long_call"
        )
        open_order_id = parent.id
        # Fill the open at the D2 open premium (1.00).
        acct.refresh_orders()
        acct.refresh_transactions()
        open_txn_id = acct.get_order(open_order_id).transaction_id
        yield acct, ps, open_txn_id, open_order_id
    finally:
        ctx.__exit__(None, None, None)


def test_close_option_position_rides_open_transaction_and_nets_flat(option_close_account):
    acct, ps, open_txn_id, open_order_id = option_close_account

    pos = acct.get_option_positions()
    assert len(pos) == 1  # the open long call

    close_order = acct.close_option_position(pos[0], order_type="market")
    # The close must ride the OPEN position's transaction (not a brand-new one).
    assert close_order.transaction_id == open_txn_id

    # Step the clock to D2 so the close fills at the D3 open premium (1.50).
    ps.set_clock(D2)
    acct.refresh_orders()
    acct.refresh_transactions()
    assert acct.get_order(close_order.id).open_price == pytest.approx(1.5)

    # Netted FLAT: the sell-to-close on the SAME txn reduces net qty to 0, so the
    # position no longer shows — NOT two positions (original long + a new short).
    assert acct.get_option_positions() == []
