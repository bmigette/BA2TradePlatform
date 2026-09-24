"""Market-condition leaves on EXIT rules: the action allow-list (plan 2026-09-24 Task B2).

Before B2 a market leaf was refused anywhere in an exit / open-positions ruleset: live had no
decision context on the exit pass, so the leaf could never fire while the backtest evaluated it.
Task B1 opened that scope; on the exit pass a failed read is UNKNOWN and the rule does not fire.
That is safe only for a rule that CLOSES, REDUCES (``decrease_instrument_share``) or ADJUSTS
TP/SL, and only when every market leaf sits in a top-level AND: a nested OR is flattened to AND
on the live export, and a NOT would turn "unknown -> does not fire" into "unknown -> fires".

Pinned here, for the tree form (``assert_market_rule_actions``), the live EventAction form
(``assert_market_rule_actions_live``) and the shared deploy converter
(``trade_rules_to_live_export``) that every deploy goes through.
"""
from __future__ import annotations

import pytest

from ba2_common.core.market_condition_rules import (
    MARKET_RULE_ACTIONS,
    assert_market_rule_actions,
    assert_market_rule_actions_live,
    assert_no_market_conditions,
)
from ba2_common.core.rules_convert import trade_rules_to_live_export
from ba2_common.core.types import ExpertActionType

ADX = "underlying_adx_14"
ROLL = ExpertActionType.ROLL_PMCC_SHORT.value
ALLOWED = ("close", "decrease_instrument_share", "adjust_stop_loss", "adjust_take_profit")


def _leaf(**over):
    leaf = {"id": "mkt-adx", "field": ADX, "op": "<", "value": 20.0}
    leaf.update(over)
    return leaf


def _ordinary(**over):
    leaf = {"id": "pl", "field": "profit_loss_percent", "op": "<", "value": -5.0}
    leaf.update(over)
    return leaf


def _action(action_type):
    if action_type in ("adjust_stop_loss", "adjust_take_profit"):
        return {"action_type": action_type, "reference_value": "order_open_price",
                "action_value": -2.0 if action_type == "adjust_stop_loss" else 20.0}
    if action_type == "decrease_instrument_share":
        return {"action_type": action_type, "action_value": 50.0}
    return {"action_type": action_type}


def _rule(*action_types, conditions=None, rid="mkt-exit"):
    return {"id": rid, "name": rid,
            "conditions": conditions if conditions is not None else
            {"id": "grp", "operator": "AND", "conditions": [_leaf()]},
            "actions": [_action(a) for a in action_types]}


def _refused(rules, *needles):
    with pytest.raises(ValueError) as e:
        assert_market_rule_actions(rules, "exit_rules")
    msg = str(e.value)
    for needle in needles:
        assert needle in msg, (needle, msg)
    return msg


# ------------------------------------------------------------------------ the allow-list
def test_the_allow_list_is_exactly_close_reduce_and_adjust_tp_sl():
    assert MARKET_RULE_ACTIONS == frozenset(ALLOWED)
    for name in ALLOWED:
        assert name in {m.value for m in ExpertActionType}, name


@pytest.mark.parametrize("action_type", ALLOWED)
def test_each_allowed_action_alone_is_accepted(action_type):
    assert assert_market_rule_actions([_rule(action_type)], "exit_rules") is None


def test_several_allowed_actions_on_one_rule_are_accepted():
    assert assert_market_rule_actions(
        [_rule("adjust_stop_loss", "adjust_take_profit")], "exit_rules") is None


@pytest.mark.parametrize("bad", ["buy", "stop_processing", ROLL])
def test_a_disallowed_action_is_refused_naming_rule_action_and_allowed_set(bad):
    msg = _refused([_rule(bad)], "mkt-exit", repr(bad), "mkt-adx")
    for name in ALLOWED:
        assert name in msg


def test_one_disallowed_action_beside_an_allowed_one_is_refused():
    """``close`` + ``stop_processing``: the stop would let an unknown read silence the rules
    below it, so the pair is refused and the message names only the offender."""
    msg = _refused([_rule("close", "stop_processing")], "'stop_processing'")
    assert "['stop_processing']" in msg


def test_a_market_rule_with_no_action_is_refused():
    _refused([_rule()], "<no action>")


def test_an_action_whose_type_cannot_be_read_is_refused_not_skipped():
    rule = _rule("close")
    rule["actions"].append({"reference_value": "order_open_price"})
    _refused([rule], "<missing>")


def test_every_action_spelling_the_converters_accept_is_read():
    """``rule_models.ActionCfg`` accepts ``action_type`` / ``action`` / ``actionType``."""
    for key in ("action_type", "action", "actionType"):
        ok = _rule()
        ok["actions"] = [{key: "close"}]
        assert assert_market_rule_actions([ok], "exit_rules") is None
        bad = _rule()
        bad["actions"] = [{key: "buy"}]
        _refused([bad], "'buy'")


def test_a_legacy_single_action_row_is_judged_by_its_top_level_action():
    """The exit_conditions shape: the action fields sit at the rule's top level."""
    ok = {"id": "legacy", "conditions": {"operator": "AND", "conditions": [_leaf()]},
          "action": "close"}
    assert assert_market_rule_actions([ok], "exit_rules") is None
    bad = dict(ok, action="buy")
    _refused([bad], "legacy", "'buy'")


# ------------------------------------------------------------------------ the nesting
def test_a_market_leaf_beside_an_ordinary_leaf_in_a_top_level_AND_is_accepted():
    rule = _rule("close", conditions={"id": "grp", "operator": "AND",
                                      "conditions": [_ordinary(), _leaf()]})
    assert assert_market_rule_actions([rule], "exit_rules") is None


def test_an_AND_inside_an_AND_is_still_an_AND():
    inner = {"id": "inner", "type": "AND", "conditions": [_leaf()]}
    rule = _rule("close", conditions={"operator": "AND", "conditions": [_ordinary(), inner]})
    assert assert_market_rule_actions([rule], "exit_rules") is None


def test_a_bare_leaf_as_the_whole_condition_is_accepted():
    assert assert_market_rule_actions([_rule("close", conditions=_leaf())], "exit_rules") is None


def test_a_market_leaf_under_OR_is_refused_even_with_an_allowed_action():
    rule = _rule("close", conditions={"operator": "OR", "conditions": [_ordinary(), _leaf()]})
    _refused([rule], "mkt-exit", "mkt-adx", "'OR'")


def test_a_market_leaf_under_an_OR_nested_in_an_AND_is_refused():
    inner = {"id": "inner", "operator": "OR", "conditions": [_leaf()]}
    rule = _rule("close", conditions={"operator": "AND", "conditions": [_ordinary(), inner]})
    _refused([rule], "'OR'")


def test_the_type_key_spelling_of_a_group_is_read_too():
    rule = _rule("close", conditions={"type": "OR", "conditions": [_leaf()]})
    _refused([rule], "'OR'")


def test_a_leaf_spelling_its_field_as_event_type_is_judged_too():
    """``ConditionLeaf`` accepts ``event_type`` as the field alias; a raw list is checked."""
    leaf = {"id": "alias", "event_type": ADX, "op": "<", "value": 20.0}
    rule = _rule("close", conditions={"operator": "OR", "conditions": [leaf]})
    _refused([rule], "alias", "'OR'")
    rule = _rule("buy", conditions={"operator": "AND", "conditions": [leaf]})
    _refused([rule], "'buy'")


def test_a_market_leaf_under_NOT_is_refused():
    """The tree format has no NOT today (``ConditionGroup`` would coerce it to AND), which is
    exactly why the check reads the raw operator: an unknown operator is refused, not coerced."""
    rule = _rule("close", conditions={"operator": "NOT", "conditions": [_leaf()]})
    _refused([rule], "'NOT'")


def test_an_OR_of_ordinary_leaves_elsewhere_in_the_rule_does_not_matter():
    """Only a market leaf's OWN ancestors are judged."""
    ors = {"operator": "OR", "conditions": [_ordinary(), _ordinary(id="pl2", value=-9.0)]}
    rule = _rule("close", conditions={"operator": "AND", "conditions": [ors, _leaf()]})
    assert assert_market_rule_actions([rule], "exit_rules") is None


# ------------------------------------------------------------------------ untouched rulesets
def test_an_ordinary_exit_ruleset_is_untouched_whatever_its_actions():
    """No market leaf, no judgement: an OR group, a stop_processing, a roll -- all unchanged."""
    rules = [
        _rule("buy", conditions={"operator": "OR", "conditions": [_ordinary()]}, rid="a"),
        _rule("stop_processing", conditions={"operator": "AND", "conditions": [_ordinary()]},
              rid="b"),
        _rule(ROLL, conditions=None, rid="c"),
    ]
    rules[2]["conditions"] = None
    assert assert_market_rule_actions(rules, "exit_rules") is None
    assert assert_market_rule_actions([], "exit_rules") is None
    assert assert_market_rule_actions(None, "exit_rules") is None


def test_only_the_gated_rule_is_judged():
    rules = [_rule("buy", conditions={"operator": "AND", "conditions": [_ordinary()]}, rid="a"),
             _rule("close", rid="b")]
    assert assert_market_rule_actions(rules, "exit_rules") is None


def test_the_old_blanket_refusal_is_still_importable_and_unchanged():
    with pytest.raises(ValueError, match="not allowed in an open-positions / exit ruleset"):
        assert_no_market_conditions([_rule("close")], "exit_rules")


# ------------------------------------------------------------------------ the live format
def _live(action_types, *, triggers=None, alias="action_type"):
    trig = triggers if triggers is not None else {
        "cond_0": {"event_type": "profit_loss_percent", "operator": "<", "value": -5.0},
        "cond_1": {"event_type": ADX, "operator": "<", "value": 20.0}}
    return ("mkt exit", trig, {f"a{i}": {alias: at} for i, at in enumerate(action_types)})


@pytest.mark.parametrize("action_type", ALLOWED)
def test_live_each_allowed_action_is_accepted(action_type):
    assert assert_market_rule_actions_live([_live([action_type])], "ruleset 'x'") is None


@pytest.mark.parametrize("bad", ["buy", "stop_processing", ROLL])
def test_live_a_disallowed_action_is_refused(bad):
    with pytest.raises(ValueError) as e:
        assert_market_rule_actions_live([_live([bad])], "ruleset 'x'")
    msg = str(e.value)
    assert "mkt exit" in msg and repr(bad) in msg and "cond_1" in msg
    for name in ALLOWED:
        assert name in msg


def test_live_the_type_alias_is_read():
    assert assert_market_rule_actions_live([_live(["close"], alias="type")], "r") is None
    with pytest.raises(ValueError, match="'buy'"):
        assert_market_rule_actions_live([_live(["buy"], alias="type")], "r")


def test_live_an_ungated_rule_is_untouched_whatever_its_actions():
    ungated = _live(["buy", "stop_processing"],
                    triggers={"cond_0": {"event_type": "profit_loss_percent"}})
    assert assert_market_rule_actions_live([ungated], "r") is None


def test_live_a_gated_rule_with_no_actions_is_refused():
    with pytest.raises(ValueError, match="<no action>"):
        assert_market_rule_actions_live([_live([])], "r")


# ------------------------------------------------------------------------ the deploy converter
def test_the_deploy_converter_exports_a_gated_close_rule_with_its_gate():
    export = trade_rules_to_live_export([], [_rule("close")], name="mx")
    ruleset, = export["rulesets"]
    assert ruleset["subtype"] == "open_positions"
    rule, = ruleset["rules"]
    assert {"event_type": ADX, "operator": "<", "value": 20.0} in rule["triggers"].values()
    assert [a["action_type"] for a in rule["actions"].values()] == ["close"]


@pytest.mark.parametrize("bad", ["buy", "stop_processing", ROLL])
def test_the_deploy_converter_refuses_a_gated_rule_with_a_disallowed_action(bad):
    with pytest.raises(ValueError, match="open_positions ruleset"):
        trade_rules_to_live_export([], [_rule(bad)])


def test_the_deploy_converter_refuses_a_gated_exit_under_OR():
    rule = _rule("close", conditions={"operator": "OR", "conditions": [_ordinary(), _leaf()]})
    with pytest.raises(ValueError, match="non-AND group"):
        trade_rules_to_live_export([], [rule])


def test_the_deploy_converter_refuses_an_unresolved_template_on_an_exit_rule():
    """An optimizer template must never leave for live, on the exit side as on the entry side."""
    template = _leaf(mode_optimize=True, mode_choices=["below", "above"],
                     value_min=10.0, value_max=40.0, value_step=5.0)
    rule = _rule("close", conditions={"operator": "AND", "conditions": [template]})
    with pytest.raises(ValueError) as e:
        trade_rules_to_live_export([], [rule])
    msg = str(e.value)
    assert "open_positions ruleset" in msg and "mode_optimize" in msg and "mkt-adx" in msg


def test_the_deploy_converter_leaves_an_ordinary_exit_ruleset_alone():
    rule = _rule("stop_processing", conditions={"operator": "AND", "conditions": [_ordinary()]})
    export = trade_rules_to_live_export([], [rule])
    assert export["rulesets"][0]["subtype"] == "open_positions"
