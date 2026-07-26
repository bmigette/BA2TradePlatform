"""Gap-through fills: a stop that the market jumped past does NOT fill at the stop (2026-07-26).

Before this, ``_evaluate_fill`` tested only ``bar.low <= stop`` and filled at ``stop`` — which
assumed you always received your stop price no matter how far the market gapped past it. That
understates losses in exactly the situation where real stops fail worst: overnight and earnings
gaps. A triggered stop is a MARKET order; it executes at the open.

The favourable direction ships with it: a limit fills at its price OR BETTER, so a bar that
OPENED beyond the limit fills at the open. Modelling only the adverse side would swap one bias
for another.
"""
import pytest
from types import SimpleNamespace

from app.services.backtest.backtest_account import BacktestAccount
from ba2_common.core.types import OrderDirection


def _acct(**cfg):
    base = {"starting_cash": 100_000.0, "commission_per_trade": 0.0, "slippage_bps": 0.0,
            "fill_model": "next_bar_open"}
    base.update(cfg)
    return BacktestAccount(id=1, price_source=SimpleNamespace(), settings=base)


def _bar(o, h, l, c):
    return {"open": o, "high": h, "low": l, "close": c}


# --------------------------------------------------------------------------- #
# stops: the adverse gap, which is the whole point
# --------------------------------------------------------------------------- #
def test_sell_stop_gapping_down_fills_at_the_open_not_the_stop():
    """Long with a stop at 100; the bar OPENS at 90 and never trades back up to 100. Filling at
    100 would book a loss the market never offered."""
    a = _acct()
    assert a._gap_stop_fill(100.0, _bar(90.0, 95.0, 88.0, 92.0), is_sell=True) == 90.0


def test_buy_stop_gapping_up_fills_at_the_open_not_the_stop():
    """Mirror for a short: stop at 100, bar opens at 112."""
    a = _acct()
    assert a._gap_stop_fill(100.0, _bar(112.0, 115.0, 108.0, 110.0), is_sell=False) == 112.0


def test_stop_touched_intrabar_still_fills_at_the_stop():
    """The ordinary case must be unchanged: the bar opens on the safe side and only later
    trades through the stop."""
    a = _acct()
    assert a._gap_stop_fill(100.0, _bar(105.0, 106.0, 98.0, 99.0), is_sell=True) == 100.0
    assert a._gap_stop_fill(100.0, _bar(95.0, 103.0, 94.0, 102.0), is_sell=False) == 100.0


def test_stop_gap_never_returns_a_price_better_than_the_stop():
    """A gap can only hurt a stop. An open on the favourable side must not improve the fill."""
    a = _acct()
    assert a._gap_stop_fill(100.0, _bar(120.0, 121.0, 99.0, 100.0), is_sell=True) == 100.0
    assert a._gap_stop_fill(100.0, _bar(80.0, 101.0, 79.0, 100.0), is_sell=False) == 100.0


# --------------------------------------------------------------------------- #
# limits: the favourable gap
# --------------------------------------------------------------------------- #
def test_sell_limit_gapping_up_fills_at_the_better_open():
    """A limit fills at its price OR BETTER: TP at 110, bar opens at 120."""
    a = _acct()
    assert a._gap_limit_fill(110.0, _bar(120.0, 122.0, 118.0, 121.0), is_sell=True) == 120.0


def test_buy_limit_gapping_down_fills_at_the_better_open():
    a = _acct()
    assert a._gap_limit_fill(110.0, _bar(100.0, 104.0, 99.0, 102.0), is_sell=False) == 100.0


def test_limit_gap_never_returns_a_price_worse_than_the_limit():
    a = _acct()
    assert a._gap_limit_fill(110.0, _bar(105.0, 112.0, 104.0, 111.0), is_sell=True) == 110.0
    assert a._gap_limit_fill(110.0, _bar(115.0, 116.0, 109.0, 110.0), is_sell=False) == 110.0


# --------------------------------------------------------------------------- #
# robustness
# --------------------------------------------------------------------------- #
def test_a_bar_without_an_open_falls_back_to_the_level():
    """Previous behaviour is the fallback — never fabricate a gap from a missing field."""
    a = _acct()
    assert a._gap_stop_fill(100.0, {"high": 105.0, "low": 90.0}, is_sell=True) == 100.0
    assert a._gap_limit_fill(110.0, {"high": 125.0, "low": 109.0}, is_sell=False) == 110.0


# --------------------------------------------------------------------------- #
# end-to-end through _evaluate_fill, including slippage ordering
# --------------------------------------------------------------------------- #
def _order(order_type, **kw):
    from ba2_common.core.types import OrderType
    o = SimpleNamespace(order_type=getattr(OrderType, order_type), limit_price=None,
                        stop_price=None, side=OrderDirection.SELL, symbol="X", quantity=10)
    for k, v in kw.items():
        setattr(o, k, v)
    return o


def test_evaluate_fill_applies_slippage_to_the_GAPPED_stop_price(monkeypatch):
    """Slippage must be applied to the price actually executed at (the open), not to the stop —
    a stop is a market order once triggered, so both effects compound."""
    a = _acct(slippage_bps=100.0)   # 1%
    bar = _bar(90.0, 95.0, 88.0, 92.0)
    monkeypatch.setattr(a, "_bar_for_fill", lambda o, t: bar)
    px = a._evaluate_fill(_order("SELL_STOP", stop_price=100.0), None)
    assert px == pytest.approx(90.0 * 0.99), "expected slippage on the 90.0 gap open, not on 100.0"


def test_evaluate_fill_sell_limit_takes_the_favourable_open(monkeypatch):
    a = _acct()
    bar = _bar(120.0, 122.0, 118.0, 121.0)
    monkeypatch.setattr(a, "_bar_for_fill", lambda o, t: bar)
    assert a._evaluate_fill(_order("SELL_LIMIT", limit_price=110.0), None) == 120.0


def test_oco_gap_down_through_the_stop_uses_the_open(monkeypatch):
    """The OCO path is the one real strategies use for TP/SL, so it must not be forgotten."""
    a = _acct()
    bar = _bar(90.0, 95.0, 88.0, 92.0)
    o = _order("OCO", limit_price=130.0, stop_price=100.0, side=OrderDirection.SELL)
    assert a._evaluate_oco_fill(o, bar) == 90.0


# --------------------------------------------------------------------------- #
# assume_stop_fills_at_price: the explicit opt-out
# --------------------------------------------------------------------------- #
def test_assume_stop_fills_at_price_ignores_the_gap():
    """Opt-in setting for 'the broker always gives me my stop'. True only of a stop-LIMIT;
    documented as hiding gap risk."""
    a = _acct(assume_stop_fills_at_price=True)
    assert a._gap_stop_fill(100.0, _bar(90.0, 95.0, 88.0, 92.0), is_sell=True) == 100.0
    assert a._gap_stop_fill(100.0, _bar(112.0, 115.0, 108.0, 110.0), is_sell=False) == 100.0


def test_the_flag_does_NOT_affect_limits():
    """A limit fills at its price or better regardless -- that half is not in dispute, so the
    flag must not accidentally clamp a favourable limit gap."""
    a = _acct(assume_stop_fills_at_price=True)
    assert a._gap_limit_fill(110.0, _bar(120.0, 122.0, 118.0, 121.0), is_sell=True) == 120.0


def test_default_is_the_accurate_gap_aware_behaviour():
    a = _acct()
    assert a._gap_stop_fill(100.0, _bar(90.0, 95.0, 88.0, 92.0), is_sell=True) == 90.0
