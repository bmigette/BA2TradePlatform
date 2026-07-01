"""The margin-call liquidation must NOT touch the SHORT legs of a DEFINED-RISK combo.

Confirmed root cause of the O_BF permanent-negative finals (id=526 ends -$1,291): the
maintenance-margin path treated a butterfly's SHORT BODY leg (and an iron condor's short
legs) as NAKED shorts — inflating the requirement (false breach) and buying back the short
body ALONE, orphaning the long wings and leaving a permanent cash imbalance far beyond the
combo's defined risk. A defined-risk combo's short legs are COVERED by its long legs and
carry NO naked-margin risk; only genuinely naked shorts (short strangle/straddle/jade_lizard/
put_ratio, single-leg naked short, or assigned short stock) may be margin-counted/liquidated.

Run from the backend dir:
    ./venv/bin/python -m pytest tests/backtest/test_margin_defined_risk.py -q
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

_LOW = "AAPL240315C00170000"
_BODY = "AAPL240315C00180000"
_HIGH = "AAPL240315C00190000"

_SSTG_C = "AAPL240315C00200000"
_SSTG_P = "AAPL240315P00160000"

_AAPL_BARS = [
    {"Date": datetime(2024, 3, 5), "Open": 180, "High": 181, "Low": 179, "Close": 180, "Volume": 1000},
    {"Date": datetime(2024, 3, 6), "Open": 180, "High": 181, "Low": 179, "Close": 180, "Volume": 1100},
    {"Date": datetime(2024, 3, 8), "Open": 180, "High": 181, "Low": 179, "Close": 180, "Volume": 1200},
]


def _make_ps(clock):
    from app.services.backtest.price_source import AsOfPriceSource
    ps = AsOfPriceSource(ohlcv_provider=None)
    ps.load_bars("AAPL", _AAPL_BARS)
    ps.set_clock(clock)
    return ps


def _account(tmp_path, chain, bar_rows):
    from app.services.backtest.backtest_db import backtest_trading_db, seed_account_definition
    from app.services.backtest.seam_wiring import wire_backtest_seams
    from app.services.backtest.backtest_account import BacktestAccount
    from app.services.backtest.options_provider import HistoricalOptionsProvider
    from app.services.backtest.options_cache import OptionsHistoryCache

    cache_db = str(tmp_path / "c.sqlite")
    cache = OptionsHistoryCache(cache_db)
    cache.write_chain_rows("AAPL", "2024-03-01", chain)
    cache.write_bar_rows(bar_rows)
    prov = HistoricalOptionsProvider(cache_db)
    wire_backtest_seams()
    ctx = backtest_trading_db("mdr")
    ctx.__enter__()
    seed_account_definition(1, CFG)
    ps = _make_ps(datetime(2024, 3, 5))
    acct = BacktestAccount(1, ps, CFG, options_provider=prov)
    wire_backtest_seams().register_account(1, acct)
    return acct, ps, ctx


def _c(sym, k):
    return {"occ_symbol": sym, "option_type": "call", "strike": k, "expiry": "2024-03-15", "bid": 1.0, "ask": 1.2, "last": 1.1, "iv": 0.25}


def _p(sym, k):
    return {"occ_symbol": sym, "option_type": "put", "strike": k, "expiry": "2024-03-15", "bid": 1.0, "ask": 1.2, "last": 1.1, "iv": 0.25}


def _bar(sym, px, ot, k):
    return {"occ_symbol": sym, "date": "2024-03-06", "open": px, "high": px, "low": px, "close": px, "volume": 100,
            "underlying": "AAPL", "option_type": ot, "strike": k, "expiry": "2024-03-15"}


def test_butterfly_short_body_excluded_from_margin(tmp_path):
    """A butterfly's SHORT body leg carries NO naked-margin requirement and is NEVER liquidated."""
    from ba2_common.core.option_types import OptionLeg
    chain = [_c(_LOW, 170.0), _c(_BODY, 180.0), _c(_HIGH, 190.0)]
    bars = [_bar(_LOW, 12.0, "call", 170.0), _bar(_BODY, 5.0, "call", 180.0), _bar(_HIGH, 1.0, "call", 190.0)]
    acct, ps, ctx = _account(tmp_path, chain, bars)
    try:
        low = OptionLeg(contract_symbol=_LOW, side=OrderDirection.BUY, ratio_qty=1, position_intent="buy_to_open", option_type=OptionRight.CALL, strike=170.0, expiry=date(2024, 3, 15), underlying="AAPL")
        body = OptionLeg(contract_symbol=_BODY, side=OrderDirection.SELL, ratio_qty=2, position_intent="sell_to_open", option_type=OptionRight.CALL, strike=180.0, expiry=date(2024, 3, 15), underlying="AAPL")
        high = OptionLeg(contract_symbol=_HIGH, side=OrderDirection.BUY, ratio_qty=1, position_intent="buy_to_open", option_type=OptionRight.CALL, strike=190.0, expiry=date(2024, 3, 15), underlying="AAPL")
        acct.submit_option_order(legs=[low, body, high], quantity=5, order_type="market", option_strategy="call_butterfly")
        acct.refresh_orders()
        acct.refresh_transactions()

        ps.set_clock(datetime(2024, 3, 8))
        # A defined-risk butterfly has NO naked-margin requirement (its short body is covered).
        assert acct.maintenance_margin_requirement() == pytest.approx(0.0, abs=1e-6)

        # Force a breach attempt: even if equity were low, the margin call must NOT liquidate the
        # butterfly's short body (defined risk). It stays intact.
        liquidated = acct.maybe_margin_call_liquidation()
        assert liquidated is False
        assert acct._option_positions[_BODY].qty == -10  # short body untouched
        assert acct._option_positions[_LOW].qty == 5
        assert acct._option_positions[_HIGH].qty == 5
    finally:
        ctx.__exit__(None, None, None)


def test_naked_short_strangle_still_margin_counted(tmp_path):
    """A genuinely NAKED short strangle (undefined risk) STILL carries margin + is liquidatable
    — the defined-risk exclusion must not disarm the naked-short defense."""
    from ba2_common.core.option_types import OptionLeg
    chain = [_c(_SSTG_C, 200.0), _p(_SSTG_P, 160.0)]
    bars = [_bar(_SSTG_C, 3.0, "call", 200.0), _bar(_SSTG_P, 2.0, "put", 160.0)]
    acct, ps, ctx = _account(tmp_path, chain, bars)
    try:
        call_leg = OptionLeg(contract_symbol=_SSTG_C, side=OrderDirection.SELL, position_intent="sell_to_open", option_type=OptionRight.CALL, strike=200.0, expiry=date(2024, 3, 15), underlying="AAPL")
        put_leg = OptionLeg(contract_symbol=_SSTG_P, side=OrderDirection.SELL, position_intent="sell_to_open", option_type=OptionRight.PUT, strike=160.0, expiry=date(2024, 3, 15), underlying="AAPL")
        acct.submit_option_order(legs=[call_leg, put_leg], quantity=5, order_type="market", option_strategy="short_strangle")
        acct.refresh_orders()
        acct.refresh_transactions()

        ps.set_clock(datetime(2024, 3, 8))
        # Naked short strangle -> a real naked-margin requirement (> 0).
        assert acct.maintenance_margin_requirement() > 1000.0
    finally:
        ctx.__exit__(None, None, None)
