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


def _build(tmp_path, strikes, spot_late, late_closes=None):
    """``late_closes``: (close_k1, close_k2, close_k3, volumes) premium prints on ``_LATER_BAR``."""
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
        for occ, px in ((_occ(k1), 11.0), (_occ(k2), 2.0), (_occ(k3), 0.5))]
        + ([{"occ_symbol": _occ(k), "date": "2024-03-08", "open": px, "high": px, "low": px,
             "close": px, "volume": vol, "underlying": "AAPL", "option_type": "call",
             "strike": float(k), "expiry": "2024-03-15"}
            for k, px, vol in zip((k1, k2, k3), late_closes[:3], late_closes[3])]
           if late_closes else []))
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


# --------------------------------------------------------------------------- held legs / loud fallbacks
def test_the_mark_clamp_uses_the_legs_still_held(unbalanced_fly):
    """One leg left from the fly (a long 100 call) is worth [0, +inf), not the whole structure's
    [-2000, 1000]; the whole structure is unchanged."""
    import math
    acct, _ = unbalanced_fly
    lots = [l for l in acct._option_positions.values() if l.qty != 0]
    (gkey,) = set(acct._option_group_bounds()[0].values())
    assert acct._held_value_bounds(gkey, lots) == (-2000.0, 1000.0)
    one = [l for l in lots if l.contract_symbol == _occ(100)]
    assert acct._held_value_bounds(gkey, one) == (0.0, math.inf)


def test_settling_a_vertical_remainder_is_not_capped_at_the_old_width(unbalanced_fly):
    """With the short body closed early, the legs left (long 100 and long 140 calls) pay
    50 + 10 = $60 a share at spot 150 = $6,000. The old ``+-width`` expiry clamp bound that at
    the 100/140 gap ($4,000); a long call is not capped by a strike gap, so the real payoff
    now stands."""
    acct, ps = unbalanced_fly
    held = [p for p in acct.get_option_positions() if p.contract_symbol != _occ(110)]
    assert len(held) == 2
    ps.set_clock(_EXPIRY_BAR)
    cash_before = acct._cash
    assert acct.settle_defined_risk_combo_expiry(held, 150.0) is True
    assert acct._cash - cash_before == pytest.approx(6_000.0)


def test_underivable_bounds_fall_back_loudly_and_are_recorded(unbalanced_fly, monkeypatch):
    import app.services.backtest.backtest_account as bt

    acct, _ = unbalanced_fly

    def boom(legs):
        raise ValueError("one bad leg")

    monkeypatch.setattr(bt, "position_value_bounds", boom)
    acct._group_bounds_memo = None
    acct._held_bounds_memo = {}
    acct._option_group_bounds()                  # does not raise: one bad leg cannot abort the mark
    assert acct._option_positions_mtm() is not None
    stats = acct.option_integrity_stats()
    assert stats["option_clamp_fallbacks"] >= 2
    assert any("one bad leg" in e for e in stats["option_clamp_fallback_examples"])


def _fly_with_late_prints(tmp_path, closes, vols=(100, 100, 100)):
    return _build(tmp_path, (100, 110, 120), spot_late=110, late_closes=(*closes, vols))


def test_a_consistent_set_of_prints_is_marked_as_printed(tmp_path):
    """11 / 4 / 0.5 satisfies monotone, width and convexity (4 <= 0.5*11 + 0.5*0.5): untouched."""
    ctx, acct, ps = _fly_with_late_prints(tmp_path, (11.0, 4.0, 0.5))
    try:
        ps.set_clock(_LATER_BAR)
        assert acct._option_positions_mtm() == pytest.approx((11.0 - 8.0 + 0.5) * 100.0)
        assert acct.option_integrity_stats()["option_mark_prints_corrected"] == 0
    finally:
        ctx.__exit__(None, None, None)


def test_a_print_above_the_convexity_bound_is_replaced_through_the_fallback_chain(tmp_path):
    """The 110 call prints 7.0 against 11.0 / 0.5 neighbours: above the 5.75 chord. The middle
    strike is marked through the existing fallback (intrinsic here: 0.0 at spot 110), not the
    junk print, and the leg-day is counted and recorded."""
    ctx, acct, ps = _fly_with_late_prints(tmp_path, (11.0, 7.0, 0.5))
    try:
        ps.set_clock(_LATER_BAR)
        # corrected: 11.0 - 2*0.0 + 0.5 = 11.5 -> 1150, clamped to the fly's 1000 maximum
        assert acct._option_positions_mtm() == pytest.approx(1_000.0)
        stats = acct.option_integrity_stats()
        assert stats["option_mark_prints_corrected"] == 1
        assert any("break convexity" in e for e in stats["option_mark_print_examples"])
        acct._option_positions_mtm()                      # re-reading the mark does not recount
        assert acct.option_integrity_stats()["option_mark_prints_corrected"] == 1
    finally:
        ctx.__exit__(None, None, None)


def test_a_width_violation_blames_the_lower_volume_leg(tmp_path):
    """110 prints 13.0 against 100 at 11.0: a 110 call cannot be worth more than the 100 call.
    The 110 call has the lower bar volume, so IT is replaced, not the 100 call."""
    ctx, acct, ps = _fly_with_late_prints(tmp_path, (11.0, 13.0, 0.5), vols=(500, 5, 500))
    try:
        ps.set_clock(_LATER_BAR)
        acct._option_positions_mtm()
        ex = acct.option_integrity_stats()["option_mark_print_examples"]
        assert len(ex) >= 1 and all(_occ(110) in e for e in ex)
    finally:
        ctx.__exit__(None, None, None)


def test_a_cancelled_child_leg_does_not_push_a_group_into_the_fallback(unbalanced_fly):
    from ba2_common.core.db import add_instance
    from ba2_common.core.models import TradingOrder
    from ba2_common.core.types import OrderStatus
    acct, _ = unbalanced_fly
    leg = next(o for o in acct.get_orders() if o.parent_order_id and o.contract_symbol == _occ(110))
    clone = TradingOrder(**{k: getattr(leg, k) for k in (
        "account_id", "symbol", "underlying_symbol", "quantity", "side", "order_type",
        "asset_class", "multiplier", "contract_symbol", "option_type", "strike", "expiry",
        "parent_order_id")}, status=OrderStatus.CANCELED)
    add_instance(clone)
    acct.invalidate_order_cache()
    acct._group_bounds_memo = None
    _, gb = acct._option_group_bounds()
    (bounds,) = gb.values()
    assert (bounds["lo"], bounds["hi"]) == (-2000.0, 1000.0)
    assert acct.option_integrity_stats()["option_clamp_fallbacks"] == 0
