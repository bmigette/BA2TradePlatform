"""Regression tests for the two OS1 option-backtest blow-up fixes (BacktestAccount):

  A. NO ORPHANED STOCK AT EXPIRY (``settle_single_leg_expiry`` /
     ``process_pending_assignment_liquidations``):
       * an ITM LONG option is NEVER exercised — it is sold to close at the expiry bar's
         premium close (intrinsic fallback), cash credited once, NO share position;
       * an ITM SHORT option is still physically assigned at the strike, but the assigned
         stock (long from a short put, short from a naked call) is liquidated in FULL at
         the next bar's open — no stock rides unmanaged beyond one bar.
     (Exercised ITM long calls riding to the end of the run were 67-85% of the OS1 runs'
     final equity.)

  B. NO-ARBITRAGE FILL GUARD (``_option_fill_price`` / ``_arb_fill_reject_reason``):
       * an ENTRY fill whose premium is below intrinsic by more than ``_ARB_FILL_TOLERANCE``
         is rejected as a junk indicative print (e.g. a $0.01 call with $54.85 of
         intrinsic) — the order stays pending like a non-crossing limit, a warning is
         logged and ``account.rejected_arb_fills`` is incremented;
       * an EXIT fill with an impossible premium (call > spot + tol, put > strike + tol)
         is rejected the same way — the position stays open and a later sane bar fills;
       * a multi-leg combo with one junk leg fills NOTHING (all-or-none).

Run from the backend dir:
    ./venv/bin/python -m pytest tests/backtest/test_option_orphan_stock_and_arb_guards.py -q
"""
from __future__ import annotations

import logging
from datetime import date, datetime

import pytest

from ba2_common.core.types import OptionRight, OrderDirection, OrderStatus


CFG = {
    "starting_cash": 100_000.0,
    "commission_per_trade": 0.0,   # zero so P&L math is exact and easy to assert
    "slippage_bps": 0.0,           # zero -> fill premium == bar open exactly
    "fill_model": "next_bar_open",
}

_CALL105 = "AAPL240315C00105000"   # 105 call, expiry 2024-03-15 (the OS1 junk-print shape)
_CALL110 = "AAPL240315C00110000"   # 110 call, expiry 2024-03-15 (multi-leg short leg)
_CALL160 = "AAPL240315C00160000"   # 160 call, expiry 2024-03-15 (normal entry)
_PUT180 = "AAPL240308P00180000"    # 180 put, expiry 2024-03-08 (short-put assignment)

# Underlying bars. Fill bar 2024-03-06 opens at 160 (105-call intrinsic 55, 110-call 50);
# the 180-put expires 2024-03-08 (close 164 < 180 -> ITM) and its assigned stock liquidates
# at the 2024-03-11 open 165; the calls expire 2024-03-15 (close 200) with the assigned
# short stock bought back at the 2024-03-18 open 200.
_AAPL_BARS = [
    {"Date": datetime(2024, 3, 5), "Open": 150, "High": 152, "Low": 149, "Close": 151, "Volume": 1000},
    {"Date": datetime(2024, 3, 6), "Open": 160, "High": 162, "Low": 159, "Close": 161, "Volume": 1100},
    {"Date": datetime(2024, 3, 7), "Open": 161, "High": 163, "Low": 160, "Close": 162, "Volume": 1200},
    {"Date": datetime(2024, 3, 8), "Open": 163, "High": 165, "Low": 162, "Close": 164, "Volume": 1300},
    {"Date": datetime(2024, 3, 11), "Open": 165, "High": 167, "Low": 164, "Close": 166, "Volume": 1400},
    {"Date": datetime(2024, 3, 15), "Open": 199, "High": 201, "Low": 198, "Close": 200, "Volume": 1500},
    {"Date": datetime(2024, 3, 18), "Open": 200, "High": 202, "Low": 199, "Close": 200, "Volume": 1600},
]

_EXP_0315 = date(2024, 3, 15)
_EXP_0308 = date(2024, 3, 8)


def _chain_row(occ, ot, strike, expiry):
    return {"occ_symbol": occ, "option_type": ot, "strike": strike,
            "expiry": expiry.isoformat(), "bid": 1.0, "ask": 1.2, "last": 1.1, "iv": 0.25}


def _bar_row(occ, d, o, ot, strike, expiry, c=None):
    return {"occ_symbol": occ, "date": d, "open": o, "high": o, "low": o,
            "close": c if c is not None else o, "volume": 100, "underlying": "AAPL",
            "option_type": ot, "strike": strike, "expiry": expiry.isoformat()}


def _build(tmp_path, name, chain_rows, bar_rows):
    """A BacktestAccount over a seeded temp options cache + the AAPL bar series."""
    from app.services.backtest.backtest_db import backtest_trading_db, seed_account_definition
    from app.services.backtest.seam_wiring import wire_backtest_seams
    from app.services.backtest.backtest_account import BacktestAccount
    from app.services.backtest.options_cache import OptionsHistoryCache
    from app.services.backtest.options_provider import HistoricalOptionsProvider
    from app.services.backtest.price_source import AsOfPriceSource

    cache_db = str(tmp_path / f"{name}_options_cache.sqlite")
    cache = OptionsHistoryCache(cache_db)
    if chain_rows:
        cache.write_chain_rows("AAPL", "2024-03-01", chain_rows)
    if bar_rows:
        cache.write_bar_rows(bar_rows)
    provider = HistoricalOptionsProvider(cache_db)

    wire_backtest_seams()
    ctx = backtest_trading_db(name)
    ctx.__enter__()
    seed_account_definition(1, CFG)
    ps = AsOfPriceSource(ohlcv_provider=None)
    ps.load_bars("AAPL", _AAPL_BARS)
    ps.set_clock(datetime(2024, 3, 5))
    acct = BacktestAccount(1, ps, CFG, options_provider=provider)
    wire_backtest_seams().register_account(1, acct)
    return acct, ps, ctx


def _engine(acct, ps):
    """A minimal engine bound to the account so ``_apply_option_expiry`` can be driven."""
    from app.services.backtest.daily_engine import DailyBacktestEngine

    eng = DailyBacktestEngine.__new__(DailyBacktestEngine)
    eng.account = acct
    eng.price = ps
    eng.config = CFG
    return eng


def _leg(occ, side, intent, ot, strike, expiry):
    from ba2_common.core.option_types import OptionLeg

    return OptionLeg(contract_symbol=occ, side=side, position_intent=intent,
                     option_type=ot, strike=strike, expiry=expiry, underlying="AAPL")


def _submit_call(acct, occ=_CALL105, side=OrderDirection.BUY, intent="buy_to_open",
                 strike=105.0, qty=1, strategy="long_call"):
    return acct.submit_option_order(
        legs=[_leg(occ, side, intent, OptionRight.CALL, strike, _EXP_0315)],
        quantity=qty, order_type="market", option_strategy=strategy)


# ---------------------------------------------------------------------------
# B1: ENTRY fill guard — premium below intrinsic - tolerance is junk
# ---------------------------------------------------------------------------
def test_junk_entry_fill_rejected_then_fills_on_sane_bar(tmp_path, caplog):
    """The OS1 print: a 105 call at $0.01 while spot opens at $160 (intrinsic $55) must NOT
    fill — no position, no cash change, counter incremented, warning logged. The order
    stays pending and fills on the next bar's SANE premium."""
    acct, ps, ctx = _build(
        tmp_path, "arbentry",
        [_chain_row(_CALL105, "call", 105.0, _EXP_0315)],
        [_bar_row(_CALL105, "2024-03-06", 0.01, "call", 105.0, _EXP_0315)],
    )
    try:
        order = _submit_call(acct)
        cash_before = acct._cash
        assert acct.rejected_arb_fills == 0

        with caplog.at_level(logging.WARNING, logger="app.services.backtest.backtest_account"):
            acct.refresh_orders()
            acct.refresh_transactions()

        # REJECTED: the order did not fill, nothing moved, the rejection was counted + logged.
        assert acct.get_order(order.id).status == OrderStatus.ACCEPTED
        assert acct.get_option_positions() == []
        assert acct._cash == pytest.approx(cash_before)
        assert acct.rejected_arb_fills == 1
        assert any("arb-inconsistent" in r.message and _CALL105 in r.message
                   for r in caplog.records)

        # A SANE premium bar on the next fill day (56.5 vs intrinsic 56.0) fills normally.
        from app.services.backtest.options_cache import OptionsHistoryCache
        from app.services.backtest.options_provider import clear_worker_options_cache

        cache_db = str(tmp_path / "arbentry_options_cache.sqlite")
        OptionsHistoryCache(cache_db).write_bar_rows(
            [_bar_row(_CALL105, "2024-03-07", 56.5, "call", 105.0, _EXP_0315)])
        clear_worker_options_cache()  # drop the memoized per-contract bar history
        ps.set_clock(datetime(2024, 3, 6))
        acct.refresh_orders()
        acct.refresh_transactions()

        filled = acct.get_order(order.id)
        assert filled.status == OrderStatus.FILLED
        assert filled.open_price == pytest.approx(56.5)
        assert acct._cash == pytest.approx(cash_before - 56.5 * 100.0)
        assert acct.rejected_arb_fills == 1   # unchanged by the sane fill
    finally:
        ctx.__exit__(None, None, None)


def test_normal_entry_fill_not_rejected(tmp_path):
    """A premium ABOVE intrinsic - tolerance fills as before (guard is invisible)."""
    acct, ps, ctx = _build(
        tmp_path, "arbnormal",
        [_chain_row(_CALL160, "call", 160.0, _EXP_0315)],
        [_bar_row(_CALL160, "2024-03-06", 6.0, "call", 160.0, _EXP_0315)],
    )
    try:
        order = _submit_call(acct, occ=_CALL160, strike=160.0)
        acct.refresh_orders()
        acct.refresh_transactions()

        assert acct.get_order(order.id).status == OrderStatus.FILLED
        assert len(acct.get_option_positions()) == 1
        assert acct.rejected_arb_fills == 0
    finally:
        ctx.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# B2: EXIT fill guard — impossible premiums (call > spot + tol) do not fill
# ---------------------------------------------------------------------------
def test_junk_exit_fill_rejected_position_stays_open_then_sane_bar_fills(tmp_path):
    """A sell-to-close at $165 while spot opens at $161 is impossible (a call can never
    cost more than the stock): the close does NOT fill and the position stays open; the
    next sane bar closes it. Expiry settlement remains the ultimate backstop."""
    acct, ps, ctx = _build(
        tmp_path, "arbexit",
        [_chain_row(_CALL105, "call", 105.0, _EXP_0315)],
        [_bar_row(_CALL105, "2024-03-06", 56.0, "call", 105.0, _EXP_0315),   # sane entry
         _bar_row(_CALL105, "2024-03-07", 165.0, "call", 105.0, _EXP_0315),  # junk exit
         _bar_row(_CALL105, "2024-03-08", 160.0, "call", 105.0, _EXP_0315)], # sane exit
    )
    try:
        _submit_call(acct)
        acct.refresh_orders()
        acct.refresh_transactions()
        assert len(acct.get_option_positions()) == 1
        cash_after_entry = acct._cash                     # 100,000 - 5,600 = 94,400

        # Close on the JUNK bar: 165.0 > spot 161 + tol -> rejected, position still open.
        pos = acct.get_option_positions()[0]
        close_order = acct.close_option_position(pos, order_type="market")
        ps.set_clock(datetime(2024, 3, 6))
        acct.refresh_orders()
        acct.refresh_transactions()
        assert acct.get_order(close_order.id).status == OrderStatus.ACCEPTED
        assert len(acct.get_option_positions()) == 1      # still held
        assert acct._cash == pytest.approx(cash_after_entry)
        assert acct.rejected_arb_fills == 1

        # The next bar's SANE premium (160.0 <= spot 163 + tol) closes the position.
        ps.set_clock(datetime(2024, 3, 7))
        acct.refresh_orders()
        acct.refresh_transactions()
        assert acct.get_order(close_order.id).status == OrderStatus.FILLED
        assert acct.get_option_positions() == []
        assert acct._cash == pytest.approx(cash_after_entry + 160.0 * 100.0)
        assert acct.rejected_arb_fills == 1
    finally:
        ctx.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# B3: multi-leg combo — one junk leg blocks the whole all-or-none fill
# ---------------------------------------------------------------------------
def test_multi_leg_combo_with_one_junk_leg_does_not_fill(tmp_path):
    """Bull-call-spread entry: the long 105c prices fine (56.0 vs intrinsic 55) but the
    short 110c's $0.02 print is junk (intrinsic $50) -> the WHOLE combo stays unfilled."""
    acct, ps, ctx = _build(
        tmp_path, "arbcombo",
        [_chain_row(_CALL105, "call", 105.0, _EXP_0315),
         _chain_row(_CALL110, "call", 110.0, _EXP_0315)],
        [_bar_row(_CALL105, "2024-03-06", 56.0, "call", 105.0, _EXP_0315),
         _bar_row(_CALL110, "2024-03-06", 0.02, "call", 110.0, _EXP_0315)],
    )
    try:
        long_leg = _leg(_CALL105, OrderDirection.BUY, "buy_to_open",
                        OptionRight.CALL, 105.0, _EXP_0315)
        short_leg = _leg(_CALL110, OrderDirection.SELL, "sell_to_open",
                         OptionRight.CALL, 110.0, _EXP_0315)
        parent = acct.submit_option_order(
            legs=[long_leg, short_leg], quantity=1, order_type="market",
            option_strategy="bull_call_spread")
        cash_before = acct._cash

        acct.refresh_orders()
        acct.refresh_transactions()

        assert acct.get_order(parent.id).status == OrderStatus.ACCEPTED  # nothing filled
        assert acct.get_option_positions() == []
        assert acct._cash == pytest.approx(cash_before)
        assert acct.rejected_arb_fills == 1   # exactly the one junk leg
        children = [o for o in acct.get_orders() if o.parent_order_id == parent.id]
        assert len(children) == 2
        assert all(c.status == OrderStatus.ACCEPTED for c in children)
    finally:
        ctx.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# A1: ITM long option at expiry — SOLD TO CLOSE, never exercised, no stock
# ---------------------------------------------------------------------------
def _open_long_call(tmp_path, name, extra_rows):
    acct, ps, ctx = _build(
        tmp_path, name,
        [_chain_row(_CALL105, "call", 105.0, _EXP_0315)],
        [_bar_row(_CALL105, "2024-03-06", 56.0, "call", 105.0, _EXP_0315)] + extra_rows,
    )
    _submit_call(acct)
    acct.refresh_orders()
    acct.refresh_transactions()
    assert len(acct.get_option_positions()) == 1
    return acct, ps, ctx


def test_long_itm_call_expiry_sold_to_close_at_premium_no_stock(tmp_path):
    """Expiry-day premium bar close 95.5 (intrinsic 95): credited at the PREMIUM, the leg
    booked exactly once, ZERO equity/shares position created."""
    acct, ps, ctx = _open_long_call(
        tmp_path, "exprem",
        [_bar_row(_CALL105, "2024-03-15", 95.5, "call", 105.0, _EXP_0315)])
    try:
        cash_after_entry = acct._cash                     # 100,000 - 5,600 = 94,400
        ps.set_clock(datetime(2024, 3, 15))
        _engine(acct, ps)._apply_option_expiry(datetime(2024, 3, 15))

        assert acct.get_option_positions() == []
        assert [p for p in acct.get_positions() if p["symbol"] == "AAPL"] == []  # NO stock
        assert acct._cash == pytest.approx(cash_after_entry + 95.5 * 100.0, abs=1.0)
        closes = [o for o in acct.get_orders() if o.comment == "option_expiry_close"]
        assert len(closes) == 1                            # leg booked exactly once
        assert closes[0].open_price == pytest.approx(95.5)
        assert closes[0].filled_qty == pytest.approx(1.0)
    finally:
        ctx.__exit__(None, None, None)


def test_long_itm_call_expiry_no_premium_bar_settles_intrinsic_no_stock(tmp_path):
    """No expiry-day premium bar -> intrinsic fallback (95.0); still no stock, one close."""
    acct, ps, ctx = _open_long_call(tmp_path, "expintr", [])
    try:
        cash_after_entry = acct._cash
        ps.set_clock(datetime(2024, 3, 15))
        _engine(acct, ps)._apply_option_expiry(datetime(2024, 3, 15))

        assert acct.get_option_positions() == []
        assert [p for p in acct.get_positions() if p["symbol"] == "AAPL"] == []
        assert acct._cash == pytest.approx(cash_after_entry + 95.0 * 100.0, abs=1.0)
        closes = [o for o in acct.get_orders() if o.comment == "option_expiry_close"]
        assert len(closes) == 1
        assert closes[0].open_price == pytest.approx(95.0)
    finally:
        ctx.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# A2: short-option assignment — stock at the strike, liquidated at the next bar open
# ---------------------------------------------------------------------------
def test_short_put_assignment_stock_liquidated_next_bar_open(tmp_path):
    """ITM short put at expiry: +100 shares delivered at the strike (cash debited), then
    the FULL assignment is sold at the next bar's open — no stock beyond one bar, cash
    accounting exact."""
    acct, ps, ctx = _build(
        tmp_path, "assnput",
        [_chain_row(_PUT180, "put", 180.0, _EXP_0308)],
        [_bar_row(_PUT180, "2024-03-06", 21.0, "put", 180.0, _EXP_0308)],
    )
    try:
        # Sell-to-open 1x 180 put @21.0 (intrinsic at entry is 20 -> the credit is sane).
        acct.submit_option_order(
            legs=[_leg(_PUT180, OrderDirection.SELL, "sell_to_open",
                       OptionRight.PUT, 180.0, _EXP_0308)],
            quantity=1, order_type="market", option_strategy="naked_put")
        acct.refresh_orders()
        acct.refresh_transactions()
        assert acct._cash == pytest.approx(100_000.0 + 2_100.0)

        # Expiry 2024-03-08: spot closes 164 < 180 -> ITM -> assigned BUY 100 sh @ 180.
        ps.set_clock(datetime(2024, 3, 8))
        _engine(acct, ps)._apply_option_expiry(datetime(2024, 3, 8))
        aapl = [p for p in acct.get_positions() if p["symbol"] == "AAPL"]
        assert len(aapl) == 1 and aapl[0]["qty"] == 100
        assert aapl[0]["avg_price"] == pytest.approx(180.0)   # cost basis == strike
        assert acct._cash == pytest.approx(102_100.0 - 18_000.0)  # 84,100
        assert acct._pending_assignment_sells == {"AAPL": 100.0}

        # Next bar (2024-03-11 open 165): the FULL 100 assigned shares are sold.
        ps.set_clock(datetime(2024, 3, 11))
        assert acct.process_pending_assignment_liquidations() is True
        aapl = [p for p in acct.get_positions() if p["symbol"] == "AAPL"]
        assert aapl == [] or aapl[0]["qty"] == 0            # no stock beyond one bar
        assert acct._cash == pytest.approx(84_100.0 + 100.0 * 165.0)  # 100,600
        closes = [o for o in acct.get_orders() if o.comment == "assignment_liquidation"]
        assert len(closes) == 1
        assert closes[0].side == OrderDirection.SELL
        assert closes[0].filled_qty == pytest.approx(100.0)
        assert closes[0].open_price == pytest.approx(165.0)  # the next bar's OPEN
        assert acct.process_pending_assignment_liquidations() is False
    finally:
        ctx.__exit__(None, None, None)


def test_short_call_assignment_short_stock_bought_back_next_bar_open(tmp_path):
    """ITM naked short call at expiry: -100 shares assigned at the strike (cash credited),
    then the short is BOUGHT BACK in full at the next bar's open."""
    acct, ps, ctx = _build(
        tmp_path, "assncall",
        [_chain_row(_CALL105, "call", 105.0, _EXP_0315)],
        [_bar_row(_CALL105, "2024-03-06", 56.0, "call", 105.0, _EXP_0315)],
    )
    try:
        _submit_call(acct, side=OrderDirection.SELL, intent="sell_to_open",
                     strategy="naked_call")
        acct.refresh_orders()
        acct.refresh_transactions()
        assert acct._cash == pytest.approx(100_000.0 + 5_600.0)

        # Expiry 2024-03-15: spot closes 200 > 105 -> ITM -> assigned SELL 100 sh @ 105.
        ps.set_clock(datetime(2024, 3, 15))
        _engine(acct, ps)._apply_option_expiry(datetime(2024, 3, 15))
        aapl = [p for p in acct.get_positions() if p["symbol"] == "AAPL"]
        assert len(aapl) == 1 and aapl[0]["qty"] == -100
        assert aapl[0]["avg_price"] == pytest.approx(105.0)
        assert acct._cash == pytest.approx(105_600.0 + 10_500.0)  # 116,100
        assert acct._pending_assignment_sells == {"AAPL": -100.0}

        # Next bar (2024-03-18 open 200): the assigned short is bought back in full.
        ps.set_clock(datetime(2024, 3, 18))
        assert acct.process_pending_assignment_liquidations() is True
        aapl = [p for p in acct.get_positions() if p["symbol"] == "AAPL"]
        assert aapl == [] or aapl[0]["qty"] == 0
        assert acct._cash == pytest.approx(116_100.0 - 100.0 * 200.0)  # 96,100
        closes = [o for o in acct.get_orders() if o.comment == "assignment_liquidation"]
        assert len(closes) == 1
        assert closes[0].side == OrderDirection.BUY          # buy-to-cover
        assert closes[0].filled_qty == pytest.approx(100.0)
        assert closes[0].open_price == pytest.approx(200.0)
    finally:
        ctx.__exit__(None, None, None)
