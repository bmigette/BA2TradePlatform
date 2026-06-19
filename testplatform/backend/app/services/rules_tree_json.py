"""Ruleset JSON (rules_export_import shape, v1.1) <-> Strategy condition tree.

Import = inverse of default_rulesets.seed_ruleset_from_tree: ruleset rules (OR) whose
triggers (AND) become condition leaves; v1.1 carries per-operand optimize {min,max,step}
and an enabled flag. Export = tree -> v1.1 JSON. Flag triggers (bullish/has_no_position)
become flag leaves (no operator/value) and are re-added on seed-back.
"""
import uuid
from typing import Any, Dict, List

# Single source of truth: the canonical field/flag maps live in ba2_common.core.rule_builders.
from ba2_common.core.rule_builders import FIELD_EVENT as _FIELD_EVENT, FLAG_FIELD_EVENT

# event_type value -> strategy field (reverse of _FIELD_EVENT, by enum .value)
_EVENT_FIELD = {et.value: field for field, et in _FIELD_EVENT.items()}
# flag event_types kept as flag leaves — the COMPLETE flag vocabulary (all 16), not a 3-flag
# subset. Derived from the shared FLAG_FIELD_EVENT so it can never drift again.
_FLAG_EVENTS = set(FLAG_FIELD_EVENT.keys())


def _new_id() -> str:
    return uuid.uuid4().hex[:8]


def _trigger_to_leaf(key: str, trig: Dict[str, Any]) -> Dict[str, Any]:
    et = trig.get("event_type")
    if et in _FLAG_EVENTS:
        return {"id": _new_id(), "field": et, "is_flag": True,
                "enabled": trig.get("enabled", True)}
    field = _EVENT_FIELD.get(et)
    if field is None:
        raise ValueError(f"unknown event_type {et!r} (no field mapping)")
    leaf: Dict[str, Any] = {
        "id": _new_id(), "field": field,
        "op": trig.get("operator", ">"), "value": trig.get("value"),
        "enabled": trig.get("enabled", True),
    }
    opt = trig.get("optimize")
    if isinstance(opt, dict):
        leaf.update({"optimize": True, "value_min": opt.get("min"),
                     "value_max": opt.get("max"), "value_step": opt.get("step")})
    else:
        leaf["optimize"] = False
    return leaf


def ruleset_json_to_tree(payload: Dict[str, Any], which: str) -> Dict[str, Any]:
    """Return a ConditionGroup (OR of rules; each rule = AND of trigger leaves)."""
    ruleset = payload.get("ruleset") or {}
    rules: List[Dict[str, Any]] = ruleset.get("rules") or []
    or_children: List[Dict[str, Any]] = []
    for rule in rules:
        and_children = [_trigger_to_leaf(k, t) for k, t in (rule.get("triggers") or {}).items()]
        or_children.append({"id": _new_id(), "operator": "AND", "conditions": and_children})
    return {"id": _new_id(), "operator": "OR", "conditions": or_children}


def _leaf_to_trigger(idx: int, leaf: Dict[str, Any]) -> Dict[str, Any]:
    if leaf.get("is_flag") or leaf.get("field") in _FLAG_EVENTS:
        return {"event_type": leaf["field"], "enabled": leaf.get("enabled", True)}
    et = _FIELD_EVENT[leaf["field"]].value  # KeyError if not mappable (caller guards UI fields)
    trig: Dict[str, Any] = {"event_type": et, "operator": leaf.get("op", ">"),
                            "value": leaf.get("value"), "enabled": leaf.get("enabled", True)}
    if leaf.get("optimize"):
        trig["optimize"] = {"min": leaf.get("value_min"), "max": leaf.get("value_max"),
                            "step": leaf.get("value_step")}
    return trig


def _iter_rules(tree: Dict[str, Any]) -> List[List[Dict[str, Any]]]:
    """Flatten a (possibly nested) tree into a list of AND-groups (each a list of leaves)."""
    if not isinstance(tree, dict):
        return []
    op = (tree.get("operator") or "AND").upper()
    kids = tree.get("conditions") or []
    if not kids and tree.get("field"):
        return [[tree]]
    if op == "OR":
        groups: List[List[Dict[str, Any]]] = []
        for k in kids:
            groups.extend(_iter_rules(k))
        return groups
    # AND: collect leaf children into one group (nested AND/OR flattened best-effort)
    leaves = [k for k in kids if isinstance(k, dict) and k.get("field")]
    return [leaves] if leaves else []


def tree_to_ruleset_json(tree: Dict[str, Any], which: str, name: str) -> Dict[str, Any]:
    subtype = "enter_market" if which == "enter" else "exit_market"
    rules = []
    for order, group in enumerate(_iter_rules(tree)):
        triggers = {f"t{i}": _leaf_to_trigger(i, leaf) for i, leaf in enumerate(group)}
        rules.append({"name": f"{name}-{order}", "triggers": triggers,
                      "actions": {"buy": {"action_type": "buy"}},
                      "continue_processing": False, "order_index": order})
    return {"export_version": "1.1", "export_type": "ruleset",
            "ruleset": {"name": name, "type": "trading_recommendation_rule",
                        "subtype": subtype, "rules": rules}}
