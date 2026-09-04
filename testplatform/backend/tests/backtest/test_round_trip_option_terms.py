"""``get_round_trip_trades`` publishes WHAT THE CONTRACT WAS, not just its price.

The recorder has always emitted ``contract_symbol`` / ``underlying_symbol`` /
``transaction_id`` / ``multiplier``. It did not emit the contract's own terms --
call or put, which strike, which expiry -- so anything downstream that wanted to
say "long AAPL 150 call" had to slice the OCC string and hope, including getting
the strike's implied three decimal places right.

The opening order carries all three as real columns (models.py: option_type /
strike / expiry), so this is a passthrough, not a derivation. These tests drive a
REAL option round trip through the account rather than hand-building the dict,
because the point is that the recorder reads the fields off the order it already
has in hand.

Run from the backend dir:
    python -m pytest tests/backtest/test_round_trip_option_terms.py -q
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

_AAPL_CALL_150 = "AAPL240315C00150000"


def _make_ps(symbol, bars, clock):
    from app.services.backtest.price_source import AsOfPriceSource

    ps = AsOfPriceSource(ohlcv_provider=None)
    ps.load_bars(symbol, bars)
    ps.set_clock(clock)
    return ps


def _account(tmp_path, tag, ps, chain, bar_rows):
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
    ctx = backtest_trading_db(tag)
    ctx.__enter__()
    seed_account_definition(1, CFG)
    acct = BacktestAccount(1, ps, CFG, options_provider=prov)
    wire_backtest_seams().register_account(1, acct)
    return acct, ctx


def _chain_row(sym, k):
    return {"occ_symbol": sym, "option_type": "call", "strike": k, "expiry": "2024-03-15",
            "bid": 4.0, "ask": 4.4, "last": 4.2, "iv": 0.25}


def _bar(sym, d, close, k):
    return {"occ_symbol": sym, "date": d, "open": close, "high": close, "low": close,
            "close": close, "volume": 100, "underlying": "AAPL", "option_type": "call",
            "strike": k, "expiry": "2024-03-15"}


def _leg(sym, side, k):
    from ba2_common.core.option_types import OptionLeg

    intent = "buy_to_open" if side == OrderDirection.BUY else "sell_to_open"
    return OptionLeg(contract_symbol=sym, side=side, ratio_qty=1, position_intent=intent,
                     option_type=OptionRight.CALL, strike=k, expiry=date(2024, 3, 15),
                     underlying="AAPL")


def _ubar(d, px):
    return {"Date": d, "Open": px, "High": px + 1, "Low": px - 1, "Close": px, "Volume": 100}


@pytest.fixture
def long_call_round_trip(tmp_path):
    """Buy 1 AAPL 150 call at 4.20, close it at 6.10 two sessions later."""
    bars = [_ubar(datetime(2024, 3, 5), 150), _ubar(datetime(2024, 3, 6), 152),
            _ubar(datetime(2024, 3, 7), 156), _ubar(datetime(2024, 3, 8), 158)]
    ps = _make_ps("AAPL", bars, datetime(2024, 3, 5))
    chain = [_chain_row(_AAPL_CALL_150, 150.0)]
    bar_rows = [_bar(_AAPL_CALL_150, "2024-03-06", 4.20, 150.0),
                _bar(_AAPL_CALL_150, "2024-03-07", 5.10, 150.0),
                _bar(_AAPL_CALL_150, "2024-03-08", 6.10, 150.0)]
    acct, ctx = _account(tmp_path, "rt-terms", ps, chain, bar_rows)
    try:
        acct.submit_option_order(
            legs=[_leg(_AAPL_CALL_150, OrderDirection.BUY, 150.0)],
            quantity=1, order_type="market", option_strategy="long_call")
        acct.refresh_orders()
        acct.refresh_transactions()

        ps.set_clock(datetime(2024, 3, 7))
        position = acct.get_option_positions()[0]
        acct.close_option_position(position)
        ps.set_clock(datetime(2024, 3, 8))
        acct.refresh_orders()
        acct.refresh_transactions()
        yield acct
    finally:
        ctx.__exit__(None, None, None)


def test_an_option_round_trip_publishes_the_contracts_own_terms(long_call_round_trip):
    trades = long_call_round_trip.get_round_trip_trades()
    assert trades, "the round trip should have been recorded"
    trade = trades[0]

    assert trade["option_type"] == "call"
    assert trade["strike"] == 150.0
    assert trade["expiry"] == "2024-03-15"
    # The fields that were already published, unchanged.
    assert trade["contract_symbol"] == _AAPL_CALL_150
    assert trade["underlying_symbol"] == "AAPL"
    assert trade["multiplier"] == 100


def test_the_expiry_is_published_as_a_string_not_a_date_object(long_call_round_trip):
    """The trade blob is JSON-serialised into the results column; a ``date`` there
    is a TypeError at persist time, discovered only when a run finishes."""
    import json

    trade = long_call_round_trip.get_round_trip_trades()[0]
    assert isinstance(trade["expiry"], str)
    json.dumps({k: v for k, v in trade.items()
                if k not in ("entry_time", "exit_time")})   # datetimes handled elsewhere


def test_the_option_type_is_published_as_its_value_not_the_enum(long_call_round_trip):
    """Same reason: ``OptionRight.CALL`` does not survive ``json.dumps``, and the
    frontend compares against the lower-case string."""
    trade = long_call_round_trip.get_round_trip_trades()[0]
    assert trade["option_type"] == "call"
    assert not hasattr(trade["option_type"], "value")


def test_an_equity_round_trip_carries_no_option_terms(tmp_path):
    """The discriminator the UI keys on. An equity row must report None, never a
    fabricated type or a zero strike."""
    from app.services.backtest.backtest_db import backtest_trading_db, seed_account_definition
    from app.services.backtest.seam_wiring import wire_backtest_seams
    from app.services.backtest.backtest_account import BacktestAccount
    from ba2_common.core.models import TradingOrder
    from ba2_common.core.types import OrderStatus, OrderType

    wire_backtest_seams()
    ps = _make_ps("AAPL", [_ubar(datetime(2024, 3, 5), 150),
                           _ubar(datetime(2024, 3, 6), 152),
                           _ubar(datetime(2024, 3, 7), 156)], datetime(2024, 3, 5))
    ctx = backtest_trading_db("rt-terms-equity")
    ctx.__enter__()
    try:
        seed_account_definition(1, CFG)
        acct = BacktestAccount(1, ps, CFG)
        wire_backtest_seams().register_account(1, acct)

        acct.submit_order(TradingOrder(
            account_id=1, symbol="AAPL", quantity=10, side=OrderDirection.BUY,
            order_type=OrderType.MARKET, status=OrderStatus.NEW))
        ps.set_clock(datetime(2024, 3, 6))
        acct.refresh_orders()
        acct.refresh_transactions()
        acct.submit_order(TradingOrder(
            account_id=1, symbol="AAPL", quantity=10, side=OrderDirection.SELL,
            order_type=OrderType.MARKET, status=OrderStatus.NEW))
        ps.set_clock(datetime(2024, 3, 7))
        acct.refresh_orders()
        acct.refresh_transactions()

        trades = acct.get_round_trip_trades()
        assert trades, "the equity round trip should have been recorded"
        trade = trades[0]
        assert trade["option_type"] is None
        assert trade["strike"] is None
        assert trade["expiry"] is None
        assert trade["contract_symbol"] is None
        assert trade["multiplier"] == 1
    finally:
        ctx.__exit__(None, None, None)
