"""The calibrated option spread model (plan Part F2) -- pinned numbers, not just shapes."""
from __future__ import annotations

import math

import pytest

from ba2_common.core import option_spread_model as m


def test_the_version_is_the_one_the_grid_identity_carries():
    assert m.SPREAD_MODEL_VERSION == "pow-2026-09-22"
    assert m.SPREAD_MODELS == ("pow-2026-09-22", "legacy-pct")


def test_the_constants_are_the_fits_own_betas_in_natural_form():
    """fit.py's ``lvol`` betas were [-1.4695, 0.6035, -0.3124] on (1, ln mid, log10 volume)."""
    assert m._COEF == pytest.approx(math.exp(-1.4695), abs=1e-4)
    assert m._PREMIUM_EXP == 0.6035
    assert m._VOLUME_EXP == pytest.approx(-0.3124 / math.log(10), abs=1e-4)


@pytest.mark.parametrize("premium,volume,expected", [
    # hand-computed: 0.2301 * p**0.6035 * v**-0.1357
    (1.0, 1, 0.2301),
    (10.0, 100, 0.494329602591411),
    (0.5, 1000, 0.05931233427193508),
    (25.0, 500, 0.6907536434325561),
    (4.0, 500, 0.22856492942925036),
])
def test_full_spread_reproduces_hand_computed_values(premium, volume, expected):
    assert m.full_spread(premium, volume) == pytest.approx(expected, rel=1e-12)
    assert m.half_spread(premium, volume) == pytest.approx(expected / 2, rel=1e-12)


def test_the_measured_bucket_medians_are_in_the_right_place():
    """Spot checks against model_vs_actual_test2024_25.csv (2024-25 held-out, actual median
    $ spread per premium x volume bucket): the model lands near the ACTUAL medians where the
    old 5%-of-premium model was 0.4x / 1.8x off."""
    # 0.05-0.5 premium, 100-999 volume: actual median 0.04 (old model 0.02)
    assert m.full_spread(0.25, 300) == pytest.approx(0.04, abs=0.01)
    # 10-20 premium, 100-999 volume: actual median 0.40 (old model 0.69)
    assert m.full_spread(14.0, 300) == pytest.approx(0.40, abs=0.15)


def test_the_floor_is_one_tick():
    assert m.full_spread(0.0001, 1) == 0.01
    assert m.full_spread(0.0, 10_000) == 0.01


@pytest.mark.parametrize("volume", [None, 0, 0.5, float("nan")])
def test_missing_or_sub_one_volume_is_the_thin_case(volume):
    assert m.full_spread(4.0, volume) == m.full_spread(4.0, 1)


def test_negative_premium_is_a_sign_not_a_price():
    assert m.full_spread(-4.0, 500) == m.full_spread(4.0, 500)


def test_a_non_finite_premium_is_refused():
    with pytest.raises(ValueError):
        m.full_spread(float("nan"), 10)
    with pytest.raises(ValueError):
        m.full_spread(None, 10)


@pytest.mark.parametrize("bid,ask,expected", [
    (1.00, 1.10, 0.05),
    (2.00, 2.00, None),         # locked: a close-proxy's zero spread, never a real quote
    (2.000, 2.004, 0.005),      # sub-tick spread floored at half the one-tick minimum
    (0.0, 0.05, None),          # zero bid: one-sided market
    (1.10, 1.00, None),         # crossed
    (None, 1.00, None),
    (1.00, None, None),
    (float("nan"), 1.0, None),
])
def test_quoted_half_spread_validity(bid, ask, expected):
    got = m.quoted_half_spread(bid, ask)
    if expected is None:
        assert got is None
    else:
        assert got == pytest.approx(expected)


def test_as_of_quote_beats_the_model():
    bar = {"bid": 3.90, "ask": 4.20, "close": 4.0, "volume": 500}
    assert m.as_of_half_spread(bar, 99.0) == (pytest.approx(0.15), m.SOURCE_QUOTE)


def test_no_valid_quote_models_from_the_as_of_close_and_volume():
    bar = {"bid": None, "ask": None, "close": 4.0, "volume": 500}
    half, src = m.as_of_half_spread(bar, 99.0)     # 99.0 must NOT be used: the bar has a close
    assert src == m.SOURCE_MODEL
    assert half == pytest.approx(0.22856492942925036 / 2, rel=1e-12)


def test_no_as_of_bar_models_the_fill_premium_as_thin():
    half, src = m.as_of_half_spread(None, 4.0)
    assert src == m.SOURCE_MODEL
    assert half == m.half_spread(4.0, 1)


def test_a_locked_as_of_quote_goes_to_the_model():
    bar = {"bid": 4.0, "ask": 4.0, "close": 4.0, "volume": 500}
    half, src = m.as_of_half_spread(bar, 4.0)
    assert src == m.SOURCE_MODEL and half == m.half_spread(4.0, 500)
