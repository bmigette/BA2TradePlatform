"""Phase 1 Task 4: BacktestAccount ALSO implements OptionsAccountInterface read methods.

These tests prove the options READ surface (chain / quote / atm-iv / positions) is wired
to an injected ``HistoricalOptionsProvider`` clamped to the simulated as-of clock, and that
an account built WITHOUT a provider still returns empty (the equity-only path is unaffected).

The provider reads a seeded temp sqlite options cache (one CALL + one PUT at strike 180,
expiry 2024-03-15, dated 2024-03-01). The price-source clock is set to 2024-03-05, so the
as-of-clamp resolves the 2024-03-01 chain (no lookahead).

Run from the backend dir:
    ./venv/bin/python -m pytest tests/backtest/test_backtest_account_options.py -q
"""
from __future__ import annotations

from datetime import date, datetime

import pytest

from ba2_common.core.types import OptionRight

CFG = {
    "starting_cash": 100_000.0,
    "commission_per_trade": 1.0,
    "slippage_bps": 5.0,
    "fill_model": "next_bar_open",
}

# A trivial underlying bar so the price source has the symbol + a clock day.
_AAPL_BARS = [
    {"Date": datetime(2024, 3, 5), "Open": 180, "High": 182, "Low": 178, "Close": 181, "Volume": 1000},
]


def _seed_cache(db_path: str) -> None:
    """Seed one CALL + one PUT (strike 180, expiry 2024-03-15) dated 2024-03-01."""
    from app.services.backtest.options_cache import OptionsHistoryCache

    cache = OptionsHistoryCache(db_path)
    cache.write_chain_rows(
        "AAPL",
        "2024-03-01",
        [
            {
                "occ_symbol": "AAPL240315C00180000",
                "option_type": "call",
                "strike": 180.0,
                "expiry": "2024-03-15",
                "bid": 3.0,
                "ask": 3.2,
                "last": 3.1,
                "iv": 0.25,
                "delta": 0.55,
                "gamma": 0.04,
                "theta": -0.05,
                "vega": 0.10,
                "open_interest": 1000,
                "volume": 200,
            },
            {
                "occ_symbol": "AAPL240315P00180000",
                "option_type": "put",
                "strike": 180.0,
                "expiry": "2024-03-15",
                "bid": 2.8,
                "ask": 3.0,
                "last": 2.9,
                "iv": 0.27,
                "delta": -0.45,
                "gamma": 0.04,
                "theta": -0.05,
                "vega": 0.10,
                "open_interest": 900,
                "volume": 150,
            },
        ],
    )


def _make_price_source():
    from app.services.backtest.price_source import AsOfPriceSource

    ps = AsOfPriceSource(ohlcv_provider=None)  # no provider; bars pre-seeded
    ps.load_bars("AAPL", _AAPL_BARS)
    ps.set_clock(datetime(2024, 3, 5))
    return ps


@pytest.fixture
def options_account(tmp_path):
    """A BacktestAccount wired to a HistoricalOptionsProvider over a seeded temp cache.

    Mirrors the existing backtest-account test construction (fresh per-run trading DB +
    seam wiring + seeded account definition) and additionally injects the options provider.
    Yields the account; the caller need not close anything (the DB context is torn down here).
    """
    from app.services.backtest.backtest_db import (
        backtest_trading_db,
        seed_account_definition,
    )
    from app.services.backtest.seam_wiring import wire_backtest_seams
    from app.services.backtest.backtest_account import BacktestAccount
    from app.services.backtest.options_provider import HistoricalOptionsProvider

    cache_db = str(tmp_path / "options_cache.sqlite")
    _seed_cache(cache_db)
    provider = HistoricalOptionsProvider(cache_db)

    wire_backtest_seams()
    ctx = backtest_trading_db("options")
    ctx.__enter__()
    seed_account_definition(1, CFG)
    ps = _make_price_source()
    acct = BacktestAccount(1, ps, CFG, options_provider=provider)
    wire_backtest_seams().register_account(1, acct)
    try:
        yield acct
    finally:
        ctx.__exit__(None, None, None)


@pytest.fixture
def backtest_account_no_options(tmp_path):
    """A BacktestAccount built WITHOUT a provider (equity-only path unaffected)."""
    from app.services.backtest.backtest_db import (
        backtest_trading_db,
        seed_account_definition,
    )
    from app.services.backtest.seam_wiring import wire_backtest_seams
    from app.services.backtest.backtest_account import BacktestAccount

    wire_backtest_seams()
    ctx = backtest_trading_db("options-noprov")
    ctx.__enter__()
    seed_account_definition(2, CFG)
    ps = _make_price_source()
    acct = BacktestAccount(2, ps, CFG)  # no options_provider
    wire_backtest_seams().register_account(2, acct)
    try:
        yield acct
    finally:
        ctx.__exit__(None, None, None)


def test_supports_options_true(options_account):
    acct = options_account
    assert acct.supports_options is True


def test_get_option_chain_reads_provider(options_account):
    chain = options_account.get_option_chain(
        "AAPL", date(2024, 3, 1), date(2024, 3, 31), OptionRight.CALL
    )
    assert chain and all(c.option_type == OptionRight.CALL for c in chain)


def test_get_option_chain_empty_without_provider(backtest_account_no_options):
    # an account built WITHOUT a provider returns [] (equity-only path unaffected)
    assert (
        backtest_account_no_options.get_option_chain(
            "AAPL", date(2024, 3, 1), date(2024, 3, 31)
        )
        == []
    )


def test_get_option_quote_reads_provider(options_account):
    # No option bar seeded -> provider returns None (quote path is wired, just no bar).
    assert options_account.get_option_quote("AAPL240315C00180000") is None


def test_get_atm_implied_volatility_reads_provider(options_account):
    iv = options_account.get_atm_implied_volatility("AAPL")
    # mean of seeded IVs (0.25, 0.27) = 0.26
    assert iv == pytest.approx(0.26)


def test_get_option_positions_empty_when_no_option_txns(options_account):
    assert options_account.get_option_positions() == []


def test_submit_single_call_stages_fillable_option_order(options_account):
    from ba2_common.core.option_types import OptionLeg
    from ba2_common.core.types import OrderDirection

    acct = options_account
    leg = OptionLeg(contract_symbol="AAPL240315C00180000", side=OrderDirection.BUY,
                    position_intent="buy_to_open", underlying="AAPL")
    order = acct.submit_option_order(legs=[leg], quantity=1, order_type="limit", limit_price=2.1,
                                     option_strategy="long_call")
    assert order is not None
    assert order.asset_class.value == "option" and order.multiplier == 100
    assert order.contract_symbol == "AAPL240315C00180000"
    # staged in a non-terminal, fillable status (matches the equity working status)
    from ba2_common.core.types import OrderStatus
    assert order.status not in OrderStatus.get_terminal_statuses()
    # same working status the equity submit path uses, so the bar-fill engine picks it up
    assert order.status == OrderStatus.ACCEPTED


def test_close_option_position_submits_opposite_side(options_account):
    from ba2_common.core.option_types import OptionLeg, OptionPosition
    from ba2_common.core.types import OrderDirection, OrderStatus, OptionRight

    acct = options_account
    pos = OptionPosition(
        contract_symbol="AAPL240315C00180000",
        underlying="AAPL",
        option_type=OptionRight.CALL,
        strike=180.0,
        expiry=date(2024, 3, 15),
        side=OrderDirection.BUY,
        quantity=1,
        avg_entry_price=3.1,
    )
    order = acct.close_option_position(pos, order_type="limit", limit_price=3.5)
    assert order is not None
    # closing a long -> SELL_TO_CLOSE, staged fillable
    assert order.side == OrderDirection.SELL
    assert order.contract_symbol == "AAPL240315C00180000"
    assert order.option_strategy == "close"
    assert order.status == OrderStatus.ACCEPTED
    assert order.status not in OrderStatus.get_terminal_statuses()
