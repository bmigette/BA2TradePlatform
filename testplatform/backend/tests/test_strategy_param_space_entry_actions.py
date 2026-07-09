"""Entry-time TP/SL bracket genes on the unified rule model.

The flat ``Strategy.entry_actions`` list is retired (migration 028): a bracket is now just
extra actions on an entry TradeRule, optimized per rule + per action via the
``entry:<rid>:a<i>:*`` namespace. These tests pin the bracket-specific behaviors on the new
shape: value genes, per-action droppability (never the open action), and source immutability.
"""
import types

from app.services.strategy_param_space import collect_param_space, decode_params


def _strategy(**kw):
    base = dict(entry_rules=[], exit_rules=[])
    base.update(kw)
    return types.SimpleNamespace(**base)


def _bracket_rule(**overrides):
    rule = {
        "id": "r1", "conditions": None, "continue_processing": False,
        "actions": [
            {"action_type": "buy"},
            {"id": "e_sl", "action_type": "adjust_stop_loss",
             "reference_value": "order_open_price",
             "action_value": -5.0, "action_value_optimize": True,
             "action_value_min": -15.0, "action_value_max": -2.0, "action_value_step": 1.0,
             "toggle_optimize": True},
        ],
    }
    rule.update(overrides)
    return rule


def test_bracket_action_value_gene_collected():
    s = _strategy(entry_rules=[_bracket_rule()])
    space = collect_param_space(s)
    assert space["entry:r1:a1:action_value"] == {"type": "float", "min": -15.0, "max": -2.0,
                                                 "step": 1.0}
    assert space["entry:r1:a1:enabled"] == {"type": "int", "min": 0, "max": 1, "step": 1}


def test_bracket_decoded_with_ga_value():
    s = _strategy(entry_rules=[_bracket_rule()])
    decoded = decode_params(s, {"entry:r1:a1:action_value": -9.0})
    sl = decoded["entry_rules"][0]["actions"][1]
    assert sl["action_value"] == -9.0


def test_bracket_action_dropped_when_toggled_off_but_open_survives():
    s = _strategy(entry_rules=[_bracket_rule()])
    decoded = decode_params(s, {"entry:r1:a1:enabled": 0})
    kinds = [a["action_type"] for a in decoded["entry_rules"][0]["actions"]]
    assert kinds == ["buy"]  # SL dropped, buy untouched


def test_entry_rules_default_empty_when_no_rules():
    s = _strategy()
    decoded = decode_params(s, {})
    assert decoded["entry_rules"] == []


def test_bracket_source_not_mutated():
    s = _strategy(entry_rules=[_bracket_rule()])
    decoded = decode_params(s, {"entry:r1:a1:action_value": -9.0})
    assert decoded["entry_rules"][0]["actions"][1]["action_value"] == -9.0
    assert s.entry_rules[0]["actions"][1]["action_value"] == -5.0
