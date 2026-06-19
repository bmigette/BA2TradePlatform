"""The rm:* param-space namespace is retired (Task 2).

RM sizing is now optimized through the expert ``model:*`` path keyed by the
REAL ba2 setting names (e.g. ``risk_per_trade_pct``). These tests pin that
``collect_param_space`` / ``decode_params`` no longer emit or accept any
``rm:`` key, and that RM sizing flows through ``expert_overrides`` instead.
"""
import types

from app.services.strategy_param_space import collect_param_space, decode_params


def _strategy():
    """A minimal Strategy-like object with NO optimizable tp/sl/conditions."""
    return types.SimpleNamespace(
        initial_tp_optimize=False,
        initial_sl_optimize=False,
        initial_tp_percent=None,
        initial_sl_percent=None,
        buy_entry_conditions=None,
        sell_entry_conditions=None,
        entry_conditions=None,
        exit_conditions=None,
    )


def test_rm_sizing_optimizes_through_model_namespace():
    expert_cfg = {
        "risk_per_trade_pct": {
            "optimize": True,
            "min": 0.5,
            "max": 3.0,
            "step": 0.5,
            "type": "float",
        }
    }
    space = collect_param_space(_strategy(), expert_cfg=expert_cfg)
    assert "model:risk_per_trade_pct" in space
    assert not any(k.startswith("rm:") for k in space)


def test_decode_has_no_rm_key_and_routes_to_expert_overrides():
    decoded = decode_params(_strategy(), {"model:risk_per_trade_pct": 1.5})
    assert decoded["expert_overrides"] == {"risk_per_trade_pct": 1.5}
    assert "rm" not in decoded
