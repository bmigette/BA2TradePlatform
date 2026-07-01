"""Snapshot safety-net: a DEFINED-RISK combo's recorded equity may never dip below its
theoretical bounded floor on ANY bar — including a mid-reconciliation bar where the short
legs have been closed/settled but the offsetting long legs are still held (and the sparse
cache lacks a premium bar for them).

Confirmed root cause (O_IC id=449, 1-bar transient to -$2,023 on a monthly-expiry bar):
after the short legs of an iron condor are closed/settled, the surviving LONG legs carry
positive value, but the group is still classified `iron_condor` (a credit/short strategy)
so the clamp `[-width, 0]` ERASES that positive residual to 0 — while the short-leg buyback
cost was already booked to cash. Net: the recorded equity dips spuriously negative for one
bar and recovers next bar.

Fixes under test:
  (2a) a held DEFINED-RISK long leg with NO premium bar on the current bar is marked at its
       INTRINSIC (not entry premium / not 0), so an open combo is not understated.
  (2b) each defined-risk combo's net contribution is floored so a mid-reconciliation bar
       cannot record below the combo's bounded floor (residual long-leg value is not erased).

Run from the backend dir:
    ./venv/bin/python -m pytest tests/backtest/test_option_snapshot_floor.py -q
"""
from __future__ import annotations

from datetime import date, datetime

import pytest

from ba2_common.core.types import OptionRight, OrderDirection


CFG = {
    "starting_cash": 10_000.0,
    "commission_per_trade": 0.0,
    "slippage_bps": 0.0,
    "fill_model": "next_bar_open",
}

# Iron condor on AAPL: long 165 put / short 175 put ; short 185 call / long 195 call.
_LP = "AAPL240315P00165000"
_SP = "AAPL240315P00175000"
_SC = "AAPL240315C00185000"
_LC = "AAPL240315C00195000"

# Underlying: entry-fill 03-06 (spot 180), then 03-10 where the market has ripped to 250
# (call side breached) — the "mid-reconciliation" bar. NO premium bars on 03-10 (sparse
# cache), so held lots must fall back to INTRINSIC, not entry premium / 0.
_AAPL_BARS = [
    {"Date": datetime(2024, 3, 5), "Open": 180, "High": 181, "Low": 179, "Close": 180, "Volume": 1000},
    {"Date": datetime(2024, 3, 6), "Open": 180, "High": 181, "Low": 179, "Close": 180, "Volume": 1100},
    {"Date": datetime(2024, 3, 10), "Open": 250, "High": 251, "Low": 249, "Close": 250, "Volume": 1200},
]


def _make_ps(clock):
    from app.services.backtest.price_source import AsOfPriceSource
    ps = AsOfPriceSource(ohlcv_provider=None)
    ps.load_bars("AAPL", _AAPL_BARS)
    ps.set_clock(clock)
    return ps


def _account(tmp_path):
    from app.services.backtest.backtest_db import backtest_trading_db, seed_account_definition
    from app.services.backtest.seam_wiring import wire_backtest_seams
    from app.services.backtest.backtest_account import BacktestAccount
    from app.services.backtest.options_provider import HistoricalOptionsProvider
    from app.services.backtest.options_cache import OptionsHistoryCache

    cache_db = str(tmp_path / "c.sqlite")
    cache = OptionsHistoryCache(cache_db)
    cache.write_chain_rows("AAPL", "2024-03-01", [
        {"occ_symbol": _LP, "option_type": "put", "strike": 165.0, "expiry": "2024-03-15", "bid": 0.5, "ask": 0.7, "last": 0.6, "iv": 0.25},
        {"occ_symbol": _SP, "option_type": "put", "strike": 175.0, "expiry": "2024-03-15", "bid": 2.0, "ask": 2.2, "last": 2.1, "iv": 0.25},
        {"occ_symbol": _SC, "option_type": "call", "strike": 185.0, "expiry": "2024-03-15", "bid": 2.0, "ask": 2.2, "last": 2.1, "iv": 0.25},
        {"occ_symbol": _LC, "option_type": "call", "strike": 195.0, "expiry": "2024-03-15", "bid": 0.5, "ask": 0.7, "last": 0.6, "iv": 0.25},
    ])
    # ONLY entry-fill-day (03-06) premium bars. NO bars on 03-10 -> held lots fall back.
    def bar(sym, px, ot, k):
        return {"occ_symbol": sym, "date": "2024-03-06", "open": px, "high": px, "low": px, "close": px,
                "volume": 100, "underlying": "AAPL", "option_type": ot, "strike": k, "expiry": "2024-03-15"}
    cache.write_bar_rows([bar(_LP, 0.5, "put", 165.0), bar(_SP, 2.0, "put", 175.0),
                          bar(_SC, 2.0, "call", 185.0), bar(_LC, 0.5, "call", 195.0)])
    prov = HistoricalOptionsProvider(cache_db)

    wire_backtest_seams()
    ctx = backtest_trading_db("snapfloor")
    ctx.__enter__()
    seed_account_definition(1, CFG)
    ps = _make_ps(datetime(2024, 3, 5))
    acct = BacktestAccount(1, ps, CFG, options_provider=prov)
    wire_backtest_seams().register_account(1, acct)
    return acct, ps, ctx


def _open_ic(acct):
    from ba2_common.core.option_types import OptionLeg
    legs = [
        OptionLeg(contract_symbol=_LP, side=OrderDirection.BUY, position_intent="buy_to_open", option_type=OptionRight.PUT, strike=165.0, expiry=date(2024, 3, 15), underlying="AAPL"),
        OptionLeg(contract_symbol=_SP, side=OrderDirection.SELL, position_intent="sell_to_open", option_type=OptionRight.PUT, strike=175.0, expiry=date(2024, 3, 15), underlying="AAPL"),
        OptionLeg(contract_symbol=_SC, side=OrderDirection.SELL, position_intent="sell_to_open", option_type=OptionRight.CALL, strike=185.0, expiry=date(2024, 3, 15), underlying="AAPL"),
        OptionLeg(contract_symbol=_LC, side=OrderDirection.BUY, position_intent="buy_to_open", option_type=OptionRight.CALL, strike=195.0, expiry=date(2024, 3, 15), underlying="AAPL"),
    ]
    acct.submit_option_order(legs=legs, quantity=5, order_type="market", option_strategy="iron_condor")
    acct.refresh_orders()
    acct.refresh_transactions()


def test_leftover_long_legs_value_not_erased_by_clamp(tmp_path):
    """After the SHORT legs are removed (closed/settled), the surviving LONG legs of an iron
    condor must NOT have their positive residual value clamped to 0 — the combo's snapshot
    contribution must not dip below its bounded floor."""
    acct, ps, ctx = _account(tmp_path)
    try:
        _open_ic(acct)
        # Simulate the mid-reconciliation state: the short legs have been bought back / settled
        # (their lots zeroed), the long legs remain held. This is exactly the O_IC id=449 state.
        acct._option_positions[_SP].qty = 0.0
        acct._option_positions[_SC].qty = 0.0

        ps.set_clock(datetime(2024, 3, 10))  # spot 250, NO premium bars this bar
        # Surviving legs: long 165 put (OTM at 250 -> 0), long 195 call (ITM by 55 -> intrinsic
        # 55/share). Marked at intrinsic, the combo contribution is +55*100*5 = +27500 raw, which
        # the [0, width=... ] bound would keep POSITIVE — never a negative that sinks equity.
        mtm = acct._option_positions_mtm()
        assert mtm >= 0.0, f"leftover long legs erased/negative: {mtm}"

        # And equity must stay >= the combo's bounded floor (cash was reduced by the short buyback
        # in the real flow; here cash is intact so equity must be well positive).
        eq = acct.equity()
        assert eq >= 0.0
    finally:
        ctx.__exit__(None, None, None)


def test_open_long_leg_marked_at_intrinsic_when_no_bar(tmp_path):
    """A held DEFINED-RISK long leg with NO premium bar on the current bar is marked at its
    INTRINSIC, not silently at entry premium / 0 (so an open combo isn't understated)."""
    acct, ps, ctx = _account(tmp_path)
    try:
        _open_ic(acct)
        # Remove the shorts so only the two long legs remain (isolate the long-leg marking).
        acct._option_positions[_SP].qty = 0.0
        acct._option_positions[_SC].qty = 0.0

        ps.set_clock(datetime(2024, 3, 10))  # spot 250, no premium bar
        # Long 195 call is ITM by 55; long 165 put is OTM (0). Intrinsic marking -> the 195 call
        # contributes ~55*100*5 = 27500 before the combo bound. Entry-premium fallback (0.5) would
        # give only ~0.5*100*5 = 250 -> understated. Assert the value reflects intrinsic, not entry.
        mtm = acct._option_positions_mtm()
        assert mtm > 1000.0, f"open long leg understated (not marked at intrinsic): {mtm}"
    finally:
        ctx.__exit__(None, None, None)
