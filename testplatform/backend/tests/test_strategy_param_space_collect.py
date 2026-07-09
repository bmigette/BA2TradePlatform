import types
from app.services.strategy_param_space import collect_param_space


def _strategy(**kw):
    """Minimal Strategy-like object with the columns collect_param_space reads
    (unified rule model: entry_rules/exit_rules TradeRule lists)."""
    base = dict(entry_rules=[], exit_rules=[])
    base.update(kw)
    return types.SimpleNamespace(**base)


def _entry_rule(rid="r1", conditions=None, actions=None, **kw):
    rule = {"id": rid, "conditions": conditions,
            "actions": actions if actions is not None else [{"action_type": "buy"}],
            "continue_processing": False}
    rule.update(kw)
    return rule


def test_rm_sizing_via_expert_model_namespace():
    """RM sizing is optimized through the expert model:* path keyed by the REAL ba2 setting
    names (e.g. risk_per_trade_pct); there is no separate rm:* namespace anymore."""
    s = _strategy()
    expert = {"risk_per_trade_pct": {"optimize": True, "min": 0.5, "max": 3.0, "step": 0.25,
                                     "type": "float"}}
    space = collect_param_space(s, expert_cfg=expert)
    assert space["model:risk_per_trade_pct"]["type"] == "float"
    assert not any(k.startswith("rm:") for k in space)


def test_collect_expert_namespaced():
    s = _strategy()
    expert = {"surprise_min_pct": {"optimize": True, "min": 1.0, "max": 20.0, "step": 1.0, "type": "float"},
              "max_days_since_report": {"optimize": False, "min": 1, "max": 30, "step": 1, "type": "int"}}
    space = collect_param_space(s, expert_cfg=expert)
    assert "model:surprise_min_pct" in space
    assert "model:max_days_since_report" not in space  # optimize=False


def test_collect_condition_value_and_confirmation_inside_entry_rule():
    conds = {"operator": "AND", "conditions": [
        {"id": "c1", "field": "model:probability", "comparison": ">=", "value": 0.6,
         "optimize": True, "value_min": 0.5, "value_max": 0.9, "value_step": 0.05,
         "confirmation_bars_min": 1, "confirmation_bars_max": 5, "confirmation_bars_step": 1},
    ]}
    s = _strategy(entry_rules=[_entry_rule(conditions=conds)])
    space = collect_param_space(s)
    assert space["cond:c1:value"] == {"type": "float", "min": 0.5, "max": 0.9, "step": 0.05}
    assert space["cond:c1:confirmation_bars"] == {"type": "int", "min": 1, "max": 5, "step": 1}


def test_collect_exit_action_value_per_action():
    s = _strategy(exit_rules=[
        {"id": "e1", "actions": [
            {"action_type": "adjust_stop_loss", "action_value": 1.0,
             "action_value_optimize": True, "action_value_min": 0.5,
             "action_value_max": 3.0, "action_value_step": 0.5},
        ], "conditions": {}},
    ])
    space = collect_param_space(s)
    assert space["exit:e1:a0:action_value"]["min"] == 0.5


def test_collect_rule_toggle_and_per_action_toggle():
    """rule.toggle_optimize -> <ns>:<rid>:enabled; action.toggle_optimize -> per-action
    toggle EXCEPT open (buy/sell) actions, which are never droppable."""
    s = _strategy(entry_rules=[_entry_rule(
        rid="tier1", toggle_optimize=True,
        actions=[
            {"action_type": "buy", "toggle_optimize": True},  # ignored: undroppable
            {"action_type": "adjust_take_profit", "reference_value": "expert_target_price",
             "action_value": -2, "toggle_optimize": True},
        ])])
    space = collect_param_space(s)
    assert space["entry:tier1:enabled"] == {"type": "int", "min": 0, "max": 1, "step": 1}
    assert "entry:tier1:a0:enabled" not in space  # buy can't be toggled off
    assert space["entry:tier1:a1:enabled"] == {"type": "int", "min": 0, "max": 1, "step": 1}


def test_per_rule_brackets_optimize_independently():
    """Two entry rules with their own TP action each -> two independent gene keys (the
    per-tier bracket the flat entry_actions design couldn't express)."""
    def tier(rid, tp):
        return _entry_rule(rid=rid, actions=[
            {"action_type": "buy"},
            {"action_type": "adjust_take_profit", "action_value": tp,
             "action_value_optimize": True, "action_value_min": tp - 5,
             "action_value_max": tp + 5, "action_value_step": 1},
        ])
    s = _strategy(entry_rules=[tier("t1", -5.0), tier("t2", 0.0)])
    space = collect_param_space(s)
    assert space["entry:t1:a1:action_value"]["min"] == -10.0
    assert space["entry:t2:a1:action_value"]["min"] == -5.0


def test_empty_space_raises():
    import pytest
    with pytest.raises(ValueError):
        collect_param_space(_strategy())


def test_bypass_keeps_only_model():
    """BYPASS expert (FactorRanker): the param space drops cond:*/entry:*/exit:* and keeps
    ONLY the expert's own model:* params."""
    s = _strategy(
        entry_rules=[_entry_rule(conditions={"operator": "AND", "conditions": [
            {"id": "c1", "field": "model:probability", "comparison": ">=", "value": 0.6,
             "optimize": True, "value_min": 0.5, "value_max": 0.9, "value_step": 0.05}]})],
        exit_rules=[{"id": "e1", "actions": [
            {"action_type": "adjust_stop_loss", "action_value": 1.0,
             "action_value_optimize": True, "action_value_min": 0.5,
             "action_value_max": 3.0, "action_value_step": 0.5}], "conditions": {}}],
    )
    expert = {"top_n": {"optimize": True, "min": 5, "max": 30, "step": 5, "type": "int"},
              "winsorize_pct": {"optimize": True, "min": 0.0, "max": 0.1, "step": 0.01,
                                "type": "float"}}
    space = collect_param_space(s, expert_cfg=expert, bypass=True)
    assert set(space) == {"model:top_n", "model:winsorize_pct"}


def test_bypass_vs_non_bypass_same_inputs_differ():
    s = _strategy(exit_rules=[{"id": "e1", "actions": [
        {"action_type": "adjust_stop_loss", "action_value": 1.0,
         "action_value_optimize": True, "action_value_min": 0.5,
         "action_value_max": 3.0, "action_value_step": 0.5}], "conditions": {}}])
    expert = {"top_n": {"optimize": True, "min": 5, "max": 30, "step": 5, "type": "int"}}

    classic = collect_param_space(s, expert_cfg=expert, bypass=False)
    bypass = collect_param_space(s, expert_cfg=expert, bypass=True)

    assert "exit:e1:a0:action_value" in classic and "model:top_n" in classic
    assert set(bypass) == {"model:top_n"}
    assert set(bypass) < set(classic)


def test_bypass_with_no_expert_params_raises():
    import pytest
    s = _strategy(entry_rules=[_entry_rule(toggle_optimize=True)])
    with pytest.raises(ValueError):
        collect_param_space(s, expert_cfg=None, bypass=True)


def test_collect_schedule_days_namespaced():
    s = _strategy()
    schedule = {"monday": {"optimize": True}, "thursday": {"optimize": True},
               "tuesday": {"optimize": False}}
    space = collect_param_space(s, expert_cfg={"x": {"optimize": True, "min": 0, "max": 1,
                                                     "step": 1, "type": "int"}},
                                schedule_cfg=schedule)
    assert space["schedule:monday"] == {"type": "int", "min": 0, "max": 1, "step": 1}
    assert space["schedule:thursday"] == {"type": "int", "min": 0, "max": 1, "step": 1}
    assert "schedule:tuesday" not in space  # optimize=False


def test_bypass_excludes_schedule_days():
    s = _strategy()
    schedule = {"monday": {"optimize": True}}
    expert = {"top_n": {"optimize": True, "min": 5, "max": 30, "step": 5, "type": "int"}}
    space = collect_param_space(s, expert_cfg=expert, bypass=True, schedule_cfg=schedule)
    assert not any(k.startswith("schedule:") for k in space)


def test_collect_expert_choice_param_emits_choice_range():
    """A categorical expert param (type='choice') -> a model:<name> choice gene the GA can
    evolve as an int index and decode back to the string (e.g. FMPRating target_price_type)."""
    from app.services.strategy_param_space import _collect_expert
    ecfg = {"target_price_type": {"optimize": True, "type": "choice",
                                  "choices": ["low", "consensus", "median", "high"]}}
    space = _collect_expert(ecfg)
    g = space["model:target_price_type"]
    assert g["type"] == "choice"
    assert g["choices"] == ["low", "consensus", "median", "high"]
    assert g["min"] == 0 and g["max"] == 3 and g["step"] == 1

    # End-to-end: the GA decodes the int index back to a valid choice STRING.
    from app.services.genetic import GeneticOptimizer
    opt = GeneticOptimizer(param_ranges=space, population_size=4, n_generations=1)
    for _ in range(15):
        dec = opt.decode_individual(opt._create_individual())
        assert dec["model:target_price_type"] in g["choices"]
