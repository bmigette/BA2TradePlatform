"""Phase 1 Task 7: per-bar option expiry / exercise / assignment -> equity ledger.

At expiry the engine resolves every held single-leg option position:

  * OTM  -> expires worthless (option transaction closed at premium 0).
  * ITM long  -> exercised  (option closed at intrinsic; a SHARE position is created
                 in the equity ledger, settled at the STRIKE).
  * ITM short -> assigned   (same conversion, opposite share side).

ITM is decided off the underlying's bar close: a CALL is ITM when spot > strike, a
PUT when spot < strike. Early American assignment is NOT modelled (at-expiry only).

The pure helper ``option_expiry_outcome`` is the contract these tests pin down; the
integration test drives ``DailyBacktestEngine._apply_option_expiry`` against a
constructed account holding one expiring long call.

Run from the backend dir:
    ./venv/bin/python -m pytest tests/backtest/test_option_expiry.py -q
"""
from __future__ import annotations

from datetime import date, datetime

import pytest

from app.services.backtest.daily_engine import option_expiry_outcome
from ba2_common.core.types import OptionRight, OrderDirection


# ---------------------------------------------------------------------------
# PURE helper (these MUST pass exactly)
# ---------------------------------------------------------------------------
def test_long_call_itm_exercises():
    assert option_expiry_outcome(
        OptionRight.CALL, OrderDirection.BUY, strike=180.0, spot=200.0, qty=1
    ) == {"action": "exercise", "side": "buy", "shares": 100, "price": 180.0}


def test_long_call_otm_worthless():
    assert option_expiry_outcome(
        OptionRight.CALL, OrderDirection.BUY, strike=180.0, spot=170.0, qty=1
    ) == {"action": "worthless"}


def test_short_put_itm_assigned_buy_shares():
    assert option_expiry_outcome(
        OptionRight.PUT, OrderDirection.SELL, strike=180.0, spot=170.0, qty=2
    ) == {"action": "assigned", "side": "buy", "shares": 200, "price": 180.0}


def test_short_call_itm_assigned_sell_shares():
    assert option_expiry_outcome(
        OptionRight.CALL, OrderDirection.SELL, strike=180.0, spot=200.0, qty=1
    ) == {"action": "assigned", "side": "sell", "shares": 100, "price": 180.0}


def test_long_put_itm_exercises_sell_shares():
    assert option_expiry_outcome(
        OptionRight.PUT, OrderDirection.BUY, strike=180.0, spot=170.0, qty=1
    ) == {"action": "exercise", "side": "sell", "shares": 100, "price": 180.0}


# Extra edge: at-the-money (spot == strike) is NOT in-the-money -> worthless.
def test_atm_call_worthless():
    assert option_expiry_outcome(
        OptionRight.CALL, OrderDirection.BUY, strike=180.0, spot=180.0, qty=1
    ) == {"action": "worthless"}


# ---------------------------------------------------------------------------
# Integration: _apply_option_expiry against a constructed account
# ---------------------------------------------------------------------------
CFG = {
    "starting_cash": 100_000.0,
    "commission_per_trade": 1.0,
    "slippage_bps": 0.0,
    "fill_model": "next_bar_open",
}

_OCC = "AAPL240315C00180000"

# Underlying bars; the expiry bar (2024-03-15) closes at 200 -> the 180 call is ITM.
_AAPL_BARS = [
    {"Date": datetime(2024, 3, 5), "Open": 180, "High": 182, "Low": 178, "Close": 181, "Volume": 1000},
    {"Date": datetime(2024, 3, 6), "Open": 181, "High": 184, "Low": 180, "Close": 183, "Volume": 1100},
    {"Date": datetime(2024, 3, 15), "Open": 199, "High": 201, "Low": 198, "Close": 200, "Volume": 1200},
]


def _seed_cache(db_path: str) -> None:
    """Seed the CALL chain + a premium bar so the order fills like in Task 6."""
    from app.services.backtest.options_cache import OptionsHistoryCache

    cache = OptionsHistoryCache(db_path)
    cache.write_chain_rows(
        "AAPL",
        "2024-03-01",
        [
            {
                "occ_symbol": _OCC,
                "option_type": "call",
                "strike": 180.0,
                "expiry": "2024-03-15",
                "bid": 3.0,
                "ask": 3.2,
                "last": 3.1,
                "iv": 0.25,
            },
        ],
    )
    cache.write_bar_rows(
        [
            {
                "occ_symbol": _OCC,
                "date": "2024-03-06",
                "open": 4.0,
                "high": 4.8,
                "low": 3.9,
                "close": 4.5,
                "volume": 500,
                "underlying": "AAPL",
                "option_type": "call",
                "strike": 180.0,
                "expiry": "2024-03-15",
            },
        ]
    )


def _make_price_source(clock: datetime):
    from app.services.backtest.price_source import AsOfPriceSource

    ps = AsOfPriceSource(ohlcv_provider=None)
    ps.load_bars("AAPL", _AAPL_BARS)
    ps.set_clock(clock)
    return ps


@pytest.fixture
def engine_with_long_call(tmp_path):
    """A DailyBacktestEngine whose account holds one FILLED long call expiring 2024-03-15.

    Builds the Task-6 options account harness, fills a market BUY call (so there is a
    real OPENED option transaction + held position), then constructs a minimal engine
    bound to that account/price source so ``_apply_option_expiry`` can be driven directly.
    """
    from app.services.backtest.backtest_db import (
        backtest_trading_db,
        seed_account_definition,
    )
    from app.services.backtest.seam_wiring import wire_backtest_seams
    from app.services.backtest.backtest_account import BacktestAccount
    from app.services.backtest.options_provider import HistoricalOptionsProvider
    from app.services.backtest.daily_engine import DailyBacktestEngine
    from ba2_common.core.option_types import OptionLeg

    cache_db = str(tmp_path / "options_cache.sqlite")
    _seed_cache(cache_db)
    provider = HistoricalOptionsProvider(cache_db)

    wire_backtest_seams()
    ctx = backtest_trading_db("optexpiry")
    ctx.__enter__()
    seed_account_definition(1, CFG)
    ps = _make_price_source(datetime(2024, 3, 5))
    acct = BacktestAccount(1, ps, CFG, options_provider=provider)
    wire_backtest_seams().register_account(1, acct)

    # Buy 1 contract of the 180 call -> fills next bar (2024-03-06) -> OPENED txn + position.
    # A real caller carries the full contract metadata on the leg (option_type/strike/expiry)
    # so the held position knows its expiry; mirror that here.
    leg = OptionLeg(
        contract_symbol=_OCC,
        side=OrderDirection.BUY,
        position_intent="buy_to_open",
        option_type=OptionRight.CALL,
        strike=180.0,
        expiry=date(2024, 3, 15),
        underlying="AAPL",
    )
    acct.submit_option_order(legs=[leg], quantity=1, order_type="market", option_strategy="long_call")
    acct.refresh_orders()
    acct.refresh_transactions()
    assert len(acct.get_option_positions()) == 1

    # A minimal engine bound to this account + price source (no experts/loop needed).
    engine = DailyBacktestEngine.__new__(DailyBacktestEngine)
    engine.account = acct
    engine.price = ps
    engine.config = CFG

    try:
        yield engine, acct, ps
    finally:
        ctx.__exit__(None, None, None)


def test_apply_option_expiry_exercises_itm_long_call(engine_with_long_call):
    engine, acct, ps = engine_with_long_call

    # Step the clock to the expiry bar: underlying closes 200 -> the 180 call is ITM.
    ps.set_clock(datetime(2024, 3, 15))

    engine._apply_option_expiry(datetime(2024, 3, 15))

    # The option position is gone (transaction closed).
    assert acct.get_option_positions() == []

    # A LONG equity position of 100 shares of AAPL settled at the strike (180) now exists.
    positions = acct.get_positions()
    aapl = [p for p in positions if p["symbol"] == "AAPL"]
    assert len(aapl) == 1
    assert aapl[0]["qty"] == 100
    assert aapl[0]["avg_price"] == pytest.approx(180.0)


def test_apply_option_expiry_skips_unexpired(engine_with_long_call):
    """A position whose expiry is after the bar is left untouched."""
    engine, acct, ps = engine_with_long_call
    ps.set_clock(datetime(2024, 3, 6))  # before the 2024-03-15 expiry

    engine._apply_option_expiry(datetime(2024, 3, 6))

    assert len(acct.get_option_positions()) == 1  # still held
    assert [p for p in acct.get_positions() if p["symbol"] == "AAPL"] == []
