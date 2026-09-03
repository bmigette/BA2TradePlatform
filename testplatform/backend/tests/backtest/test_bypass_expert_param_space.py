"""GA wiring (spec §3.4): a BYPASS expert's gene space is its own model:* params
ONLY — classic cond:/exit:/tp/sl genes are stripped by collect_param_space(bypass=True).

Nothing here is PremiumSeller-specific and never was; the file was named
``test_premium_seller_ga.py`` because that expert was the first bypass expert. It was
deleted on 2026-08-31 and the name outlived it, pointing every reader at code that no
longer exists. Renamed 2026-09-01 to what the file actually pins (FactorRanker is the
bypass expert this now guards)."""
import pytest


def test_bypass_param_space_is_model_only():
    from app.services.strategy_param_space import collect_param_space
    expert_cfg = {
        "target_delta": {"optimize": True, "min": 0.15, "max": 0.35, "step": 0.05, "type": "float"},
        "roll_dte": {"optimize": True, "min": 7, "max": 28, "step": 7, "type": "int"},
        "static_universe": {"optimize": False},
    }
    space = collect_param_space(None, expert_cfg=expert_cfg, bypass=True)
    assert set(space) == {"model:target_delta", "model:roll_dte"}
    assert not any(k.startswith(("cond:", "exit:", "entry:")) for k in space)


def test_bypass_param_space_requires_optimizable():
    from app.services.strategy_param_space import collect_param_space
    with pytest.raises(ValueError, match="bypass expert"):
        collect_param_space(None, expert_cfg={"target_delta": {"optimize": False}}, bypass=True)
