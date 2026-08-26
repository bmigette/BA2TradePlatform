"""Optimizer gene for the option WING WIDTH selection param (unified rule model).

An exit rule action that opens a multi-leg option position (iron condor / jade lizard /
butterfly / ratio) carries a wing width the optimizer should be able to tune, via
option_wing_width_optimize/_min/_max/_step ON THE ACTION.

collect_param_space must emit exit:<rid>:a<i>:option_wing_width (a float range);
decode_params must write the chosen value back onto the ACTION as option_wing_width_pct —
the key rule_builders._OPTION_ACTION_PARAM_KEYS reads (mapping wing_width_pct <-
("option_wing_width_pct", "option_wing_width")).

The wing is a plain float param (mirrors option_strike_param); unlike
option_dte it does NOT decode to a window — the chosen value is applied directly.
"""
import types

from app.services.strategy_param_space import collect_param_space, decode_params


def _strategy(**kw):
    base = dict(entry_rules=[], exit_rules=[])
    base.update(kw)
    return types.SimpleNamespace(**base)


def _wing_rule(**action_overrides):
    action = {
        "action_type": "open_iron_condor", "option_strategy": "iron_condor",
        "option_wing_width_optimize": True,
        "option_wing_width_min": 3.0, "option_wing_width_max": 10.0,
        "option_wing_width_step": 1.0,
    }
    action.update(action_overrides)
    return {"id": "e1", "actions": [action], "conditions": {}}


def test_wing_width_param_becomes_gene():
    space = collect_param_space(_strategy(exit_rules=[_wing_rule()]))
    assert space["exit:e1:a0:option_wing_width"] == {
        "type": "float", "min": 3.0, "max": 10.0, "step": 1.0,
    }


def test_wing_width_gene_absent_when_not_optimized():
    rule = _wing_rule(
        option_wing_width_optimize=False,
        # Need at least one optimizable param so collect doesn't raise.
        action_value_optimize=True, action_value_min=0.5,
        action_value_max=3.0, action_value_step=0.5,
    )
    space = collect_param_space(_strategy(exit_rules=[rule]))
    assert "exit:e1:a0:option_wing_width" not in space


def test_wing_width_decode_applies_pct_onto_action():
    s = _strategy(exit_rules=[_wing_rule()])
    decoded = decode_params(s, {"exit:e1:a0:option_wing_width": 5.0})
    action = decoded["exit_rules"][0]["actions"][0]
    # decode writes the rule_builders key directly (no window logic).
    assert action["option_wing_width_pct"] == 5.0
    # Source strategy is never mutated.
    assert "option_wing_width_pct" not in s.exit_rules[0]["actions"][0]
