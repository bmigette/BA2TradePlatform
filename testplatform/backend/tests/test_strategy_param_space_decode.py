import copy, types
from app.services.strategy_param_space import decode_params


def _strategy(**kw):
    base = dict(
        initial_tp_percent=5.0, initial_sl_percent=2.0,
        buy_entry_conditions=None, sell_entry_conditions=None, exit_conditions=[],
    )
    base.update(kw)
    return types.SimpleNamespace(**base)


def test_decode_tp_sl_and_expert_incl_rm_sizing():
    """RM sizing rides on the expert model:* path keyed by the real ba2 names
    (risk_per_trade_pct), landing in expert_overrides — there is no rm key."""
    s = _strategy()
    out = decode_params(s, {"tp": 8.0, "sl": 3.0,
                            "model:risk_per_trade_pct": 2.5,
                            "model:surprise_min_pct": 12.0})
    assert out["tp"] == 8.0 and out["sl"] == 3.0
    assert "rm" not in out
    assert out["expert_overrides"] == {
        "risk_per_trade_pct": 2.5,
        "surprise_min_pct": 12.0,
    }


def test_decode_substitutes_condition_by_id_without_mutating_source():
    buy = {"operator": "AND", "conditions": [
        {"id": "c1", "field": "model:probability", "comparison": ">=", "value": 0.6},
    ]}
    s = _strategy(buy_entry_conditions=buy)
    original = copy.deepcopy(buy)
    out = decode_params(s, {"cond:c1:value": 0.8, "cond:c1:confirmation_bars": 3})
    assert out["buy_tree"]["conditions"][0]["value"] == 0.8
    assert out["buy_tree"]["conditions"][0]["confirmation_bars"] == 3
    assert s.buy_entry_conditions == original  # source untouched


def test_decode_exit_action_value():
    s = _strategy(exit_conditions=[{"id": "e1", "action": "adjust_sl",
                                    "action_value": 1.0, "conditions": {}}])
    out = decode_params(s, {"exit:e1:action_value": 2.5})
    assert out["exit_rules"][0]["action_value"] == 2.5


def test_decode_falls_back_to_strategy_defaults():
    s = _strategy()
    out = decode_params(s, {})  # nothing optimized this trial
    assert out["tp"] == 5.0 and out["sl"] == 2.0
    assert out["expert_overrides"] == {}
    assert "rm" not in out
