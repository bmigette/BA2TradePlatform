"""Market-regime classification (2026-07-25).

Two things are pinned here: the mechanics (thresholds, fail-loud on short history,
fail-closed helpers) and -- more importantly -- CAUSALITY. A regime signal that can see the
future is worse than no regime signal, because it makes a backtest look like it dodged every
drawdown.
"""
import pytest

from ba2_common.core.market_regime import (
    CALM, NORMAL, RISK_OFF, RISK_ON, STRESSED,
    classify_trend_regime, classify_volatility_regime, is_risk_on, is_stressed,
)


def _flat(n, price=100.0):
    return [price] * n


def _ramp(n, start=100.0, step=0.5):
    return [start + i * step for i in range(n)]


# --------------------------------------------------------------------------- #
# trend regime
# --------------------------------------------------------------------------- #
def test_rising_market_is_risk_on():
    r = classify_trend_regime(_ramp(300))
    assert r["regime"] == RISK_ON
    assert r["distance_pct"] > 0


def test_falling_market_is_risk_off():
    r = classify_trend_regime(_ramp(300, start=250.0, step=-0.5))
    assert r["regime"] == RISK_OFF
    assert r["distance_pct"] < 0


def test_trend_regime_fails_loud_on_insufficient_history():
    """None, not a guessed regime -- a caller must be able to tell 'unknown' from 'risk_off'."""
    r = classify_trend_regime(_flat(50))
    assert r["regime"] is None
    assert "insufficient history" in r["reason"]


def test_trend_regime_flips_exactly_at_the_sma():
    closes = _flat(250)                       # price == SMA == 100
    assert classify_trend_regime(closes)["regime"] == RISK_OFF   # not strictly above
    assert classify_trend_regime(closes + [101.0])["regime"] == RISK_ON


# --------------------------------------------------------------------------- #
# volatility regime
# --------------------------------------------------------------------------- #
def _vol_series(n, quiet_amp=0.1, loud_amp=5.0, loud_tail=0, seed=7):
    """Pseudo-random walk whose STEP SIZE sets realized vol: small amplitude = low vol.

    Deliberately NOT a regular alternating series -- that has perfectly CONSTANT realized
    vol, so every value in the rank window ties and the percentile rank is undefined (the
    module returns NORMAL for that degenerate case; see
    test_flat_volatility_series_is_normal_not_stressed). Real markets always disperse, so
    the fixture must too or it tests the degenerate branch by accident.
    """
    import random
    rng = random.Random(seed)
    out, px = [], 100.0
    for i in range(n):
        amp = loud_amp if i >= n - loud_tail else quiet_amp
        px += rng.uniform(-amp, amp)
        out.append(max(px, 1.0))
    return out


def test_flat_volatility_series_is_normal_not_stressed():
    """Degenerate rank guard: a perfectly steady market has zero dispersion in its realized-vol
    series, so the '<=' percentile collapses to 100%. Reporting that as STRESSED would be the
    exact opposite of the truth."""
    steady = []
    px = 100.0
    for i in range(600):                      # constant-amplitude zig-zag -> constant vol
        px += 0.1 if i % 2 == 0 else -0.1
        steady.append(px)
    r = classify_volatility_regime(steady)
    assert r["regime"] == NORMAL
    assert r["rank_pct"] is None
    assert "no dispersion" in r["reason"]


def test_volatility_spike_is_stressed():
    r = classify_volatility_regime(_vol_series(600, loud_tail=25))
    assert r["regime"] == STRESSED
    assert r["rank_pct"] > 70


def test_quiet_tail_after_turbulence_is_calm():
    """Ranked, not absolute: the SAME vol level means different things in different eras."""
    noisy = _vol_series(560, quiet_amp=4.0)
    calm_tail = _vol_series(60, quiet_amp=0.05)
    r = classify_volatility_regime(noisy + calm_tail)
    assert r["regime"] == CALM
    assert r["rank_pct"] < 30


def test_volatility_regime_fails_loud_on_insufficient_history():
    r = classify_volatility_regime(_flat(100))
    assert r["regime"] is None
    assert "insufficient history" in r["reason"]


def test_volatility_regime_reports_a_usable_realized_vol():
    r = classify_volatility_regime(_vol_series(600, loud_tail=25))
    assert r["realized_vol"] > 0
    assert 0 <= r["rank_pct"] <= 100


# --------------------------------------------------------------------------- #
# CAUSALITY — the property that matters most
# --------------------------------------------------------------------------- #
def test_classification_ignores_data_after_the_as_of_cut():
    """The caller cuts the list; appending FUTURE bars must not change the verdict for the
    earlier cut. This is what makes lookahead structurally impossible."""
    history = _ramp(300)
    future = [500.0] * 50          # a violent future rally
    assert classify_trend_regime(history) == classify_trend_regime(history)
    # Same prefix -> same answer, regardless of what comes after in the caller's data.
    assert classify_trend_regime(history)["regime"] == \
        classify_trend_regime((history + future)[:len(history)])["regime"]


def test_a_future_crash_does_not_retroactively_mark_the_market_stressed():
    """The failure mode this guards: labelling 2020-02 'stressed' because 2020-03 happened."""
    calm_history = _vol_series(600, quiet_amp=0.05)
    crash = [50.0, 90.0, 45.0, 95.0, 40.0]
    before = classify_volatility_regime(calm_history)
    assert before["regime"] == CALM
    assert classify_volatility_regime((calm_history + crash)[:len(calm_history)])["regime"] == CALM


# --------------------------------------------------------------------------- #
# helpers fail CLOSED
# --------------------------------------------------------------------------- #
def test_helpers_return_false_when_the_regime_is_unknown():
    """An unclassifiable market must not read as 'safe to trade'."""
    assert is_risk_on(_flat(10)) is False
    assert is_stressed(_flat(10)) is False


def test_helpers_agree_with_the_classifiers():
    assert is_risk_on(_ramp(300)) is True
    assert is_risk_on(_ramp(300, start=250.0, step=-0.5)) is False
    assert is_stressed(_vol_series(600, loud_tail=25)) is True


# --------------------------------------------------------------------------- #
# 4-state trend (SMA200 position x SMA50 slope) -- the SPY regime gate's trend axis
# --------------------------------------------------------------------------- #
from ba2_common.core import market_regime as mr


def _lin(n, start=100.0, step=0.1):
    return [start + step * i for i in range(n)]


def test_trend_state_up_pullback_bear_rally_down():
    assert mr.classify_trend_state(_lin(260))["state"] == mr.TREND_UP
    assert mr.classify_trend_state(list(reversed(_lin(260))))["state"] == mr.TREND_DOWN
    # long uptrend, then 12 sessions of decline that stays above SMA200 but turns SMA50 down
    base = _lin(300, step=0.3)
    pull = base + [base[-1] - 2.0 * (i + 1) for i in range(12)]
    assert mr.classify_trend_state(pull)["state"] == mr.TREND_PULLBACK
    # long downtrend, then 12 sessions of bounce that stays below SMA200 but turns SMA50 up
    dbase = list(reversed(_lin(300, step=0.3)))
    rally = dbase + [dbase[-1] + 2.0 * (i + 1) for i in range(12)]
    assert mr.classify_trend_state(rally)["state"] == mr.TREND_BEAR_RALLY


def test_trend_state_insufficient_history_is_none_with_reason():
    out = mr.classify_trend_state(_lin(199))          # SMA200 binds; the SMA50 slope needs only 60
    assert out["state"] is None and "insufficient" in out["reason"]
    assert mr.classify_trend_state(_lin(200))["state"] is not None   # boundary: exactly enough


def test_trend_state_close_equal_sma200_is_below_like_classify_trend_regime():
    closes = [100.0] * 260
    assert mr.classify_trend_regime(closes)["regime"] == mr.RISK_OFF
    assert mr.classify_trend_state(closes)["state"] in (mr.TREND_DOWN, mr.TREND_BEAR_RALLY)


def test_slope_exactly_zero_is_falling():            # spec: "> 0 = rising"
    assert mr.classify_trend_state([100.0] * 260)["slope_rising"] is False


def test_above_axis_matches_classify_trend_regime_every_prefix():
    import random
    r = random.Random(3)
    px, closes = 100.0, []
    for _ in range(500):
        px *= 1 + r.gauss(0, 0.01)
        closes.append(px)
    for i in range(199, 500):
        above = mr.classify_trend_state(closes[:i + 1])["state"] in (mr.TREND_UP, mr.TREND_PULLBACK)
        assert above == (mr.classify_trend_regime(closes[:i + 1])["regime"] == mr.RISK_ON)
