"""Phase 2 Task 3: BacktestAccount fill engine + TP/SL/OCO legs (GATE items 2 + 6).

These tests prove the simulator's order mechanics against hand-built bar series — NO
network, NO real provider. Orders are routed through the INHERITED ``submit_order`` so
the real validation/persistence/auto-transaction path runs; the fill engine
(``refresh_orders``) then evaluates each working order against the current bar.

Fill model under test (default ``next_bar_open``): a MARKET order placed with the clock on
day N fills at day N+1's open; LIMIT/STOP orders fill on the first later bar whose range
crosses the trigger. TP/SL/OCO legs created via ``adjust_tp_sl`` wait (WAITING_TRIGGER)
until the entry order FILLS, then become live and close the position when their side is hit.

Run from the backend dir:
    ./venv/bin/python -m pytest tests/backtest/test_backtest_account_fills.py -v
"""
from __future__ import annotations

from datetime import datetime

import pytest

# No slippage / no commission in the base CFG so price assertions are exact; specific
# tests override slippage/commission to prove those legs.
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
D5 = datetime(2024, 1, 8)


def _bars(rows):
    """rows: list of (date, open, high, low, close) -> OHLCV row dicts."""
    return [
        {"Date": d, "Open": o, "High": h, "Low": low, "Close": c, "Volume": 1000}
        for (d, o, h, low, c) in rows
    ]


def _acct(rows, cfg=CFG, symbol="AAPL", account_id=1):
    """Build a wired BacktestAccount over a fresh per-run backtest DB + hand-built bars.

    Returns (account, db_context, price_source). Caller MUST close the context.
    """
    from app.services.backtest.backtest_db import (
        backtest_trading_db,
        seed_account_definition,
    )
    from app.services.backtest.seam_wiring import wire_backtest_seams
    from app.services.backtest.backtest_account import BacktestAccount
    from app.services.backtest.price_source import AsOfPriceSource

    wire_backtest_seams()
    ctx = backtest_trading_db(f"fills-{account_id}")
    ctx.__enter__()
    seed_account_definition(account_id, cfg)
    ps = AsOfPriceSource(ohlcv_provider=None)
    ps.load_bars(symbol, _bars(rows))
    acct = BacktestAccount(account_id, ps, cfg)
    wire_backtest_seams().register_account(account_id, acct)
    return acct, ctx, ps


def _market(symbol, qty, side):
    from ba2_common.core.models import TradingOrder
    from ba2_common.core.types import OrderType, OrderStatus

    return TradingOrder(
        account_id=1,
        symbol=symbol,
        quantity=qty,
        side=side,
        order_type=OrderType.MARKET,
        status=OrderStatus.NEW,
        comment="fill-test",
    )


def _limit(symbol, qty, side, order_type, limit):
    from ba2_common.core.models import TradingOrder
    from ba2_common.core.types import OrderStatus

    return TradingOrder(
        account_id=1,
        symbol=symbol,
        quantity=qty,
        side=side,
        order_type=order_type,
        limit_price=limit,
        status=OrderStatus.NEW,
        comment="fill-test",
    )


def _stop(symbol, qty, side, order_type, stop):
    from ba2_common.core.models import TradingOrder
    from ba2_common.core.types import OrderStatus

    return TradingOrder(
        account_id=1,
        symbol=symbol,
        quantity=qty,
        side=side,
        order_type=order_type,
        stop_price=stop,
        status=OrderStatus.NEW,
        comment="fill-test",
    )


# ---------------------------------------------------------------------------
# CASH-SECURED safeguard (no leverage)
# ---------------------------------------------------------------------------
def test_cash_secured_buy_clamped_to_affordable():
    """A BUY sized beyond available cash is CLAMPED to the affordable share count so the backtest
    can never silently run on leverage (regression guard for the over-deployment safeguard)."""
    from ba2_common.core.types import OrderDirection, OrderStatus

    cfg = {**CFG, "starting_cash": 1000.0}
    acct, ctx, ps = _acct([(D1, 100, 101, 99, 100), (D2, 102, 103, 101, 102)], cfg=cfg)
    try:
        ps.set_clock(D1)
        o = _market("AAPL", 100, OrderDirection.BUY)  # 100*102 = $10,200 >> $1000 cash
        acct.submit_order(o)
        acct.refresh_orders()  # fills at D2 open = 102, clamped to affordable
        filled = acct.get_order(o.broker_order_id)
        assert filled.status == OrderStatus.FILLED
        assert filled.filled_qty == 9  # floor(1000 / 102)
        assert acct.get_balance() == pytest.approx(1000.0 - 9 * 102.0)
        assert acct.get_balance() >= 0  # never negative — cash-secured
    finally:
        ctx.__exit__(None, None, None)


def test_cash_secured_buy_canceled_when_unaffordable():
    """When not even 1 share is affordable, the BUY is CANCELED (not filled, cash untouched) —
    exercises the affordable<1 branch of the safeguard."""
    from ba2_common.core.types import OrderDirection, OrderStatus

    cfg = {**CFG, "starting_cash": 50.0}  # < one $102 share
    acct, ctx, ps = _acct([(D1, 100, 101, 99, 100), (D2, 102, 103, 101, 102)], cfg=cfg)
    try:
        ps.set_clock(D1)
        o = _market("AAPL", 10, OrderDirection.BUY)
        acct.submit_order(o)
        acct.refresh_orders()
        filled = acct.get_order(o.broker_order_id)
        assert filled.status == OrderStatus.CANCELED
        assert acct.get_balance() == pytest.approx(50.0)  # cash untouched
        assert acct.get_positions() == []
    finally:
        ctx.__exit__(None, None, None)


def test_affordable_buy_not_clamped():
    """A BUY within cash is untouched (the safeguard must not false-trigger on normal sizing)."""
    from ba2_common.core.types import OrderDirection, OrderStatus

    cfg = {**CFG, "starting_cash": 1000.0}
    acct, ctx, ps = _acct([(D1, 100, 101, 99, 100), (D2, 102, 103, 101, 102)], cfg=cfg)
    try:
        ps.set_clock(D1)
        o = _market("AAPL", 5, OrderDirection.BUY)  # 5*102 = $510 < $1000
        acct.submit_order(o)
        acct.refresh_orders()
        filled = acct.get_order(o.broker_order_id)
        assert filled.status == OrderStatus.FILLED and filled.filled_qty == 5
        assert acct.get_balance() == pytest.approx(1000.0 - 5 * 102.0)
    finally:
        ctx.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# MARKET
# ---------------------------------------------------------------------------
def test_market_buy_fills_next_bar_open():
    from ba2_common.core.types import OrderDirection, OrderStatus

    acct, ctx, ps = _acct([(D1, 100, 101, 99, 100), (D2, 102, 103, 101, 102)])
    try:
        ps.set_clock(D1)
        o = _market("AAPL", 10, OrderDirection.BUY)
        acct.submit_order(o)
        acct.refresh_orders()  # fills at D2 open = 102
        filled = acct.get_order(o.broker_order_id)
        assert filled.status == OrderStatus.FILLED
        assert filled.open_price == 102.0
        assert acct.get_balance() == pytest.approx(100_000.0 - 10 * 102.0)
        pos = acct.get_positions()
        assert len(pos) == 1 and pos[0]["qty"] == 10
    finally:
        ctx.__exit__(None, None, None)


def test_market_sell_short_fills_and_credits_cash():
    from ba2_common.core.types import OrderDirection, OrderStatus

    acct, ctx, ps = _acct([(D1, 100, 101, 99, 100), (D2, 102, 103, 101, 102)])
    try:
        ps.set_clock(D1)
        o = _market("AAPL", 5, OrderDirection.SELL)
        acct.submit_order(o)
        acct.refresh_orders()  # fills at D2 open = 102
        filled = acct.get_order(o.broker_order_id)
        assert filled.status == OrderStatus.FILLED
        assert filled.open_price == 102.0
        # Short sale credits cash: +5*102
        assert acct.get_balance() == pytest.approx(100_000.0 + 5 * 102.0)
        pos = acct.get_positions()
        assert len(pos) == 1 and pos[0]["qty"] == -5
    finally:
        ctx.__exit__(None, None, None)


def test_market_does_not_fill_on_entry_bar():
    """No look-ahead: a MARKET order does not fill against the bar it was placed on."""
    from ba2_common.core.types import OrderDirection, OrderStatus

    acct, ctx, ps = _acct([(D1, 100, 101, 99, 100), (D2, 102, 103, 101, 102)])
    try:
        ps.set_clock(D2)  # clock on the LAST bar -> there is no "next bar"
        o = _market("AAPL", 10, OrderDirection.BUY)
        acct.submit_order(o)
        acct.refresh_orders()
        assert acct.get_order(o.broker_order_id).status != OrderStatus.FILLED
    finally:
        ctx.__exit__(None, None, None)


def test_slippage_worsens_market_buy():
    from ba2_common.core.types import OrderDirection

    cfg = {**CFG, "slippage_bps": 50.0}  # 0.5%
    acct, ctx, ps = _acct([(D1, 100, 101, 99, 100), (D2, 102, 103, 101, 102)], cfg=cfg)
    try:
        ps.set_clock(D1)
        o = _market("AAPL", 10, OrderDirection.BUY)
        acct.submit_order(o)
        acct.refresh_orders()
        assert acct.get_order(o.broker_order_id).open_price == pytest.approx(102.0 * 1.005)
    finally:
        ctx.__exit__(None, None, None)


def test_slippage_worsens_market_sell():
    from ba2_common.core.types import OrderDirection

    cfg = {**CFG, "slippage_bps": 50.0}  # 0.5% -> sells fill LOWER
    acct, ctx, ps = _acct([(D1, 100, 101, 99, 100), (D2, 102, 103, 101, 102)], cfg=cfg)
    try:
        ps.set_clock(D1)
        o = _market("AAPL", 10, OrderDirection.SELL)
        acct.submit_order(o)
        acct.refresh_orders()
        assert acct.get_order(o.broker_order_id).open_price == pytest.approx(102.0 * 0.995)
    finally:
        ctx.__exit__(None, None, None)


def test_commission_charged_per_fill():
    from ba2_common.core.types import OrderDirection

    cfg = {**CFG, "commission_per_trade": 1.0}
    acct, ctx, ps = _acct([(D1, 100, 101, 99, 100), (D2, 102, 103, 101, 102)], cfg=cfg)
    try:
        ps.set_clock(D1)
        o = _market("AAPL", 10, OrderDirection.BUY)
        acct.submit_order(o)
        acct.refresh_orders()
        assert acct.get_balance() == pytest.approx(100_000.0 - 10 * 102.0 - 1.0)
    finally:
        ctx.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# LIMIT
# ---------------------------------------------------------------------------
def test_buy_limit_does_not_fill_when_bar_does_not_cross():
    from ba2_common.core.types import OrderDirection, OrderType, OrderStatus

    # D2 low is 101 -> a BUY_LIMIT @ 98 never trades.
    acct, ctx, ps = _acct([(D1, 100, 101, 99, 100), (D2, 102, 103, 101, 102)])
    try:
        ps.set_clock(D1)
        o = _limit("AAPL", 10, OrderDirection.BUY, OrderType.BUY_LIMIT, 98.0)
        acct.submit_order(o)
        acct.refresh_orders()
        assert acct.get_order(o.broker_order_id).status != OrderStatus.FILLED
    finally:
        ctx.__exit__(None, None, None)


def test_buy_limit_fills_at_limit_when_bar_crosses():
    from ba2_common.core.types import OrderDirection, OrderType, OrderStatus

    # D2 low is 99 -> a BUY_LIMIT @ 100 trades and fills AT the limit (100).
    acct, ctx, ps = _acct([(D1, 100, 101, 99, 100), (D2, 101, 102, 99, 101)])
    try:
        ps.set_clock(D1)
        o = _limit("AAPL", 10, OrderDirection.BUY, OrderType.BUY_LIMIT, 100.0)
        acct.submit_order(o)
        acct.refresh_orders()
        filled = acct.get_order(o.broker_order_id)
        assert filled.status == OrderStatus.FILLED
        assert filled.open_price == 100.0  # limit price, no slippage on limit fills
    finally:
        ctx.__exit__(None, None, None)


def test_sell_limit_fills_when_bar_rises_to_limit():
    from ba2_common.core.types import OrderDirection, OrderType, OrderStatus

    # D2 high is 105 -> a SELL_LIMIT @ 104 fills at 104.
    acct, ctx, ps = _acct([(D1, 100, 101, 99, 100), (D2, 102, 105, 101, 103)])
    try:
        ps.set_clock(D1)
        o = _limit("AAPL", 10, OrderDirection.SELL, OrderType.SELL_LIMIT, 104.0)
        acct.submit_order(o)
        acct.refresh_orders()
        filled = acct.get_order(o.broker_order_id)
        assert filled.status == OrderStatus.FILLED
        assert filled.open_price == 104.0
    finally:
        ctx.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# STOP
# ---------------------------------------------------------------------------
def test_buy_stop_triggers_at_stop_plus_slippage():
    from ba2_common.core.types import OrderDirection, OrderType, OrderStatus

    cfg = {**CFG, "slippage_bps": 100.0}  # 1%
    # D2 high is 110 -> BUY_STOP @ 105 triggers; fills at 105 * 1.01.
    acct, ctx, ps = _acct([(D1, 100, 101, 99, 100), (D2, 104, 110, 103, 108)], cfg=cfg)
    try:
        ps.set_clock(D1)
        o = _stop("AAPL", 10, OrderDirection.BUY, OrderType.BUY_STOP, 105.0)
        acct.submit_order(o)
        acct.refresh_orders()
        filled = acct.get_order(o.broker_order_id)
        assert filled.status == OrderStatus.FILLED
        assert filled.open_price == pytest.approx(105.0 * 1.01)
    finally:
        ctx.__exit__(None, None, None)


def test_sell_stop_triggers_at_stop_minus_slippage():
    from ba2_common.core.types import OrderDirection, OrderType, OrderStatus

    cfg = {**CFG, "slippage_bps": 100.0}  # 1%
    # D2 low is 90 -> SELL_STOP @ 95 triggers; fills at 95 * 0.99.
    acct, ctx, ps = _acct([(D1, 100, 101, 99, 100), (D2, 98, 99, 90, 92)], cfg=cfg)
    try:
        ps.set_clock(D1)
        o = _stop("AAPL", 10, OrderDirection.SELL, OrderType.SELL_STOP, 95.0)
        acct.submit_order(o)
        acct.refresh_orders()
        filled = acct.get_order(o.broker_order_id)
        assert filled.status == OrderStatus.FILLED
        assert filled.open_price == pytest.approx(95.0 * 0.99)
    finally:
        ctx.__exit__(None, None, None)


def test_buy_stop_does_not_trigger_below_stop():
    from ba2_common.core.types import OrderDirection, OrderType, OrderStatus

    # D2 high is 103 -> BUY_STOP @ 110 never triggers.
    acct, ctx, ps = _acct([(D1, 100, 101, 99, 100), (D2, 101, 103, 100, 102)])
    try:
        ps.set_clock(D1)
        o = _stop("AAPL", 10, OrderDirection.BUY, OrderType.BUY_STOP, 110.0)
        acct.submit_order(o)
        acct.refresh_orders()
        assert acct.get_order(o.broker_order_id).status != OrderStatus.FILLED
    finally:
        ctx.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# TP / SL legs (single)
# ---------------------------------------------------------------------------
def _open_long_with_legs(acct, ps, txn_lookup_after=None):
    """Helper: open a LONG MARKET entry, return (entry_order, transaction)."""
    from ba2_common.core.types import OrderDirection
    from ba2_common.core.models import Transaction
    from ba2_common.core.db import get_instance

    o = _market("AAPL", 10, OrderDirection.BUY)
    acct.submit_order(o)  # auto-creates a WAITING transaction
    txn = get_instance(Transaction, o.transaction_id)
    return o, txn


def test_adjust_tp_sets_transaction_take_profit():
    """LEAN simulator: adjust_tp records the level on the TRANSACTION — no WAITING_TRIGGER leg order
    is materialised (that live-broker mechanism is gone; the fill engine checks the level directly)."""
    from ba2_common.core.models import Transaction
    from ba2_common.core.db import get_instance

    acct, ctx, ps = _acct([(D1, 100, 101, 99, 100), (D2, 102, 103, 101, 102)])
    try:
        ps.set_clock(D1)
        entry, txn = _open_long_with_legs(acct, ps)
        assert acct.adjust_tp(txn, 120.0) is True
        assert get_instance(Transaction, txn.id).take_profit == 120.0
        assert [o for o in acct.get_orders() if o.depends_on_order == entry.id] == []  # no leg row
    finally:
        ctx.__exit__(None, None, None)


def test_adjust_sl_sets_transaction_stop_loss():
    """LEAN simulator: adjust_sl records the level on the transaction — no SELL_STOP leg order."""
    from ba2_common.core.models import Transaction
    from ba2_common.core.db import get_instance

    acct, ctx, ps = _acct([(D1, 100, 101, 99, 100), (D2, 102, 103, 101, 102)])
    try:
        ps.set_clock(D1)
        entry, txn = _open_long_with_legs(acct, ps)
        assert acct.adjust_sl(txn, 90.0) is True
        assert get_instance(Transaction, txn.id).stop_loss == 90.0
        assert [o for o in acct.get_orders() if o.depends_on_order == entry.id] == []
    finally:
        ctx.__exit__(None, None, None)


def test_tp_closes_position_when_price_crosses():
    """OUTCOME: after the entry fills, a bar crossing the TP closes the position AT the TP price
    (take_profit), via the direct bracket check (no WAITING_TRIGGER leg)."""
    from ba2_common.core.types import OrderStatus

    # D2 entry fills @102; D3 high 107 crosses TP @106 -> closes @106.
    acct, ctx, ps = _acct(
        [(D1, 100, 101, 99, 100), (D2, 102, 103, 101, 102), (D3, 104, 107, 103, 106)]
    )
    try:
        ps.set_clock(D1)
        entry, txn = _open_long_with_legs(acct, ps)
        acct.adjust_tp(txn, 106.0)
        ps.set_clock(D1); acct.refresh_orders(); acct.refresh_transactions()  # entry fills @102, OPENED
        assert acct.get_order(entry.broker_order_id).status == OrderStatus.FILLED
        ps.set_clock(D2); acct.refresh_orders(); acct.refresh_transactions()  # bracket vs D3 -> TP @106
        assert acct.get_positions() == []
        rts = acct.get_round_trip_trades()
        assert len(rts) == 1
        assert rts[0]["exit_price"] == pytest.approx(106.0)
        assert rts[0]["exit_reason"] == "take_profit"
    finally:
        ctx.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# Paired TP+SL (no OCO leg — both stored on the transaction)
# ---------------------------------------------------------------------------
def test_adjust_tp_sl_sets_both_on_transaction():
    from ba2_common.core.models import Transaction
    from ba2_common.core.db import get_instance

    acct, ctx, ps = _acct([(D1, 100, 101, 99, 100), (D2, 102, 103, 101, 102)])
    try:
        ps.set_clock(D1)
        entry, txn = _open_long_with_legs(acct, ps)
        assert acct.adjust_tp_sl(txn, new_tp_price=120.0, new_sl_price=90.0) is True
        t = get_instance(Transaction, txn.id)
        assert t.take_profit == 120.0 and t.stop_loss == 90.0
        assert [o for o in acct.get_orders() if o.depends_on_order == entry.id] == []  # no OCO leg row
    finally:
        ctx.__exit__(None, None, None)


def test_oco_tp_side_closes_at_take_profit():
    """Paired TP+SL: price rises to TP -> position closes at TP, transaction CLOSED."""
    from ba2_common.core.types import TransactionStatus
    from ba2_common.core.models import Transaction
    from ba2_common.core.db import get_instance

    # D2 entry fills @102; D3 high 125 crosses TP @120 (low 103 above SL 90) -> TP closes.
    acct, ctx, ps = _acct(
        [(D1, 100, 101, 99, 100), (D2, 102, 103, 101, 102), (D3, 104, 125, 103, 120)]
    )
    try:
        ps.set_clock(D1)
        entry, txn = _open_long_with_legs(acct, ps)
        acct.adjust_tp_sl(txn, new_tp_price=120.0, new_sl_price=90.0)
        ps.set_clock(D1); acct.refresh_orders(); acct.refresh_transactions()  # entry fills @102, OPENED
        ps.set_clock(D2); acct.refresh_orders(); acct.refresh_transactions()  # bracket vs D3 -> TP @120
        assert acct.get_positions() == []
        assert get_instance(Transaction, txn.id).status == TransactionStatus.CLOSED
        rts = acct.get_round_trip_trades()
        assert len(rts) == 1 and rts[0]["exit_price"] == pytest.approx(120.0)
        assert rts[0]["exit_reason"] == "take_profit"
    finally:
        ctx.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# open_date sim-stamping (days_opened correctness)
# ---------------------------------------------------------------------------
def test_open_date_stamped_to_sim_fill_date_not_wall_clock():
    """An OPENED transaction's ``open_date`` must equal the entry's SIM fill bar.

    The inherited lifecycle stamps ``open_date = datetime.now()`` (WALL clock) on
    WAITING->OPENED. In a backtest the sim clock is years off wall time, so a
    wall-clock open_date makes ``days_opened`` ~= 0 forever (the bug). The backtest
    account must re-stamp ``open_date`` to the entry order's simulated fill date.
    """
    from datetime import timezone
    from ba2_common.core.types import OrderStatus, TransactionStatus
    from ba2_common.core.models import Transaction
    from ba2_common.core.db import get_instance

    acct, ctx, ps = _acct([(D1, 100, 101, 99, 100), (D2, 102, 103, 101, 102)])
    try:
        ps.set_clock(D1)
        entry, txn = _open_long_with_legs(acct, ps)

        ps.set_clock(D1)
        acct.refresh_orders()  # entry MARKET fills at D2 open
        ps.set_clock(D2)
        acct.refresh_transactions()  # WAITING -> OPENED

        reloaded = get_instance(Transaction, txn.id)
        assert reloaded.status == TransactionStatus.OPENED
        assert reloaded.open_date is not None
        od = reloaded.open_date
        if od.tzinfo is not None:
            od = od.replace(tzinfo=None)
        # open_date is the entry order's recorded SIM fill bar (the same convention the
        # engine uses for close_date / round-trip trades) — a 2024 date, NOT wall-clock 2026.
        sim_fill = acct._fill_dates[entry.id]
        if sim_fill.tzinfo is not None:
            sim_fill = sim_fill.replace(tzinfo=None)
        assert od == sim_fill, f"open_date should equal entry sim fill {sim_fill}, got {od}"
        assert od.year == 2024, f"open_date must be a SIM date, not wall-clock; got {od}"
    finally:
        ctx.__exit__(None, None, None)


def test_open_date_sim_stamp_survives_close():
    """A CLOSED transaction keeps its SIM open_date (entry fill), not wall-clock."""
    from ba2_common.core.types import OrderStatus, TransactionStatus
    from ba2_common.core.models import Transaction
    from ba2_common.core.db import get_instance

    # D2 entry fills @102; D3 high 125 crosses TP @120 -> closes on D3.
    acct, ctx, ps = _acct(
        [
            (D1, 100, 101, 99, 100),
            (D2, 102, 103, 101, 102),
            (D3, 104, 125, 103, 120),
        ]
    )
    try:
        ps.set_clock(D1)
        entry, txn = _open_long_with_legs(acct, ps)
        acct.adjust_tp_sl(txn, new_tp_price=120.0, new_sl_price=90.0)

        ps.set_clock(D1)
        acct.refresh_orders()  # entry fills @102 (D2)
        ps.set_clock(D2)
        acct.refresh_transactions()  # OPENED, open_date stamped to D2
        acct.refresh_orders()  # activate OCO, evaluate vs D3 -> TP @120 fills
        acct.refresh_transactions()  # CLOSED

        reloaded = get_instance(Transaction, txn.id)
        assert reloaded.status == TransactionStatus.CLOSED
        od = reloaded.open_date
        if od.tzinfo is not None:
            od = od.replace(tzinfo=None)
        sim_fill = acct._fill_dates[entry.id]
        if sim_fill.tzinfo is not None:
            sim_fill = sim_fill.replace(tzinfo=None)
        assert od == sim_fill, f"open_date should remain entry sim fill {sim_fill}, got {od}"
        assert od.year == 2024, f"open_date must be a SIM date, not wall-clock; got {od}"
        # close_date is the current sim bar (D3) when the OCO TP filled — a 2024 date.
        cd = reloaded.close_date
        if cd is not None and cd.tzinfo is not None:
            cd = cd.replace(tzinfo=None)
        assert cd is not None and cd.year == 2024
    finally:
        ctx.__exit__(None, None, None)


def test_oco_sl_side_fills_when_both_straddled():
    """When a single bar straddles BOTH levels, the STOP (loss) side wins (conservative) — the
    position closes at the SL price, not the TP. Direct bracket check, no OCO leg."""
    # D3 range 85..125 crosses TP@120 AND SL@90 -> SL wins, closes at 90.
    acct, ctx, ps = _acct(
        [(D1, 100, 101, 99, 100), (D2, 102, 103, 101, 102), (D3, 104, 125, 85, 100)]
    )
    try:
        ps.set_clock(D1)
        entry, txn = _open_long_with_legs(acct, ps)
        acct.adjust_tp_sl(txn, new_tp_price=120.0, new_sl_price=90.0)
        ps.set_clock(D1); acct.refresh_orders(); acct.refresh_transactions()  # entry fills, OPENED
        ps.set_clock(D2); acct.refresh_orders(); acct.refresh_transactions()  # bracket vs D3 -> SL @90
        assert acct.get_positions() == []
        rts = acct.get_round_trip_trades()
        assert len(rts) == 1
        assert rts[0]["exit_price"] == pytest.approx(90.0)  # SL, not TP
        assert rts[0]["exit_reason"] == "stop_loss"
    finally:
        ctx.__exit__(None, None, None)


def test_paired_bracket_closes_once_no_double_close():
    """With BOTH a TP and an SL set, a bar crossing (only) the TP closes the position EXACTLY once
    at the TP — the lean bracket never double-closes (the old model relied on OCO sibling-cancel;
    here a single close per transaction per bar is enforced by construction)."""
    # D2 entry fills @102; D3 high 125 hits TP@120 (low 103 above SL@90) -> closes once at TP.
    acct, ctx, ps = _acct(
        [(D1, 100, 101, 99, 100), (D2, 102, 103, 101, 102), (D3, 104, 125, 103, 120)]
    )
    try:
        ps.set_clock(D1)
        entry, txn = _open_long_with_legs(acct, ps)
        acct.adjust_tp_sl(txn, new_tp_price=120.0, new_sl_price=90.0)
        ps.set_clock(D1); acct.refresh_orders(); acct.refresh_transactions()  # entry fills, OPENED
        ps.set_clock(D2); acct.refresh_orders(); acct.refresh_transactions()  # bracket vs D3 -> TP @120
        assert acct.get_positions() == []  # flat, not net-short (no double close)
        rts = acct.get_round_trip_trades()
        assert len(rts) == 1  # exactly one round-trip
        assert rts[0]["exit_price"] == pytest.approx(120.0)
        assert rts[0]["exit_reason"] == "take_profit"
    finally:
        ctx.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# cancel / modify
# ---------------------------------------------------------------------------
def test_cancel_order_marks_canceled_and_blocks_fill():
    from ba2_common.core.types import OrderDirection, OrderStatus

    acct, ctx, ps = _acct([(D1, 100, 101, 99, 100), (D2, 102, 103, 101, 102)])
    try:
        ps.set_clock(D1)
        o = _market("AAPL", 10, OrderDirection.BUY)
        acct.submit_order(o)
        acct.cancel_order(o.broker_order_id)
        assert acct.get_order(o.broker_order_id).status == OrderStatus.CANCELED
        acct.refresh_orders()  # canceled order must NOT fill
        assert acct.get_order(o.broker_order_id).status == OrderStatus.CANCELED
        assert acct.get_positions() == []
    finally:
        ctx.__exit__(None, None, None)


def test_modify_order_edits_limit_price_pre_fill():
    from ba2_common.core.types import OrderDirection, OrderType, OrderStatus

    acct, ctx, ps = _acct([(D1, 100, 101, 99, 100), (D2, 101, 102, 99, 101)])
    try:
        ps.set_clock(D1)
        o = _limit("AAPL", 10, OrderDirection.BUY, OrderType.BUY_LIMIT, 95.0)
        acct.submit_order(o)
        # Mutate then push via modify_order: raise the limit to 100 (D2 low 99 crosses).
        working = acct.get_order(o.broker_order_id)
        working.limit_price = 100.0
        from ba2_common.core.db import update_instance

        update_instance(working)
        modified = acct.modify_order(o.broker_order_id)
        assert modified.limit_price == 100.0
        acct.refresh_orders()
        assert acct.get_order(o.broker_order_id).status == OrderStatus.FILLED
        assert acct.get_order(o.broker_order_id).open_price == 100.0
    finally:
        ctx.__exit__(None, None, None)


def test_modify_terminal_order_returns_none():
    from ba2_common.core.types import OrderDirection

    acct, ctx, ps = _acct([(D1, 100, 101, 99, 100), (D2, 102, 103, 101, 102)])
    try:
        ps.set_clock(D1)
        o = _market("AAPL", 10, OrderDirection.BUY)
        acct.submit_order(o)
        acct.cancel_order(o.broker_order_id)  # now terminal
        assert acct.modify_order(o.broker_order_id) is None
    finally:
        ctx.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# price-cache busting (GATE item 6)
# ---------------------------------------------------------------------------
def test_price_cache_busted_across_bars():
    acct, ctx, ps = _acct([(D1, 100, 101, 99, 100), (D2, 200, 201, 199, 200)])
    try:
        ps.set_clock(D1)
        assert acct.get_instrument_current_price("AAPL") == 100.0
        ps.set_clock(D2)
        assert acct.get_instrument_current_price("AAPL") == 200.0  # not stale 100.0
    finally:
        ctx.__exit__(None, None, None)
