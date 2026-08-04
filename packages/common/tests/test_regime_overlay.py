"""Regime overlay: neutrality, the leak check, and the precomputed calendar's exactness.

The overlay's whole safety story is "1.0 is an exact no-op", so most of this file is about
proving the disabled/neutral paths change NOTHING. The calendar tests exist because
StressedCalendar reimplements market_regime's arithmetic for speed -- if the two ever drift, a
grid would search a gene that means something different from what the classifier says.
"""
import math
import random
from datetime import date, datetime, timedelta, timezone

import pytest

from ba2_common.core import market_regime as mr
from ba2_common.core.regime_overlay import (
    NEUTRAL_SCALE, StressedCalendar, get_stressed, overlay_enabled, regime_scale,
    reset_stressed, scale_percent, set_stressed,
)


class _Expert:
    """Minimal stand-in for a MarketExpertInterface: only the settings getter is used."""

    def __init__(self, **settings):
        self._settings = settings

    def get_setting_with_interface_default(self, name, log_warning=True):
        return self._settings.get(name)


@pytest.fixture(autouse=True)
def _clean_bar_state():
    reset_stressed()
    yield
    reset_stressed()


# --------------------------------------------------------------------------------------------
# Per-bar state seam
# --------------------------------------------------------------------------------------------

def test_unpublished_regime_is_none():
    assert get_stressed() is None


def test_set_and_reset_stressed():
    set_stressed(True)
    assert get_stressed() is True
    set_stressed(False)
    assert get_stressed() is False
    set_stressed(None)
    assert get_stressed() is None
    set_stressed(True)
    reset_stressed()
    assert get_stressed() is None


# --------------------------------------------------------------------------------------------
# regime_scale: every path that must be neutral
# --------------------------------------------------------------------------------------------

def test_disabled_overlay_is_neutral_even_when_stressed():
    expert = _Expert(regime_overlay_enabled=False, regime_tp_scale=2.0)
    assert regime_scale(expert, "regime_tp_scale", True) == NEUTRAL_SCALE


def test_enabled_but_not_stressed_is_neutral():
    expert = _Expert(regime_overlay_enabled=True, regime_tp_scale=2.0)
    assert regime_scale(expert, "regime_tp_scale", False) == NEUTRAL_SCALE


def test_unclassified_regime_is_neutral():
    """None means the benchmark could not be classified -- fail closed, do NOT scale."""
    expert = _Expert(regime_overlay_enabled=True, regime_tp_scale=2.0)
    assert regime_scale(expert, "regime_tp_scale", None) == NEUTRAL_SCALE


def test_missing_expert_is_neutral():
    assert regime_scale(None, "regime_tp_scale", True) == NEUTRAL_SCALE


def test_absent_setting_is_neutral():
    """Every genome persisted before this feature existed has no such key."""
    expert = _Expert(regime_overlay_enabled=True)
    assert regime_scale(expert, "regime_tp_scale", True) == NEUTRAL_SCALE


def test_unparseable_setting_is_neutral():
    expert = _Expert(regime_overlay_enabled=True, regime_tp_scale="not-a-number")
    assert regime_scale(expert, "regime_tp_scale", True) == NEUTRAL_SCALE


def test_enabled_and_stressed_applies_the_scale():
    expert = _Expert(regime_overlay_enabled=True, regime_tp_scale=1.5,
                     regime_stop_scale=0.5, regime_risk_scale=2.0)
    assert regime_scale(expert, "regime_tp_scale", True) == 1.5
    assert regime_scale(expert, "regime_stop_scale", True) == 0.5
    assert regime_scale(expert, "regime_risk_scale", True) == 2.0


def test_leak_check_neutral_scales_equal_disabled():
    """enabled=1 with every scale at 1.0 must be indistinguishable from enabled=0."""
    on = _Expert(regime_overlay_enabled=True, regime_risk_scale=1.0,
                 regime_stop_scale=1.0, regime_tp_scale=1.0)
    off = _Expert(regime_overlay_enabled=False, regime_risk_scale=1.0,
                  regime_stop_scale=1.0, regime_tp_scale=1.0)
    for name in ("regime_risk_scale", "regime_stop_scale", "regime_tp_scale"):
        for stressed in (True, False, None):
            assert regime_scale(on, name, stressed) == regime_scale(off, name, stressed)


def test_unknown_setting_name_raises():
    """A typo'd gene name must fail loudly, not silently resolve to neutral forever."""
    expert = _Expert(regime_overlay_enabled=True)
    with pytest.raises(ValueError):
        regime_scale(expert, "regime_typo_scale", True)


def test_overlay_enabled_defaults_false():
    assert overlay_enabled(None) is False
    assert overlay_enabled(_Expert()) is False
    assert overlay_enabled(_Expert(regime_overlay_enabled=True)) is True


# --------------------------------------------------------------------------------------------
# scale_percent
# --------------------------------------------------------------------------------------------

def test_scale_percent_preserves_none_and_sign():
    assert scale_percent(None, 2.0) is None
    assert scale_percent(10.0, 1.5) == 15.0
    assert scale_percent(-9.0, 2.0) == -18.0          # entry stops are written negative
    assert scale_percent(10.0, NEUTRAL_SCALE) == 10.0


# --------------------------------------------------------------------------------------------
# StressedCalendar -- must equal the classifier, day for day
# --------------------------------------------------------------------------------------------

def _synthetic_closes(n: int, seed: int = 7):
    """A price path with a deliberate volatility burst, so the sample spans calm AND stressed."""
    rng = random.Random(seed)
    dates, closes = [], []
    px = 100.0
    d = date(2015, 1, 1)
    for i in range(n):
        # Two vol regimes so the percentile rank actually crosses its band.
        sigma = 0.035 if 600 <= i < 700 else 0.007
        px *= math.exp(rng.gauss(0.0002, sigma))
        d += timedelta(days=1)
        dates.append(d)
        closes.append(px)
    return dates, closes


def test_calendar_matches_classifier_every_day():
    """The speed reimplementation must be bit-identical to market_regime.is_stressed."""
    dates, closes = _synthetic_closes(700)
    cal = StressedCalendar(dates, closes)
    for i, d in enumerate(dates):
        assert cal.at(d) == mr.is_stressed(closes[:i + 1]), f"diverged on day {i} ({d})"


def test_calendar_sample_actually_contains_both_states():
    """Guards the test above from passing vacuously on an all-False series."""
    dates, closes = _synthetic_closes(700)
    cal = StressedCalendar(dates, closes)
    flags = [cal.at(d) for d in dates]
    assert any(flags), "sample never reaches STRESSED -- the equivalence test proves nothing"
    assert not all(flags)


def test_calendar_is_causal():
    """Truncating the future must not change any past day's answer."""
    dates, closes = _synthetic_closes(700)
    full = StressedCalendar(dates, closes)
    cut = 650
    truncated = StressedCalendar(dates[:cut], closes[:cut])
    for d in dates[:cut]:
        assert full.at(d) == truncated.at(d)


def test_calendar_before_first_day_is_none():
    dates, closes = _synthetic_closes(30)
    cal = StressedCalendar(dates, closes)
    assert cal.at(date(2000, 1, 1)) is None


def test_calendar_holds_between_benchmark_days():
    """A weekend/holiday bar resolves to the last benchmark day at or before it."""
    dates, closes = _synthetic_closes(700)
    cal = StressedCalendar(dates, closes)
    probe = dates[500]
    assert cal.at(probe + timedelta(hours=13)) == cal.at(probe)


def test_calendar_accepts_datetime_and_string_keys():
    dates, closes = _synthetic_closes(700)
    cal = StressedCalendar(dates, closes)
    probe = dates[500]
    expected = cal.at(probe)
    assert cal.at(datetime(probe.year, probe.month, probe.day, 14, 30, tzinfo=timezone.utc)) == expected
    assert cal.at(probe.isoformat()) == expected


def test_calendar_empty_input():
    assert StressedCalendar([], []).at(date(2020, 1, 1)) is None


def test_calendar_short_history_is_not_stressed():
    """Below window+lookback closes the classifier cannot answer; that must read as NOT stressed."""
    dates, closes = _synthetic_closes(100)
    cal = StressedCalendar(dates, closes)
    assert all(cal.at(d) is False for d in dates)
