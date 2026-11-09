"""The bar an equity entry FILLS on is tested against its stop-loss / take-profit.

The finding (review 2026-11-09): a MARKET entry decided on clock D fills at the open of the next
bar (D+1). Its transaction is still WAITING when the bracket pass (``_apply_bracket_exits``, OPENED
transactions only) runs, and the status roll comes after it; the NEXT pass tests the bar after that
(D+2). So the bar the entry filled on -- from the fill at its open to its close -- was never checked
against the protective levels. Live, the stop rests at the broker from the fill and triggers on D+1.

These tests pin the corrected behaviour on hand-built bars (no network, no provider):

  * the fill bar's low pierces the stop -> stopped on that bar at the stop (or at the open when the
    stop already sits at/above the open, the existing gap rule);
  * the fill bar touches neither level -> nothing changes, the next bar is tested as before;
  * the fill bar touches both -> the stop wins (the existing SL-first rule);
  * the take-profit side is tested on that bar too (testing only the stop would bias the fix);
  * ``same_bar_close`` fills at the bar's CLOSE, so there is no remainder of the bar to test.
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


def _bars(rows):
    return [{"Date": d, "Open": o, "High": h, "Low": low, "Close": c, "Volume": 1000}
            for (d, o, h, low, c) in rows]


def _acct(rows, cfg=CFG, symbol="AAPL", account_id=1):
    from app.services.backtest.backtest_db import backtest_trading_db, seed_account_definition
    from app.services.backtest.seam_wiring import wire_backtest_seams
    from app.services.backtest.backtest_account import BacktestAccount
    from app.services.backtest.price_source import AsOfPriceSource

    wire_backtest_seams()
    ctx = backtest_trading_db(f"fillbar-{account_id}")
    ctx.__enter__()
    seed_account_definition(account_id, cfg)
    ps = AsOfPriceSource(ohlcv_provider=None)
    ps.load_bars(symbol, _bars(rows))
    acct = BacktestAccount(account_id, ps, cfg)
    wire_backtest_seams().register_account(account_id, acct)
    return acct, ctx, ps


def _enter_long(acct, qty=10):
    """Submit a long MARKET entry (auto-creates its WAITING transaction)."""
    from ba2_common.core.models import Transaction, TradingOrder
    from ba2_common.core.db import get_instance
    from ba2_common.core.types import OrderDirection, OrderStatus, OrderType

    o = TradingOrder(account_id=acct.id, symbol="AAPL", quantity=qty, side=OrderDirection.BUY,
                     order_type=OrderType.MARKET, status=OrderStatus.NEW, comment="fill-bar-test")
    acct.submit_order(o)
    return o, get_instance(Transaction, o.transaction_id)


def _engine_pass(acct, ps, clock):
    """One engine bar tail: the fill engine, then the transaction roll (daily_engine order)."""
    ps.set_clock(clock)
    if acct.refresh_orders():
        acct.refresh_transactions()


def _status(txn_id):
    from ba2_common.core.models import Transaction
    from ba2_common.core.db import get_instance
    return get_instance(Transaction, txn_id).status


@pytest.mark.parametrize("inmem", ["1", "0"], ids=["inmem-store", "sqlite"])
def test_fill_bar_low_through_the_stop_stops_out_on_the_fill_bar(monkeypatch, inmem):
    """Decision D1, fill at D2's open 100, stop 95; D2 trades down to 94 -> out at 95 ON D2.
    Both trade stores (the GA's in-memory store and the SQLite escape hatch)."""
    from ba2_common.core.types import TransactionStatus
    from tests.backtest.test_max_loss_stop_engine import _store_mode

    _store_mode(monkeypatch, inmem)
    acct, ctx, ps = _acct([(D1, 100, 101, 99, 100), (D2, 100, 101, 94, 96),
                           (D3, 96, 99, 95.5, 98)], account_id=2 if inmem == "0" else 1)
    try:
        ps.set_clock(D1)
        entry, txn = _enter_long(acct)
        acct.adjust_sl(txn, 95.0)
        _engine_pass(acct, ps, D1)          # entry fills @ D2 open 100; D2 low 94 <= 95
        assert _status(txn.id) == TransactionStatus.CLOSED
        assert acct.get_positions() == []
        rts = acct.get_round_trip_trades()
        assert len(rts) == 1
        assert rts[0]["entry_price"] == pytest.approx(100.0)
        assert rts[0]["exit_price"] == pytest.approx(95.0)
        assert rts[0]["exit_reason"] == "stop_loss"
        assert acct.get_balance() == pytest.approx(100_000.0 - 10 * 5.0)
    finally:
        ctx.__exit__(None, None, None)


def test_stop_at_or_above_the_fill_open_exits_at_the_open_gap_rule():
    """The levels were built from D1's close (110); D2 OPENS at 100, below the 105 stop. The stop
    is a market order the moment it rests, so it fills at the open (``_gap_stop_fill``), not 105."""
    from ba2_common.core.types import TransactionStatus

    acct, ctx, ps = _acct([(D1, 110, 111, 109, 110), (D2, 100, 102, 98, 101),
                           (D3, 101, 103, 100, 102)])
    try:
        ps.set_clock(D1)
        entry, txn = _enter_long(acct)
        acct.adjust_sl(txn, 105.0)
        _engine_pass(acct, ps, D1)
        assert _status(txn.id) == TransactionStatus.CLOSED
        rts = acct.get_round_trip_trades()
        assert len(rts) == 1
        assert rts[0]["exit_price"] == pytest.approx(100.0)
        assert rts[0]["exit_reason"] == "stop_loss"
    finally:
        ctx.__exit__(None, None, None)


def test_fill_bar_touching_neither_level_changes_nothing():
    """D2 stays inside (95, 110): the trade is still open after D2, and D3 is tested as before."""
    from ba2_common.core.types import TransactionStatus

    acct, ctx, ps = _acct([(D1, 100, 101, 99, 100), (D2, 100, 103, 97, 101),
                           (D3, 101, 111, 100, 110)])
    try:
        ps.set_clock(D1)
        entry, txn = _enter_long(acct)
        acct.adjust_tp_sl(txn, new_tp_price=110.0, new_sl_price=95.0)
        _engine_pass(acct, ps, D1)
        assert _status(txn.id) == TransactionStatus.OPENED
        assert len(acct.get_positions()) == 1
        _engine_pass(acct, ps, D2)          # D3 high 111 >= 110 -> TP
        assert _status(txn.id) == TransactionStatus.CLOSED
        rts = acct.get_round_trip_trades()
        assert rts[0]["exit_price"] == pytest.approx(110.0)
        assert rts[0]["exit_reason"] == "take_profit"
    finally:
        ctx.__exit__(None, None, None)


def test_fill_bar_touching_both_levels_the_stop_wins():
    """D2's range spans the 95 stop AND the 110 target: intrabar order unknown -> stop (worst case)."""
    from ba2_common.core.types import TransactionStatus

    acct, ctx, ps = _acct([(D1, 100, 101, 99, 100), (D2, 100, 112, 94, 100),
                           (D3, 100, 101, 99, 100)])
    try:
        ps.set_clock(D1)
        entry, txn = _enter_long(acct)
        acct.adjust_tp_sl(txn, new_tp_price=110.0, new_sl_price=95.0)
        _engine_pass(acct, ps, D1)
        assert _status(txn.id) == TransactionStatus.CLOSED
        rts = acct.get_round_trip_trades()
        assert len(rts) == 1
        assert rts[0]["exit_price"] == pytest.approx(95.0)
        assert rts[0]["exit_reason"] == "stop_loss"
    finally:
        ctx.__exit__(None, None, None)


def test_fill_bar_reaching_the_target_takes_profit_on_the_fill_bar():
    """The take-profit rests from the fill as well; testing only the stop would bias the fix."""
    from ba2_common.core.types import TransactionStatus

    acct, ctx, ps = _acct([(D1, 100, 101, 99, 100), (D2, 100, 112, 99, 111),
                           (D3, 111, 113, 110, 112)])
    try:
        ps.set_clock(D1)
        entry, txn = _enter_long(acct)
        acct.adjust_tp_sl(txn, new_tp_price=110.0, new_sl_price=95.0)
        _engine_pass(acct, ps, D1)
        assert _status(txn.id) == TransactionStatus.CLOSED
        rts = acct.get_round_trip_trades()
        assert rts[0]["exit_price"] == pytest.approx(110.0)
        assert rts[0]["exit_reason"] == "take_profit"
    finally:
        ctx.__exit__(None, None, None)


def test_the_fill_bar_is_the_bar_the_entry_filled_on_even_if_the_roll_runs_later():
    """The test reads the bar the entry FILLED on (recorded at the fill), never the bar after the
    clock of a later roll -- no look-ahead past the fill bar."""
    from ba2_common.core.types import TransactionStatus

    # D2 (the fill bar) touches nothing; D3 would pierce the stop. Rolling at clock D2 must not
    # test D3 early on the fill-bar pass.
    acct, ctx, ps = _acct([(D1, 100, 101, 99, 100), (D2, 100, 103, 97, 101),
                           (D3, 99, 100, 90, 92)])
    try:
        ps.set_clock(D1)
        entry, txn = _enter_long(acct)
        acct.adjust_sl(txn, 95.0)
        ps.set_clock(D1)
        acct.refresh_orders()
        ps.set_clock(D2)
        acct.refresh_transactions()
        assert _status(txn.id) == TransactionStatus.OPENED
    finally:
        ctx.__exit__(None, None, None)


def test_same_bar_close_fill_model_has_no_remainder_to_test():
    """``same_bar_close`` fills at the bar's CLOSE: its range happened BEFORE the fill, so the fill
    bar must not be tested (that would be the look-ahead the opposite way)."""
    from ba2_common.core.types import TransactionStatus

    cfg = {**CFG, "fill_model": "same_bar_close"}
    acct, ctx, ps = _acct([(D1, 100, 101, 90, 100), (D2, 100, 101, 99, 100)], cfg=cfg)
    try:
        ps.set_clock(D1)
        entry, txn = _enter_long(acct)
        acct.adjust_sl(txn, 95.0)
        _engine_pass(acct, ps, D1)          # fills @ D1 close 100; D1 low 90 was BEFORE the fill
        assert _status(txn.id) == TransactionStatus.OPENED
    finally:
        ctx.__exit__(None, None, None)


def test_intraday_clock_the_5min_fill_bar_is_tested_through_the_real_engine(monkeypatch):
    """Full ``engine.run()`` on the 5-minute clock: decision 09:40 (price 100), fill at the 09:45
    bar's open (100), stop 8% under = 92. The 09:45 bar itself trades down to 91 -> stopped at 92 on
    the 09:45 bar. The later bars never reach 92, so before the fix this trade stayed open."""
    import tests.backtest.test_fill_rebase_clocks_options as M
    from tests.backtest.test_max_loss_stop_engine import _store_mode

    prior = [(datetime(2023, 12, 29, 9, 30 + 5 * i), 100, 100.5, 99.5, 100) for i in range(4)]
    day = [
        (datetime(2024, 1, 2, 9, 30), 100, 100.5, 99.5, 100),
        (datetime(2024, 1, 2, 9, 35), 100, 100.5, 99.5, 100),
        (datetime(2024, 1, 2, 9, 40), 100, 100.5, 99.5, 100),
        (datetime(2024, 1, 2, 9, 45), 100, 100.5, 91, 95),     # the FILL bar pierces 92
        (datetime(2024, 1, 2, 9, 50), 95, 96, 94, 95),
        (datetime(2024, 1, 2, 9, 55), 95, 96, 94.5, 95),
        (datetime(2024, 1, 2, 10, 0), 95, 96, 94.5, 95),
    ]
    monkeypatch.setattr(M, "_intraday_bars", lambda: prior + day)
    _store_mode(monkeypatch, "1")
    _levels, trades = M._run_intraday(1390)
    [t] = trades
    assert t["exit_reason"] == "stop_loss"
    assert t["entry_price"] == pytest.approx(100.0) and t["exit_price"] == pytest.approx(92.0)
    assert M.run_intraday_account[-1].fill_bar_exit_counters["exits_on_fill_bar"] == 1


def test_the_fill_bar_check_is_counted():
    """The number of trades that left on their fill bar is observable in the run results."""
    acct, ctx, ps = _acct([(D1, 100, 101, 99, 100), (D2, 100, 101, 94, 96),
                           (D3, 96, 99, 95.5, 98)])
    try:
        ps.set_clock(D1)
        entry, txn = _enter_long(acct)
        acct.adjust_sl(txn, 95.0)
        _engine_pass(acct, ps, D1)
        assert acct.fill_bar_exit_counters["exits_on_fill_bar"] == 1
        assert acct.fill_bar_exit_counters["stop_loss"] == 1
        assert acct.fill_bar_exit_counters["take_profit"] == 0
    finally:
        ctx.__exit__(None, None, None)
