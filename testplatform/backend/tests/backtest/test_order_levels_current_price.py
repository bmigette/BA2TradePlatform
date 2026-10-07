"""Every price-anchored order parameter is built from the CURRENT price (owner rule, 2026-10-07).

Row 1088 (FMPEarningsDrift) lost 94% of its phantom profit once the take-profit stopped being
anchored on the decision day's own daily CLOSE: on a rally day the TP sat far above the 09:35 entry,
on a down day it fell back to the 2% floor, so the exit level was selected by the future.  These tests
pin the mechanism in miniature and the guard that stops the class of bug from returning.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from ba2_common.core.knowability import (
    DecisionPrice, StaleAnchorPrice, intraday_decisions, require_decision_price)
from app.services.backtest.daily_engine import _BacktestProviderBundle
from app.services.backtest.price_source import AsOfPriceSource


def _wall(y, m, d, hh, mm):
    return datetime(y, m, d, hh, mm, tzinfo=timezone.utc)


def _session(d, close_of_day, open_=100.0):
    """A session that opens at ``open_`` and drifts linearly to ``close_of_day``: 78 five-minute bars."""
    rows, t = [], datetime(d.year, d.month, d.day, 9, 30)
    n = 78
    for i in range(n):
        px = open_ + (close_of_day - open_) * (i + 1) / n
        rows.append({"Date": t, "Open": px, "High": px + 0.01, "Low": px - 0.01, "Close": px,
                     "Volume": 1000})
        t += timedelta(minutes=5)
    return rows


def _ps(day_close, interval="5min"):
    ps = AsOfPriceSource(ohlcv_provider=None, interval=interval)
    ps.load_bars("X", _session(date(2024, 1, 2), 100.0) + _session(date(2024, 1, 3), day_close))
    return ps


# --------------------------------------------------------------------------- (a) the mechanism
@pytest.mark.parametrize("day_close", [120.0, 110.0, 100.0, 95.0, 80.0])
def test_tp_distance_from_the_fill_is_independent_of_the_days_later_close(day_close):
    """A day that rallies 20% (or falls 20%) after the open: the TP is within the configured percent
    of the DECISION price, identically on up days and down days."""
    epp = 4.0
    ps = _ps(day_close)
    T = _wall(2024, 1, 3, 10, 0)
    ps.set_clock(T)
    bundle = _BacktestProviderBundle(lambda *a, **k: None, ps)
    with intraday_decisions(True):
        price_at_date = bundle.price_at_date("X", T)
        tp = price_at_date * (1 + epp / 100)           # EXPERT_TARGET_PRICE: price_at_date x (1 + epp)
    fill = ps.next_bar("X", T)["open"]
    assert price_at_date == ps.decision_price("X", T)
    # the decision price is the last ENDED bar's close: it never saw the day's close
    assert price_at_date == pytest.approx(100.0 + (day_close - 100.0) * 6 / 78)
    dist = (tp - fill) / fill * 100
    # the fill is one bar after the decision bar: two bars of drift from the decision price
    per_bar_pct = abs(day_close - 100.0) / 78
    assert abs(dist - epp) <= 3 * per_bar_pct + 1e-9   # vs ~20 points if anchored on the close


def test_the_decision_price_carries_no_knowledge_of_the_days_close():
    T_open = _wall(2024, 1, 3, 9, 30)          # nothing of today has ended: the prior session's last close
    assert _ps(130.0).decision_price("X", T_open) == _ps(70.0).decision_price("X", T_open) == 100.0


# --------------------------------------------------------------------------- (c) the guard
def test_decision_price_carries_its_bar_stamp():
    ps = _ps(110.0)
    T = _wall(2024, 1, 3, 10, 0)
    px = ps.decision_price("X", T)
    assert isinstance(px, DecisionPrice)
    assert px.stamp == datetime(2024, 1, 3, 9, 55)        # the latest bar that has ENDED at 10:00
    assert px.as_of == T


def test_the_account_hands_out_the_marked_price():
    from app.services.backtest.backtest_account import BacktestAccount
    from tests.backtest.test_max_loss_stop_engine import CFG

    ps = _ps(110.0)
    T = _wall(2024, 1, 3, 10, 0)
    ps.set_clock(T)
    px = BacktestAccount(997, ps, CFG).get_instrument_current_price("X")
    assert isinstance(px, DecisionPrice)


def test_guard_refuses_a_plain_float_on_the_intraday_clock():
    with intraday_decisions(True):
        with pytest.raises(StaleAnchorPrice):
            require_decision_price(101.5, what="TP", symbol="X")           # e.g. a daily close


def test_guard_refuses_a_daily_stamped_price_on_the_intraday_clock():
    with intraday_decisions(True):
        with pytest.raises(StaleAnchorPrice):
            require_decision_price(DecisionPrice(100.0, datetime(2024, 1, 3), None), what="TP")


def test_guard_refuses_an_anchor_later_than_the_decision():
    with intraday_decisions(True):
        with pytest.raises(StaleAnchorPrice):
            require_decision_price(
                DecisionPrice(100.0, datetime(2024, 1, 3, 10, 5), _wall(2024, 1, 3, 10, 0)), what="TP")


def test_guard_accepts_the_decision_price():
    ps = _ps(110.0)
    px = ps.decision_price("X", _wall(2024, 1, 3, 10, 0))
    with intraday_decisions(True):
        assert require_decision_price(px, what="TP") is px


def test_guard_never_fires_off_the_intraday_clock():
    """Live and the DAILY clock: a plain float (a daily close IS the current price there)."""
    assert require_decision_price(101.5, what="TP") == 101.5
    with intraday_decisions(False):
        assert require_decision_price(101.5, what="TP") == 101.5


def test_an_intraday_engine_run_sizes_and_stops_on_the_decision_price(monkeypatch):
    """End to end on the 5-minute clock: a BUY recommendation is sized by the risk manager and gets
    its safeguard stop; every guarded builder accepts what the engine hands it (no StaleAnchorPrice)
    and the order is placed."""
    from ba2_common.core.types import OrderRecommendation, Recommendation
    from app.services.backtest.backtest_account import BacktestAccount
    from tests.backtest import test_intraday_daily_knowability as k

    calls = []

    def analyze_as_of(self, as_of, context):
        px = context.providers.price_at_date(k.SYMBOL, as_of)
        calls.append((as_of, px))
        return Recommendation(signal=OrderRecommendation.BUY, confidence=80.0, current_price=px,
                              details="buy", expected_profit_percent=4.0)

    monkeypatch.setattr(k._ProbeExpert, "analyze_as_of", analyze_as_of)
    orig_save = k._ProbeExpert.save_settings
    monkeypatch.setattr(k._ProbeExpert, "save_settings", lambda self, s: orig_save(self, {
        **s, "sizing_mode": ("risk_atr", "str"), "risk_per_trade_pct": (8.0, "float"),
        "min_stop_loss_pct": (8.0, "float"), "use_atr_stop": (False, "bool")}))
    from ba2_common.core import TradeRiskManagement as trm, position_sizing as psz
    guarded = []
    for mod in (trm, psz):
        orig_guard = mod.require_decision_price

        def counting(price, _orig=orig_guard, **kw):
            guarded.append((kw.get("what"), type(price).__name__))
            return _orig(price, **kw)

        monkeypatch.setattr(mod, "require_decision_price", counting)
    placed = []
    real = BacktestAccount.submit_order

    def spy(self, trading_order, *a, **kw):
        placed.append((trading_order.symbol, trading_order.quantity, trading_order.stop_price))
        return real(self, trading_order, *a, **kw)

    monkeypatch.setattr(BacktestAccount, "submit_order", spy)
    k._run("5min", 996, times=["09:40"])
    assert guarded, f"the guarded sizing path was not exercised (placed={placed}, analysed={calls})"
    assert {t for _w, t in guarded} == {"DecisionPrice"}, guarded
    assert placed, f"no order reached the account (guards ran: {guarded})"
    assert all(q and q > 0 for _s, q, _sl in placed)


def _mc_resolver():
    from ba2_common.core.market_calendar import regular_sessions_ending_at
    from tests.backtest.test_market_condition_bt_context import _reader, _source
    from app.services.backtest.market_condition_bt import BacktestMarketConditionResolver
    daily = _source({"AAA": regular_sessions_ending_at(date(2025, 7, 31), 200)})
    return BacktestMarketConditionResolver(_reader(daily))


def test_market_condition_gate_reads_the_last_finished_session_on_an_intraday_clock():
    """The gate reads the daily row of ``prior_session`` (close, high, low, ATR ...). On an intraday
    clock the calendar date of the clock IS the decision session, whose row is unknown until its
    close: a 10:00 decision on Tue 2025-06-03 must read Mon 06-02's row, never 06-03's."""
    from types import SimpleNamespace
    resolver = _mc_resolver()
    intr = AsOfPriceSource(ohlcv_provider=None, interval="5min")
    intr.set_clock(_wall(2025, 6, 3, 10, 0))
    ctx = resolver(SimpleNamespace(_as_of_date=lambda: date(2025, 6, 3), _price=intr), "AAA", None)
    assert ctx.prior_session == date(2025, 6, 2)
    assert ctx.session_label == date(2025, 6, 3)
    intr.set_clock(_wall(2025, 6, 4, 10, 0))                       # a new session -> a new context
    ctx2 = resolver(SimpleNamespace(_as_of_date=lambda: date(2025, 6, 4), _price=intr), "AAA", None)
    assert ctx2.prior_session == date(2025, 6, 3)


def test_market_condition_gate_is_unchanged_on_the_daily_clock():
    from types import SimpleNamespace
    daily = AsOfPriceSource(ohlcv_provider=None, interval="1d")
    ctx = _mc_resolver()(SimpleNamespace(_as_of_date=lambda: date(2025, 6, 3), _price=daily), "AAA", None)
    assert (ctx.prior_session, ctx.session_label) == (date(2025, 6, 3), date(2025, 6, 4))


def test_the_daily_clock_price_is_unchanged_and_unmarked():
    ps = AsOfPriceSource(ohlcv_provider=None, interval="1d")
    ps.load_bars("X", [{"Date": datetime(2024, 1, 2), "Open": 1.0, "High": 2.0, "Low": 0.5, "Close": 1.5,
                        "Volume": 1}])
    T = datetime(2024, 1, 2, tzinfo=timezone.utc)
    ps.set_clock(T)
    px = ps.decision_price("X", T)
    assert px == 1.5 and not isinstance(px, DecisionPrice)
