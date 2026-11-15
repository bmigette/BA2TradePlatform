"""iv-v1 gates on the way to a LIVE instance (docs/plans/2026-11-15-option-iv-conditions.md).

* the EXPORT carries a decoded iv leaf end to end as an ordinary condition (the strict field names
  include all five iv fields, so an older server refuses rather than drops them);
* an unresolved iv template is refused like any other mode gene;
* the IMPORT refuses an iv leaf because live has no source for it -- the gate would refuse every
  live entry -- while a payload whose iv genes all decoded OFF imports (nothing reads the profile).
"""
from __future__ import annotations

import importlib.util
import os
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # testplatform/backend
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from ba2_common.core.market_condition_rules import (  # noqa: E402
    STRICT_FIELD_NAMES, assert_market_conditions_resolved, assert_market_fields_live_servable,
    assert_market_fields_served,
)
from ba2_common.core.market_conditions import IV_PROFILE, PROFILES  # noqa: E402
from ba2_common.core.rules_convert import trade_rules_to_live_export  # noqa: E402

from app.services.strategy_param_space import decode_params  # noqa: E402

_LAUNCHER = os.path.normpath(os.path.join(_ROOT, "..", "ba2test_launcher.py"))
_spec = importlib.util.spec_from_file_location("ba2test_launcher_ivdeploy", _LAUNCHER)
launcher = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(launcher)


def _decoded(genome):
    saved = launcher._MARKET_CONDITION_PROFILES
    launcher._MARKET_CONDITION_PROFILES = (IV_PROFILE,)
    try:
        strat = launcher._build_strategy("O_BULLPS", "ivdeploy", "FMPRating")
    finally:
        launcher._MARKET_CONDITION_PROFILES = saved
    return decode_params(strat, genome)


_ON = {"cond:o_bullps-market-ivrv:mode": "above", "cond:o_bullps-market-ivrv:value": 1.3,
       "cond:o_bullps-market-ivterm:mode": "below", "cond:o_bullps-market-ivterm:value": 1.05}
_OFF = {"cond:o_bullps-market-ivrv:mode": "off", "cond:o_bullps-market-ivrv:value": 1.3,
        "cond:o_bullps-market-ivterm:mode": "off", "cond:o_bullps-market-ivterm:value": 1.05}


def test_every_iv_field_is_strict():
    assert {f.name for f in PROFILES[IV_PROFILE].fields} <= STRICT_FIELD_NAMES


def test_a_decoded_iv_genome_exports_as_ordinary_triggers():
    decoded = _decoded(_ON)
    assert_market_conditions_resolved(decoded["entry_rules"], "entry_rules")
    export = trade_rules_to_live_export(decoded["entry_rules"], decoded["exit_rules"], name="ivdeploy")
    triggers = [t for rs in export["rulesets"] for r in rs["rules"] for t in r["triggers"].values()]
    by_event = {t["event_type"]: t for t in triggers if t["event_type"].startswith("underlying_iv")}
    assert by_event["underlying_iv_rv_ratio_30_20"]["operator"] == ">"
    assert by_event["underlying_iv_rv_ratio_30_20"]["value"] == pytest.approx(1.3)
    assert by_event["underlying_iv_term_slope_30_60"]["operator"] == "<"
    assert by_event["underlying_iv_term_slope_30_60"]["value"] == pytest.approx(1.05)


def test_an_unresolved_iv_template_is_refused():
    saved = launcher._MARKET_CONDITION_PROFILES
    launcher._MARKET_CONDITION_PROFILES = (IV_PROFILE,)
    try:
        strat = launcher._build_strategy("O_BULLPS", "ivdeploy", "FMPRating")
    finally:
        launcher._MARKET_CONDITION_PROFILES = saved
    with pytest.raises(ValueError, match="mode_optimize"):
        assert_market_conditions_resolved(strat.entry_rules, "entry_rules")


def test_the_served_check_requires_the_iv_profile_in_the_setting():
    rules = _decoded(_ON)["entry_rules"]
    assert_market_fields_served(rules, (IV_PROFILE,), where="entry")
    assert_market_fields_served(rules, ("ohlcv-v1", IV_PROFILE), where="entry")
    with pytest.raises(ValueError, match="no configured profile"):
        assert_market_fields_served(rules, ("ohlcv-v1",), where="entry")


def test_the_live_import_refuses_an_iv_gate_with_the_open_decision():
    rules = _decoded(_ON)["entry_rules"]
    with pytest.raises(ValueError, match="NO live data source") as e:
        assert_market_fields_live_servable(rules, "ivdeploy: entry rules")
    assert "o_bullps-market-ivrv" in str(e.value) and "iv-v1" in str(e.value)


def test_an_all_off_iv_genome_imports():
    rules = _decoded(_OFF)["entry_rules"]
    assert_market_fields_live_servable(rules, "entry")         # nothing reads iv-v1
    assert_market_conditions_resolved(rules, "entry")


def test_the_importer_drops_an_unread_iv_profile_and_keeps_a_read_one():
    """Review 2026-11-15: an all-off iv genome still carries iv-v1 in its setting; live would build a
    refusing reader and log "NO live data source" every pass for a profile nothing reads."""
    from ba2_common.core.market_condition_rules import drop_unread_live_unavailable_profiles

    off = _decoded(_OFF)
    assert drop_unread_live_unavailable_profiles(("ohlcv-v1", IV_PROFILE), off["entry_rules"], off["exit_rules"]) \
        == (("ohlcv-v1",), (IV_PROFILE,))
    assert drop_unread_live_unavailable_profiles((IV_PROFILE,), off["entry_rules"]) == ((), (IV_PROFILE,))
    on = _decoded(_ON)
    assert drop_unread_live_unavailable_profiles((IV_PROFILE,), on["entry_rules"], on["exit_rules"]) \
        == ((IV_PROFILE,), ())
    # an OHLCV profile nothing reads is NOT this helper's business: it has a live source
    assert drop_unread_live_unavailable_profiles(("ohlcv-v1",), off["entry_rules"]) == (("ohlcv-v1",), ())


def test_the_import_tool_drops_it_before_the_served_and_live_checks():
    tools = os.path.normpath(os.path.join(_ROOT, "..", "..", "tools"))
    body = open(os.path.join(tools, "import_deploy_payload.py"), encoding="utf-8").read()
    body = body[body.index("def main("):]
    drop = body.index("drop_unread_live_unavailable_profiles(mc_profiles, entry_rules, exit_rules)")
    assert drop < body.index("assert_market_fields_live_servable(entry_rules")
    assert drop < body.index("assert_market_fields_served(entry_rules")
    assert drop < body.index("save_settings(")
    assert "expert_params[PROFILE_SETTING] = \",\".join(mc_profiles)" in body
