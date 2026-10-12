"""The API's screener-optimization weave (``strategies._merge_screener_opt``): the web UI (``Backtesting.tsx``) sends
``screener_opt = {param_ranges, cadence_days, base_settings[, store]}`` only -- no ``apply_to_expert_settings``, no panel stamp, no
``screener_universe_rule`` / ``enabled_instruments``.  Such a request cannot build the job's static universe, so it is REFUSED with
a message naming the CLI (the UI shows ``detail``); a request replaying a launcher-stamped config (classic or bypass) is accepted."""
import pytest
from fastapi import HTTPException

from app.api.strategies import _merge_screener_opt
from ba2_providers.screener.universe_superset import RULE_ID

UI_BODY = {"param_ranges": {"market_cap_min": {"optimize": True, "min": 1e9, "max": 5e9, "step": 1e9, "type": "float"}},
           "cadence_days": 7, "base_settings": {"market_cap_min": 2e9}}


def test_the_web_ui_request_shape_is_refused_with_the_cli_instruction_for_classic_and_factor_ranker():
    for cfg in ({"backtest": {"enabled_instruments": ["AAPL"]}}, {"backtest": {}}, {}):
        with pytest.raises(HTTPException) as e:
            _merge_screener_opt(cfg, dict(UI_BODY))
        assert e.value.status_code == 400 and "ba2-test optimize --screener" in e.value.detail
        assert "backtest" not in cfg or "screener_opt" not in cfg["backtest"]
    with pytest.raises(HTTPException):                    # a bypass flag alone (no stamp, no universe) is not enough
        _merge_screener_opt({"backtest": {}}, {**UI_BODY, "apply_to_expert_settings": True})


def test_a_launcher_stamped_classic_and_bypass_config_is_accepted():
    classic = {"backtest": {"screener_universe_rule": RULE_ID, "enabled_instruments": ["AAPL"]}}
    _merge_screener_opt(classic, {**UI_BODY, "criteria_version": "v", "panel": "screener/daily_panel/x", "panel_fingerprint": "f"})
    assert classic["backtest"]["screener_opt"]["panel_fingerprint"] == "f"
    assert "screener:market_cap_min" in classic["expert_params"]
    bypass = {"backtest": {"screener_universe_rule": RULE_ID, "enabled_instruments": ["AAPL"]}}
    _merge_screener_opt(bypass, {**UI_BODY, "apply_to_expert_settings": True})
    assert bypass["backtest"]["screener_opt"]["apply_to_expert_settings"] is True
