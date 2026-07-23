"""Regression tests for the option-fill VOLUME PARTICIPATION cap (BacktestAccount).

An option order may only fill on a bar whose traded volume can absorb it: the required
contracts (single-leg: the order quantity; multi-leg child: parent structures x leg
ratio_qty, checked per leg against ITS bar) must be at most
``_OPTION_FILL_MAX_VOLUME_PARTICIPATION`` (10%) of the bar's volume. Before this guard a
backtest filled option orders of ANY size at a bar's price regardless of liquidity (e.g.
2,100 contracts of a CVX call filled at a bar whose total volume was 1 contract — a real
order that size would move the market or not fill at all).

  * an entry over the cap does NOT fill (no position/cash change,
    ``account.rejected_illiquid_fills`` increments, a warning is logged) and retries the
    next bar — a later sufficiently-liquid bar fills it;
  * an entry within the cap fills normally (the guard is invisible);
  * an EXIT over the cap stays open, retries, and closes on a higher-volume bar (expiry
    settlement remains the ultimate backstop);
  * a multi-leg combo with ONE illiquid leg fills NOTHING (all-or-none);
  * a bar with missing/None or 0 volume is treated as 0 — nothing fills on it.

Run from the backend dir:
    ./venv/bin/python -m pytest tests/backtest/test_option_volume_participation.py -q
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

_CALL105 = "AAPL240315C00105000"   # 105 call, expiry 2024-03-15 (entry/exit shape)
_CALL110 = "AAPL240315C00110000"   # 110 call, expiry 2024-03-15 (multi-leg short leg)

# Underlying bars. Fill bar 2024-03-06 opens at 160 (105-call intrinsic 55, 110-call 50);
# 2024-03-07 opens 161 (intrinsic 56), 2024-03-08 opens 163 — all premiums used below are
# arb-consistent with those opens so ONLY the volume cap can reject.
_AAPL_BARS = [
    {"Date": datetime(2024, 3, 5), "Open": 150, "High": 152, "Low": 149, "Close": 151, "Volume": 1000},
    {"Date": datetime(2024, 3, 6), "Open": 160, "High": 162, "Low": 159, "Close": 161, "Volume": 1100},
    {"Date": datetime(2024, 3, 7), "Open": 161, "High": 163, "Low": 160, "Close": 162, "Volume": 1200},
    {"Date": datetime(2024, 3, 8), "Open": 163, "High": 165, "Low": 162, "Close": 164, "Volume": 1300},
]

_EXP_0315 = date(2024, 3, 15)


def _chain_row(occ, ot, strike, expiry):
    return {"occ_symbol": occ, "option_type": ot, "strike": strike,
            "expiry": expiry.isoformat(), "bid": 1.0, "ask": 1.2, "last": 1.1, "iv": 0.25}


def _bar_row(occ, d, o, ot, strike, expiry, v=100, c=None):
    row = {"occ_symbol": occ, "date": d, "open": o, "high": o, "low": o,
           "close": c if c is not None else o, "underlying": "AAPL",
           "option_type": ot, "strike": strike, "expiry": expiry.isoformat()}
    if v is not None:  # v=None writes NO volume key -> the cache stores NULL volume
        row["volume"] = v
    return row


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


def _leg(occ, side, intent, ot, strike, expiry):
    from ba2_common.core.option_types import OptionLeg

    return OptionLeg(contract_symbol=occ, side=side, position_intent=intent,
                     option_type=ot, strike=strike, expiry=expiry, underlying="AAPL")


def _submit_call(acct, occ=_CALL105, side=OrderDirection.BUY, intent="buy_to_open",
                 strike=105.0, qty=1, strategy="long_call"):
    return acct.submit_option_order(
        legs=[_leg(occ, side, intent, OptionRight.CALL, strike, _EXP_0315)],
        quantity=qty, order_type="market", option_strategy=strategy)


def _add_bars(tmp_path, name, rows):
    """Append premium bars to the seeded cache and drop the memoized bar history."""
    from app.services.backtest.options_cache import OptionsHistoryCache
    from app.services.backtest.options_provider import clear_worker_options_cache

    cache_db = str(tmp_path / f"{name}_options_cache.sqlite")
    OptionsHistoryCache(cache_db).write_bar_rows(rows)
    clear_worker_options_cache()


# ---------------------------------------------------------------------------
# ENTRY over the cap: no fill this bar; a later liquid bar fills
# ---------------------------------------------------------------------------
def test_illiquid_entry_rejected_then_fills_on_liquid_bar(tmp_path, caplog):
    """1 contract against a volume-5 bar (cap 0.5) must NOT fill — no position, no cash
    change, counter incremented, warning logged. The order stays pending and fills on the
    next bar's sufficiently-liquid volume."""
    acct, ps, ctx = _build(
        tmp_path, "volentry",
        [_chain_row(_CALL105, "call", 105.0, _EXP_0315)],
        [_bar_row(_CALL105, "2024-03-06", 56.0, "call", 105.0, _EXP_0315, v=5)],
    )
    try:
        order = _submit_call(acct)
        cash_before = acct._cash
        assert acct.rejected_illiquid_fills == 0

        with caplog.at_level(logging.WARNING, logger="app.services.backtest.backtest_account"):
            acct.refresh_orders()
            acct.refresh_transactions()

        # REJECTED: the order did not fill, nothing moved, the rejection was counted + logged.
        assert acct.get_order(order.id).status == OrderStatus.ACCEPTED
        assert acct.get_option_positions() == []
        assert acct._cash == pytest.approx(cash_before)
        assert acct.rejected_illiquid_fills == 1
        assert any("illiquid" in r.message and _CALL105 in r.message
                   for r in caplog.records)

        # A LIQUID bar on the next fill day (volume 100 -> cap 10 contracts) fills normally.
        _add_bars(tmp_path, "volentry",
                  [_bar_row(_CALL105, "2024-03-07", 56.5, "call", 105.0, _EXP_0315, v=100)])
        ps.set_clock(datetime(2024, 3, 6))
        acct.refresh_orders()
        acct.refresh_transactions()

        filled = acct.get_order(order.id)
        assert filled.status == OrderStatus.FILLED
        assert filled.open_price == pytest.approx(56.5)
        assert acct._cash == pytest.approx(cash_before - 56.5 * 100.0)
        assert acct.rejected_illiquid_fills == 1   # unchanged by the liquid fill
    finally:
        ctx.__exit__(None, None, None)


def test_entry_within_cap_fills_normally(tmp_path):
    """1 contract against a volume-100 bar (cap 10) fills as before (guard is invisible)."""
    acct, ps, ctx = _build(
        tmp_path, "volnormal",
        [_chain_row(_CALL105, "call", 105.0, _EXP_0315)],
        [_bar_row(_CALL105, "2024-03-06", 56.0, "call", 105.0, _EXP_0315, v=100)],
    )
    try:
        order = _submit_call(acct)
        acct.refresh_orders()
        acct.refresh_transactions()

        assert acct.get_order(order.id).status == OrderStatus.FILLED
        assert len(acct.get_option_positions()) == 1
        assert acct.rejected_illiquid_fills == 0
    finally:
        ctx.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# EXIT over the cap: position stays open, retries, closes on a liquid bar
# ---------------------------------------------------------------------------
def test_illiquid_exit_stays_open_then_closes_on_higher_volume_bar(tmp_path):
    """A sell-to-close whose size exceeds the fill bar's cap does NOT fill and the
    position stays open; the next sufficiently-liquid bar closes it. Expiry settlement
    remains the ultimate backstop."""
    acct, ps, ctx = _build(
        tmp_path, "volexit",
        [_chain_row(_CALL105, "call", 105.0, _EXP_0315)],
        [_bar_row(_CALL105, "2024-03-06", 56.0, "call", 105.0, _EXP_0315, v=100),  # entry
         _bar_row(_CALL105, "2024-03-07", 58.0, "call", 105.0, _EXP_0315, v=5),    # thin exit
         _bar_row(_CALL105, "2024-03-08", 58.5, "call", 105.0, _EXP_0315, v=100)], # liquid exit
    )
    try:
        _submit_call(acct)
        acct.refresh_orders()
        acct.refresh_transactions()
        assert len(acct.get_option_positions()) == 1
        cash_after_entry = acct._cash                     # 100,000 - 5,600 = 94,400

        # Close on the THIN bar: 1 contract > cap 0.5 -> rejected, position still held.
        pos = acct.get_option_positions()[0]
        close_order = acct.close_option_position(pos, order_type="market")
        ps.set_clock(datetime(2024, 3, 6))
        acct.refresh_orders()
        acct.refresh_transactions()
        assert acct.get_order(close_order.id).status == OrderStatus.ACCEPTED
        assert len(acct.get_option_positions()) == 1      # still held
        assert acct._cash == pytest.approx(cash_after_entry)
        assert acct.rejected_illiquid_fills == 1

        # The next bar's LIQUID volume (cap 10) closes the position.
        ps.set_clock(datetime(2024, 3, 7))
        acct.refresh_orders()
        acct.refresh_transactions()
        assert acct.get_order(close_order.id).status == OrderStatus.FILLED
        assert acct.get_option_positions() == []
        assert acct._cash == pytest.approx(cash_after_entry + 58.5 * 100.0)
        assert acct.rejected_illiquid_fills == 1
    finally:
        ctx.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# Multi-leg combo: one illiquid leg blocks the whole all-or-none fill
# ---------------------------------------------------------------------------
def test_multi_leg_combo_with_one_illiquid_leg_does_not_fill(tmp_path):
    """Bull-call-spread entry: the long 105c fits its bar (1 <= 10) but the short 110c's
    volume-5 bar (cap 0.5) cannot absorb 1 contract -> the WHOLE combo stays unfilled."""
    acct, ps, ctx = _build(
        tmp_path, "volcombo",
        [_chain_row(_CALL105, "call", 105.0, _EXP_0315),
         _chain_row(_CALL110, "call", 110.0, _EXP_0315)],
        [_bar_row(_CALL105, "2024-03-06", 56.0, "call", 105.0, _EXP_0315, v=100),
         _bar_row(_CALL110, "2024-03-06", 50.0, "call", 110.0, _EXP_0315, v=5)],
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
        assert acct.rejected_illiquid_fills == 1   # exactly the one illiquid leg
        children = [o for o in acct.get_orders() if o.parent_order_id == parent.id]
        assert len(children) == 2
        assert all(c.status == OrderStatus.ACCEPTED for c in children)
    finally:
        ctx.__exit__(None, None, None)


def test_multi_leg_combo_all_legs_liquid_fills(tmp_path):
    """Both legs within their bars' caps -> the combo fills normally (guard is invisible)."""
    acct, ps, ctx = _build(
        tmp_path, "volcombo2",
        [_chain_row(_CALL105, "call", 105.0, _EXP_0315),
         _chain_row(_CALL110, "call", 110.0, _EXP_0315)],
        [_bar_row(_CALL105, "2024-03-06", 56.0, "call", 105.0, _EXP_0315, v=100),
         _bar_row(_CALL110, "2024-03-06", 50.0, "call", 110.0, _EXP_0315, v=100)],
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

        assert acct.get_order(parent.id).status == OrderStatus.FILLED
        assert len(acct.get_option_positions()) == 2      # one lot per leg
        # Net debit 56.0 - 50.0 = 6.0/share -> $600 moved per structure.
        assert acct._cash == pytest.approx(cash_before - 6.0 * 100.0)
        assert acct.rejected_illiquid_fills == 0
    finally:
        ctx.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# Missing/None or zero bar volume is treated as 0 — nothing fills on that bar
# ---------------------------------------------------------------------------
def test_missing_or_zero_volume_bar_never_fills(tmp_path):
    """A bar with NO volume key (NULL in the cache) rejects; a volume-0 bar rejects too —
    the order keeps retrying instead of filling against unobserved liquidity."""
    acct, ps, ctx = _build(
        tmp_path, "volnone",
        [_chain_row(_CALL105, "call", 105.0, _EXP_0315)],
        [_bar_row(_CALL105, "2024-03-06", 56.0, "call", 105.0, _EXP_0315, v=None),
         _bar_row(_CALL105, "2024-03-07", 56.5, "call", 105.0, _EXP_0315, v=0)],
    )
    try:
        order = _submit_call(acct)
        cash_before = acct._cash

        # NULL-volume bar (2024-03-06): treated as 0 -> no fill.
        acct.refresh_orders()
        acct.refresh_transactions()
        assert acct.get_order(order.id).status == OrderStatus.ACCEPTED
        assert acct.get_option_positions() == []
        assert acct._cash == pytest.approx(cash_before)
        assert acct.rejected_illiquid_fills == 1

        # Zero-volume bar (2024-03-07): same — still pending.
        ps.set_clock(datetime(2024, 3, 6))
        acct.refresh_orders()
        acct.refresh_transactions()
        assert acct.get_order(order.id).status == OrderStatus.ACCEPTED
        assert acct.get_option_positions() == []
        assert acct._cash == pytest.approx(cash_before)
        assert acct.rejected_illiquid_fills == 2
    finally:
        ctx.__exit__(None, None, None)
