"""Down-trend trigger of the regime overlay: regime_scale is the PRODUCT of the active triggers."""
import pytest

from ba2_common.core import benchmark_regime as br
from ba2_common.core import market_regime as mr
from ba2_common.core.regime_overlay import (
    NEUTRAL_SCALE, is_downtrend_now, regime_scale, reset_stressed,
)

_PAIRS = (("regime_risk_scale", "downtrend_risk_scale"),
          ("regime_stop_scale", "downtrend_stop_scale"),
          ("regime_tp_scale", "downtrend_tp_scale"))


class _Expert:
    def __init__(self, **settings):
        self._settings = settings

    def get_setting_with_interface_default(self, name, log_warning=True):
        return self._settings.get(name)


@pytest.fixture(autouse=True)
def _clean():
    reset_stressed()
    br.reset_benchmark_regime()
    yield
    reset_stressed()
    br.reset_benchmark_regime()


def _publish(trend):
    br.set_benchmark_regime(br.BenchmarkRegime(trend, mr.NORMAL))


def _expert(stressed_on=True, down_on=True):
    return _Expert(regime_overlay_enabled=stressed_on, regime_risk_scale=0.5,
                   regime_stop_scale=1.5, regime_tp_scale=2.0,
                   downtrend_overlay_enabled=down_on, downtrend_risk_scale=0.75,
                   downtrend_stop_scale=0.5, downtrend_tp_scale=1.25)


def test_stressed_only():
    _publish(mr.TREND_UP)
    assert regime_scale(_expert(), "regime_risk_scale", True) == 0.5


@pytest.mark.parametrize("trend", [mr.TREND_DOWN, mr.TREND_BEAR_RALLY])
def test_below_200_only(trend):
    _publish(trend)
    e = _expert()
    for _stressed_name, down_name in _PAIRS:
        assert regime_scale(e, _stressed_name, False) == e._settings[down_name]


def test_both_is_the_product():
    _publish(mr.TREND_DOWN)
    e = _expert()
    assert regime_scale(e, "regime_risk_scale", True) == pytest.approx(0.5 * 0.75)
    assert regime_scale(e, "regime_stop_scale", True) == pytest.approx(1.5 * 0.5)
    assert regime_scale(e, "regime_tp_scale", True) == pytest.approx(2.0 * 1.25)


@pytest.mark.parametrize("trend", [mr.TREND_UP, mr.TREND_PULLBACK])
def test_above_200_does_not_trigger(trend):
    _publish(trend)
    assert not is_downtrend_now()
    assert regime_scale(_expert(), "regime_risk_scale", False) == NEUTRAL_SCALE


def test_unknown_trend_and_nothing_published_are_off():
    assert regime_scale(_expert(), "regime_risk_scale", False) == NEUTRAL_SCALE
    br.set_benchmark_regime(br.BenchmarkRegime(None, None, "insufficient"))
    assert not is_downtrend_now()
    assert regime_scale(_expert(), "regime_risk_scale", None) == NEUTRAL_SCALE


def test_disabled_trigger_is_ignored_even_in_a_downtrend():
    _publish(mr.TREND_DOWN)
    e = _expert(down_on=False)
    assert regime_scale(e, "regime_risk_scale", False) == NEUTRAL_SCALE
    assert regime_scale(e, "regime_risk_scale", True) == 0.5


def test_downtrend_works_without_the_stressed_overlay():
    _publish(mr.TREND_DOWN)
    e = _expert(stressed_on=False)
    assert regime_scale(e, "regime_stop_scale", True) == 0.5


def test_default_settings_leave_every_existing_result_unchanged():
    # no downtrend keys at all (old expert / genome), whatever the benchmark publishes
    for trend in (mr.TREND_UP, mr.TREND_DOWN, mr.TREND_BEAR_RALLY, None):
        _publish(trend)
        old = _Expert(regime_overlay_enabled=True, regime_risk_scale=0.5)
        assert regime_scale(old, "regime_risk_scale", True) == 0.5
        assert regime_scale(old, "regime_risk_scale", False) == NEUTRAL_SCALE
        assert regime_scale(old, "regime_tp_scale", True) == NEUTRAL_SCALE
        assert regime_scale(None, "regime_risk_scale", True) == NEUTRAL_SCALE


def test_interface_declares_the_four_settings_default_off():
    from ba2_common.core.interfaces.MarketExpertInterface import MarketExpertInterface

    MarketExpertInterface._ensure_builtin_settings()
    defs = MarketExpertInterface._builtin_settings
    assert defs["downtrend_overlay_enabled"]["default"] is False
    for name in ("downtrend_risk_scale", "downtrend_stop_scale", "downtrend_tp_scale"):
        assert defs[name]["type"] == "float" and defs[name]["default"] == 1.0
