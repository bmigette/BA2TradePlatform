"""Controlled tests for DEFINED-RISK multi-leg mid-life mark-to-market clamping.

Reproduces the O_BF (call butterfly) blow-up (Backtest id=383): the sparse/noisy
options cache emits an OUTLIER premium bar for one leg, and because
_option_positions_mtm marks each leg independently at its own per-contract premium
(qty x premium x 100), a single bad print x many contracts produced ±$100k equity
swings and a -473% recorded max_drawdown — even though the REALIZED result was fine.

A defined-risk combo can only ever be worth between 0 and its max spread width (long)
or between -(width) and 0 (short credit combo). We clamp the GROUP's net MTM
contribution to that theoretical no-arbitrage range so mid-life equity/drawdown can't
swing outside what the structure can actually be worth. Realized P&L is unchanged.

Run from the backend dir:
    ./venv/bin/python -m pytest tests/backtest/test_option_mtm_clamp.py -q
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

# Call butterfly on AAPL: long 170, short 2x 180 (body), long 190. Wing width = 10.
_BF_LOW = "AAPL240315C00170000"
_BF_BODY = "AAPL240315C00180000"
_BF_HIGH = "AAPL240315C00190000"

# Iron condor on AAPL: short 175 put / long 165 put ; short 185 call / long 195 call.
# Widest wing = 10.
_IC_LP = "AAPL240315P00165000"
_IC_SP = "AAPL240315P00175000"
_IC_SC = "AAPL240315C00185000"
_IC_LC = "AAPL240315C00195000"

# Underlying: entry-fill bar 2024-03-06 (spot ~180), a MID-LIFE bar 2024-03-08 (the
# outlier bar), and the expiry bar 2024-03-15 (spot 180 -> butterfly max value at body).
_AAPL_BARS = [
    {"Date": datetime(2024, 3, 5), "Open": 180, "High": 181, "Low": 179, "Close": 180, "Volume": 1000},
    {"Date": datetime(2024, 3, 6), "Open": 180, "High": 181, "Low": 179, "Close": 180, "Volume": 1100},
    {"Date": datetime(2024, 3, 8), "Open": 180, "High": 181, "Low": 179, "Close": 180, "Volume": 1200},
    {"Date": datetime(2024, 3, 15), "Open": 180, "High": 181, "Low": 179, "Close": 180, "Volume": 1300},
]


def _make_price_source(clock: datetime):
    from app.services.backtest.price_source import AsOfPriceSource

    ps = AsOfPriceSource(ohlcv_provider=None)
    ps.load_bars("AAPL", _AAPL_BARS)
    ps.set_clock(clock)
    return ps


def _base_account(tmp_path, chain_rows, bar_rows):
    from app.services.backtest.backtest_db import backtest_trading_db, seed_account_definition
    from app.services.backtest.seam_wiring import wire_backtest_seams
    from app.services.backtest.backtest_account import BacktestAccount
    from app.services.backtest.options_provider import HistoricalOptionsProvider
    from app.services.backtest.options_cache import OptionsHistoryCache

    cache_db = str(tmp_path / "options_cache.sqlite")
    cache = OptionsHistoryCache(cache_db)
    cache.write_chain_rows("AAPL", "2024-03-01", chain_rows)
    cache.write_bar_rows(bar_rows)
    provider = HistoricalOptionsProvider(cache_db)

    wire_backtest_seams()
    ctx = backtest_trading_db("mtmclamp")
    ctx.__enter__()
    seed_account_definition(1, CFG)
    ps = _make_price_source(datetime(2024, 3, 5))
    acct = BacktestAccount(1, ps, CFG, options_provider=provider)
    wire_backtest_seams().register_account(1, acct)
    return acct, ps, ctx


# ---------------------------------------------------------------------------
# Call butterfly (LONG / debit defined-risk): MTM must stay in [0, width*100*qty]
# ---------------------------------------------------------------------------
@pytest.fixture
def acct_call_butterfly(tmp_path):
    from ba2_common.core.option_types import OptionLeg

    def _c(sym, strike, entry_open, mid_close):
        return {"occ_symbol": sym, "option_type": "call", "strike": strike,
                "expiry": "2024-03-15", "bid": 1.0, "ask": 1.2, "last": 1.1, "iv": 0.25}

    chain = [
        _c(_BF_LOW, 170.0, 12.0, 12.0),
        _c(_BF_BODY, 180.0, 5.0, 5.0),
        _c(_BF_HIGH, 190.0, 1.0, 1.0),
    ]
    bars = [
        # entry-fill day 2024-03-06 (real premiums)
        {"occ_symbol": _BF_LOW, "date": "2024-03-06", "open": 12.0, "high": 12.5, "low": 11.5,
         "close": 12.0, "volume": 100, "underlying": "AAPL", "option_type": "call",
         "strike": 170.0, "expiry": "2024-03-15"},
        {"occ_symbol": _BF_BODY, "date": "2024-03-06", "open": 5.0, "high": 5.5, "low": 4.5,
         "close": 5.0, "volume": 100, "underlying": "AAPL", "option_type": "call",
         "strike": 180.0, "expiry": "2024-03-15"},
        {"occ_symbol": _BF_HIGH, "date": "2024-03-06", "open": 1.0, "high": 1.2, "low": 0.8,
         "close": 1.0, "volume": 100, "underlying": "AAPL", "option_type": "call",
         "strike": 190.0, "expiry": "2024-03-15"},
        # MID-LIFE 2024-03-08: BODY leg has an ABSURD OUTLIER close ($500 on a ~$5 option).
        {"occ_symbol": _BF_LOW, "date": "2024-03-08", "open": 12.0, "high": 12.0, "low": 12.0,
         "close": 12.0, "volume": 100, "underlying": "AAPL", "option_type": "call",
         "strike": 170.0, "expiry": "2024-03-15"},
        {"occ_symbol": _BF_BODY, "date": "2024-03-08", "open": 5.0, "high": 5.0, "low": 5.0,
         "close": 500.0, "volume": 100, "underlying": "AAPL", "option_type": "call",
         "strike": 180.0, "expiry": "2024-03-15"},
        {"occ_symbol": _BF_HIGH, "date": "2024-03-08", "open": 1.0, "high": 1.0, "low": 1.0,
         "close": 1.0, "volume": 100, "underlying": "AAPL", "option_type": "call",
         "strike": 190.0, "expiry": "2024-03-15"},
    ]
    acct, ps, ctx = _base_account(tmp_path, chain, bars)

    low = OptionLeg(contract_symbol=_BF_LOW, side=OrderDirection.BUY, position_intent="buy_to_open",
                    option_type=OptionRight.CALL, strike=170.0, expiry=date(2024, 3, 15), underlying="AAPL")
    body = OptionLeg(contract_symbol=_BF_BODY, side=OrderDirection.SELL, position_intent="sell_to_open",
                     option_type=OptionRight.CALL, strike=180.0, expiry=date(2024, 3, 15),
                     underlying="AAPL", ratio_qty=2)
    high = OptionLeg(contract_symbol=_BF_HIGH, side=OrderDirection.BUY, position_intent="buy_to_open",
                     option_type=OptionRight.CALL, strike=190.0, expiry=date(2024, 3, 15), underlying="AAPL")
    acct.submit_option_order(legs=[low, body, high], quantity=1, order_type="market",
                             option_strategy="call_butterfly")
    acct.refresh_orders()
    acct.refresh_transactions()
    try:
        yield acct, ps
    finally:
        ctx.__exit__(None, None, None)


def test_butterfly_outlier_mtm_clamped(acct_call_butterfly):
    """The mid-life MTM of a call butterfly must stay within [0, width*100*qty] despite an
    absurd single-leg outlier premium — equity must NOT swing to a huge value."""
    acct, ps = acct_call_butterfly
    ps.set_clock(datetime(2024, 3, 8))  # the outlier bar

    mtm = acct._option_positions_mtm()
    width = 10.0  # 170/180/190 -> wing width 10
    # Long defined-risk combo: theoretical value in [0, width*100*qty].
    assert 0.0 <= mtm <= width * 100.0 * 1.0 + 1e-6

    # And equity must be bounded near starting cash minus the debit — NOT ±$100k.
    eq = acct.equity()
    assert 0.0 < eq < CFG["starting_cash"] + width * 100.0 + 1e-6


def test_butterfly_realized_pnl_unchanged_by_clamp(acct_call_butterfly):
    """The clamp is a MARK-TO-MARKET display bound only: realized cash P&L at expiry is
    unchanged (the butterfly at spot==body-strike is worth the full wing width)."""
    from app.services.backtest.daily_engine import DailyBacktestEngine

    acct, ps = acct_call_butterfly
    engine = DailyBacktestEngine.__new__(DailyBacktestEngine)
    engine.account = acct
    engine.price = ps
    engine.config = CFG

    # Entry debit: buy 170 @12 + buy 190 @1 - 2x sell 180 @5 = 13 - 10 = 3.0 net debit
    # -> cash -= 3.0*100 = -300. Starting 10000 -> 9700 after entry.
    cash_after_entry = acct._cash
    assert cash_after_entry == pytest.approx(9700.0, abs=1.0)

    ps.set_clock(datetime(2024, 3, 15))
    engine._apply_option_expiry(datetime(2024, 3, 15))

    # At expiry spot 180: 170 call intrinsic 10, 180 calls intrinsic 0, 190 call 0.
    # Long 170 assigned/settled at intrinsic; the structure realises ~wing width (10) minus the
    # 3.0 debit = +7.0/share -> +700. Final equity ~ 10000 + 700 = ~10700, bounded and positive.
    eq = acct.equity()
    assert eq == pytest.approx(10_700.0, abs=200.0)
    assert eq > CFG["starting_cash"]  # a WINNING butterfly, no phantom loss


# ---------------------------------------------------------------------------
# Iron condor (SHORT / credit defined-risk): MTM must stay in [-width*100*qty, 0]
# ---------------------------------------------------------------------------
@pytest.fixture
def acct_iron_condor(tmp_path):
    from ba2_common.core.option_types import OptionLeg

    chain = [
        {"occ_symbol": _IC_LP, "option_type": "put", "strike": 165.0, "expiry": "2024-03-15",
         "bid": 0.5, "ask": 0.7, "last": 0.6, "iv": 0.25},
        {"occ_symbol": _IC_SP, "option_type": "put", "strike": 175.0, "expiry": "2024-03-15",
         "bid": 2.0, "ask": 2.2, "last": 2.1, "iv": 0.25},
        {"occ_symbol": _IC_SC, "option_type": "call", "strike": 185.0, "expiry": "2024-03-15",
         "bid": 2.0, "ask": 2.2, "last": 2.1, "iv": 0.25},
        {"occ_symbol": _IC_LC, "option_type": "call", "strike": 195.0, "expiry": "2024-03-15",
         "bid": 0.5, "ask": 0.7, "last": 0.6, "iv": 0.25},
    ]

    def _bar(sym, d, close, ot, strike):
        return {"occ_symbol": sym, "date": d, "open": close, "high": close, "low": close,
                "close": close, "volume": 100, "underlying": "AAPL", "option_type": ot,
                "strike": strike, "expiry": "2024-03-15"}

    bars = [
        _bar(_IC_LP, "2024-03-06", 0.5, "put", 165.0),
        _bar(_IC_SP, "2024-03-06", 2.0, "put", 175.0),
        _bar(_IC_SC, "2024-03-06", 2.0, "call", 185.0),
        _bar(_IC_LC, "2024-03-06", 0.5, "call", 195.0),
        # MID-LIFE outlier on the short call leg
        _bar(_IC_LP, "2024-03-08", 0.5, "put", 165.0),
        _bar(_IC_SP, "2024-03-08", 2.0, "put", 175.0),
        _bar(_IC_SC, "2024-03-08", 800.0, "call", 185.0),  # absurd outlier
        _bar(_IC_LC, "2024-03-08", 0.5, "call", 195.0),
    ]
    acct, ps, ctx = _base_account(tmp_path, chain, bars)

    lp = OptionLeg(contract_symbol=_IC_LP, side=OrderDirection.BUY, position_intent="buy_to_open",
                   option_type=OptionRight.PUT, strike=165.0, expiry=date(2024, 3, 15), underlying="AAPL")
    sp = OptionLeg(contract_symbol=_IC_SP, side=OrderDirection.SELL, position_intent="sell_to_open",
                   option_type=OptionRight.PUT, strike=175.0, expiry=date(2024, 3, 15), underlying="AAPL")
    sc = OptionLeg(contract_symbol=_IC_SC, side=OrderDirection.SELL, position_intent="sell_to_open",
                   option_type=OptionRight.CALL, strike=185.0, expiry=date(2024, 3, 15), underlying="AAPL")
    lc = OptionLeg(contract_symbol=_IC_LC, side=OrderDirection.BUY, position_intent="buy_to_open",
                   option_type=OptionRight.CALL, strike=195.0, expiry=date(2024, 3, 15), underlying="AAPL")
    acct.submit_option_order(legs=[lp, sp, sc, lc], quantity=1, order_type="market",
                             option_strategy="iron_condor")
    acct.refresh_orders()
    acct.refresh_transactions()
    try:
        yield acct, ps
    finally:
        ctx.__exit__(None, None, None)


def test_iron_condor_outlier_mtm_clamped(acct_iron_condor):
    """A short/credit defined-risk combo's MTM contribution must stay in [-width*100*qty, 0]
    despite an absurd single-leg outlier premium."""
    acct, ps = acct_iron_condor
    ps.set_clock(datetime(2024, 3, 8))  # the outlier bar

    mtm = acct._option_positions_mtm()
    width = 10.0  # widest wing 165/175 or 185/195 -> 10
    assert -(width * 100.0 * 1.0) - 1e-6 <= mtm <= 0.0 + 1e-6
