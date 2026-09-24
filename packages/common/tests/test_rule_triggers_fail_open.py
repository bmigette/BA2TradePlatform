"""A TradeRule must never become ALWAYS TRUE, or lose a market gate, on its way to EventActions.

``triggers_from_condition_tree`` drops a leaf it cannot map (unknown field, missing value) with a
WARNING, so a partially edited tree still seeds. When EVERY leaf drops, the triggers are empty,
and an EventAction with no trigger fires on every position: a close rule would close them all.
``rule_triggers_from_tree`` refuses that, and refuses a dropped market-condition leaf, on the live
export (here) and the backtest seeder (tests/backtest/test_seed_refuses_fail_open_rules.py in the
test platform). An ordinary partial drop is left alone: existing rulesets rely on it.
"""
from __future__ import annotations

import pytest

from ba2_common.core.rule_builders import rule_triggers_from_tree
from ba2_common.core.rules_convert import trade_rules_to_live_export

ADX = "underlying_adx_14"


def _tree(*leaves):
    return {"type": "AND", "conditions": list(leaves)}


def _close(rid, *leaves, **extra):
    return {"id": rid, "conditions": _tree(*leaves), "actions": [{"action_type": "close"}],
            **extra}


UNKNOWN = {"id": "u", "field": "no_such_field_ever", "op": ">", "value": 1}
NO_VALUE = {"id": "nv", "field": "days_opened", "op": ">"}
DAYS = {"id": "d", "field": "days_opened", "op": ">", "value": 10}
MARKET = {"id": "m", "field": ADX, "op": ">", "value": 25.0}
MARKET_NO_VALUE = {"id": "mnv", "field": ADX, "op": ">"}


@pytest.mark.parametrize("leaves", [(UNKNOWN,), (NO_VALUE,), (UNKNOWN, NO_VALUE)])
def test_every_leaf_dropped_is_refused(leaves):
    with pytest.raises(ValueError, match="ALWAYS TRUE"):
        rule_triggers_from_tree(_tree(*leaves), "rule 'x'")


def test_a_dropped_market_leaf_is_refused_even_when_other_leaves_remain():
    with pytest.raises(ValueError, match="without its market gate"):
        rule_triggers_from_tree(_tree(DAYS, MARKET_NO_VALUE), "rule 'x'")


def test_ordinary_partial_drop_is_unchanged():
    """Long-standing behaviour: the unmappable ordinary leaf drops, the rest stays."""
    triggers = rule_triggers_from_tree(_tree(DAYS, UNKNOWN, MARKET), "rule 'x'")
    assert [t["event_type"] for t in triggers.values()] == ["days_opened", ADX]


@pytest.mark.parametrize("tree", [None, {}, _tree(), {"type": "AND", "conditions": [_tree()]}])
def test_a_tree_with_no_leaf_is_not_refused(tree):
    """No leaf authored (or all removed upstream): not this check's to judge."""
    assert rule_triggers_from_tree(tree, "rule 'x'") == {}


def test_live_export_names_the_refused_rule():
    with pytest.raises(ValueError, match="'bad-close'.*ALWAYS TRUE"):
        trade_rules_to_live_export(exit_rules=[_close("ok", DAYS), _close("bad-close", UNKNOWN)])
    with pytest.raises(ValueError, match="'bad-entry'.*ALWAYS TRUE"):
        trade_rules_to_live_export(entry_rules=[
            {"id": "bad-entry", "conditions": _tree(UNKNOWN), "actions": [{"action_type": "buy"}]}])


def test_live_export_ignores_a_disabled_rule():
    """Only rules that will run are judged: an ``enabled: False`` rule exports nothing."""
    out = trade_rules_to_live_export(exit_rules=[_close("ok", DAYS),
                                                 _close("off", UNKNOWN, enabled=False)])
    (rs,) = out["rulesets"]
    assert [r["triggers"] for r in rs["rules"]] == [
        {"cond_0": {"event_type": "days_opened", "operator": ">", "value": 10}}]
