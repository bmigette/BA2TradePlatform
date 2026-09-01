"""The two ``O_ERN`` timing gates must survive every backend path a strategy travels.

Task 9 builds only the CONDITION layer; the launcher's ``O_ERN`` strategy definition and its
genes land in Task 10. What that later task will rely on -- and what has silently failed
three times in this codebase already -- is the chain between a condition leaf and the engine:

    leaf {"field": "rec_days_to_earnings", ...}
      -> triggers_from_condition_tree   (drops any field missing from FIELD_EVENT, MUTELY)
      -> create_condition               (raises for any event_type missing from CONDITION_MAP)
      -> the evaluated gate

plus the persistence path a saved strategy takes, ``rules_tree_json``, whose reverse map is
built from ``FIELD_EVENT`` by ``.value`` and raises ``unknown event_type`` for a field that is
registered in one direction only.

The GA-gene half is asserted through the UNMODIFIED collector on the real launcher Strategy
object: ``collect_param_space`` emits ``cond:<id>:value`` / ``:enabled`` for any optimizable
leaf and knows nothing about field names. Pinning that here means Task 10 only has to author
the rule, not teach the collector anything -- and a "fix" that special-cases field names
fails here.
"""
import importlib.util
import os
import sys

import pytest

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # testplatform/backend
_launcher = os.path.normpath(os.path.join(_root, "..", "ba2test_launcher.py"))
if _root not in sys.path:
    sys.path.insert(0, _root)
_spec = importlib.util.spec_from_file_location("ba2test_launcher", _launcher)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)

from ba2_common.core.TradeConditions import (  # noqa: E402
    DaysAfterEventCondition,
    RecommendationDaysToEarningsCondition,
    create_condition,
)
from ba2_common.core.rule_builders import triggers_from_condition_tree  # noqa: E402
from ba2_common.core.types import ExpertEventType  # noqa: E402

#: (field, operator, the design's searched default, the condition class it must build).
#: The operators are the ones design 2026-08-31 S9 specifies: entry "<= X" (X in 1..5),
#: exit ">= Y" (Y in 0..2).
GATES = [
    ("rec_days_to_earnings", "<=", 3, RecommendationDaysToEarningsCondition),
    ("days_after_event", ">=", 1, DaysAfterEventCondition),
]
GATE_IDS = [g[0] for g in GATES]


@pytest.mark.parametrize("field,op,value,cls", GATES, ids=GATE_IDS)
def test_a_leaf_naming_the_gate_becomes_a_real_engine_trigger(field, op, value, cls):
    """``triggers_from_condition_tree`` skips an unknown field SILENTLY. A gate dropped here
    is a strategy that runs ungated while the GA tunes its threshold for a whole campaign."""
    triggers = triggers_from_condition_tree(
        {"type": "AND", "conditions": [{"id": "g", "field": field, "op": op, "value": value}]})
    assert list(triggers.values()) == [
        {"event_type": field, "operator": op, "value": value}], (
        f"the {field!r} leaf was dropped before the engine saw it")


@pytest.mark.parametrize("field,op,value,cls", GATES, ids=GATE_IDS)
def test_the_engine_can_build_the_condition_for_that_trigger(field, op, value, cls):
    """The last link: the seeded event_type must resolve to a real condition class."""
    cfg = list(triggers_from_condition_tree(
        {"type": "AND",
         "conditions": [{"id": "g", "field": field, "op": op, "value": value}]}).values())[0]
    cond = create_condition(
        event_type=ExpertEventType(cfg["event_type"]), account=object(),
        instrument_name="AAPL", expert_recommendation=object(), existing_order=None,
        operator_str=cfg["operator"], value=cfg["value"])
    assert isinstance(cond, cls)


@pytest.mark.parametrize("field,op,value,cls", GATES, ids=GATE_IDS)
def test_the_gate_round_trips_through_the_ruleset_json_converters(field, op, value, cls):
    """``rules_tree_json`` is the persistence path: tree -> ruleset JSON -> tree. Its reverse
    map is derived from ``FIELD_EVENT`` by ``.value``, so a field registered in only one
    direction raises ``unknown event_type`` on the way back in -- a saved strategy that
    cannot be reloaded."""
    from app.services.rules_tree_json import ruleset_json_to_tree, tree_to_ruleset_json

    tree = {"id": "root", "operator": "AND",
            "conditions": [{"id": "g", "field": field, "op": op, "value": value,
                            "enabled": True, "optimize": False}]}
    payload = tree_to_ruleset_json(tree, which="exit", name="ern")
    trig = list(payload["ruleset"]["rules"][0]["triggers"].values())[0]
    assert trig["event_type"] == field and trig["operator"] == op and trig["value"] == value

    back = ruleset_json_to_tree(payload, which="exit")
    leaves = _leaves(back)
    assert [(l["field"], l["op"], l["value"]) for l in leaves] == [(field, op, value)]


@pytest.mark.parametrize("field,op,value,cls", GATES, ids=GATE_IDS)
def test_an_optimizable_leaf_naming_the_gate_yields_genes_from_the_stock_collector(
        field, op, value, cls):
    """Task 10 authors the rule; the collector must already handle it. ``collect_param_space``
    walks condition nodes and knows nothing about field names -- pinned by injecting a leaf
    onto a REAL launcher strategy rather than by reading the collector's source."""
    from app.services.strategy_param_space import collect_param_space, decode_params

    strategy = mod._build_strategy_option("O_LC")
    rule_id, cond_id = f"ern_{field}", f"c_{field}"
    strategy.exit_rules = list(strategy.exit_rules) + [{
        "id": rule_id, "action_type": "close_option", "toggle_optimize": True,
        "conditions": {"type": "AND", "conditions": [
            {"id": cond_id, "field": field, "op": op, "value": value, "optimize": True,
             "value_min": 0, "value_max": 5, "value_step": 1}]}}]

    space = collect_param_space(strategy)
    assert space[f"cond:{cond_id}:value"] == {"type": "float", "min": 0.0, "max": 5.0,
                                              "step": 1.0}
    assert space[f"exit:{rule_id}:enabled"] == {"type": "int", "min": 0, "max": 1, "step": 1}

    decoded = decode_params(strategy, {f"cond:{cond_id}:value": 0})
    leaf = next(c for r in decoded["exit_rules"] if r["id"] == rule_id
                for c in r["conditions"]["conditions"])
    assert leaf["value"] == 0, "0 is a searched level (exit day-of, entry is 1..5); a falsy" \
                               " bug would leave the default in place"
    assert leaf["field"] == field


def _leaves(node, acc=None):
    acc = acc if acc is not None else []
    if isinstance(node, dict):
        kids = node.get("conditions")
        if isinstance(kids, list):
            for k in kids:
                _leaves(k, acc)
        elif node.get("field"):
            acc.append(node)
    return acc
