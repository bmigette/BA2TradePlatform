"""TDD for per-bar maintenance-margin check + forced liquidation (Part B).

A broker force-liquidates a book whose net-liquidating-value falls below its
maintenance-margin requirement, so a backtest's equity cannot blow arbitrarily
negative (the -256% drawdown in Backtest id=299). This pins:

  * maintenance_margin_requirement: naked-option margin for short option legs +
    30% notional for short stock; long stock/options require 0 extra (marked).
  * maybe_margin_call_liquidation: when equity < requirement (or < 0), close the
    breaching short positions at the current bar's close/premium and book the loss,
    leaving equity BOUNDED (>= 0-ish) and the offending positions closed.

Run from the backend dir:
    ./venv/bin/python -m pytest tests/backtest/test_margin_liquidation.py -q
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

_CALL_OCC = "AMD240315C00500000"   # 500 call, way OTM at entry, deep ITM later

# AMD rips from ~450 to ~520: a naked short 500 call goes deep ITM. On a $10k account
# a 3-contract short call is ~$150k notional -> unbounded loss without a margin call.
_AMD_BARS = [
    {"Date": datetime(2024, 3, 5), "Open": 450, "High": 452, "Low": 448, "Close": 450, "Volume": 1000},
    {"Date": datetime(2024, 3, 6), "Open": 450, "High": 455, "Low": 449, "Close": 452, "Volume": 1100},
    {"Date": datetime(2024, 3, 10), "Open": 519, "High": 522, "Low": 517, "Close": 520, "Volume": 1200},
]


def _seed_cache(db_path: str) -> None:
    from app.services.backtest.options_cache import OptionsHistoryCache

    cache = OptionsHistoryCache(db_path)
    cache.write_chain_rows(
        "AMD", "2024-03-01",
        [{"occ_symbol": _CALL_OCC, "option_type": "call", "strike": 500.0,
          "expiry": "2024-03-15", "bid": 3.0, "ask": 3.2, "last": 3.1, "iv": 0.5}],
    )
    cache.write_bar_rows(
        [
            # entry-fill day premium (cheap OTM call)
            {"occ_symbol": _CALL_OCC, "date": "2024-03-06", "open": 3.0, "high": 3.5,
             "low": 2.9, "close": 3.2, "volume": 500, "underlying": "AMD",
             "option_type": "call", "strike": 500.0, "expiry": "2024-03-15"},
            # blow-up day: call now deep ITM (~20 intrinsic + time)
            {"occ_symbol": _CALL_OCC, "date": "2024-03-10", "open": 21.0, "high": 23.0,
             "low": 20.0, "close": 22.0, "volume": 800, "underlying": "AMD",
             "option_type": "call", "strike": 500.0, "expiry": "2024-03-15"},
        ]
    )


def _make_price_source(clock: datetime):
    from app.services.backtest.price_source import AsOfPriceSource

    ps = AsOfPriceSource(ohlcv_provider=None)
    ps.load_bars("AMD", _AMD_BARS)
    ps.set_clock(clock)
    return ps


@pytest.fixture
def acct_short_call(tmp_path):
    from app.services.backtest.backtest_db import backtest_trading_db, seed_account_definition
    from app.services.backtest.seam_wiring import wire_backtest_seams
    from app.services.backtest.backtest_account import BacktestAccount
    from app.services.backtest.options_provider import HistoricalOptionsProvider
    from ba2_common.core.option_types import OptionLeg

    cache_db = str(tmp_path / "options_cache.sqlite")
    _seed_cache(cache_db)
    provider = HistoricalOptionsProvider(cache_db)

    wire_backtest_seams()
    ctx = backtest_trading_db("margincall")
    ctx.__enter__()
    seed_account_definition(1, CFG)
    ps = _make_price_source(datetime(2024, 3, 5))
    acct = BacktestAccount(1, ps, CFG, options_provider=provider)
    wire_backtest_seams().register_account(1, acct)

    # Sell 3 naked 500 calls (~$150k notional on a $10k account).
    leg = OptionLeg(contract_symbol=_CALL_OCC, side=OrderDirection.SELL,
                    position_intent="sell_to_open", option_type=OptionRight.CALL,
                    strike=500.0, expiry=date(2024, 3, 15), underlying="AMD")
    acct.submit_option_order(legs=[leg], quantity=3, order_type="market",
                             option_strategy="naked_call")
    acct.refresh_orders()
    acct.refresh_transactions()
    assert len(acct.get_option_positions()) == 1
    try:
        yield acct, ps
    finally:
        ctx.__exit__(None, None, None)


def test_maintenance_margin_requirement_short_option(acct_short_call):
    """A short option leg requires naked-option maintenance margin (> 0, ~notional-scaled)."""
    acct, ps = acct_short_call
    ps.set_clock(datetime(2024, 3, 6))
    req = acct.maintenance_margin_requirement()
    # 3 contracts, naked margin ~ max(0.2*spot - OTM, 0.1*spot)*100 each -> thousands.
    assert req > 5_000.0


def test_margin_call_liquidates_and_bounds_equity(acct_short_call):
    """When the short call goes deep ITM the account breaches maintenance margin; the
    liquidation closes the position and equity stays bounded (never -256%)."""
    acct, ps = acct_short_call

    # Blow-up bar: AMD 520, the 500 call marks ~22 -> short 3 lots MTM = -22*3*100 = -6600
    # against ~+960 credit; equity dips but the naked-margin requirement on a ~$156k-notional
    # position dwarfs the ~$10k account -> breach.
    ps.set_clock(datetime(2024, 3, 10))
    equity_before = acct.equity()
    req = acct.maintenance_margin_requirement()
    assert equity_before < req  # breach

    liquidated = acct.maybe_margin_call_liquidation()
    assert liquidated is True

    # Position is closed and equity is bounded — not catastrophically negative.
    assert acct.get_option_positions() == []
    eq = acct.equity()
    assert eq > -1_000.0            # bounded, nowhere near -256% of $10k (~ -$25k)
    assert eq < CFG["starting_cash"]  # a real loss was still booked

    # Idempotent once flat: nothing left to liquidate.
    assert acct.maybe_margin_call_liquidation() is False
