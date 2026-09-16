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

import pytest

from ba2_common.core import rule_builders
from ba2_common.core.market_condition_rules import (
    STRICT_FIELD_NAMES,
    assert_market_conditions_resolved,
    assert_no_market_conditions,
    market_condition_fields,
)
from ba2_common.core.market_conditions import PROFILES
from ba2_common.core.rules_convert import trade_rules_to_live_export


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


def _resolved_leaf(**over):
    """The same leaf after a decode: the chosen mode written on as operator + threshold. The
    RANGE survives the decode (only op/comparison/value/mode are rewritten), which is what keeps
    the leaf numeric to ``rule_models``."""
    leaf = _template_leaf(mode="below", op="<", comparison="<", value=18.0, **over)
    leaf.pop("mode_optimize")
    return leaf


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
    assert trigger == {"event_type": "underlying_adx_14", "operator": "<", "value": 18.0}


#: A DECODED categorical leaf: the mode became ``== <registry code>``. Its field is strict but
#: not registered until Task 10 lands ``ta-structure-v1``, which makes it the honest stand-in for
#: "a payload from a NEWER server" in the old-server test below.
CATEGORICAL_LEAF = {"id": "o_lc-market-structure-state", "field": "structure_state",
                    "mode": "bull", "op": "==", "comparison": "==", "value": 1.0}


def test_a_resolved_categorical_leaf_is_accepted_as_an_equality_on_the_code():
    """A categorical mode decodes to ``== <code>`` and no threshold; that IS resolved."""
    assert_market_conditions_resolved(_entry_rule(CATEGORICAL_LEAF), "entry_rules")
    unresolved = dict(CATEGORICAL_LEAF, mode_optimize=True,
                      mode_choices=["off", "bull", "bear"])
    with pytest.raises(ValueError, match="mode_optimize"):
        assert_market_conditions_resolved(_entry_rule(unresolved), "entry_rules")


def test_a_payload_from_a_newer_server_is_refused_rather_than_deployed_ungated():
    """``structure_state`` is a strict name this build has no event type for (Task 10 registers
    it). Exporting it to live must refuse -- dropping the gate would deploy a different strategy
    under the same name and the same label."""
    with pytest.raises(ValueError, match="no event type for"):
        trade_rules_to_live_export(_entry_rule(CATEGORICAL_LEAF), [])


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
