"""Review 2026-08-30 F7 (engine side) — exits pay the modelled spread instead of filtering.

The backtest's synthetic quotes are ``bid == ask == close`` (the MID), and the fill
engine crosses the modelled spread FIRST and re-tests the limit (``_option_cross``): a
close quoted at the mid therefore only filled after the fill-day mid drifted half a
spread in the position's favor. ``CloseOptionAction`` now concedes on the account's
``option_modelled_half_spread`` seam (unit-pinned in
``packages/common/tests/test_option_close_concession.py``); these tests pin the ENGINE
glue: the real ``BacktestAccount`` spread model feeds the concession, and the conceded
limit actually FILLS on its fill bar with flat premiums — paying the spread as a cost.

Also here: the margin-liquidation buyback (``_liquidate_option_lot``) is a FORCED close
and now crosses the modelled spread too (buy side pays ``close + half``), still clamped
into the F1 no-arb bounds.

Run from the backend dir:
    ./venv/bin/python -m pytest tests/backtest/test_option_close_concession_fills.py -q
"""
from __future__ import annotations

from datetime import date, datetime
from types import SimpleNamespace

import pytest

from ba2_common.core.TradeActions import CloseOptionAction
from ba2_common.core.option_types import OptionPosition
from ba2_common.core.types import OptionRight, OrderDirection, OrderStatus


CFG = {
    "starting_cash": 10_000.0,
    "commission_per_trade": 0.0,
    "slippage_bps": 0.0,
    "fill_model": "next_bar_open",
    "option_spread_pct": 5.0,       # half-spread = premium * 2.5% on a liquid bar
    "option_spread_min_tick": 0.0,
}

_PUT_140 = "AAPL240315P00140000"
_CALL_500 = "AMD240315C00500000"


def _make_ps(symbol, bars, clock):
    from app.services.backtest.price_source import AsOfPriceSource

    ps = AsOfPriceSource(ohlcv_provider=None)
    ps.load_bars(symbol, bars)
    ps.set_clock(clock)
    return ps


def _account(tmp_path, tag, ps, chain_underlying, chain, bar_rows, cfg=CFG):
    from app.services.backtest.backtest_db import backtest_trading_db, seed_account_definition
    from app.services.backtest.seam_wiring import wire_backtest_seams
    from app.services.backtest.backtest_account import BacktestAccount
    from app.services.backtest.options_provider import HistoricalOptionsProvider
    from app.services.backtest.options_cache import OptionsHistoryCache

    cache_db = str(tmp_path / "c.sqlite")
    cache = OptionsHistoryCache(cache_db)
    if chain:
        cache.write_chain_rows(chain_underlying, "2024-03-01", chain)
    if bar_rows:
        cache.write_bar_rows(bar_rows)
    prov = HistoricalOptionsProvider(cache_db)
    wire_backtest_seams()
    ctx = backtest_trading_db(tag)
    ctx.__enter__()
    seed_account_definition(1, cfg)
    acct = BacktestAccount(1, ps, cfg, options_provider=prov)
    wire_backtest_seams().register_account(1, acct)
    return acct, ctx


def _bar(sym, d, close, ot, k, underlying="AAPL", v=100_000, open_=None):
    o = close if open_ is None else open_
    return {"occ_symbol": sym, "date": d, "open": o, "high": max(o, close),
            "low": min(o, close), "close": close, "volume": v,
            "underlying": underlying, "option_type": ot, "strike": k,
            "expiry": "2024-03-15"}


def _ubar(d, px):
    return {"Date": d, "Open": px, "High": px + 1, "Low": px - 1, "Close": px, "Volume": 100}


def _leg(sym, side, ot, k, underlying="AAPL"):
    from ba2_common.core.option_types import OptionLeg

    intent = "buy_to_open" if side == OrderDirection.BUY else "sell_to_open"
    return OptionLeg(contract_symbol=sym, side=side, ratio_qty=1, position_intent=intent,
                     option_type=ot, strike=k, expiry=date(2024, 3, 15),
                     underlying=underlying)


def _short_put_account(tmp_path, tag, drift_open=None):
    """Short 1 AAPL 140 put on a FLAT tape: spot pinned at 150, premium pinned at 4.0
    (bid == ask == 4.0 in the synthetic quote; modelled half-spread = 4.0 x 2.5% = 0.10).
    Entry SELL_LIMIT 3.8, placed 2024-03-05, fills off the 03-06 bar at the crossed 3.90
    (``next_bar_open``). ``drift_open`` overrides the 03-07 bar's OPEN (the close order's
    fill reference) — None keeps the tape flat."""
    bars = [_ubar(datetime(2024, 3, 5), 150), _ubar(datetime(2024, 3, 6), 150),
            _ubar(datetime(2024, 3, 7), 150)]
    ps = _make_ps("AAPL", bars, datetime(2024, 3, 5))
    chain = [{"occ_symbol": _PUT_140, "option_type": "put", "strike": 140.0,
              "expiry": "2024-03-15", "bid": 4.0, "ask": 4.0, "last": 4.0, "iv": 0.25}]
    bar_rows = [
        _bar(_PUT_140, "2024-03-05", 4.0, "put", 140.0),
        _bar(_PUT_140, "2024-03-06", 4.0, "put", 140.0),
        _bar(_PUT_140, "2024-03-07", 4.0, "put", 140.0, open_=drift_open),
    ]
    acct, ctx = _account(tmp_path, tag, ps, "AAPL", chain, bar_rows)
    acct.submit_option_order(
        legs=[_leg(_PUT_140, OrderDirection.SELL, OptionRight.PUT, 140.0)],
        quantity=1, order_type="limit", limit_price=3.8, option_strategy="naked_put")
    acct.refresh_orders()
    acct.refresh_transactions()
    assert len(acct.get_option_positions()) == 1
    assert acct._cash == pytest.approx(10_000.0 + 390.0, abs=0.01)  # sold at 4.0 - 0.10
    ps.set_clock(datetime(2024, 3, 6))
    return acct, ps, ctx


def _close_action(acct, *, forced):
    action = CloseOptionAction.__new__(CloseOptionAction)
    action.instrument_name = "AAPL"
    action.account = acct
    action.forced_exit = forced
    return action


def _position():
    return OptionPosition(
        contract_symbol=_PUT_140, underlying="AAPL", option_type=OptionRight.PUT,
        strike=140.0, expiry=date(2024, 3, 15), side=OrderDirection.SELL,
        quantity=1.0, avg_entry_price=3.9)


def test_forced_close_fills_on_its_fill_bar_paying_the_modelled_spread(tmp_path):
    """FLAT premiums: the fill-day mid never moves, so the OLD mid-quoted close could
    never fill. A forced close's limit crosses the modelled spread fully (4.0 + 0.10)
    and fills on its fill bar, paying the spread as a cost: cash 10,390 -> 9,980."""
    acct, ps, ctx = _short_put_account(tmp_path, "f7forced")
    try:
        entry = SimpleNamespace(open_price=3.9, limit_price=3.8, data={},
                                parent_order_id=None)
        limit = _close_action(acct, forced=True)._close_limit_price(_position(), entry)
        assert limit == pytest.approx(4.0 + 0.10)   # mid + FULL modelled half-spread
        acct.close_option_position(_position(), order_type="limit", limit_price=limit)
        acct.refresh_orders()            # fills off the 03-07 open (4.0): 4.10 <= 4.10
        acct.refresh_transactions()
        assert acct.get_option_positions() == []
        assert acct._cash == pytest.approx(10_390.0 - 410.0, abs=0.01)
    finally:
        ctx.__exit__(None, None, None)


def test_discretionary_close_concedes_the_entry_fraction_and_fills_on_half_the_drift(tmp_path):
    """A TP close whose entry conceded 0.5 quotes 4.0 + 0.5 x 0.10 = 4.05. The fill-day
    open drifts to 3.94: the crossed price 3.94 + 0.0985 = 4.0385 clears 4.05 and FILLS —
    while the OLD mid quote (4.00) would still have filtered it out (4.0385 > 4.00).
    Paying the conceded half-spread is exactly what turns the exit from a filter into a
    cost."""
    acct, ps, ctx = _short_put_account(tmp_path, "f7tp", drift_open=3.94)
    try:
        entry = SimpleNamespace(open_price=3.9, limit_price=3.8,
                                data={"entry_cross": 0.5}, parent_order_id=None)
        limit = _close_action(acct, forced=False)._close_limit_price(_position(), entry)
        assert limit == pytest.approx(4.05)
        crossed_fill = 3.94 + 3.94 * 0.05 / 2.0     # what the fill engine charges
        assert crossed_fill > 4.0                    # the mid quote would NOT fill...
        assert crossed_fill <= limit                 # ...the conceded quote does
        acct.close_option_position(_position(), order_type="limit", limit_price=limit)
        acct.refresh_orders()
        acct.refresh_transactions()
        assert acct.get_option_positions() == []
    finally:
        ctx.__exit__(None, None, None)


def test_mid_quoted_close_still_filters_on_a_flat_tape(tmp_path):
    """The CONTROL, i.e. the pre-F7 behaviour an entry that conceded nothing still gets:
    a mid-quoted close (limit 4.0) cannot clear the crossed 4.10 on a flat tape, and the
    DAY sweep expires it the next bar — the exit is a filter that re-quotes tomorrow.
    This is what F7 removed for concession entries and forced exits."""
    acct, ps, ctx = _short_put_account(tmp_path, "f7mid")
    try:
        entry = SimpleNamespace(open_price=3.9, limit_price=3.8, data={},
                                parent_order_id=None)
        limit = _close_action(acct, forced=False)._close_limit_price(_position(), entry)
        assert limit == pytest.approx(4.0)          # no concession persisted -> the mid
        acct.close_option_position(_position(), order_type="limit", limit_price=limit)
        acct.refresh_orders()                        # 4.10 > 4.00: stays pending
        assert len(acct.get_option_positions()) == 1
        ps.set_clock(datetime(2024, 3, 7))
        acct.refresh_orders()                        # DAY sweep kills it
        assert len(acct.get_option_positions()) == 1
        assert any(o.status == OrderStatus.EXPIRED for o in acct.get_orders()
                   if getattr(o, "option_strategy", None) == "close"
                   or getattr(o, "position_intent", None) == "buy_to_close")
    finally:
        ctx.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# margin-liquidation buyback: a FORCED close pays the spread too
# ---------------------------------------------------------------------------
def test_margin_liquidation_buyback_crosses_the_modelled_spread(tmp_path):
    """Short 3 AMD 500 calls; AMD gaps to 520 with a sane 22.0 premium print. The forced
    buyback pays the crossed 22.0 + 0.55 (half-spread at 5%), inside the F1 bounds
    [20, 520] — the blow-up costs the spread on top of the print, exactly like every
    other forced exit."""
    bars = [_ubar(datetime(2024, 3, 5), 450), _ubar(datetime(2024, 3, 6), 450),
            _ubar(datetime(2024, 3, 10), 520)]
    ps = _make_ps("AMD", bars, datetime(2024, 3, 5))
    chain = [{"occ_symbol": _CALL_500, "option_type": "call", "strike": 500.0,
              "expiry": "2024-03-15", "bid": 3.0, "ask": 3.0, "last": 3.0, "iv": 0.25}]
    bar_rows = [
        _bar(_CALL_500, "2024-03-06", 3.0, "call", 500.0, underlying="AMD"),
        _bar(_CALL_500, "2024-03-10", 22.0, "call", 500.0, underlying="AMD"),
    ]
    acct, ctx = _account(tmp_path, "f7margin", ps, "AMD", chain, bar_rows)
    try:
        acct.submit_option_order(
            legs=[_leg(_CALL_500, OrderDirection.SELL, OptionRight.CALL, 500.0,
                       underlying="AMD")],
            quantity=3, order_type="limit", limit_price=2.9, option_strategy="naked_call")
        acct.refresh_orders()
        acct.refresh_transactions()
        # Sold at the crossed 3.0 - 0.075 = 2.925 -> +877.50.
        cash_after_entry = acct._cash
        assert cash_after_entry == pytest.approx(10_877.5, abs=0.01)

        ps.set_clock(datetime(2024, 3, 10))
        assert acct.maybe_margin_call_liquidation() is True
        assert acct.get_option_positions() == []
        # Buyback at 22.0 + half(22.0 x 5% / 2 = 0.55) = 22.55 x 3 x 100.
        assert acct._cash == pytest.approx(cash_after_entry - 3 * 22.55 * 100.0, abs=0.01)
    finally:
        ctx.__exit__(None, None, None)
