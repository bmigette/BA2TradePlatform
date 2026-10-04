"""The daily mark clamp and the expiry clamp of a call butterfly use the structure's TRUE value
range, not ``[0, width]`` / ``+-width``.

A 1-2-1 call fly whose UPPER wing is wider than its lower one (100/110/130) is worth
``(110-100) - (130-110) = -10`` a share above 130: a real liability. ``[0, width]`` floored the
daily mark at zero (148 group-days on the O_BF optimization's genome A) and so hid the loss until
the expiry, where the cash moved.
"""
from __future__ import annotations

from datetime import date, datetime

import pytest

from ba2_common.core.types import OptionRight, OrderDirection
from tests.backtest._spread_cfg import LEGACY_ZERO_SPREAD as _LEGACY_ZERO_SPREAD

CFG = {
    **_LEGACY_ZERO_SPREAD, "starting_cash": 50_000.0,
    "commission_per_trade": 0.0, "slippage_bps": 0.0, "fill_model": "next_bar_open",
}
_ENTRY_BAR = datetime(2024, 3, 5)
_FILL_BAR = datetime(2024, 3, 6)
_LATER_BAR = datetime(2024, 3, 8)
_EXPIRY_BAR = datetime(2024, 3, 15)


def _occ(k):
    return f"AAPL240315C{int(k * 1000):08d}"


def _build(tmp_path, strikes, spot_late):
    from app.services.backtest.backtest_db import backtest_trading_db, seed_account_definition
    from app.services.backtest.seam_wiring import wire_backtest_seams
    from app.services.backtest.backtest_account import BacktestAccount
    from app.services.backtest.options_cache import OptionsHistoryCache
    from app.services.backtest.options_provider import HistoricalOptionsProvider
    from app.services.backtest.price_source import AsOfPriceSource
    from ba2_common.core.option_types import OptionLeg

    k1, k2, k3 = strikes
    terms = {_occ(k1): k1, _occ(k2): k2, _occ(k3): k3}
    cache_db = str(tmp_path / "fly.sqlite")
    cache = OptionsHistoryCache(cache_db)
    cache.write_chain_rows("AAPL", "2024-03-01", [
        {"occ_symbol": occ, "option_type": "call", "strike": k, "expiry": "2024-03-15",
         "bid": 1.0, "ask": 1.2, "last": 1.1, "iv": 0.25} for occ, k in terms.items()])
    cache.write_bar_rows([
        {"occ_symbol": occ, "date": "2024-03-06", "open": px, "high": px, "low": px, "close": px,
         "volume": 500, "underlying": "AAPL", "option_type": "call", "strike": terms[occ],
         "expiry": "2024-03-15"}
        for occ, px in ((_occ(k1), 11.0), (_occ(k2), 2.0), (_occ(k3), 0.5))])
    bars = [{"Date": d, "Open": 100, "High": 101, "Low": 99, "Close": c, "Volume": 1000}
            for d, c in ((_ENTRY_BAR, 100), (_FILL_BAR, 100), (_LATER_BAR, spot_late),
                         (_EXPIRY_BAR, spot_late))]

    wire_backtest_seams()
    ctx = backtest_trading_db("fly")
    ctx.__enter__()
    seed_account_definition(1, CFG)
    ps = AsOfPriceSource(ohlcv_provider=None)
    ps.load_bars("AAPL", bars)
    ps.set_clock(_ENTRY_BAR)
    acct = BacktestAccount(1, ps, CFG, options_provider=HistoricalOptionsProvider(cache_db))
    wire_backtest_seams().register_account(1, acct)

    def _leg(occ, side, ratio):
        return OptionLeg(contract_symbol=occ, side=side, ratio_qty=ratio,
                         position_intent=("buy_to_open" if side == OrderDirection.BUY
                                          else "sell_to_open"),
                         option_type=OptionRight.CALL, strike=terms[occ],
                         expiry=date(2024, 3, 15), underlying="AAPL")

    acct.submit_option_order(
        legs=[_leg(_occ(k1), OrderDirection.BUY, 1), _leg(_occ(k2), OrderDirection.SELL, 2),
              _leg(_occ(k3), OrderDirection.BUY, 1)],
        quantity=1, order_type="market", option_strategy="call_butterfly")
    acct.refresh_orders()
    acct.refresh_transactions()
    assert len(acct.get_option_positions()) == 3
    return ctx, acct, ps


@pytest.fixture
def unbalanced_fly(tmp_path):
    """100/110/140: upper wing 30 > TWICE the lower wing 10 (the shape where the old symmetric
    expiry bound also clipped it). Spot is 150 from the 8th on, so every leg is
    marked at intrinsic: 50 - 2*40 + 10 = -20 a share = -$2,000 for one structure."""
    ctx, acct, ps = _build(tmp_path, (100, 110, 140), spot_late=150)
    try:
        yield acct, ps
    finally:
        ctx.__exit__(None, None, None)


@pytest.fixture
def balanced_fly(tmp_path):
    ctx, acct, ps = _build(tmp_path, (100, 110, 120), spot_late=150)
    try:
        yield acct, ps
    finally:
        ctx.__exit__(None, None, None)


def test_group_bounds_carry_the_true_value_range(unbalanced_fly):
    acct, _ = unbalanced_fly
    _, gb = acct._option_group_bounds()
    (bounds,) = gb.values()
    assert (bounds["lo"], bounds["hi"]) == (-2000.0, 1000.0)


def test_the_daily_mark_is_not_floored_at_zero(unbalanced_fly):
    """Spot 150, above the top strike: the book is worth -$2,000, not the 0 a [0, width] clamp
    reports."""
    acct, ps = unbalanced_fly
    ps.set_clock(_LATER_BAR)
    assert acct._option_positions_mtm() == pytest.approx(-2_000.0)


def test_a_balanced_fly_mark_is_unchanged(balanced_fly):
    """Above the top strike a balanced fly is worth exactly 0, as before."""
    acct, ps = balanced_fly
    ps.set_clock(_LATER_BAR)
    assert acct._option_positions_mtm() == pytest.approx(0.0)
    _, gb = acct._option_group_bounds()
    (bounds,) = gb.values()
    assert (bounds["lo"], bounds["hi"]) == (0.0, 1000.0)


def test_the_expiry_settlement_books_the_real_liability(unbalanced_fly):
    from app.services.backtest.daily_engine import DailyBacktestEngine
    acct, ps = unbalanced_fly
    engine = DailyBacktestEngine.__new__(DailyBacktestEngine)
    engine.account, engine.price, engine.config = acct, ps, CFG
    cash_before = acct._cash
    ps.set_clock(_EXPIRY_BAR)
    engine._apply_option_expiry(_EXPIRY_BAR)
    assert acct.get_option_positions() == []
    assert acct._cash - cash_before == pytest.approx(-2_000.0)
