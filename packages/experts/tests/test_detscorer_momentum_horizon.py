"""vol_adjusted_momentum must divide by the vol over the RETURN'S OWN HORIZON.

Regression 2026-08-11. momentum_12_1 is a TOTAL RETURN over (lookback - skip) trading days;
realized_vol_daily is a ONE-DAY sigma. The original code divided one by the other, mixing units
and inflating the result by sqrt(horizon) ~ 15x. Fed through tanh(x / 1.5) -- the scale the module
docstring calls "vol-adj momentum units" -- 83% of 160 measured symbol-dates came out at |1.0|,
so the heaviest technical leg (weight 0.45) was the SIGN of momentum rather than a score.
"""
import math

import numpy as np
import pandas as pd

from ba2_experts.DeterministicScorer.technical import (
    DEF_MOM_LOOKBACK, DEF_MOM_SKIP, DEF_SCALE_MOMVOL, DEF_VOL_WINDOW,
    momentum_12_1, realized_vol_daily, tanh_scale, vol_adjusted_momentum,
)


def _series(n=400, drift=0.0008, vol=0.015, seed=7):
    rng = np.random.default_rng(seed)
    steps = rng.normal(drift, vol, n)
    return pd.Series(100.0 * np.exp(np.cumsum(steps)))


def test_denominator_is_the_horizon_scaled_sigma_not_the_daily_one():
    s = _series()
    got = vol_adjusted_momentum(s)
    mom = momentum_12_1(s, DEF_MOM_LOOKBACK, DEF_MOM_SKIP)
    sig = realized_vol_daily(s, DEF_VOL_WINDOW)
    horizon = DEF_MOM_LOOKBACK - DEF_MOM_SKIP

    assert got == pytest_approx(mom / (sig * math.sqrt(horizon)))
    # ...and is sqrt(horizon) SMALLER than the old, unit-mismatched value.
    assert got == pytest_approx((mom / sig) / math.sqrt(horizon))


def test_the_result_lands_in_the_range_the_tanh_scale_was_written_for():
    """The point of the fix: tanh(x/1.5) must DISCRIMINATE, not saturate. Across a spread of
    drifts the normalized value must vary rather than pin at +/-1."""
    normalized = []
    for i, drift in enumerate((-0.0015, -0.0005, 0.0, 0.0005, 0.0015, 0.0025)):
        v = vol_adjusted_momentum(_series(drift=drift, seed=100 + i))
        assert v is not None
        normalized.append(tanh_scale(v, DEF_SCALE_MOMVOL))

    saturated = sum(1 for x in normalized if abs(x) >= 0.99)
    assert saturated <= 1, f"still saturating: {[round(x, 3) for x in normalized]}"
    assert np.std(normalized) > 0.15, "component must carry ranking information, not just a sign"


def test_horizon_follows_the_configured_lookback_and_skip():
    """A shorter lookback is a shorter horizon, so the denominator must shrink with it -- the
    scaling is derived from the arguments, never a hardcoded 231."""
    s = _series()
    long_h = vol_adjusted_momentum(s, lookback=252, skip=21)
    short_h = vol_adjusted_momentum(s, lookback=126, skip=21)
    sig = realized_vol_daily(s, DEF_VOL_WINDOW)
    assert short_h == pytest_approx(
        momentum_12_1(s, 126, 21) / (sig * math.sqrt(126 - 21)))
    assert long_h is not None and short_h is not None


def pytest_approx(v):
    import pytest
    return pytest.approx(v, rel=1e-9)
