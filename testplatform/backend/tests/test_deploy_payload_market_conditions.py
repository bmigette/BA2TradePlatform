"""What a market-condition gate may and may not do on the way to a LIVE instance (Task 8, D§5/6).

Three refusals, each closing a failure that is otherwise silent:

* an UNRESOLVED mode gene must not be exported. ``mode_optimize`` is the optimizer's search
  template; live has no optimizer, so the leaf would arrive describing a space instead of a rule.
* a market-condition leaf must not sit in an OPEN-POSITIONS / exit ruleset. Outside a decision
  scope the live resolver has no context, so the gate reads ``no_context``, the rule never fires,
  and the exit or protective-order adjustment silently stops happening.
* a field the TARGET server cannot map must REJECT, not drop. ``triggers_from_condition_tree``
  warns and drops an unknown field -- correct for a hand-edited tree, catastrophic for a deploy:
  the gates vanish and the instance trades the strategy ungated under its own name.

A RESOLVED leaf, by contrast, has to travel end to end as an ordinary condition -- that is the
whole point of the mode decode.
"""
from __future__ import annotations

import importlib.util
import os
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # testplatform/backend
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from ba2_common.core import rule_builders
from ba2_common.core.market_condition_rules import (
    STRICT_FIELD_NAMES,
    assert_market_conditions_resolved,
    assert_no_market_conditions,
    iter_market_condition_leaves,
    market_condition_fields,
)
from ba2_common.core.market_conditions import PROFILES
from ba2_common.core.rules_convert import trade_rules_to_live_export

from app.services.strategy_param_space import collect_param_space, decode_params

_LAUNCHER = os.path.normpath(os.path.join(_ROOT, "..", "ba2test_launcher.py"))
_spec = importlib.util.spec_from_file_location("ba2test_launcher_deploy", _LAUNCHER)
launcher = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(launcher)


def _leaf(**over):
    leaf = {"id": "o_lc-market-adx", "field": "underlying_adx_14", "op": "<", "value": 25.0}
    leaf.update(over)
    return leaf


def _template_leaf(**over):
    """The launcher's authored leaf: a mode gene over a DECLARED threshold range. The range is
    what makes it numeric to ``rule_models`` -- ``value`` alone cannot, because a decoded
    CATEGORICAL leaf carries its registry code as ``value``."""
    return _leaf(mode_optimize=True, mode_choices=["off", "below", "above"],
                 value_min=10.0, value_max=40.0, value_step=5.0, **over)


def decoded_gated_rules(genome=None, kind="O_LC"):
    """A REAL decoded gated genome: the launcher builds the gated strategy, the GA space is
    collected from it, and ``decode_params`` produces the entry rules a run would persist.

    Hand-writing this fixture is what hid the defect it now guards: a decoded leaf that still
    carried ``mode_optimize`` made every real gated genome unexportable, and a fixture that popped
    the flag by hand agreed with the exporter about a shape the decoder never produced.
    """
    saved = launcher._MARKET_CONDITION_PROFILES
    launcher._MARKET_CONDITION_PROFILES = ("ohlcv-v1",)
    try:
        strategy = launcher._build_strategy(kind, f"deploy-{kind}", "FMPRating")
    finally:
        launcher._MARKET_CONDITION_PROFILES = saved
    space = collect_param_space(strategy)
    flat = dict(genome or {"cond:o_lc-market-adx:mode": "below",
                           "cond:o_lc-market-adx:value": 15.0,
                           "cond:o_lc-market-slope:mode": "off",
                           "cond:o_lc-market-slope:value": 0.1,
                           "cond:o_lc-market-rv:mode": "above",
                           "cond:o_lc-market-rv:value": 1.25})
    missing = [g for g in flat if g not in space]
    assert not missing, f"{kind} does not emit {missing}; the decode below would test nothing"
    decoded = decode_params(strategy, flat)
    return decoded["entry_rules"], decoded["exit_rules"]


def _resolved_leaf(**over):
    """One decoded market leaf, taken from the real decode above."""
    entry_rules, _exits = decoded_gated_rules()
    leaf = _market_leaf(entry_rules, "o_lc-market-adx")
    leaf.update(over)
    return leaf


def _market_leaf(rules, leaf_id):
    for label, leaf in iter_market_condition_leaves(rules, "rules"):
        if label == leaf_id:
            return dict(leaf)
    raise AssertionError(f"{leaf_id} not found in {[l for l, _ in iter_market_condition_leaves(rules, 'r')]}")


def _entry_rule(*leaves):
    return [{"id": "o_lc-entry", "name": "O_LC-entry",
             "conditions": {"id": "o_lc-root", "operator": "AND", "conditions": list(leaves)},
             "actions": [{"action_type": "buy"}]}]


def _exit_rule(*leaves):
    return [{"id": "x", "name": "exit", "actions": [{"action_type": "close"}],
             "conditions": {"id": "xr", "operator": "AND", "conditions": list(leaves)}}]


# --------------------------------------------------------------------- the registry of names
def test_every_registered_field_is_a_strict_name():
    """A registered field that is not strict would be DROPPED by an older server instead of
    refused -- so registering one without listing it here is the defect this catches."""
    registered = {f.name for prof in PROFILES.values() for f in prof.fields}
    assert registered <= STRICT_FIELD_NAMES
    assert registered <= market_condition_fields()


# --------------------------------------------------------------------- unresolved mode genes
def test_an_unresolved_template_leaf_is_refused():
    template = _template_leaf()
    with pytest.raises(ValueError, match="mode_optimize"):
        assert_market_conditions_resolved(_entry_rule(template), "entry_rules")
    with pytest.raises(ValueError, match="unresolved market-condition gene"):
        trade_rules_to_live_export(_entry_rule(template), [])


def test_an_off_mode_that_survived_the_decode_is_refused():
    """``off`` REMOVES the leaf; a payload that still carries one was never decoded."""
    with pytest.raises(ValueError, match="an off leaf is REMOVED"):
        assert_market_conditions_resolved(_entry_rule(_leaf(mode="off")), "entry_rules")


def test_a_mode_with_no_operator_or_no_threshold_is_refused():
    no_op = _leaf(mode="below")
    no_op.pop("op")
    with pytest.raises(ValueError, match="resolved to no operator"):
        assert_market_conditions_resolved(_entry_rule(no_op), "entry_rules")
    no_value = _leaf(mode="above", op=">", value=None)
    with pytest.raises(ValueError, match="no threshold value"):
        assert_market_conditions_resolved(_entry_rule(no_value), "entry_rules")


def test_a_resolved_numeric_leaf_exports_as_an_ordinary_condition():
    export = trade_rules_to_live_export(_entry_rule(_resolved_leaf()), [])
    rule, = export["rulesets"][0]["rules"]
    trigger, = [t for t in rule["triggers"].values() if t["event_type"] == "underlying_adx_14"]
    assert trigger == {"event_type": "underlying_adx_14", "operator": "<", "value": 15.0}


#: A DECODED categorical leaf of the REAL registry field: the mode became ``== <registry code>``.
#: ``ta-structure-v1`` is registered since Task 10, so this now travels end to end.
CATEGORICAL_LEAF = {"id": "o_lc-market-structure", "field": "structure_state",
                    "mode": "bull", "op": "==", "comparison": "==", "value": 1.0}


def test_a_resolved_categorical_leaf_is_accepted_as_an_equality_on_the_code():
    """A categorical mode decodes to ``== <code>`` and no threshold; that IS resolved."""
    assert_market_conditions_resolved(_entry_rule(CATEGORICAL_LEAF), "entry_rules")
    unresolved = dict(CATEGORICAL_LEAF, mode_optimize=True,
                      mode_choices=["off", "bull", "bear"])
    with pytest.raises(ValueError, match="mode_optimize"):
        assert_market_conditions_resolved(_entry_rule(unresolved), "entry_rules")


def test_a_resolved_categorical_leaf_exports_as_an_equality_on_the_registry_code():
    """The round trip Task 8 could only describe: with ``ta-structure-v1`` registered, a decoded
    categorical gate leaves for live as an ordinary ``==`` trigger whose value is the field's own
    registry CODE -- not the mode token, which live has no vocabulary for."""
    from ba2_common.core.market_conditions import field_codes

    export = trade_rules_to_live_export(_entry_rule(CATEGORICAL_LEAF), [])
    rule, = export["rulesets"][0]["rules"]
    trigger, = [t for t in rule["triggers"].values() if t["event_type"] == "structure_state"]
    assert trigger == {"event_type": "structure_state", "operator": "==", "value": 1.0}
    assert trigger["value"] == float(field_codes("structure_state")["bull"])
    # The OTHER regime exports as its own code, so the two are not interchangeable downstream.
    bear = trade_rules_to_live_export(
        _entry_rule(dict(CATEGORICAL_LEAF, mode="bear", value=2.0)), [])
    bear_trigger, = [t for rule in bear["rulesets"][0]["rules"]
                     for t in rule["triggers"].values() if t["event_type"] == "structure_state"]
    assert bear_trigger["value"] == float(field_codes("structure_state")["bear"]) == 2.0


def test_a_payload_from_a_newer_server_is_refused_rather_than_deployed_ungated(monkeypatch):
    """A strict name this build has NO event type for -- what a payload searched on a newer
    server looks like here. Exporting it must refuse: dropping the gate would deploy a different
    strategy under the same name and the same label.

    Every name in today's ``STRICT_FIELD_NAMES`` is registered (Task 10 completed the list), so
    the future field is added to the strict set for the duration of this test -- which is exactly
    the state an older server is in when the list travels ahead of its registry.
    """
    from ba2_common.core import market_condition_rules as rules_mod

    monkeypatch.setattr(rules_mod, "STRICT_FIELD_NAMES",
                        rules_mod.STRICT_FIELD_NAMES | {"market_regime_v2"})
    future = dict(CATEGORICAL_LEAF, id="o_lc-market-regime", field="market_regime_v2")
    with pytest.raises(ValueError, match="no event type for"):
        trade_rules_to_live_export(_entry_rule(future), [])


def test_a_really_decoded_genome_carries_no_template_metadata_and_exports():
    """THE case the hand-written fixture used to hide: a genome the GA actually produced.

    The `off` leaf is gone from the tree, the two active ones are ordinary numeric conditions,
    and nothing anywhere still claims to be an optimizer template.
    """
    entry_rules, exit_rules = decoded_gated_rules()
    leaves = dict(iter_market_condition_leaves(entry_rules, "entry_rules"))
    assert sorted(leaves) == ["o_lc-market-adx", "o_lc-market-rv"]  # the slope leaf decoded off
    for leaf in leaves.values():
        for key in ("mode_optimize", "modeOptimize", "mode_choices", "modeChoices"):
            assert key not in leaf, key
    assert_market_conditions_resolved(entry_rules, "entry_rules")
    assert_no_market_conditions(exit_rules, "exit_rules")

    export = trade_rules_to_live_export(entry_rules, exit_rules, name="gated")
    enter, = [r for r in export["rulesets"] if r["subtype"] == "enter_market"]
    triggers = [t for rule in enter["rules"] for t in rule["triggers"].values()]
    assert {"event_type": "underlying_adx_14", "operator": "<", "value": 15.0} in triggers
    assert {"event_type": "underlying_realized_vol_ratio_5_20", "operator": ">",
            "value": 1.25} in triggers
    assert not any(t["event_type"] == "underlying_trend_slope_50_atr14" for t in triggers)


def test_the_export_payload_of_a_decoded_genome_is_not_a_400(tmp_path):
    """End to end through the API derivation AND the deploy exporter script."""
    import json
    from types import SimpleNamespace

    from app.api.backtests import _derive_export_payload

    entry_rules, exit_rules = decoded_gated_rules()
    backtest = SimpleNamespace(
        id=4242, name="gated-run", expert_name="FMPRating", engine_type="daily_expert",
        strategy_params={"entryRules": entry_rules, "exitRules": exit_rules,
                         "cond:o_lc-market-adx:value": 15.0},
        start_date=None, end_date=None, initial_capital=20_000.0)
    payload = _derive_export_payload(backtest, "ruleset", None)
    exported = dict(iter_market_condition_leaves(payload["entry_rules"], "entry_rules"))
    assert sorted(exported) == ["o_lc-market-adx", "o_lc-market-rv"]
    assert exported["o_lc-market-adx"]["value"] == 15.0

    # ...and the same payload survives the live-export conversion the importer runs.
    live = trade_rules_to_live_export(payload["entry_rules"], payload["exit_rules"], name="gated")
    assert [r["subtype"] for r in live["rulesets"]] == ["enter_market", "open_positions"]
    json.dump(live, open(tmp_path / "payload.json", "w"))  # serialisable, as the tools write it


def test_an_undecoded_template_still_fails_that_same_export_path():
    """The counterpart: the search TEMPLATE (what the strategy row holds) must not export."""
    from fastapi import HTTPException
    from types import SimpleNamespace

    from app.api.backtests import _derive_export_payload

    saved = launcher._MARKET_CONDITION_PROFILES
    launcher._MARKET_CONDITION_PROFILES = ("ohlcv-v1",)
    try:
        strategy = launcher._build_strategy("O_LC", "deploy-template", "FMPRating")
    finally:
        launcher._MARKET_CONDITION_PROFILES = saved
    backtest = SimpleNamespace(
        id=4243, name="template", expert_name="FMPRating", engine_type="daily_expert",
        strategy_params={"entryRules": strategy.entry_rules, "exitRules": strategy.exit_rules},
        start_date=None, end_date=None, initial_capital=20_000.0)
    with pytest.raises(HTTPException) as e:
        _derive_export_payload(backtest, "ruleset", None)
    assert e.value.status_code == 400 and "mode_optimize" in str(e.value.detail)


# --------------------------------------------------------------------- exit rulesets
def test_a_market_leaf_in_an_exit_ruleset_is_refused():
    with pytest.raises(ValueError, match="not allowed in an open-positions / exit ruleset"):
        assert_no_market_conditions(_exit_rule(_leaf()), "exit_rules")
    with pytest.raises(ValueError, match="not allowed in an open-positions / exit ruleset"):
        trade_rules_to_live_export(_entry_rule(_resolved_leaf()), _exit_rule(_leaf()))


def test_an_ordinary_exit_ruleset_is_untouched():
    exits = _exit_rule({"id": "xtp", "field": "profit_loss_percent", "op": ">", "value": 20})
    assert_no_market_conditions(exits, "exit_rules")
    export = trade_rules_to_live_export([], exits)
    assert export["rulesets"][0]["subtype"] == "open_positions"


def test_the_api_save_path_refuses_a_market_leaf_on_an_exit_rule():
    from fastapi import HTTPException

    from app.api.strategies import StrategyCreate, _resolve_rule_lists

    ok = StrategyCreate(name="s", entry_rules=_entry_rule(_resolved_leaf()),
                        exit_rules=_exit_rule({"id": "xtp", "field": "profit_loss_percent",
                                               "op": ">", "value": 20}))
    entry, exits = _resolve_rule_lists(ok)
    assert entry and exits
    bad = StrategyCreate(name="s", entry_rules=[], exit_rules=_exit_rule(_leaf()))
    with pytest.raises(HTTPException) as e:
        _resolve_rule_lists(bad)
    assert e.value.status_code == 400 and "exit ruleset" in str(e.value.detail)


def test_the_save_path_still_accepts_the_optimizer_template_on_an_entry_rule():
    """A SAVED strategy is the search template; only the export/deploy requires a resolved one."""
    from app.api.strategies import StrategyCreate, _resolve_rule_lists

    payload = StrategyCreate(name="s", entry_rules=_entry_rule(_template_leaf()), exit_rules=[])
    entry, _exits = _resolve_rule_lists(payload)
    assert entry[0]["conditions"]["conditions"][0]["modeOptimize"] is True


def test_the_deploy_tools_route_through_the_checked_paths_and_report_a_refusal():
    """``tools/export_deploy_payload.py`` is a thin wrapper around ``_derive_export_payload`` and
    ``tools/import_deploy_payload.py`` around ``trade_rules_to_live_export`` -- which is what makes
    the checks above cover the deploy path. Both must turn a refusal into a message and write
    NOTHING for that entry: a half-written plan is worse than none, because the missing entry is
    the one nobody notices.

    Read from the scripts' source: running them means a live DB and a chdir into the backend.
    """
    tools = os.path.normpath(os.path.join(_ROOT, "..", "..", "tools"))
    exporter = open(os.path.join(tools, "export_deploy_payload.py"), encoding="utf-8").read()
    importer = open(os.path.join(tools, "import_deploy_payload.py"), encoding="utf-8").read()

    assert "_derive_export_payload(bt, \"ruleset\", db)" in exporter
    body = exporter[exporter.index("def main("):]
    guarded = body[body.index("_derive_export_payload"):]
    assert "FATAL" in guarded and "return 1" in guarded
    assert body.index("_derive_export_payload") < body.index("json.dump(payloads")

    assert "trade_rules_to_live_export(entry_rules, exit_rules, name=label)" in importer
    after = importer[importer.index("trade_rules_to_live_export(entry_rules"):]
    assert "except ValueError" in after and "FATAL" in after


# --------------------------------------------------------------------- an older target server
def test_a_field_this_server_cannot_map_is_refused_instead_of_dropped(monkeypatch):
    """Simulates the OLD-SERVER import: the field is strict, the event vocabulary lacks it."""
    mapping = dict(rule_builders.FIELD_EVENT)
    mapping.pop("underlying_adx_14")
    monkeypatch.setattr(rule_builders, "FIELD_EVENT", mapping)
    monkeypatch.setattr("ba2_common.core.rules_convert.FIELD_EVENT", mapping, raising=False)
    with pytest.raises(ValueError, match="no event type for"):
        trade_rules_to_live_export(_entry_rule(_resolved_leaf()), [])


def test_an_unknown_NON_market_field_is_still_dropped_with_a_warning(monkeypatch):
    """The editing paths keep their forgiving behaviour: only the market names are strict.

    The ba2_common logger does not propagate to the root logger, so the warning is recorded
    through the module's own logger object rather than caplog.
    """
    warnings: list = []
    monkeypatch.setattr(rule_builders.logger, "warning",
                        lambda msg, *a: warnings.append(msg % a if a else msg))
    export = trade_rules_to_live_export(
        _entry_rule({"id": "weird", "field": "not_a_registered_field", "op": ">", "value": 1}), [])
    rule, = export["rulesets"][0]["rules"]
    assert rule["triggers"] == {}
    assert any("DROPPED" in w for w in warnings)


# ------------------------------------------------- the profile setting travels with the payload
def test_the_export_payload_carries_the_profile_setting_from_the_runs_expert_spec():
    """Task 12: the profile is an expert SETTING, so it rides in ``settings.expert_params`` --
    the same dict the importer feeds to ``save_settings`` -- and needs no transport of its own.

    Pinned because the export builds ``expert_params`` from the optimization's expert spec
    settings, which is where ``_apply_market_conditions`` writes the setting; a change to either
    end would silently deploy a gated ruleset with an empty profile.
    """
    from types import SimpleNamespace

    from app.api.backtests import _derive_export_payload

    entry_rules, exit_rules = decoded_gated_rules()
    opt_block = {
        "experts": [{"class": "FMPRating",
                     "settings": {"market_condition_profile": "ohlcv-v1"}}],
        "enabled_instruments": ["AAA"], "seed": 1, "warmup_days": 0, "account_settings": {},
        "market_condition_profiles": ["ohlcv-v1"],
        "market_condition_manifests": {"ohlcv-v1": "a" * 64},
    }
    backtest = SimpleNamespace(
        id=4244, name="gated-run", expert_name="FMPRating", engine_type="daily_expert",
        strategy_params={"entryRules": entry_rules, "exitRules": exit_rules},
        optimization_id=None, start_date=None, end_date=None, initial_capital=20_000.0)
    import app.api.backtests as bt_api

    saved = bt_api._opt_backtest_block
    bt_api._opt_backtest_block = lambda backtest, db: (opt_block, None)
    try:
        payload = _derive_export_payload(backtest, "expert_settings", None)
    finally:
        bt_api._opt_backtest_block = saved
    assert payload["settings"]["expert_params"]["market_condition_profile"] == "ohlcv-v1"


def _import_refusal(expert_params, entry_rules):
    """The importer's market-condition refusal, as the tool runs it (same two functions, same
    order). Returns the message, or None when the payload is accepted."""
    from ba2_common.core.market_condition_rules import (
        PROFILE_SETTING, assert_market_fields_served, parse_profile_setting,
    )

    try:
        profiles = parse_profile_setting(expert_params.get(PROFILE_SETTING))
        assert_market_fields_served(entry_rules, profiles, where="label: entry rules")
    except ValueError as e:
        return str(e)
    return None


def test_a_served_gated_payload_imports():
    entry_rules, _ = decoded_gated_rules()
    assert _import_refusal({"market_condition_profile": "ohlcv-v1"}, entry_rules) is None


def test_the_import_refuses_a_profile_this_server_does_not_register():
    """A payload built against a NEWER ba2_common. The live resolver could not build a reader for
    it at all, so every gated entry would be refused for ever -- said at import instead."""
    entry_rules, _ = decoded_gated_rules()
    msg = _import_refusal({"market_condition_profile": "ohlcv-v9"}, entry_rules)
    assert msg and "ohlcv-v9" in msg and "not a registered" in msg


def test_the_import_refuses_a_gated_ruleset_whose_setting_serves_nothing():
    """THE deploy-time shape of this whole task: the ruleset half arrives gated and the setting
    half arrives empty, and the instance comes up enabled, scheduled and unable to enter."""
    entry_rules, _ = decoded_gated_rules()
    for params in ({}, {"market_condition_profile": ""}):
        msg = _import_refusal(params, entry_rules)
        assert msg and "o_lc-market-adx" in msg and "underlying_adx_14" in msg
        assert "market_condition_profile" in msg and "empty" in msg


def test_the_import_refuses_a_leaf_whose_field_the_listed_profile_does_not_serve():
    """Two profiles exist; naming the wrong one is not the same as naming none, and the message
    has to say which field is unserved rather than "no profile"."""
    entry_rules, _ = decoded_gated_rules()
    msg = _import_refusal({"market_condition_profile": "ta-structure-v1"}, entry_rules)
    assert msg and "underlying_adx_14" in msg and "ta-structure-v1" in msg


def test_an_ungated_payload_is_unaffected_by_the_refusal():
    """Every existing deploy: no market leaf, so any setting (including none at all) passes."""
    plain = _entry_rule({"id": "conf", "field": "confidence", "op": ">", "value": 70})
    assert _import_refusal({}, plain) is None
    assert _import_refusal({"market_condition_profile": "ohlcv-v1"}, plain) is None


def test_the_import_tool_runs_that_refusal_before_it_writes_anything():
    """Read from the script's source (running it means a live DB): the two calls must sit between
    the expert_params assembly and the first ``save_settings``/``update_instance``, and a refusal
    must print FATAL and return without writing -- the same contract the ruleset conversion has."""
    tools = os.path.normpath(os.path.join(_ROOT, "..", "..", "tools"))
    importer = open(os.path.join(tools, "import_deploy_payload.py"), encoding="utf-8").read()
    body = importer[importer.index("def main("):]

    assert "parse_profile_setting(expert_params.get(PROFILE_SETTING))" in body
    assert "assert_market_fields_served(entry_rules, mc_profiles" in body
    guard = body.index("assert_market_fields_served(entry_rules")
    after = body[guard:]
    assert "FATAL" in after[:600] and "return 1" in after[:600]
    assert guard < body.index("save_settings")
