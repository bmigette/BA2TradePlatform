"""``trigger_leaf_ids`` maps the positional ``cond_{i}`` keys back to leaf ids with the SAME walk as
``triggers_from_condition_tree`` (a dropped leaf leaves a gap), and does not change the triggers."""
from __future__ import annotations

from ba2_common.core.rule_builders import trigger_leaf_ids, triggers_from_condition_tree

UNKNOWN = {"id": "u", "field": "no_such_field_ever", "op": ">", "value": 1}
NO_VALUE = {"id": "nv", "field": "days_opened", "op": ">"}
DAYS = {"id": "shared-days", "field": "days_opened", "op": ">", "value": 10}
ADX = {"id": "o_lc-market-adx", "field": "underlying_adx_14", "op": ">", "value": 25.0}
ANON = {"field": "days_opened", "op": "<", "value": 3}


def _tree(*leaves):
    return {"type": "AND", "conditions": [{"type": "OR", "conditions": [leaves[0]]}, *leaves[1:]]}


def test_leaf_ids_equal_trigger_keys_with_dropped_leaves_shifting_positions():
    tree = _tree(DAYS, UNKNOWN, ADX, NO_VALUE, ANON)
    triggers = triggers_from_condition_tree(tree)
    ids = trigger_leaf_ids(tree)
    assert list(ids) == list(triggers) == ["cond_0", "cond_2", "cond_4"]
    assert ids == {"cond_0": "shared-days", "cond_2": "o_lc-market-adx", "cond_4": "days_opened@4"}


def test_triggers_unchanged_by_the_refactor():
    t = triggers_from_condition_tree(_tree(ADX))
    assert t == {"cond_0": {"event_type": t["cond_0"]["event_type"], "operator": ">", "value": 25.0}}


def test_empty_tree():
    assert trigger_leaf_ids(None) == {}
