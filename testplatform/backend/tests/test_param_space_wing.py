"""Task C1: optimizer gene for the option WING WIDTH selection param.

An exit (or entry) rule that opens a multi-leg option position (iron condor /
jade lizard / butterfly / ratio) carries a wing width the optimizer should be
able to tune, via option_wing_width_optimize/_min/_max/_step.

collect_param_space must emit exit:<id>:option_wing_width (a float range);
decode_params must write the chosen value back onto the rule as
option_wing_width_pct — the key rule_builders._OPTION_ACTION_PARAM_KEYS reads
(mapping wing_width_pct <- ("option_wing_width_pct", "option_wing_width")).

The wing is a plain float param (mirrors option_strike_param / option_delta);
unlike option_dte it does NOT decode to a window — the chosen value is applied
directly.
"""
import types

from app.services.strategy_param_space import collect_param_space, decode_params


def _strategy(**kw):
    base = dict(
        initial_tp_optimize=False, initial_tp_min=None, initial_tp_max=None, initial_tp_step=None,
        initial_sl_optimize=False, initial_sl_min=None, initial_sl_max=None, initial_sl_step=None,
        buy_entry_conditions=None, sell_entry_conditions=None, entry_conditions=None,
        initial_tp_percent=None, initial_sl_percent=None,
        exit_conditions=[],
    )
    base.update(kw)
    return types.SimpleNamespace(**base)


_WING_EXIT = {
    "id": "e1", "action": "open_iron_condor", "option_strategy": "iron_condor",
    "option_wing_width_optimize": True,
    "option_wing_width_min": 3.0, "option_wing_width_max": 10.0,
    "option_wing_width_step": 1.0,
}


def test_wing_width_param_becomes_gene():
    space = collect_param_space(_strategy(exit_conditions=[dict(_WING_EXIT)]))
    assert "exit:e1:option_wing_width" in space
    assert space["exit:e1:option_wing_width"] == {
        "type": "float", "min": 3.0, "max": 10.0, "step": 1.0,
    }


def test_wing_width_gene_absent_when_not_optimized():
    rule = dict(_WING_EXIT)
    rule["option_wing_width_optimize"] = False
    # Need at least one optimizable param so collect doesn't raise.
    rule["action_value_optimize"] = True
    rule["action_value_min"] = 0.5
    rule["action_value_max"] = 3.0
    rule["action_value_step"] = 0.5
    space = collect_param_space(_strategy(exit_conditions=[rule]))
    assert "exit:e1:option_wing_width" not in space


def test_wing_width_decode_applies_pct_onto_rule():
    s = _strategy(exit_conditions=[dict(_WING_EXIT)])
    decoded = decode_params(s, {"exit:e1:option_wing_width": 5.0})
    rule = decoded["exit_rules"][0]
    # decode writes the rule_builders key directly (no window logic).
    assert rule["option_wing_width_pct"] == 5.0
    # Source strategy is never mutated.
    assert "option_wing_width_pct" not in s.exit_conditions[0]
