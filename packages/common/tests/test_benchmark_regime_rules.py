"""The SPY benchmark gates through the rule validators and converters (plan 2026-10-24-spy-regime-gate, Task 4).

The gate is an ordinary market leaf to every refusal: it must survive the live export (never be
dropped), an unresolved template must be refused, and the exit-rule action / nesting rules apply.
"""
from __future__ import annotations

import pytest

from ba2_common.core.benchmark_regime import TREND_MODE_CODES
from ba2_common.core.market_condition_rules import (
    assert_market_conditions_resolved,
    assert_market_fields_served,
    assert_market_rule_actions,
)
from ba2_common.core.rule_builders import rule_triggers_from_tree
from ba2_common.core.rules_convert import trade_rules_to_live_export

ADX = "underlying_adx_14"
DAYS = {"id": "d", "field": "days_opened", "op": ">", "value": 10}
#: A DECODED leaf, as the GA decode writes it: the chosen mode on the leaf and the mode's code as value.
DECODED = {"id": "o_csp-spy-trend", "field": "spy_trend_regime", "op": "==", "comparison": "==",
           "mode": "above_200", "value": float(TREND_MODE_CODES["above_200"])}
#: The search template, before decode: no mode chosen, no value.
TEMPLATE = {"id": "o_csp-spy-trend", "field": "spy_trend_regime", "field_type": "numeric",
            "mode_optimize": True, "op": "==",
            "mode_choices": ["off", *TREND_MODE_CODES]}


def _tree(*leaves, op="AND"):
    return {"id": "grp", "type": op, "operator": op, "conditions": list(leaves)}


def _rule(rid, tree, *actions):
    return {"id": rid, "conditions": tree, "actions": [{"action_type": a} for a in actions]}


def test_a_decoded_leaf_survives_the_live_export_as_a_trigger():
    out = trade_rules_to_live_export(entry_rules=[_rule("entry", _tree(DAYS, DECODED), "buy")])
    (rs,) = out["rulesets"]
    triggers = list(rs["rules"][0]["triggers"].values())
    assert {"event_type": "spy_trend_regime", "operator": "==", "value": 5.0} in triggers
    assert rule_triggers_from_tree(_tree(DECODED), "rule 'x'")      # not dropped, not refused


def test_an_unresolved_template_leaf_is_refused_on_either_side():
    with pytest.raises(ValueError, match="unresolved market-condition gene"):
        assert_market_conditions_resolved([_rule("e", _tree(TEMPLATE), "buy")], "enter_market ruleset")
    with pytest.raises(ValueError, match="unresolved market-condition gene"):
        trade_rules_to_live_export(entry_rules=[_rule("e", _tree(TEMPLATE), "buy")])


def test_served_without_any_profile_but_ordinary_market_leaves_still_need_one():
    assert_market_fields_served(_tree(DECODED), [])                       # no profile needed
    with pytest.raises(ValueError):
        assert_market_fields_served(_tree({"id": "m", "field": ADX, "op": ">", "value": 25.0}), [])


@pytest.mark.parametrize("action", ["close", "adjust_stop_loss", "adjust_take_profit"])
def test_benchmark_leaf_on_an_exit_rule_allows_close_and_adjust(action):
    rule = _rule("x", _tree(DECODED), action)
    if action.startswith("adjust"):
        rule["actions"][0].update(reference_value="order_open_price", action_value=-2.0)
    assert_market_rule_actions([rule], "open_positions ruleset")


def test_benchmark_leaf_on_an_exit_rule_refuses_stop_processing_and_opens():
    for action in ("stop_processing", "buy"):
        with pytest.raises(ValueError, match="x"):
            assert_market_rule_actions([_rule("x", _tree(DECODED), action)], "open_positions ruleset")


def test_benchmark_leaf_under_a_nested_or_is_refused():
    nested = _tree(DAYS, _tree(DECODED, DAYS, op="OR"))
    with pytest.raises(ValueError, match="non-AND group"):
        assert_market_rule_actions([_rule("x", nested, "close")], "open_positions ruleset")
