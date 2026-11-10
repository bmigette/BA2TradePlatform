"""Down-trend overlay genes: searched under atr-regime-searched, pinned OFF otherwise; the run
builds the SPY benchmark calendar whenever the trigger can fire (even with the spy gate flag off)."""
import pytest

import ba2test_launcher as L
from app.services.backtest.daily_backtest_handler import (
    _config_uses_downtrend_overlay, _run_needs_benchmark_regime,
)
from app.services.strategy_param_space import INERT_RM_TOGGLES, pinned_rm_toggles

_SCALES = ("downtrend_risk_scale", "downtrend_stop_scale", "downtrend_tp_scale")


def test_genes_present_under_the_searched_policy():
    block = L._rm_opt_for("S1", atr_searched=True, regime_searched=True)
    assert block["downtrend_overlay_enabled"] == {"optimize": True, "min": 0, "max": 1,
                                                  "step": 1, "type": "int"}
    for gene in _SCALES:
        assert block[gene] == {"optimize": True, "min": 0.5, "max": 2.0, "step": 0.25,
                               "type": "float"}


def test_no_downtrend_scale_genes_elsewhere_and_toggle_not_searched():
    for kwargs in ({}, {"atr_searched": True}):
        block = L._rm_opt_for("S1", **kwargs)
        assert not any(g in block for g in _SCALES)
        assert block.get("downtrend_overlay_enabled", {"optimize": False})["optimize"] is False


def test_off_unless_the_regime_policy_searches_it():
    # not a gene (so never decoded on) under the pinned and atr-searched policies, and OFF by
    # its interface default (see test_regime_overlay_downtrend)
    for kwargs in ({}, {"atr_searched": True}):
        assert L._rm_opt_for("S1", **kwargs).get("downtrend_overlay_enabled",
                                                 {"optimize": False})["optimize"] is False
    assert INERT_RM_TOGGLES == L._INERT_RM_TOGGLES
    assert pinned_rm_toggles([]) == L._pinned_rm_toggles([])


def _cfg(value):
    return {"experts": [{"class": "FMPRating", "settings": {"downtrend_overlay_enabled": value}}]}


@pytest.mark.parametrize("value,expected", [(1, True), ("1", True), (True, True),
                                            (0, False), ("0", False), (False, False)])
def test_calendar_needed_whenever_the_trigger_can_fire(value, expected):
    assert _config_uses_downtrend_overlay(_cfg(value)) is expected
    # spy gate flag OFF in every case
    assert _run_needs_benchmark_regime(_cfg(value)) is expected


def test_no_setting_no_calendar():
    assert not _run_needs_benchmark_regime({"experts": [{"class": "FMPRating", "settings": {}}]})
