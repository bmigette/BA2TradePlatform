from datetime import date

import pytest

from ba2_common.core.ml_forecast import mapping
from ba2_common.core.ml_forecast.forecast_row import SL_GRID_PCT, TP_GRID_PCT, ForecastRow
from ba2_common.core.ml_forecast.mapping import (
    MappingGenes, map_forecast, map_held_position, p_up_from_deciles, quantile_return,
)
from ba2_common.core.types import OrderRecommendation as OR

BULL = tuple(0.01 * i - 0.02 for i in range(9))          # -2% .. +6%, zero at q30 -> P(up) = 0.7
BEAR = tuple(-x for x in reversed(BULL))                  # -6% .. +2%, zero at q70 -> P(up) = 0.3


def genes(**over):
    base = dict(horizon=5, min_edge_pct=1.0, min_prob=60, max_uncertainty=5.0, tp_quantile=80, sl_quantile=20,
                exit_flip_prob=60, enable_sell=True)
    base.update(over)
    return MappingGenes(**base)


def row(dec=BULL, probs=None, horizons=(5, 10)):
    return ForecastRow("AAPL", date(2024, 3, 1), {h: dec for h in horizons}, probs)


def grid_probs(p=0.5, cells=None):
    d = {(x, h, tp, sl): p for x in ("long", "short") for h in (5, 10) for tp in TP_GRID_PCT for sl in SL_GRID_PCT}
    d.update(cells or {})
    return d


# ------------------------------------------------------------------ p_up
def test_p_up_interpolation():
    assert p_up_from_deciles(BULL) == pytest.approx(0.7)
    assert p_up_from_deciles(BEAR) == pytest.approx(0.3)
    # zero halfway between q40 (-1%) and q50 (+1%) -> F(0)=0.45
    d = (-0.05, -0.04, -0.02, -0.01, 0.01, 0.02, 0.03, 0.04, 0.05)
    assert p_up_from_deciles(d) == pytest.approx(0.55)


def test_p_up_tails_clamped():
    assert p_up_from_deciles(tuple(0.01 + 0.01 * i for i in range(9))) == pytest.approx(0.95)    # all positive
    assert p_up_from_deciles(tuple(-0.1 + 0.01 * i for i in range(9))) == pytest.approx(0.05)    # all negative


def test_p_up_flat_zero_segment_takes_midpoint():
    d = (-0.02, -0.01, 0.0, 0.0, 0.0, 0.0, 0.01, 0.02, 0.03)    # zero spans q30..q60 -> F = 0.45
    assert p_up_from_deciles(d) == pytest.approx(0.55)


def test_quantile_return_interpolates():
    assert quantile_return(BULL, 80) == pytest.approx(0.05)
    assert quantile_return(BULL, 75) == pytest.approx(0.045)
    with pytest.raises(ValueError):
        quantile_return(BULL, 95)


# ------------------------------------------------------------------ gene validation
@pytest.mark.parametrize("over", [
    dict(horizon=0), dict(min_edge_pct=-1), dict(min_prob=50), dict(min_prob=101), dict(max_uncertainty=0),
    dict(tp_quantile=50), dict(tp_quantile=95), dict(sl_quantile=50), dict(sl_quantile=5),
    dict(exit_flip_prob=50), dict(enable_sell=1),
])
def test_bad_genes_raise(over):
    with pytest.raises((ValueError, TypeError)):
        genes(**over)


# ------------------------------------------------------------------ entries
def test_bullish_buy():
    r = map_forecast(row(), genes(), decision_price=100.0, atr=2.0)
    assert r.signal == OR.BUY
    assert r.confidence == pytest.approx(70.0)
    assert r.expected_profit_percent == pytest.approx(2.0)
    assert r.tp_price == pytest.approx(105.0) and r.target_price == pytest.approx(105.0)
    assert r.sl_price == pytest.approx(99.0)
    assert "BUY" in r.reason


def test_bearish_sell_prices_mirror():
    r = map_forecast(row(BEAR), genes(), 100.0, 2.0)
    assert r.signal == OR.SELL
    assert r.confidence == pytest.approx(70.0)
    assert r.expected_profit_percent == pytest.approx(2.0)      # profit in the trade direction
    assert r.tp_price == pytest.approx(95.0) and r.sl_price == pytest.approx(101.0)


def test_sell_disabled_holds_on_bearish():
    r = map_forecast(row(BEAR), genes(enable_sell=False), 100.0, 2.0)
    assert r.signal == OR.HOLD and "sell disabled" in r.reason
    assert r.tp_price is None and r.sl_price is None


def test_edge_too_small_holds():
    r = map_forecast(row(), genes(min_edge_pct=2.5), 100.0, 2.0)     # q50 = +2%
    assert r.signal == OR.HOLD and "min_edge" in r.reason


def test_prob_too_low_holds():
    r = map_forecast(row(), genes(min_prob=75), 100.0, 2.0)           # P(up) = 0.7
    assert r.signal == OR.HOLD and "min_prob" in r.reason


def test_uncertainty_too_wide_holds():
    r = map_forecast(row(), genes(max_uncertainty=3.9), 100.0, 2.0)   # width 8 / atr 2 = 4 ATR
    assert r.signal == OR.HOLD and "max_uncertainty" in r.reason
    assert map_forecast(row(), genes(max_uncertainty=4.0), 100.0, 2.0).signal == OR.BUY


def test_uncertainty_scales_with_price_and_atr():
    # same row, price 50 and atr 2: width 4 / 2 = 2 ATR
    assert map_forecast(row(), genes(max_uncertainty=2.0), 50.0, 2.0).signal == OR.BUY


def test_zero_median_holds():
    d = (-0.04, -0.03, -0.02, -0.01, 0.0, 0.01, 0.02, 0.03, 0.04)
    assert map_forecast(row(d), genes(min_edge_pct=0.0, min_prob=51), 100.0, 2.0).signal == OR.HOLD


def test_horizon_selects_distribution():
    r = ForecastRow("AAPL", date(2024, 3, 1), {5: BEAR, 10: BULL})
    assert map_forecast(r, genes(horizon=10), 100.0, 2.0).signal == OR.BUY
    assert map_forecast(r, genes(horizon=5), 100.0, 2.0).signal == OR.SELL
    with pytest.raises(ValueError):
        map_forecast(r, genes(horizon=20), 100.0, 2.0)


STRONG = tuple(0.01 + 0.01 * i for i in range(9))      # q10 = +1%: a q10 "stop" would sit above the entry


def test_stop_on_profit_side_still_signals_without_a_stop():
    r = map_forecast(row(STRONG), genes(), 100.0, 2.0)
    assert r.signal == OR.BUY and r.sl_price is None
    assert r.tp_price == pytest.approx(100.0 * (1 + 0.08))     # q80 = +8%
    assert "forecast stop not on loss side" in r.reason and "ruleset/ATR safeguard applies" in r.reason


def test_tp_on_wrong_side_gives_no_tp():
    # bullish median but q80 below zero is impossible (monotone); use a short with tp level on the wrong side
    d = tuple(-0.09 + 0.01 * i for i in range(9))          # all negative: bearish, q10..q90 = -9%..-1%
    r = map_forecast(row(d), genes(sl_quantile=40), 100.0, 2.0)   # short: sl = q60 = -4% (profit side)
    assert r.signal == OR.SELL and r.sl_price is None and r.tp_price is not None
    assert "forecast stop not on loss side" in r.reason


def test_tiny_stop_distance_is_dropped():
    from ba2_common.core.ml_forecast.mapping import MIN_EXIT_DISTANCE_ATR
    assert MIN_EXIT_DISTANCE_ATR == 0.25
    d = (-0.02, -0.01, -0.005, -0.001, 0.02, 0.03, 0.04, 0.05, 0.06)
    r = map_forecast(row(d), genes(min_prob=55, sl_quantile=40), 100.0, 2.0)     # sl distance 0.1 < 0.25 * 2
    assert r.signal == OR.BUY and r.sl_price is None and r.tp_price == pytest.approx(105.0)
    assert "minimum" in r.reason


def test_tiny_tp_distance_is_dropped():
    d = (-0.02, -0.01, 0.0, 0.001, 0.002, 0.003, 0.004, 0.0045, 0.005)
    r = map_forecast(row(d), genes(min_edge_pct=0.1, min_prob=55, max_uncertainty=6), 100.0, 2.0)
    assert r.signal == OR.BUY and r.tp_price is None and r.target_price is None
    assert "take-profit" in r.reason and "minimum" in r.reason


def test_confidence_always_1_to_100():
    for dec in (BULL, tuple(0.01 + 0.01 * i for i in range(9))):
        r = map_forecast(row(dec), genes(min_prob=55, max_uncertainty=6, sl_quantile=10), 100.0, 2.0)
        assert 1.0 <= r.confidence <= 100.0
    held = map_held_position(row(BEAR), genes(), "long")
    assert 1.0 <= held.confidence <= 100.0
    hold = map_forecast(row(BULL), genes(min_prob=75), 100.0, 2.0)
    assert 1.0 <= hold.confidence <= 100.0


@pytest.mark.parametrize("price,atr", [(0.0, 1.0), (-1.0, 1.0), (100.0, 0.0), (100.0, float("nan")),
                                       (float("inf"), 1.0), (None, 1.0), (100.0, None)])
def test_bad_price_or_atr_raises(price, atr):
    with pytest.raises(ValueError):
        map_forecast(row(), genes(), price, atr)


# ------------------------------------------------------------------ path models
def test_path_model_uses_nearest_grid_cell_for_probability():
    # BULL h5: tp = q80 = +5% -> TP cell 5, sl = q20 = -1% -> SL cell 1
    probs = grid_probs(0.5, {("long", 5, 5, 1): 0.66})
    r = map_forecast(row(probs=probs), genes(min_prob=60), 100.0, 2.0)
    assert r.signal == OR.BUY and r.confidence == pytest.approx(66.0)
    assert "tp_cell=5" in r.reason and "sl_cell=1" in r.reason


def test_path_model_prob_gate_blocks_even_when_deciles_pass():
    r = map_forecast(row(probs=grid_probs(0.5)), genes(min_prob=60), 100.0, 2.0)   # P(up)=0.7 but P(TP<SL)=0.5
    assert r.signal == OR.HOLD and "min_prob" in r.reason


def test_path_model_nearest_cell_rounding():
    # tp = q75 = +4.5% -> nearest of (1,2,3,5,8,12) is 5 (tie-free: |4.5-5|=.5 < |4.5-3|=1.5)
    probs = grid_probs(0.5, {("long", 5, 5, 1): 0.9})
    r = map_forecast(row(probs=probs), genes(tp_quantile=75), 100.0, 2.0)
    assert r.confidence == pytest.approx(90.0)


def _dist_row(tp_ret, sl_ret):
    """Deciles with q80 = tp_ret and q20 = sl_ret (linear elsewhere), bullish."""
    d = [sl_ret - 0.01, sl_ret, 0.0, 0.5 * tp_ret * 0.5, 0.5 * tp_ret, 0.75 * tp_ret, 0.9 * tp_ret, tp_ret, tp_ret + 0.01]
    return row(tuple(d), probs=grid_probs(0.9))


@pytest.mark.parametrize("tp_ret,sl_ret", [
    (0.20, -0.01),     # TP beyond the grid
    (0.05, -0.005),    # SL below the grid
    (0.04, -0.01),     # TP halfway between cells 3 and 5: more than half a step from the nearest
    (0.05, -0.04),     # SL halfway between cells 3 and 5
])
def test_path_model_off_grid_distance_holds_with_grid_gate(tp_ret, sl_ret):
    r = map_forecast(_dist_row(tp_ret, sl_ret), genes(min_edge_pct=0.1, max_uncertainty=100), 100.0, 2.0)
    assert r.signal == OR.HOLD and "gate=grid" in r.reason


def test_path_model_wrong_side_stop_falls_back_to_decile_probability():
    r = map_forecast(row(STRONG, probs=grid_probs(0.1)), genes(), 100.0, 2.0)    # grid says 0.1; deciles say 0.95
    assert r.signal == OR.BUY and r.sl_price is None and r.tp_price is not None
    assert r.confidence == pytest.approx(95.0)
    assert ("path grid has no cell for a forecast stop on the profit side; decile probability used; "
            "ruleset/ATR safeguard applies") in r.reason


def test_path_model_on_grid_distance_passes():
    r = map_forecast(_dist_row(0.05, -0.02), genes(min_edge_pct=0.1, max_uncertainty=100), 100.0, 2.0)
    assert r.signal == OR.BUY and r.confidence == pytest.approx(90.0)


def test_path_model_short_uses_short_columns():
    probs = grid_probs(0.5, {("short", 5, 5, 1): 0.8, ("long", 5, 5, 1): 0.1})
    r = map_forecast(row(BEAR, probs=probs), genes(), 100.0, 2.0)
    assert r.signal == OR.SELL and r.confidence == pytest.approx(80.0)


# ------------------------------------------------------------------ held positions
def test_held_long_flips_to_sell():
    r = map_held_position(row(BEAR), genes(exit_flip_prob=60), "long")      # P(down) = 0.7
    assert r.signal == OR.SELL and r.confidence == pytest.approx(70.0)
    assert "flip" in r.reason


def test_held_long_stays_when_below_threshold():
    r = map_held_position(row(BEAR), genes(exit_flip_prob=70.5), "long")
    assert r.signal == OR.HOLD
    assert map_held_position(row(BULL), genes(), "long").signal == OR.HOLD


def test_held_short_flips_to_buy():
    r = map_held_position(row(BULL), genes(exit_flip_prob=65), "short")
    assert r.signal == OR.BUY and r.confidence == pytest.approx(70.0)
    assert map_held_position(row(BEAR), genes(), "short").signal == OR.HOLD


def test_held_flip_at_exact_threshold_and_bad_side():
    assert map_held_position(row(BEAR), genes(exit_flip_prob=70), "long").signal == OR.SELL
    with pytest.raises(ValueError):
        map_held_position(row(BEAR), genes(), "flat")


def test_held_flip_ignores_enable_sell():
    # closing a long is not opening a short: the flip must still fire when shorting is disabled
    assert map_held_position(row(BEAR), genes(enable_sell=False), "long").signal == OR.SELL


def test_bullish_score_is_p_up_then_the_median_for_deciles():
    from ba2_common.core.ml_forecast.mapping import bullish_score
    assert bullish_score(row(), 5) == (pytest.approx(0.7), pytest.approx(BULL[4]))
    # both clamp P(up) at 0.95; the larger median ranks higher
    a = tuple(0.01 + 0.01 * i for i in range(9))
    b = tuple(0.03 + 0.01 * i for i in range(9))
    assert bullish_score(row(a), 5)[0] == bullish_score(row(b), 5)[0] == pytest.approx(0.95)
    assert bullish_score(row(b), 5) > bullish_score(row(a), 5)
    assert bullish_score(row(BEAR), 5) < bullish_score(row(), 5)


def test_bullish_score_class_rows_use_the_conditional_up_probability_and_expected_return():
    from ba2_common.core.ml_forecast.mapping import bullish_score
    cf = ((0.2, -0.03), (0.3, 0.002), (0.5, 0.03))
    r = ForecastRow("AAPL", date(2025, 3, 3), {}, class_probs={5: cf})
    p, e = bullish_score(r, 5)
    assert p == pytest.approx(0.5 / 0.7) and e == pytest.approx(-0.006 + 0.0006 + 0.015)
    with pytest.raises(ValueError, match="horizon"):
        bullish_score(r, 10)


def test_confidence_pct_is_the_public_probability_to_confidence_conversion():
    """Confidence is stored on the 1-100 scale; the expert uses the same conversion for its own HOLDs."""
    assert mapping.confidence_pct(0.783) == pytest.approx(78.3)
    assert mapping.confidence_pct(0.0) == 1.0 and mapping.confidence_pct(1.5) == 100.0
    assert not hasattr(mapping, "_conf")
