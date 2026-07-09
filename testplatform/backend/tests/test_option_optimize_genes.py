"""Optimizer genes for option-action selection params (unified rule model).

An exit rule ACTION that is an OPTION action carries selection params the optimizer
should be able to tune:
  - option_strike_param (delta), via option_strike_param_optimize/_min/_max/_step
  - DTE, via option_dte_optimize/_min_range/_max_range/_step

collect_param_space must emit exit:<rid>:a<i>:option_delta and exit:<rid>:a<i>:option_dte;
decode_params must write them back onto the ACTION (option_strike_param, and a DTE *window*
[option_dte_min, option_dte_max] centered on the tuned value).

Why a WINDOW and not a single day: the option_dte gene tunes the DTE window CENTER.
Real option chains expire on discrete (weekly) dates, so a single-day target
(option_dte_min == option_dte_max == 30) almost never matches an actual expiry ->
0 fills whenever option_dte_optimize=True. Decoding to a window that spans at least
one weekly expiry is what makes the gene non-degenerate.
"""
import types

from app.services.strategy_param_space import collect_param_space, decode_params


def _strategy(**kw):
    base = dict(entry_rules=[], exit_rules=[])
    base.update(kw)
    return types.SimpleNamespace(**base)


def _option_action(**overrides):
    action = {
        "action_type": "buy_call", "option_strategy": "buy_call",
        "option_strike_param": 0.3,
        "option_strike_param_optimize": True,
        "option_strike_param_min": 0.2, "option_strike_param_max": 0.4,
        "option_strike_param_step": 0.05,
        "option_dte_optimize": True,
        "option_dte_min_range": 20, "option_dte_max_range": 45, "option_dte_step": 5,
    }
    action.update(overrides)
    return action


def _option_rule(**action_overrides):
    return {"id": "o1", "actions": [_option_action(**action_overrides)], "conditions": {}}


def test_option_selection_params_become_genes():
    space = collect_param_space(_strategy(exit_rules=[_option_rule()]))
    assert space["exit:o1:a0:option_delta"] == {"type": "float", "min": 0.2, "max": 0.4,
                                                "step": 0.05}
    assert space["exit:o1:a0:option_dte"] == {"type": "int", "min": 20, "max": 45, "step": 5}


def test_option_genes_absent_when_not_optimized():
    rule = _option_rule(
        option_strike_param_optimize=False, option_dte_optimize=False,
        # Need at least one optimizable param so collect doesn't raise.
        action_value_optimize=True, action_value_min=0.5,
        action_value_max=3.0, action_value_step=0.5,
    )
    space = collect_param_space(_strategy(exit_rules=[rule]))
    assert "exit:o1:a0:option_delta" not in space
    assert "exit:o1:a0:option_dte" not in space


def test_option_selection_params_decode_roundtrip():
    s = _strategy(exit_rules=[_option_rule()])
    decoded = decode_params(s, {"exit:o1:a0:option_delta": 0.35,
                                "exit:o1:a0:option_dte": 30})
    action = decoded["exit_rules"][0]["actions"][0]
    assert action["option_strike_param"] == 0.35
    # option_dte decodes to a WINDOW centered on the tuned value (NOT a single impossible
    # day). The window must contain the center and span >= 14 days so it covers a real
    # (weekly) expiry instead of an exact day that almost never matches the chain.
    assert action["option_dte_min"] <= 30 <= action["option_dte_max"]
    assert action["option_dte_max"] - action["option_dte_min"] >= 14
    # Source strategy is never mutated.
    assert s.exit_rules[0]["actions"][0]["option_strike_param"] == 0.3


def test_option_dte_window_uses_base_window_half_width():
    """An action that carries a BASE window [30, 60] (half-width 15) decodes the DTE gene to
    a window of that half-width centered on the tuned value: gene 40 -> [25, 55]."""
    s = _strategy(exit_rules=[_option_rule(option_dte_min=30, option_dte_max=60)])
    decoded = decode_params(s, {"exit:o1:a0:option_dte": 40})
    out = decoded["exit_rules"][0]["actions"][0]
    assert out["option_dte_min"] == 25
    assert out["option_dte_max"] == 55


def test_option_dte_window_default_half_width_when_no_base_window():
    """An action with NO base window falls back to a +/-7 day half-width (at least one weekly
    expiry falls in-window): gene 30 -> [23, 37]."""
    s = _strategy(exit_rules=[_option_rule()])
    decoded = decode_params(s, {"exit:o1:a0:option_dte": 30})
    out = decoded["exit_rules"][0]["actions"][0]
    assert out["option_dte_min"] == 23
    assert out["option_dte_max"] == 37
