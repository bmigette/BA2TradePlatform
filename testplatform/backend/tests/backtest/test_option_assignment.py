"""Controlled regression tests for SHORT-option assignment accounting at expiry.

Reproduces the bug behind Backtest id=299 (short strangle, max_drawdown -256% then
"recovery"): an ITM SHORT option at expiry must

  * create the assigned SHARE position at cost basis == STRIKE (not ~0),
  * realise the assignment loss into net-liquidating-value and have it PERSIST
    (equity must NOT revert to the pre-loss level on the next bar), and
  * report the round-trip trade with the correct realised P&L (not -market*100*qty).

Covers BOTH the single-leg short call AND the multi-leg short strangle path
(the strangle is a parent order with two child legs, each keyed by its own OCC).

Run from the backend dir:
    ./venv/bin/python -m pytest tests/backtest/test_option_assignment.py -q
"""
from __future__ import annotations

from datetime import date, datetime

import pytest

from ba2_common.core.types import OptionRight, OrderDirection


CFG = {
    "starting_cash": 100_000.0,
    "commission_per_trade": 0.0,   # zero so P&L math is exact and easy to assert
    "slippage_bps": 0.0,
    "fill_model": "next_bar_open",
}

_CALL_OCC = "AAPL240315C00180000"   # 180 call
_PUT_OCC = "AAPL240315P00160000"    # 160 put

# Underlying bars. Expiry bar 2024-03-15 closes at 200 -> 180 call ITM (by 20/sh),
# 160 put OTM. A LATER bar (2024-03-18) also closes at 200 so we can assert the
# assignment loss PERSISTS (short 100 sh @180 basis, marked at 200 -> -2000 unrealised
# on top of the realised intrinsic settlement).
_AAPL_BARS = [
    {"Date": datetime(2024, 3, 5), "Open": 180, "High": 182, "Low": 178, "Close": 181, "Volume": 1000},
    {"Date": datetime(2024, 3, 6), "Open": 181, "High": 184, "Low": 180, "Close": 183, "Volume": 1100},
    {"Date": datetime(2024, 3, 15), "Open": 199, "High": 201, "Low": 198, "Close": 200, "Volume": 1200},
    {"Date": datetime(2024, 3, 18), "Open": 200, "High": 202, "Low": 199, "Close": 200, "Volume": 1300},
]


def _seed_cache(db_path: str) -> None:
    from app.services.backtest.options_cache import OptionsHistoryCache

    cache = OptionsHistoryCache(db_path)
    cache.write_chain_rows(
        "AAPL",
        "2024-03-01",
        [
            {"occ_symbol": _CALL_OCC, "option_type": "call", "strike": 180.0,
             "expiry": "2024-03-15", "bid": 3.0, "ask": 3.2, "last": 3.1, "iv": 0.25},
            {"occ_symbol": _PUT_OCC, "option_type": "put", "strike": 160.0,
             "expiry": "2024-03-15", "bid": 2.0, "ask": 2.2, "last": 2.1, "iv": 0.25},
        ],
    )
    # Premium bars on the entry-fill day (2024-03-06) so the market SELL fills.
    cache.write_bar_rows(
        [
            {"occ_symbol": _CALL_OCC, "date": "2024-03-06", "open": 4.0, "high": 4.8,
             "low": 3.9, "close": 4.5, "volume": 500, "underlying": "AAPL",
             "option_type": "call", "strike": 180.0, "expiry": "2024-03-15"},
            {"occ_symbol": _PUT_OCC, "date": "2024-03-06", "open": 2.5, "high": 2.9,
             "low": 2.4, "close": 2.7, "volume": 400, "underlying": "AAPL",
             "option_type": "put", "strike": 160.0, "expiry": "2024-03-15"},
        ]
    )


def _make_price_source(clock: datetime):
    from app.services.backtest.price_source import AsOfPriceSource

    ps = AsOfPriceSource(ohlcv_provider=None)
    ps.load_bars("AAPL", _AAPL_BARS)
    ps.set_clock(clock)
    return ps


def _make_engine(tmp_path, legs, option_strategy):
    """Build an account holding the given FILLED short option leg(s) + a minimal engine."""
    from app.services.backtest.backtest_db import backtest_trading_db, seed_account_definition
    from app.services.backtest.seam_wiring import wire_backtest_seams
    from app.services.backtest.backtest_account import BacktestAccount
    from app.services.backtest.options_provider import HistoricalOptionsProvider
    from app.services.backtest.daily_engine import DailyBacktestEngine

    cache_db = str(tmp_path / "options_cache.sqlite")
    _seed_cache(cache_db)
    provider = HistoricalOptionsProvider(cache_db)

    wire_backtest_seams()
    ctx = backtest_trading_db("optassign")
    ctx.__enter__()
    seed_account_definition(1, CFG)
    ps = _make_price_source(datetime(2024, 3, 5))
    acct = BacktestAccount(1, ps, CFG, options_provider=provider)
    wire_backtest_seams().register_account(1, acct)

    acct.submit_option_order(legs=legs, quantity=1, order_type="market",
                             option_strategy=option_strategy)
    acct.refresh_orders()
    acct.refresh_transactions()

    engine = DailyBacktestEngine.__new__(DailyBacktestEngine)
    engine.account = acct
    engine.price = ps
    engine.config = CFG
    return engine, acct, ps, ctx


# ---------------------------------------------------------------------------
# Single-leg short call
# ---------------------------------------------------------------------------
@pytest.fixture
def engine_short_call(tmp_path):
    from ba2_common.core.option_types import OptionLeg

    leg = OptionLeg(contract_symbol=_CALL_OCC, side=OrderDirection.SELL,
                    position_intent="sell_to_open", option_type=OptionRight.CALL,
                    strike=180.0, expiry=date(2024, 3, 15), underlying="AAPL")
    engine, acct, ps, ctx = _make_engine(tmp_path, [leg], "naked_call")
    assert len(acct.get_option_positions()) == 1
    try:
        yield engine, acct, ps
    finally:
        ctx.__exit__(None, None, None)


def test_short_call_assignment_cost_basis_is_strike(engine_short_call):
    """ITM short call at expiry -> SHORT 100 sh assigned at cost basis == strike (180)."""
    engine, acct, ps = engine_short_call
    ps.set_clock(datetime(2024, 3, 15))
    engine._apply_option_expiry(datetime(2024, 3, 15))

    assert acct.get_option_positions() == []
    aapl = [p for p in acct.get_positions() if p["symbol"] == "AAPL"]
    assert len(aapl) == 1
    assert aapl[0]["qty"] == -100                       # short 100 shares
    assert aapl[0]["avg_price"] == pytest.approx(180.0)  # NOT ~0


def test_short_call_assignment_loss_persists(engine_short_call):
    """The assignment loss must be reflected in equity AND persist to the next bar
    (a real ITM assignment cannot 'recover')."""
    engine, acct, ps = engine_short_call

    # Entry credit already collected (sold call @ premium open 4.0 -> +400 cash).
    ps.set_clock(datetime(2024, 3, 15))
    engine._apply_option_expiry(datetime(2024, 3, 15))
    equity_expiry = acct.equity()

    # Step to the NEXT bar (2024-03-18, still spot 200). Short 100 sh @180 basis marked
    # at 200 -> the loss must still be there, NOT revert to ~starting cash.
    ps.set_clock(datetime(2024, 3, 18))
    equity_next = acct.equity()

    # Short stock @180 basis marked at 200 = -20/sh * 100 = -2000 vs cash. Equity must be
    # well below starting cash and must NOT snap back up.
    assert equity_next < CFG["starting_cash"] - 1000
    assert equity_next == pytest.approx(equity_expiry, abs=1.0)


def test_short_call_assignment_round_trip_pnl_sane(engine_short_call):
    """The round-trip trade for the option must not report -market*100*qty with entry~0."""
    engine, acct, ps = engine_short_call
    ps.set_clock(datetime(2024, 3, 15))
    engine._apply_option_expiry(datetime(2024, 3, 15))

    # A single-leg option's round-trip carries the UNDERLYING symbol (the entry order's
    # ``symbol`` is the underlying by convention), so match by the short-option direction.
    trips = acct.get_round_trip_trades()
    opt = [t for t in trips if t["direction"] == "sell" and t["entry_price"] == pytest.approx(4.0, abs=0.5)]
    assert len(opt) == 1
    t = opt[0]
    # Sold @4.0, settled (bought back) at intrinsic 20 -> loss (4-20)*100 = -1600.
    assert t["exit_price"] == pytest.approx(20.0, abs=0.5)
    assert t["pnl"] == pytest.approx(-1600.0, abs=50.0)
    assert t["pnl"] > -100_000  # sanity: NOT -market*100*qty (would be ~ -519.85*100*qty)


# ---------------------------------------------------------------------------
# Multi-leg short strangle (the actual O_SSTG shape)
# ---------------------------------------------------------------------------
@pytest.fixture
def engine_short_strangle(tmp_path):
    from ba2_common.core.option_types import OptionLeg

    call_leg = OptionLeg(contract_symbol=_CALL_OCC, side=OrderDirection.SELL,
                         position_intent="sell_to_open", option_type=OptionRight.CALL,
                         strike=180.0, expiry=date(2024, 3, 15), underlying="AAPL")
    put_leg = OptionLeg(contract_symbol=_PUT_OCC, side=OrderDirection.SELL,
                        position_intent="sell_to_open", option_type=OptionRight.PUT,
                        strike=160.0, expiry=date(2024, 3, 15), underlying="AAPL")
    engine, acct, ps, ctx = _make_engine(tmp_path, [call_leg, put_leg], "short_strangle")
    assert len(acct.get_option_positions()) == 2  # two legs
    try:
        yield engine, acct, ps
    finally:
        ctx.__exit__(None, None, None)


def test_strangle_itm_call_leg_assigned_at_strike(engine_short_strangle):
    """At expiry: 180 call ITM -> assigned SHORT 100 @180; 160 put OTM -> worthless."""
    engine, acct, ps = engine_short_strangle
    ps.set_clock(datetime(2024, 3, 15))
    engine._apply_option_expiry(datetime(2024, 3, 15))

    # Both option legs resolved -> no option positions remain.
    assert acct.get_option_positions() == []

    aapl = [p for p in acct.get_positions() if p["symbol"] == "AAPL"]
    assert len(aapl) == 1
    assert aapl[0]["qty"] == -100
    assert aapl[0]["avg_price"] == pytest.approx(180.0)   # NOT ~0


def test_strangle_assignment_loss_persists(engine_short_strangle):
    engine, acct, ps = engine_short_strangle
    ps.set_clock(datetime(2024, 3, 15))
    engine._apply_option_expiry(datetime(2024, 3, 15))
    equity_expiry = acct.equity()

    ps.set_clock(datetime(2024, 3, 18))
    equity_next = acct.equity()

    assert equity_next < CFG["starting_cash"] - 1000
    assert equity_next == pytest.approx(equity_expiry, abs=1.0)


def test_strangle_settled_leg_not_reassigned(engine_short_strangle):
    """A settled leg must NOT be reported as held on later bars: running expiry again must
    NOT re-assign more shares (the phantom re-assignment behind the -8974% blow-up)."""
    engine, acct, ps = engine_short_strangle
    ps.set_clock(datetime(2024, 3, 15))
    engine._apply_option_expiry(datetime(2024, 3, 15))

    aapl_after_first = [p for p in acct.get_positions() if p["symbol"] == "AAPL"]
    assert aapl_after_first[0]["qty"] == -100

    # Re-run expiry on the NEXT bar. The call leg is already settled + netted, so no option
    # position should remain and the AMD/AAPL short must NOT double.
    ps.set_clock(datetime(2024, 3, 18))
    assert acct.get_option_positions() == []
    engine._apply_option_expiry(datetime(2024, 3, 18))

    aapl_after_second = [p for p in acct.get_positions() if p["symbol"] == "AAPL"]
    assert aapl_after_second[0]["qty"] == -100  # unchanged — NOT -200


def test_strangle_round_trip_per_leg_entry_not_zero(engine_short_strangle):
    """The multi-leg strangle must produce a PER-LEG round-trip whose entry is the leg's
    premium (NOT ~0) and whose pnl is bounded (NOT -market*100*qty). This is the
    entry_price~0 / pnl=-(stock*100*qty) reporting defect from Backtest id=299."""
    engine, acct, ps = engine_short_strangle
    ps.set_clock(datetime(2024, 3, 15))
    engine._apply_option_expiry(datetime(2024, 3, 15))

    trips = acct.get_round_trip_trades()
    # One round-trip per leg (call + put), keyed by the OCC contract symbol — NOT one lumped
    # underlying-symbol row.
    call = [t for t in trips if t["symbol"] == _CALL_OCC]
    put = [t for t in trips if t["symbol"] == _PUT_OCC]
    assert len(call) == 1
    assert len(put) == 1

    # Call leg: sold @4.0 (premium), settled at intrinsic 20 -> loss ~ (4-20)*100 = -1600.
    assert call[0]["entry_price"] == pytest.approx(4.0, abs=0.5)   # NOT ~0
    assert call[0]["pnl"] == pytest.approx(-1600.0, abs=100.0)
    assert call[0]["pnl"] > -100_000                                # NOT -market*100*qty

    # Put leg: sold @2.5, expired worthless -> small profit (kept the premium).
    assert put[0]["entry_price"] == pytest.approx(2.5, abs=0.5)     # NOT ~0
    assert put[0]["pnl"] == pytest.approx(250.0, abs=50.0)
