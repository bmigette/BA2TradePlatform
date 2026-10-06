"""EXPERIMENT (exp/verify-lookahead-and-entry-bar): can a stop / TP trigger on the entry bar?

Not a regression test: it PRINTS what the real BacktestAccount does (run with -s) and asserts only the
observed behaviour so the finding is pinned. Same fixtures/clock order as test_backtest_account_fills.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from tests.backtest.test_backtest_account_fills import CFG, _bars, _market


def _build(rows, interval):
    from app.services.backtest.backtest_db import backtest_trading_db, seed_account_definition
    from app.services.backtest.seam_wiring import wire_backtest_seams
    from app.services.backtest.backtest_account import BacktestAccount
    from app.services.backtest.price_source import AsOfPriceSource

    wire_backtest_seams()
    ctx = backtest_trading_db("exp-entry-bar")
    ctx.__enter__()
    seed_account_definition(1, CFG)
    ps = AsOfPriceSource(ohlcv_provider=None, interval=interval)
    ps.load_bars("AAPL", _bars(rows))
    acct = BacktestAccount(1, ps, CFG)
    wire_backtest_seams().register_account(1, acct)
    return acct, ctx, ps


def _scenario(label, rows, interval, sl, tp):
    """Decision on rows[0]; BUY 10 with SL/TP on the transaction; then the engine's per-bar
    set_clock -> refresh_orders -> refresh_transactions for EVERY bar (like _fills_and_settlements)."""
    from ba2_common.core.types import OrderDirection
    from ba2_common.core.models import Transaction
    from ba2_common.core.db import get_instance

    acct, ctx, ps = _build(rows, interval)
    out = []
    try:
        ps.set_clock(rows[0][0])
        o = _market("AAPL", 10, OrderDirection.BUY)
        acct.submit_order(o)
        txn = get_instance(Transaction, o.transaction_id)
        acct.adjust_tp_sl(txn, new_tp_price=tp, new_sl_price=sl)
        for r in rows:
            ps.set_clock(r[0])
            filled = acct.refresh_orders()
            if filled:
                acct.refresh_transactions()
            pos = [(p["symbol"], p["qty"]) for p in acct.get_positions()]
            rts = [t for t in acct.get_round_trip_trades() if t.get("exit_reason") != "open_at_end"]
            out.append((str(r[0]), filled, pos, [(t["exit_price"], t["exit_reason"], round(t["pnl"], 2),
                                                  "entry", str(t.get("entry_time"))[:19], t.get("entry_price"),
                                                  "exit_time", str(t.get("exit_time"))[:19]) for t in rts]))
        e = acct.get_order(o.broker_order_id)
        print(f"\n=== {label}  SL={sl} TP={tp}  entry filled_at={e.open_price} status={e.status}")
        for i, r in enumerate(rows):
            print(f"  bar {r[0]} O={r[1]} H={r[2]} L={r[3]} C={r[4]}")
        for line in out:
            print("  after clock", line)
        return out
    finally:
        ctx.__exit__(None, None, None)


DAILY = [datetime(2024, 1, 2) + timedelta(days=i) for i in range(5)]
M5 = [datetime(2024, 1, 2, 9, 30) + timedelta(minutes=5 * i) for i in range(5)]


@pytest.mark.parametrize("name,times,interval", [("daily", DAILY, "1d"), ("5min", M5, "5min")])
def test_entry_bar_scenarios(name, times, interval):
    t = times
    # (a) entry bar (t1) opens 100, low 94 < SL 95 AFTER the fill; next bar t2 low 93.
    a = [(t[0], 100, 101, 99, 100), (t[1], 100, 101, 94, 97), (t[2], 97, 98, 93, 94), (t[3], 94, 95, 93, 94), (t[4], 94, 95, 93, 94)]
    _scenario(f"{name} (a) low through stop on entry bar", a, interval, sl=95.0, tp=120.0)
    # (a2) entry bar dips through the stop but the NEXT bar does not touch it -> BT never exits.
    a2 = [(t[0], 100, 101, 99, 100), (t[1], 100, 101, 94, 99), (t[2], 99, 101, 98, 100), (t[3], 100, 101, 99, 100), (t[4], 100, 101, 99, 100)]
    _scenario(f"{name} (a2) entry bar dips through stop, next bars clean", a2, interval, sl=95.0, tp=120.0)
    # (b) the fill itself gaps below the stop: entry bar opens 90 (stop 95).
    b = [(t[0], 100, 101, 99, 100), (t[1], 90, 91, 89, 90), (t[2], 91, 92, 89, 90), (t[3], 90, 91, 89, 90), (t[4], 90, 91, 89, 90)]
    _scenario(f"{name} (b) fill gaps below stop", b, interval, sl=95.0, tp=120.0)
    # (c) entry bar high reaches TP 108.
    c = [(t[0], 100, 101, 99, 100), (t[1], 100, 110, 99, 105), (t[2], 105, 106, 104, 105), (t[3], 105, 106, 104, 105), (t[4], 105, 106, 104, 105)]
    _scenario(f"{name} (c) entry bar high reaches TP", c, interval, sl=80.0, tp=108.0)
