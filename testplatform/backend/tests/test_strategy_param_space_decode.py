import copy, types
from app.services.strategy_param_space import decode_params


def _strategy(**kw):
    base = dict(entry_rules=[], exit_rules=[])
    base.update(kw)
    return types.SimpleNamespace(**base)


def test_decode_expert_incl_rm_sizing():
    """RM sizing rides on the expert model:* path keyed by the real ba2 names
    (risk_per_trade_pct), landing in expert_overrides — there is no rm key."""
    s = _strategy()
    out = decode_params(s, {"model:risk_per_trade_pct": 2.5,
                            "model:surprise_min_pct": 12.0})
    assert "rm" not in out and "tp" not in out and "sl" not in out
    assert out["expert_overrides"] == {
        "risk_per_trade_pct": 2.5,
        "surprise_min_pct": 12.0,
    }


def test_decode_substitutes_condition_by_id_without_mutating_source():
    entry_rules = [{"id": "r1", "actions": [{"action_type": "buy"}],
                    "conditions": {"operator": "AND", "conditions": [
                        {"id": "c1", "field": "model:probability",
                         "comparison": ">=", "value": 0.6}]},
                    "continue_processing": False}]
    s = _strategy(entry_rules=entry_rules)
    original = copy.deepcopy(entry_rules)
    out = decode_params(s, {"cond:c1:value": 0.8, "cond:c1:confirmation_bars": 3})
    leaf = out["entry_rules"][0]["conditions"]["conditions"][0]
    assert leaf["value"] == 0.8 and leaf["confirmation_bars"] == 3
    assert s.entry_rules == original  # source untouched


def test_decode_exit_action_value_per_action():
    s = _strategy(exit_rules=[{"id": "e1", "actions": [
        {"action_type": "adjust_stop_loss", "action_value": 1.0}], "conditions": {}}])
    out = decode_params(s, {"exit:e1:a0:action_value": 2.5})
    a = out["exit_rules"][0]["actions"][0]
    assert a["action_value"] == 2.5 and a["value"] == 2.5


def test_decode_rule_toggle_drops_whole_rule():
    s = _strategy(exit_rules=[
        {"id": "keep", "actions": [{"action_type": "close"}], "conditions": {}},
        {"id": "drop", "actions": [{"action_type": "close"}], "conditions": {}},
    ])
    out = decode_params(s, {"exit:drop:enabled": 0, "exit:keep:enabled": 1})
    assert [r["id"] for r in out["exit_rules"]] == ["keep"]


def test_decode_action_toggle_drops_action_but_never_the_open():
    s = _strategy(entry_rules=[{"id": "r1", "conditions": None, "actions": [
        {"action_type": "buy"},
        {"action_type": "adjust_take_profit", "action_value": -2},
        {"action_type": "adjust_stop_loss", "action_value": -10},
    ], "continue_processing": False}])
    out = decode_params(s, {
        "entry:r1:a1:enabled": 0,  # TP dropped
        "entry:r1:a0:enabled": 0,  # buy toggle ignored: undroppable
        "entry:r1:a2:enabled": 1,
    })
    kinds = [a["action_type"] for a in out["entry_rules"][0]["actions"]]
    assert kinds == ["buy", "adjust_stop_loss"]


def test_decode_per_rule_brackets_apply_independently():
    def tier(rid, tp):
        return {"id": rid, "conditions": None, "continue_processing": False,
                "actions": [{"action_type": "buy"},
                            {"action_type": "adjust_take_profit", "action_value": tp}]}
    s = _strategy(entry_rules=[tier("t1", -5.0), tier("t2", 0.0)])
    out = decode_params(s, {"entry:t1:a1:action_value": -7.0,
                            "entry:t2:a1:action_value": 2.0})
    assert out["entry_rules"][0]["actions"][1]["action_value"] == -7.0
    assert out["entry_rules"][1]["actions"][1]["action_value"] == 2.0


def test_decode_empty_genes_passthrough():
    s = _strategy(entry_rules=[{"id": "r1", "conditions": None,
                                "actions": [{"action_type": "buy"}],
                                "continue_processing": True}])
    out = decode_params(s, {})  # nothing optimized this trial
    assert out["entry_rules"][0]["continue_processing"] is True
    assert out["expert_overrides"] == {}
    assert out["schedule_days"] is None  # no schedule:* genes -> caller keeps static override


def test_decode_schedule_days_all_seven_keys_present():
    s = _strategy()
    out = decode_params(s, {"schedule:monday": 1, "schedule:thursday": 0})
    assert out["schedule_days"] == {
        "monday": True, "tuesday": False, "wednesday": False, "thursday": False,
        "friday": False, "saturday": False, "sunday": False,
    }


def test_decode_schedule_days_repairs_all_off_to_first_day():
    """An all-days-OFF individual is a dead config (never scans for entries) -- repaired to
    Monday rather than wasting a trial evaluating it."""
    s = _strategy()
    out = decode_params(s, {"schedule:monday": 0, "schedule:tuesday": 0, "schedule:wednesday": 0})
    assert out["schedule_days"]["monday"] is True
    assert sum(out["schedule_days"].values()) == 1
