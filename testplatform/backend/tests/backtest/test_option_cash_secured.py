"""Cash-secured guard for DEBIT option entries (fill-time affordability cap).

A DEBIT option entry (a lone long call/put, or a debit combo — bull_call_spread /
bear_put_spread / call_butterfly) is sized from ANALYSIS-time quotes, but fills at
the sparse cache's next-bar premiums, which can diverge sharply upward. Without a
guard the entry buys far more debit than the account holds, driving cash
persistently negative (O_BF id=415: cash -$85k on a $10k account for the whole run).

A debit structure physically cannot lose more than the premium paid, so cash must
never go below zero on a debit entry. This guard caps the number of structures that
FILL to floor(available_cash / actual_debit_per_structure) using the ACTUAL fill
premiums; if not even one structure is affordable the entry does not open. Multi-leg
combos scale all legs together by the capped count so the combo stays balanced.

Credit/undefined structures are NOT guarded here (a credit entry receives cash) —
the margin-liquidation path bounds those.

Run from the backend dir:
    ./venv/bin/python -m pytest tests/backtest/test_option_cash_secured.py -q
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
_LC = "AAPL240315C00180000"  # lone long call reuses the 180 strike

_AAPL_BARS = [
    {"Date": datetime(2024, 3, 5), "Open": 180, "High": 181, "Low": 179, "Close": 180, "Volume": 1000},
    {"Date": datetime(2024, 3, 6), "Open": 180, "High": 181, "Low": 179, "Close": 180, "Volume": 1100},
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
    ctx = backtest_trading_db("cashsecured")
    ctx.__enter__()
    seed_account_definition(1, CFG)
    ps = _make_price_source(datetime(2024, 3, 5))
    acct = BacktestAccount(1, ps, CFG, options_provider=provider)
    wire_backtest_seams().register_account(1, acct)
    return acct, ps, ctx


def _c(sym, strike):
    return {"occ_symbol": sym, "option_type": "call", "strike": strike,
            "expiry": "2024-03-15", "bid": 1.0, "ask": 1.2, "last": 1.1, "iv": 0.25}


def _bar(sym, d, o, strike):
    return {"occ_symbol": sym, "date": d, "open": o, "high": o, "low": o, "close": o,
            "volume": 100, "underlying": "AAPL", "option_type": "call",
            "strike": strike, "expiry": "2024-03-15"}


# ---------------------------------------------------------------------------
# Multi-leg call butterfly: fill debit >> cash -> capped so cash stays >= 0
# ---------------------------------------------------------------------------
def _submit_butterfly(acct, qty):
    from ba2_common.core.option_types import OptionLeg
    low = OptionLeg(contract_symbol=_LOW, side=OrderDirection.BUY, ratio_qty=1,
                    position_intent="buy_to_open", option_type=OptionRight.CALL,
                    strike=170.0, expiry=date(2024, 3, 15), underlying="AAPL")
    body = OptionLeg(contract_symbol=_BODY, side=OrderDirection.SELL, ratio_qty=2,
                     position_intent="sell_to_open", option_type=OptionRight.CALL,
                     strike=180.0, expiry=date(2024, 3, 15), underlying="AAPL")
    high = OptionLeg(contract_symbol=_HIGH, side=OrderDirection.BUY, ratio_qty=1,
                     position_intent="buy_to_open", option_type=OptionRight.CALL,
                     strike=190.0, expiry=date(2024, 3, 15), underlying="AAPL")
    acct.submit_option_order(legs=[low, body, high], quantity=qty, order_type="market",
                             option_strategy="call_butterfly")


def test_butterfly_fill_debit_capped_to_cash(tmp_path):
    """Sized 20 structures but the FILL debit per structure is huge -> the number that fills is
    capped so cash never goes negative; the combo stays balanced (leg ratio 1:2:1 preserved)."""
    # Analysis-time quotes were cheap; the FILL premiums (2024-03-06 open) are absurdly high:
    # per-structure debit = low(90) + high(30) - 2*body(20) = 90 + 30 - 40 = 80 /share -> $8000.
    chain = [_c(_LOW, 170.0), _c(_BODY, 180.0), _c(_HIGH, 190.0)]
    bars = [
        _bar(_LOW, "2024-03-06", 90.0, 170.0),
        _bar(_BODY, "2024-03-06", 20.0, 180.0),
        _bar(_HIGH, "2024-03-06", 30.0, 190.0),
    ]
    acct, ps, ctx = _base_account(tmp_path, chain, bars)
    try:
        _submit_butterfly(acct, 20)  # 20 structures = $160k debit on a $10k account
        acct.refresh_orders()
        acct.refresh_transactions()

        # $10k / $8000 per structure -> only 1 structure affordable.
        assert acct._cash >= 0.0
        low_lot = acct._option_positions.get(_LOW)
        body_lot = acct._option_positions.get(_BODY)
        high_lot = acct._option_positions.get(_HIGH)
        assert low_lot is not None and low_lot.qty == 1      # 1 structure * ratio 1
        assert body_lot is not None and body_lot.qty == -2   # 1 structure * ratio 2 (short)
        assert high_lot is not None and high_lot.qty == 1    # balanced combo
        # cash = 10000 - 8000 = 2000
        assert acct._cash == pytest.approx(2000.0, abs=1.0)
    finally:
        ctx.__exit__(None, None, None)


def test_butterfly_unaffordable_does_not_open(tmp_path):
    """If not even ONE structure is affordable, the entry does not open (no lots, cash intact)."""
    # per-structure debit = low(150) + high(50) - 2*body(20) = 160 /share -> $16000 > $10k.
    chain = [_c(_LOW, 170.0), _c(_BODY, 180.0), _c(_HIGH, 190.0)]
    bars = [
        _bar(_LOW, "2024-03-06", 150.0, 170.0),
        _bar(_BODY, "2024-03-06", 20.0, 180.0),
        _bar(_HIGH, "2024-03-06", 50.0, 190.0),
    ]
    acct, ps, ctx = _base_account(tmp_path, chain, bars)
    try:
        _submit_butterfly(acct, 5)
        acct.refresh_orders()
        acct.refresh_transactions()

        assert acct._cash == pytest.approx(10_000.0, abs=1e-6)   # untouched
        assert all(l.qty == 0 for l in acct._option_positions.values())
        assert acct.get_option_positions() == []
    finally:
        ctx.__exit__(None, None, None)


def test_butterfly_affordable_unchanged(tmp_path):
    """An AFFORDABLE debit combo fills at its full sized quantity (guard is a no-op)."""
    # per-structure debit = low(12) + high(1) - 2*body(5) = 3 /share -> $300 per structure.
    chain = [_c(_LOW, 170.0), _c(_BODY, 180.0), _c(_HIGH, 190.0)]
    bars = [
        _bar(_LOW, "2024-03-06", 12.0, 170.0),
        _bar(_BODY, "2024-03-06", 5.0, 180.0),
        _bar(_HIGH, "2024-03-06", 1.0, 190.0),
    ]
    acct, ps, ctx = _base_account(tmp_path, chain, bars)
    try:
        _submit_butterfly(acct, 5)  # 5 * $300 = $1500 < $10k -> all fill
        acct.refresh_orders()
        acct.refresh_transactions()

        assert acct._option_positions[_LOW].qty == 5
        assert acct._option_positions[_BODY].qty == -10
        assert acct._option_positions[_HIGH].qty == 5
        assert acct._cash == pytest.approx(8500.0, abs=1.0)   # 10000 - 5*300
    finally:
        ctx.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# Lone long call: fill premium > cash -> capped
# ---------------------------------------------------------------------------
def test_long_call_capped_to_cash(tmp_path):
    """A lone long call sized for many contracts but whose fill premium is huge is capped so
    cash stays >= 0."""
    from ba2_common.core.option_types import OptionLeg
    chain = [_c(_LC, 180.0)]
    bars = [_bar(_LC, "2024-03-06", 40.0, 180.0)]  # $4000 per contract
    acct, ps, ctx = _base_account(tmp_path, chain, bars)
    try:
        leg = OptionLeg(contract_symbol=_LC, side=OrderDirection.BUY, ratio_qty=1,
                        position_intent="buy_to_open", option_type=OptionRight.CALL,
                        strike=180.0, expiry=date(2024, 3, 15), underlying="AAPL")
        acct.submit_option_order(legs=[leg], quantity=10, order_type="market",
                                 option_strategy="long_call")  # 10 * $4000 = $40k
        acct.refresh_orders()
        acct.refresh_transactions()

        assert acct._cash >= 0.0
        lot = acct._option_positions.get(_LC)
        assert lot is not None and lot.qty == 2   # floor(10000/4000) = 2
        assert acct._cash == pytest.approx(2000.0, abs=1.0)
    finally:
        ctx.__exit__(None, None, None)
