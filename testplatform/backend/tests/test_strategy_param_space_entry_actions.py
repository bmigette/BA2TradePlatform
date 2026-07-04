"""Task 5: entry_actions gene collection + decode, mirroring exit_conditions.

Strategy.entry_actions is a JSON column (list of rule dicts, SAME shape
exit_conditions rows use, minus a nested 'conditions' sub-tree — an entry
action fires on the same gate as the buy/sell action, it has no condition
of its own). collect_param_space must emit entry:<id>:action_value /
entry:<id>:enabled genes exactly like the exit:<id>:* ones; decode_params
must build a decoded entry_rules list the same way exit_rules is built.
"""
import types

from app.services.strategy_param_space import collect_param_space, decode_params


def _strategy(**kw):
    base = dict(
        buy_entry_conditions=None, sell_entry_conditions=None, entry_conditions=None,
        exit_conditions=[], entry_actions=[],
    )
    base.update(kw)
    return types.SimpleNamespace(**base)


def _entry_rule(**overrides):
    rule = {
        "id": "e_sl", "action_type": "adjust_stop_loss", "reference_value": "order_open_price",
        "action_value": -5.0, "action_value_optimize": True,
        "action_value_min": -15.0, "action_value_max": -2.0, "action_value_step": 1.0,
    }
    rule.update(overrides)
    return rule


def test_entry_action_value_gene_collected():
    s = _strategy(entry_actions=[_entry_rule()])
    space = collect_param_space(s)
    assert space["entry:e_sl:action_value"] == {"type": "float", "min": -15.0, "max": -2.0, "step": 1.0}


def test_entry_rules_decoded_with_ga_value():
    s = _strategy(entry_actions=[_entry_rule()])
    decoded = decode_params(s, {"entry:e_sl:action_value": -9.0})
    rule = next(r for r in decoded["entry_rules"] if r["id"] == "e_sl")
    assert rule["action_value"] == -9.0


def test_entry_rule_dropped_when_toggled_off():
    s = _strategy(entry_actions=[_entry_rule(toggle_optimize=True)])
    decoded = decode_params(s, {"entry:e_sl:enabled": 0})
    assert not any(r["id"] == "e_sl" for r in decoded["entry_rules"])


def test_entry_rules_default_empty_when_no_entry_actions():
    """Backward compat: a Strategy with no entry_actions decodes to an empty list,
    not a crash — mirrors exit_rules' behavior when exit_conditions is empty."""
    s = _strategy()
    decoded = decode_params(s, {})
    assert decoded["entry_rules"] == []


def test_entry_rule_source_not_mutated():
    s = _strategy(entry_actions=[_entry_rule()])
    decoded = decode_params(s, {"entry:e_sl:action_value": -9.0})
    assert decoded["entry_rules"][0]["action_value"] == -9.0
    assert s.entry_actions[0]["action_value"] == -5.0
