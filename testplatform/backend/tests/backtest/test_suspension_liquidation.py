"""Exchange suspension / delisting (Alpaca history cache): a held position is CLOSED at the last exchange close, working orders
are cancelled, nothing is entered while suspended, re-entry is possible from the resume session, and an account without events is
untouched (ba2_common.core.suspension_events is the one pure rule; BacktestAccount.liquidate_suspended_positions applies it).

Run from the backend dir:
    ./venv/bin/python -m pytest tests/backtest/test_suspension_liquidation.py -q
"""
from __future__ import annotations

from datetime import date, datetime

import pytest

from ba2_common.core.models import TradingOrder
from ba2_common.core.types import OrderDirection, OrderStatus, OrderType

CFG = {"starting_cash": 100_000.0, "commission_per_trade": 0.0, "slippage_bps": 0.0, "fill_model": "next_bar_open"}

D1, D2, D3 = datetime(2024, 1, 2), datetime(2024, 1, 3), datetime(2024, 1, 4)          # D3 = the last real session
AFTER = datetime(2024, 1, 5)                                                            # first clock tick after it (other symbols' bar)
RESUME, RESUME2 = datetime(2024, 3, 1), datetime(2024, 3, 4)
# NESR-like: 100 -> bars stop after D3 (close 150) -> relists at 400 on RESUME
ROWS = [(D1, 100, 101, 99, 100), (D2, 100, 110, 99, 105), (D3, 140, 151, 139, 150), (RESUME, 400, 410, 395, 400), (RESUME2, 405, 410, 400, 402)]
EVENTS = {"AAPL": ((date(2024, 1, 4), date(2024, 3, 1)),)}
GONE = {"AAPL": ((date(2024, 1, 4), None),)}


def _acct(events=None, rows=ROWS):
    from app.services.backtest.backtest_account import BacktestAccount
    from app.services.backtest.backtest_db import backtest_trading_db, seed_account_definition
    from app.services.backtest.price_source import AsOfPriceSource
    from app.services.backtest.seam_wiring import wire_backtest_seams

    wire_backtest_seams()
    ctx = backtest_trading_db("suspension")
    ctx.__enter__()
    seed_account_definition(1, CFG)
    ps = AsOfPriceSource(ohlcv_provider=None)
    ps.load_bars("AAPL", [{"Date": d, "Open": o, "High": h, "Low": lo, "Close": c, "Volume": 1000} for d, o, h, lo, c in rows])
    acct = BacktestAccount(1, ps, CFG)
    if events is not None:
        acct.set_suspension_events(events)
    wire_backtest_seams().register_account(1, acct)
    return acct, ctx, ps


def _order(kind, qty, side, **kw):
    return TradingOrder(account_id=1, symbol="AAPL", quantity=qty, side=side, order_type=kind, status=OrderStatus.NEW,
                        comment="t", **kw)


def _hold(acct, ps, qty=10):
    """Buy ``qty`` AAPL: market at D1, fills at D2's open (100)."""
    ps.set_clock(D1)
    acct.submit_order(_order(OrderType.MARKET, qty, OrderDirection.BUY))
    acct.refresh_orders()
    acct.refresh_transactions()
    assert acct.get_positions()[0]["qty"] == qty


def test_held_position_is_closed_at_the_last_exchange_close_on_the_first_bar_after():
    acct, ctx, ps = _acct(EVENTS)
    try:
        _hold(acct, ps)
        cash_after_entry = acct.get_balance()
        ps.set_clock(D3)
        assert acct.liquidate_suspended_positions(D3) is False        # the last real session is still tradable
        ps.set_clock(AFTER)
        assert acct.liquidate_suspended_positions(AFTER) is True
        acct.refresh_transactions()
        assert acct.get_positions() == []
        assert acct.get_balance() == pytest.approx(cash_after_entry + 10 * 150.0)    # the D3 close, not a relisted 400
        (trade,) = acct.get_round_trip_trades()
        assert trade["exit_reason"] == "suspension_liquidation" and trade["pnl"] == pytest.approx(10 * (150.0 - 100.0))
        assert acct.liquidate_suspended_positions(AFTER) is False     # idempotent: nothing left
    finally:
        ctx.__exit__(None, None, None)


def test_a_delisted_symbol_never_comes_back_and_is_labelled_so():
    acct, ctx, ps = _acct(GONE, rows=ROWS[:3])         # a delisted symbol has no bar after D3
    try:
        _hold(acct, ps)
        ps.set_clock(RESUME2)                                          # long after: still closed at the D3 close
        assert acct.liquidate_suspended_positions(RESUME2) is True
        acct.refresh_transactions()
        (trade,) = acct.get_round_trip_trades()
        assert trade["exit_reason"] == "delisting_liquidation" and trade["exit_price"] == pytest.approx(150.0)
    finally:
        ctx.__exit__(None, None, None)


def test_working_orders_of_a_suspended_symbol_are_cancelled_even_without_a_position():
    acct, ctx, ps = _acct(EVENTS)
    try:
        ps.set_clock(D3)
        resting = _order(OrderType.BUY_LIMIT, 5, OrderDirection.BUY, limit_price=50.0)
        market = _order(OrderType.MARKET, 5, OrderDirection.BUY)
        acct.submit_order(resting)
        acct.submit_order(market)                                      # would fill at the relisted open otherwise
        ps.set_clock(AFTER)
        assert acct.liquidate_suspended_positions(AFTER) is True
        assert acct.get_order(resting.broker_order_id).status == OrderStatus.CANCELED
        assert acct.get_order(market.broker_order_id).status == OrderStatus.CANCELED
        assert acct.get_positions() == []
    finally:
        ctx.__exit__(None, None, None)


def test_resume_session_is_not_a_liquidation_and_re_entry_fills():
    acct, ctx, ps = _acct(EVENTS)
    try:
        _hold(acct, ps)
        ps.set_clock(AFTER)
        acct.liquidate_suspended_positions(AFTER)
        acct.refresh_transactions()
        ps.set_clock(RESUME)
        assert acct.liquidate_suspended_positions(RESUME) is False
        acct.submit_order(_order(OrderType.MARKET, 3, OrderDirection.BUY))
        acct.refresh_orders()                                          # fills at the next bar's open (405)
        assert acct.get_positions()[0]["qty"] == 3
        ps.set_clock(RESUME2)
        assert acct.liquidate_suspended_positions(RESUME2) is False    # holding after the resume is untouched
    finally:
        ctx.__exit__(None, None, None)


def test_no_events_is_a_no_op_that_never_reads_a_price():
    acct, ctx, ps = _acct()                                            # the FMP source / a cache without the file
    try:
        _hold(acct, ps)
        ps.set_clock(AFTER)

        def boom(*a, **k):
            raise AssertionError("price read with no events")
        ps.close_asof_dated = boom
        ps.close_asof = boom
        assert acct.liquidate_suspended_positions(AFTER) is False
        assert acct._positions["AAPL"].qty == 10
    finally:
        ctx.__exit__(None, None, None)


def test_a_table_that_does_not_match_the_cache_raises():
    acct, ctx, ps = _acct({"AAPL": ((date(2024, 1, 3), date(2024, 3, 1)),)})     # claims suspended after D2, but D3 has a bar
    try:
        _hold(acct, ps)
        ps.set_clock(AFTER)
        with pytest.raises(ValueError, match="does not match the cache"):
            acct.liquidate_suspended_positions(AFTER)
    finally:
        ctx.__exit__(None, None, None)


def _engine(acct, ps):
    from app.services.backtest.daily_engine import DailyBacktestEngine
    e = DailyBacktestEngine.__new__(DailyBacktestEngine)
    e.account, e.price, e.config, e._screener_runtime = acct, ps, {"enabled_instruments": ["AAPL"]}, None
    e._suspension_noted = set()
    return e


def test_no_entry_universe_while_suspended_but_the_resume_session_is_decidable():
    acct, ctx, ps = _acct(EVENTS)
    try:
        e = _engine(acct, ps)
        for clock in (D3, RESUME, RESUME2):
            ps.set_clock(clock)
            assert e._bar_universes(clock)[0] == ["AAPL"]
        # a tick inside the suspension on which the price source still answers a (stale) bar: not decidable
        ps.set_clock(AFTER)
        ps.bar_at = lambda s, t=None: {"close": 150.0}
        assert e._bar_universes(AFTER)[0] == [] and e._suspension_noted == {"AAPL"}
    finally:
        ctx.__exit__(None, None, None)


def test_engine_settlement_pass_liquidates_and_rolls_the_transaction():
    acct, ctx, ps = _acct(EVENTS)
    try:
        _hold(acct, ps)
        e = _engine(acct, ps)
        ps.set_clock(AFTER)
        e._fills_and_settlements(AFTER)
        assert acct.get_positions() == []
        from ba2_common.core.trade_store import transactions_where
        from ba2_common.core.types import TransactionStatus
        assert transactions_where(status=TransactionStatus.OPENED, symbol="AAPL") == []     # rolled the same bar
    finally:
        ctx.__exit__(None, None, None)


def test_handler_reads_events_for_alpaca_only(tmp_path, monkeypatch):
    import json
    from app.services.backtest import daily_backtest_handler as h
    (tmp_path / "_meta").mkdir()
    (tmp_path / "_meta" / "suspensions.json").write_text(json.dumps(
        {"build": "R built t", "rule": {}, "symbols": {"X": [["2024-01-04", None, "delisted", 9, None]]}}))
    from app.services.backtest import history_source
    monkeypatch.setattr(history_source, "job_history_cache_dir", lambda cfg: str(tmp_path))
    assert h._suspension_events_for({"ohlcv_history_source": "fmp"}) == {}
    assert h._suspension_events_for({"ohlcv_history_source": "alpaca"}) == {"X": ((date(2024, 1, 4), None),)}
    (tmp_path / "_meta" / "suspensions.json").unlink()
    assert h._suspension_events_for({"ohlcv_history_source": "alpaca"}) == {}
