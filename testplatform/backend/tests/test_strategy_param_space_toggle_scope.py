"""``cond:<id>:enabled == 0`` removes only a node that DECLARES ``toggle_optimize``.

Genes are keyed by node id across the whole strategy (entry and exit rules share one ``cond:``
namespace). Before the fix, a toggle gene collected for one node also removed EVERY other node
with that id. When the other node was the only leaf of a close rule, the rule decoded to an empty
AND, which is always true: it would close every position. The collector emits the gene only for a
``toggle_optimize`` node (``_walk_condition_nodes``), so every collector-produced genome decodes
exactly as before; only a colliding or hand-built gene stops reaching an undeclared node.
"""
import copy
import types

from app.services.strategy_param_space import collect_param_space, decode_params


def _leaf(lid, field="days_opened", value=5, **extra):
    return {"id": lid, "field": field, "op": ">", "value": value, **extra}


def _rule(rid, *leaves, action="close"):
    return {"id": rid, "conditions": {"type": "AND", "conditions": list(leaves)},
            "actions": [{"action_type": action}]}


def test_toggle_gene_still_removes_the_declaring_node():
    s = types.SimpleNamespace(entry_rules=None, exit_rules=[
        _rule("r", _leaf("keep"), _leaf("t", toggle_optimize=True))])
    assert "cond:t:enabled" in collect_param_space(s)
    out = decode_params(s, {"cond:t:enabled": 0})
    assert [c["id"] for c in out["exit_rules"][0]["conditions"]["conditions"]] == ["keep"]
    on = decode_params(s, {"cond:t:enabled": 1})
    assert [c["id"] for c in on["exit_rules"][0]["conditions"]["conditions"]] == ["keep", "t"]


def test_camel_case_declaration_is_honoured_too():
    s = types.SimpleNamespace(entry_rules=None, exit_rules=[
        _rule("r", _leaf("keep"), _leaf("t", toggleOptimize=True))])
    out = decode_params(s, {"cond:t:enabled": 0})
    assert [c["id"] for c in out["exit_rules"][0]["conditions"]["conditions"]] == ["keep"]


def test_colliding_gene_does_not_empty_another_rules_only_leaf():
    entry = _rule("e", _leaf("bull", field="bullish"), _leaf("shared", toggle_optimize=True),
                  action="buy")
    close = _rule("x", _leaf("shared", field="profit_loss_percent", value=-5))
    s = types.SimpleNamespace(entry_rules=[entry], exit_rules=[close])
    assert "cond:shared:enabled" in collect_param_space(s)
    out = decode_params(s, {"cond:shared:enabled": 0})
    assert [c["id"] for c in out["entry_rules"][0]["conditions"]["conditions"]] == ["bull"]
    assert out["exit_rules"][0]["conditions"] == close["conditions"]


def test_undeclared_node_ignores_a_hand_built_gene():
    s = types.SimpleNamespace(entry_rules=None, exit_rules=[_rule("r", _leaf("only"))])
    before = copy.deepcopy(s.exit_rules)
    assert decode_params(s, {"cond:only:enabled": 0})["exit_rules"] == before
