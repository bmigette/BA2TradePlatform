from app.services.rules_tree_json import ruleset_json_to_tree, tree_to_ruleset_json

# v1.1 ruleset JSON (one EventAction: bullish AND confidence>0.7 with an optimize range)
RULESET_JSON = {
    "export_version": "1.1", "export_type": "ruleset",
    "ruleset": {"name": "enter", "type": "trading_recommendation_rule",
                "subtype": "enter_market", "rules": [{
        "triggers": {
            "bullish": {"event_type": "bullish", "enabled": True},
            "gate_0": {"event_type": "confidence", "operator": ">", "value": 0.7,
                       "enabled": True, "optimize": {"min": 0.5, "max": 0.9, "step": 0.05}},
        },
        "actions": {"buy": {"action_type": "buy"}},
        "continue_processing": False, "order_index": 0}]}}


def test_import_builds_tree_with_value_and_optimize():
    tree = ruleset_json_to_tree(RULESET_JSON, which="enter")
    # OR of rules -> AND of triggers; find the confidence leaf
    leaves = _all_leaves(tree)
    conf = next(l for l in leaves if l.get("field") == "confidence")
    assert conf["op"] == ">" and conf["value"] == 0.7
    assert conf["optimize"] is True
    assert (conf["value_min"], conf["value_max"], conf["value_step"]) == (0.5, 0.9, 0.05)
    # flag trigger preserved as a flag node
    assert any(l.get("field") == "bullish" for l in leaves)


def test_export_roundtrips_back_to_triggers():
    tree = ruleset_json_to_tree(RULESET_JSON, which="enter")
    out = tree_to_ruleset_json(tree, which="enter", name="enter")
    trig = out["ruleset"]["rules"][0]["triggers"]
    gate = next(v for v in trig.values() if v["event_type"] == "confidence")
    assert gate["value"] == 0.7 and gate["optimize"] == {"min": 0.5, "max": 0.9, "step": 0.05}
    assert out["export_version"] == "1.1"


def test_unknown_event_type_is_reported_not_silently_dropped():
    bad = {"export_version": "1.1", "export_type": "ruleset", "ruleset": {"rules": [{
        "triggers": {"x": {"event_type": "totally_unknown", "value": 1}},
        "actions": {"buy": {"action_type": "buy"}}}]}}
    import pytest
    with pytest.raises(ValueError, match="unknown event_type"):
        ruleset_json_to_tree(bad, which="enter")


def _all_leaves(node, acc=None):
    acc = acc if acc is not None else []
    if isinstance(node, dict):
        kids = node.get("conditions")
        if kids:
            for k in kids:
                _all_leaves(k, acc)
        elif node.get("field"):
            acc.append(node)
    return acc
