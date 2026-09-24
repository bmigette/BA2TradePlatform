"""Pure-signal tests for PullbackReversion (``pullback_signal``).

No DB, providers or expert class: the function takes completed daily bars and settings and
returns the decision for the last bar. The series are hand-built so each test states the
market shape it needs, and every precondition that depends on a platform calculator
(trend slope, swing structure) is asserted against that calculator rather than hard-coded.
"""
import math

import numpy as np
import pandas as pd
import pytest

from ba2_common.core.market_conditions import (
    STATUS_INVALID_PRICES, STATUS_VALID, STRUCTURE_STATE_CODES, WINDOW, compute_chart_structure,
    compute_market_conditions)
from ba2_experts.PullbackReversion import _wilder_rsi, pullback_signal


# ----------------------------------------------------------------------------- builders
def bars_from_returns(returns, start=100.0, first_date="2019-01-01"):
    """Daily OHLCV from per-bar close returns. The open gaps 30% of the move in its direction,
    so consecutive bars never share an extreme (the pivot finder needs strict extremes)."""
    closes, opens = [], []
    prev = start
    for r in returns:
        opens.append(prev * (1 + 0.3 * r))
        prev = prev * (1 + r)
        closes.append(prev)
    o, c = np.array(opens), np.array(closes)
    h = np.maximum(o, c) * 1.002
    lo = np.minimum(o, c) * 0.998
    idx = pd.bdate_range(first_date, periods=len(returns))
    return pd.DataFrame({"Open": o, "High": h, "Low": lo, "Close": c,
                         "Volume": np.full(len(returns), 1e6)}, index=idx)


def zigzag(n, up, down):
    """Alternating up/down returns: a steady trend with a down move on every other bar."""
    return [up if t % 2 == 0 else down for t in range(n)]


def swings(cycles, down_bars, down, up_bars, up):
    """Repeated legs: ``down_bars`` of ``down`` then ``up_bars`` of ``up`` per cycle."""
    return ([down] * down_bars + [up] * up_bars) * cycles


UPTREND = zigzag(260, 0.008, -0.004)
DOWNTREND = zigzag(260, -0.008, 0.004)
DIP = [-0.015] * 3
RALLY = [0.015] * 3


def settings(**over):
    base = {"direction": "long", "trend_gate": "sma200", "rsi_period": 2,
            "entry_threshold": 10.0, "exit_mode": "sma5", "rsi_exit": 70.0}
    base.update(over)
    return base


def window(bars):
    tail = bars.iloc[-WINDOW:]
    return tuple(tail[k].to_numpy(float) for k in ("Open", "High", "Low", "Close", "Volume"))


def closes_vs_smas(bars):
    c = bars["Close"]
    return float(c.iloc[-1]), float(c.iloc[-200:].mean()), float(c.iloc[-5:].mean())


# ----------------------------------------------------------------------------- entries
def test_oversold_dip_in_uptrend_is_a_long_entry():
    bars = bars_from_returns(UPTREND + DIP)
    close, sma200, _ = closes_vs_smas(bars)
    assert close > sma200  # precondition: still an uptrend after the dip
    out = pullback_signal(bars, settings())
    assert out["action"] == "entry"
    assert out["trend_ok"] is True
    assert out["rsi"] < 10.0
    assert out["sma200"] == pytest.approx(sma200)
    assert set(out) == {"action", "rsi", "sma200", "sma5", "trend_ok", "structure_state", "reason"}
    assert out["structure_state"] is None  # not computed outside sma5_or_choch
    assert isinstance(out["reason"], str) and out["reason"]


def test_same_dip_in_downtrend_is_not_a_long_entry():
    bars = bars_from_returns(DOWNTREND + DIP)
    close, sma200, sma5 = closes_vs_smas(bars)
    assert close < sma200 and close < sma5  # precondition: no trend, and no sma5 exit either
    out = pullback_signal(bars, settings())
    assert out["rsi"] < 10.0  # the dip is just as oversold
    assert out["trend_ok"] is False
    assert out["action"] == "none"


def test_overbought_rally_in_downtrend_is_a_short_entry():
    bars = bars_from_returns(DOWNTREND + RALLY)
    close, sma200, _ = closes_vs_smas(bars)
    assert close < sma200
    out = pullback_signal(bars, settings(direction="short"))
    assert out["rsi"] > 90.0
    assert out["trend_ok"] is True
    assert out["action"] == "entry"


def test_overbought_rally_in_uptrend_is_not_a_short_entry():
    bars = bars_from_returns(UPTREND + RALLY)
    out = pullback_signal(bars, settings(direction="short", exit_mode="time"))
    assert out["rsi"] > 90.0
    assert out["trend_ok"] is False
    assert out["action"] == "none"


def test_slope_gate_follows_the_platform_trend_slope():
    up = bars_from_returns(UPTREND + DIP)
    down = bars_from_returns(DOWNTREND + DIP)
    up_slope = compute_market_conditions(*window(up)).trend_slope
    down_slope = compute_market_conditions(*window(down)).trend_slope
    assert up_slope.status == STATUS_VALID and up_slope.value > 0
    assert down_slope.status == STATUS_VALID and down_slope.value < 0
    assert pullback_signal(up, settings(trend_gate="slope_ohlcv_v1"))["action"] == "entry"
    out = pullback_signal(down, settings(trend_gate="slope_ohlcv_v1"))
    assert out["trend_ok"] is False and out["action"] == "none"


def _corrupt_high(bars, back=10):
    bars = bars.copy()
    bars.iloc[-back, bars.columns.get_loc("High")] = bars["Low"].iloc[-back] * 0.9  # high < low
    return bars


def test_slope_gate_on_corrupt_bars_raises():
    bars = _corrupt_high(bars_from_returns(UPTREND + DIP))
    assert compute_market_conditions(*window(bars)).trend_slope.status == STATUS_INVALID_PRICES
    with pytest.raises(ValueError, match="Corrupt bars"):
        pullback_signal(bars, settings(trend_gate="slope_ohlcv_v1"))


def test_slope_gate_with_unmeasurable_slope_is_not_trend_ok(monkeypatch):
    """A non-valid status that is NOT corrupt data (none is reachable with 128 validated bars
    today) reads as "trend not ok", never as passing."""
    import dataclasses

    import ba2_experts.PullbackReversion as mod
    from ba2_common.core.market_conditions import STATUS_INSUFFICIENT_HISTORY, Observation

    real = mod.compute_market_conditions

    def unmeasurable(*arrays):
        return dataclasses.replace(real(*arrays), trend_slope=Observation(
            None, STATUS_INSUFFICIENT_HISTORY, "test"))

    monkeypatch.setattr(mod, "compute_market_conditions", unmeasurable)
    out = pullback_signal(bars_from_returns(UPTREND + DIP), settings(trend_gate="slope_ohlcv_v1"))
    assert out["trend_ok"] is False
    assert out["action"] != "entry"


def test_corrupt_bars_outside_the_calculator_modes_do_not_matter():
    """Only the close enters the SMA gates and exits; the OHLV bar checks belong to the
    calculators, so a corrupt high does not block an sma200/sma5 decision."""
    bars = _corrupt_high(bars_from_returns(UPTREND + DIP))
    assert pullback_signal(bars, settings())["action"] == "entry"


# ----------------------------------------------------------------------------- exits
def test_bounce_above_sma5_after_a_long_entry_is_an_exit():
    entry_bars = bars_from_returns(UPTREND + DIP)
    assert pullback_signal(entry_bars, settings())["action"] == "entry"
    bars = bars_from_returns(UPTREND + DIP + [0.06])
    close, _, sma5 = closes_vs_smas(bars)
    assert close > sma5
    out = pullback_signal(bars, settings())
    assert out["action"] == "exit"
    assert out["sma5"] == pytest.approx(sma5)


def test_time_exit_mode_never_signals_an_exit():
    bars = bars_from_returns(UPTREND + DIP + [0.06])
    out = pullback_signal(bars, settings(exit_mode="time"))
    assert out["action"] == "none"
    # every bar of an uptrend that keeps bouncing: still never an exit
    for k in range(200, len(bars) + 1):
        assert pullback_signal(bars.iloc[:k], settings(exit_mode="time"))["action"] != "exit"


def test_rsi_exit_mode_uses_the_rsi_exit_level():
    bars = bars_from_returns(UPTREND + DIP + [0.06])
    rsi = pullback_signal(bars, settings(exit_mode="rsi"))["rsi"]
    assert 50.0 < rsi < 90.0  # precondition: the threshold below and above it are both legal
    assert pullback_signal(bars, settings(exit_mode="rsi", rsi_exit=math.floor(rsi)))["action"] == "exit"
    assert pullback_signal(bars, settings(exit_mode="rsi", rsi_exit=math.ceil(rsi)))["action"] == "none"


def test_short_exit_below_sma5():
    bars = bars_from_returns(DOWNTREND + RALLY + [-0.06])
    close, _, sma5 = closes_vs_smas(bars)
    assert close < sma5
    assert pullback_signal(bars, settings(direction="short"))["action"] == "exit"


def _bear_structure_bars():
    """Lower highs and lower lows, ending mid down-leg: close below SMA5 and SMA200."""
    return bars_from_returns(swings(24, 8, -0.02, 5, 0.02) + [-0.02] * 4)


def test_choch_exit_fires_on_bear_structure_even_below_sma5():
    bars = _bear_structure_bars()
    structure = compute_chart_structure(*window(bars)).structure_state
    assert structure.status == STATUS_VALID
    assert structure.value == float(STRUCTURE_STATE_CODES["bear"])  # precondition, from the calculator
    close, sma200, sma5 = closes_vs_smas(bars)
    assert close < sma5 and close < sma200  # no sma5 exit and no long entry

    assert pullback_signal(bars, settings(exit_mode="sma5"))["action"] == "none"
    out = pullback_signal(bars, settings(exit_mode="sma5_or_choch"))
    assert out["action"] == "exit"
    assert out["structure_state"] == "bear"


def test_choch_exit_fires_on_bull_structure_for_a_short():
    bars = bars_from_returns(swings(24, 5, -0.02, 8, 0.02) + [0.02] * 4)
    structure = compute_chart_structure(*window(bars)).structure_state
    assert structure.status == STATUS_VALID
    assert structure.value == float(STRUCTURE_STATE_CODES["bull"])
    close, sma200, sma5 = closes_vs_smas(bars)
    assert close > sma5 and close > sma200

    assert pullback_signal(bars, settings(direction="short", exit_mode="sma5"))["action"] == "none"
    out = pullback_signal(bars, settings(direction="short", exit_mode="sma5_or_choch"))
    assert out["action"] == "exit"
    assert out["structure_state"] == "bull"


def test_choch_mode_on_corrupt_bars_raises():
    bars = _corrupt_high(_bear_structure_bars())
    assert compute_chart_structure(*window(bars)).structure_state.status == STATUS_INVALID_PRICES
    with pytest.raises(ValueError, match="Corrupt bars"):
        pullback_signal(bars, settings(exit_mode="sma5_or_choch"))


def test_choch_mode_reports_unmeasurable_structure_as_None_and_no_choch(monkeypatch):
    import dataclasses

    import ba2_experts.PullbackReversion as mod
    from ba2_common.core.market_conditions import STATUS_INSUFFICIENT_HISTORY, Observation

    real = mod.compute_chart_structure

    def unmeasurable(*arrays):
        return dataclasses.replace(real(*arrays), structure_state=Observation(
            None, STATUS_INSUFFICIENT_HISTORY, "test"))

    monkeypatch.setattr(mod, "compute_chart_structure", unmeasurable)
    out = pullback_signal(_bear_structure_bars(), settings(exit_mode="sma5_or_choch"))
    assert out["structure_state"] is None
    assert out["action"] == "none"  # below SMA5 and no measurable CHoCH


def test_entry_beats_exit_on_the_same_bar():
    # A long rise, then a mild bear swing structure that stays above SMA200 and ends in a dip.
    bars = bars_from_returns([0.01] * 260 + swings(10, 8, -0.005, 5, 0.0075) + [-0.005] * 5)
    structure = compute_chart_structure(*window(bars)).structure_state
    assert structure.status == STATUS_VALID
    assert structure.value == float(STRUCTURE_STATE_CODES["bear"])  # the exit condition holds
    close, sma200, _ = closes_vs_smas(bars)
    assert close > sma200
    out = pullback_signal(bars, settings(exit_mode="sma5_or_choch", entry_threshold=30.0))
    assert out["rsi"] < 30.0  # the entry condition holds too
    assert out["action"] == "entry"
    assert out["structure_state"] == "bear"  # reported on entry bars too


# ----------------------------------------------------------------------------- SPY gate
def test_spy_gate_blocks_a_short_when_spy_is_above_its_sma200():
    stock = bars_from_returns(DOWNTREND + RALLY)
    spy_up = bars_from_returns(UPTREND + [0.001] * 3)
    spy_down = bars_from_returns(DOWNTREND + [-0.001] * 3)
    assert spy_up.index.equals(stock.index)
    s = settings(direction="short", trend_gate="sma200_and_spy")
    assert pullback_signal(stock, settings(direction="short"))["action"] == "entry"  # stock alone passes

    blocked = pullback_signal(stock, s, spy_bars=spy_up)
    assert blocked["trend_ok"] is False
    assert blocked["action"] != "entry"
    assert pullback_signal(stock, s, spy_bars=spy_down)["action"] == "entry"


def test_spy_gate_needs_spy_above_sma200_for_a_long():
    stock = bars_from_returns(UPTREND + DIP)
    spy_up = bars_from_returns(UPTREND + [0.001] * 3)
    spy_down = bars_from_returns(DOWNTREND + [-0.001] * 3)
    s = settings(trend_gate="sma200_and_spy")
    assert pullback_signal(stock, s, spy_bars=spy_up)["action"] == "entry"
    assert pullback_signal(stock, s, spy_bars=spy_down)["trend_ok"] is False


def test_spy_gate_missing_short_or_misaligned_spy_raises():
    stock = bars_from_returns(DOWNTREND + RALLY)
    s = settings(direction="short", trend_gate="sma200_and_spy")
    with pytest.raises(ValueError):
        pullback_signal(stock, s)
    with pytest.raises(ValueError):
        pullback_signal(stock, s, spy_bars=bars_from_returns(DOWNTREND + RALLY).iloc[-150:])
    with pytest.raises(ValueError):  # SPY ends a session early: stale regime
        pullback_signal(stock, s, spy_bars=bars_from_returns(DOWNTREND + RALLY).iloc[:-1])
    with pytest.raises(ValueError):  # SPY holds a session after the decision bar: lookahead
        pullback_signal(stock.iloc[:-1], s, spy_bars=bars_from_returns(DOWNTREND + RALLY))


def test_spy_gate_timezone_mismatch_names_the_timezones():
    stock = bars_from_returns(DOWNTREND + RALLY)
    spy = bars_from_returns(DOWNTREND + RALLY)
    spy.index = spy.index.tz_localize("UTC")
    s = settings(direction="short", trend_gate="sma200_and_spy")
    with pytest.raises(ValueError, match="Timezone mismatch.*tz-naive.*UTC"):
        pullback_signal(stock, s, spy_bars=spy)
    stock.index = stock.index.tz_localize("America/New_York")
    with pytest.raises(ValueError, match="Timezone mismatch.*America/New_York.*UTC"):
        pullback_signal(stock, s, spy_bars=spy)


def test_range_index_raises_for_bars_and_spy():
    stock = bars_from_returns(DOWNTREND + RALLY)
    with pytest.raises(ValueError, match="DatetimeIndex"):
        pullback_signal(stock.reset_index(names="Date"), settings())
    with pytest.raises(ValueError, match="DatetimeIndex"):
        pullback_signal(stock, settings(direction="short", trend_gate="sma200_and_spy"),
                        spy_bars=bars_from_returns(DOWNTREND + RALLY).reset_index(names="Date"))


# ----------------------------------------------------------------------------- RSI
def _pandas_rsi(close, n):
    """The probe's ``rsi()`` verbatim, last value."""
    d = pd.Series(close).diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return float((100 - 100 / (1 + up / dn.replace(0, np.nan))).iloc[-1])


@pytest.mark.parametrize("n", [2, 3, 4, 5])
def test_wilder_rsi_matches_the_pandas_ewm_formulation(n):
    rng = np.random.default_rng(n)
    close = 100 * np.cumprod(1 + rng.normal(0, 0.02, 600))
    for k in (3, 10, 199, 200, 350, 600):
        assert _wilder_rsi(close[:k], n) == pytest.approx(_pandas_rsi(close[:k], n), abs=1e-9)
    for series in (UPTREND + DIP, DOWNTREND + RALLY, UPTREND + DIP + [0.06]):
        c = bars_from_returns(series)["Close"].to_numpy()
        assert _wilder_rsi(c, n) == pytest.approx(_pandas_rsi(c, n), abs=1e-9)


def test_no_down_move_gives_rsi_100():
    """The probe's RSI is NaN when the smoothed history holds no down move, so neither its entry
    nor its RSI exit ever fired on that bar. Here the RSI is 100:
    * a long in ``rsi`` exit mode now EXITS (the probe held);
    * the short-entry RSI condition holds too, but a history with no down move closes above its
      SMA200 and has a positive slope, so every short trend gate refuses it: no short entry."""
    bars = bars_from_returns([0.004] * 262)
    assert math.isnan(_pandas_rsi(bars["Close"].to_numpy(), 2))
    out = pullback_signal(bars, settings(exit_mode="rsi"))
    assert out["rsi"] == 100.0
    assert out["action"] == "exit"
    for gate in ("sma200", "slope_ohlcv_v1"):
        short = pullback_signal(bars, settings(direction="short", trend_gate=gate, exit_mode="time"))
        assert short["rsi"] == 100.0
        assert short["trend_ok"] is False and short["action"] == "none"


# ----------------------------------------------------------------------------- determinism
ALL_MODES = [settings(direction=d, trend_gate=g, exit_mode=e)
             for d in ("long", "short")
             for g in ("sma200", "slope_ohlcv_v1", "sma200_and_spy")
             for e in ("sma5", "rsi", "sma5_or_choch", "time")]


def test_prefix_results_are_deterministic_carry_no_state_and_do_not_mutate_inputs():
    """Pure-function guard: a prefix's result does not depend on bars after it, on earlier
    calls, or on call order, and inputs are untouched. The real lookahead test (``as_of``
    slicing of provider data) belongs to the expert in Task A2."""
    base = bars_from_returns(UPTREND + DIP + [0.06] + DOWNTREND[:20] + RALLY)
    wild = bars_from_returns(UPTREND + DIP + [0.06] + DOWNTREND[:20] + RALLY)
    cut = 270
    # the future after ``cut`` is replaced by a completely different path
    for i, r in enumerate([0.2, -0.3, 0.25, -0.1] * 4):
        pos = cut + i
        if pos >= len(wild):
            break
        wild.iloc[pos] = wild.iloc[pos] * (1 + r)
    spy = bars_from_returns(UPTREND + DIP + [0.06] + DOWNTREND[:20] + RALLY)
    before = base.copy()
    for s in ALL_MODES:
        for k in (200, 230, 262, 263, 264, cut):
            a = pullback_signal(base.iloc[:k], s, spy_bars=spy.iloc[:k])
            b = pullback_signal(wild.iloc[:k], s, spy_bars=spy.iloc[:k])
            pullback_signal(base.iloc[:k + 1], s, spy_bars=spy.iloc[:k + 1])  # no carried state
            again = pullback_signal(base.iloc[:k], s, spy_bars=spy.iloc[:k])
            assert a == b == again, (s, k)
    pd.testing.assert_frame_equal(base, before)  # inputs are not mutated


# ----------------------------------------------------------------------------- validation
@pytest.mark.parametrize("key,value", [
    ("direction", "buy"), ("direction", None),
    ("trend_gate", "sma50"),
    ("rsi_period", 1), ("rsi_period", 6), ("rsi_period", 2.5), ("rsi_period", True), ("rsi_period", "3"),
    ("entry_threshold", 0.5), ("entry_threshold", 31.0), ("entry_threshold", float("nan")),
    ("entry_threshold", False),
    ("exit_mode", "stop"),
    ("rsi_exit", 49.0), ("rsi_exit", 91.0), ("rsi_exit", float("inf")),
])
def test_bad_settings_raise(key, value):
    bars = bars_from_returns(UPTREND + DIP)
    with pytest.raises(ValueError):
        pullback_signal(bars, settings(**{key: value}))


@pytest.mark.parametrize("key", ["direction", "trend_gate", "rsi_period", "entry_threshold",
                                 "exit_mode", "rsi_exit"])
def test_missing_setting_raises(key):
    s = settings()
    del s[key]
    with pytest.raises(ValueError):
        pullback_signal(bars_from_returns(UPTREND + DIP), s)


def test_boundary_settings_are_accepted():
    bars = bars_from_returns(UPTREND + DIP)
    for s in (settings(rsi_period=5, entry_threshold=1, rsi_exit=50),
              settings(rsi_period=np.int64(2), entry_threshold=30.0, rsi_exit=90.0)):
        pullback_signal(bars, s)


@pytest.mark.parametrize("gate,exit_mode", [("sma200", "sma5"), ("slope_ohlcv_v1", "sma5"),
                                            ("sma200", "sma5_or_choch")])
def test_short_history_raises(gate, exit_mode):
    bars = bars_from_returns(UPTREND + DIP)
    pullback_signal(bars.iloc[-200:], settings(trend_gate=gate, exit_mode=exit_mode))  # 200 is enough
    with pytest.raises(ValueError):
        pullback_signal(bars.iloc[-199:], settings(trend_gate=gate, exit_mode=exit_mode))


def test_invalid_final_close_raises():
    bars = bars_from_returns(UPTREND + DIP)
    for bad in (float("nan"), 0.0, -1.0, float("inf")):
        b = bars.copy()
        b.iloc[-1, b.columns.get_loc("Close")] = bad
        with pytest.raises(ValueError):
            pullback_signal(b, settings())


def test_invalid_close_inside_the_history_raises():
    bars = bars_from_returns(UPTREND + DIP)
    bars.iloc[-50, bars.columns.get_loc("Close")] = float("nan")
    with pytest.raises(ValueError):
        pullback_signal(bars, settings())


def test_malformed_frames_raise():
    bars = bars_from_returns(UPTREND + DIP)
    with pytest.raises(ValueError):
        pullback_signal(bars.drop(columns=["Volume"]), settings())
    with pytest.raises(ValueError):
        pullback_signal(bars.iloc[::-1], settings())  # descending index
    with pytest.raises(ValueError):
        pullback_signal(pd.concat([bars, bars.iloc[[-1]]]), settings())  # duplicate session
    with pytest.raises(ValueError):
        pullback_signal(None, settings())


def test_flat_history_has_undefined_rsi_and_raises():
    bars = bars_from_returns([0.0] * 260)
    with pytest.raises(ValueError):
        pullback_signal(bars, settings())
