"""``min_one_contract`` (the 1-contract sizing floor, plan 2026-09-24 Task 8) must REACH the
option action on the grid path -- set by the launcher, carried through decode and the trial
config, converted by the shared rule builder.

The sizing itself is pinned in ``packages/common/tests/test_option_min_one_contract_floor.py``.
These pin the WIRING, because the failure this project keeps hitting is a knob that is parsed,
stored and logged at every layer and then dropped by one whitelist, so the grid runs without it
while every log says it is on (memory: "trial-config whitelist drops new knobs").

It is a FIXED param for the stage-1 relaunch, NOT a gene: the plan decided the floor, the GA
does not get to search whether to use it.
"""
import importlib.util
import os
import sys

import pytest

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # testplatform/backend
_launcher = os.path.normpath(os.path.join(_root, "..", "ba2test_launcher.py"))
if _root not in sys.path:
    sys.path.insert(0, _root)
_spec = importlib.util.spec_from_file_location("ba2test_launcher", _launcher)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)

from app.services.strategy_param_space import collect_param_space, decode_params  # noqa: E402

_SINGLES = sorted(mod._OPTION_STRATS)
#: Convex harvest opts OUT (operator decision 2026-09-25): its design is many small tickets at
#: ~1% sizing, and the floor would let one ticket the budget cannot afford grow to the
#: per-instrument cap. Every O_CONVEX* key, derived by prefix so a new arm is covered too.
_CONVEX = sorted(k for k in _SINGLES if k.startswith("O_CONVEX"))
_FLOORED = sorted(set(_SINGLES) - set(_CONVEX))


def test_the_convex_exclusion_is_not_vacuous():
    assert set(_CONVEX) >= {"O_CONVEXC", "O_CONVEXP"}, _CONVEX
    assert _FLOORED


@pytest.mark.parametrize("kind", _FLOORED)
def test_every_non_convex_pure_option_entry_carries_the_floor_as_a_fixed_param(kind):
    cfg = mod._option_entry_action_for(kind)
    assert cfg["option_min_one_contract"] is True


@pytest.mark.parametrize("kind", _CONVEX)
def test_the_convex_arms_do_not_carry_the_floor(kind):
    cfg = mod._option_entry_action_for(kind)
    assert "option_min_one_contract" not in cfg, cfg


def test_the_convex_group_emits_no_floored_action():
    """The launchable key is the GROUP ``O_CONVEX``; nothing it builds may carry the floor."""
    from app.services.strategy_param_space import decode_params
    from ba2_common.core.rules_convert import live_actions_from_trade_rule

    strat = mod._build_strategy_option_group("O_CONVEX")
    decoded = decode_params(strat, {})
    for rule in decoded["entry_rules"]:
        for a in rule.get("actions") or []:
            if isinstance(a, dict):
                assert "option_min_one_contract" not in a, a
        for v in (live_actions_from_trade_rule(rule) or {}).values():
            assert "min_one_contract" not in v, v
    assert "option_min_one_contract" not in (getattr(strat, "entry_action", None) or {})


@pytest.mark.parametrize("kind", _SINGLES)
def test_the_floor_is_not_a_gene(kind):
    space = collect_param_space(mod._build_strategy_option(kind))
    assert not [g for g in space if "min_one_contract" in g], sorted(space)


def test_the_overlay_actions_do_not_carry_it():
    """O_CC / O_PP size off HELD shares and never reach the cost sizer."""
    for action_type in ("sell_covered_call", "buy_protective_put"):
        cfg = mod._option_overlay_action(action_type, strike_param=5.0, strike_min=2.0,
                                         strike_max=10.0, strike_step=2.0)
        assert "option_min_one_contract" not in cfg


def _decoded_entry_action(kind):
    strat = mod._build_strategy_option(kind)
    decoded = decode_params(strat, {})
    actions = [a for r in decoded["entry_rules"] for a in (r.get("actions") or [])
               if isinstance(a, dict) and a.get("option_min_one_contract") is not None]
    assert actions, f"{kind}: no decoded entry action carries option_min_one_contract"
    return strat, decoded, actions[0]


@pytest.mark.parametrize("kind", ["O_LC", "O_LP", "O_CSP", "O_IC", "O_PMCC"])
def test_it_survives_decode_and_the_shared_rule_builder(kind):
    """genome -> decoded TradeRule -> the live/backtest action config the evaluator reads."""
    from ba2_common.core.rule_builders import action_from_rule
    from ba2_common.core.rules_convert import live_actions_from_trade_rule
    from ba2_common.core.TradeActionEvaluator import _OPTION_ENTRY_PARAM_KEYS

    _, decoded, action = _decoded_entry_action(kind)
    assert action_from_rule(action)["act"]["min_one_contract"] is True
    rule = next(r for r in decoded["entry_rules"] if action in (r.get("actions") or []))
    live = live_actions_from_trade_rule(rule)
    assert any(v.get("min_one_contract") is True for v in live.values()), live
    assert "min_one_contract" in _OPTION_ENTRY_PARAM_KEYS


def test_the_trial_config_whitelist_carries_it():
    """``_build_daily_trial_config`` rebuilds the trial config key by key. The flag rides the
    decoded entry rules AND the run-level ``entry_action``; both must arrive."""
    from app.services.strategy_optimization_handler import _build_daily_trial_config

    strat, decoded, _ = _decoded_entry_action("O_LC")
    entry_action = strat.entry_action
    assert entry_action["option_min_one_contract"] is True
    backtest_cfg = {
        "backtest_id": 1, "name": "t", "start_date": "2024-06-03", "end_date": "2024-07-01",
        "enabled_instruments": ["AAPL"], "initial_capital": 20000.0, "warmup_days": 0,
        "seed": 42, "account_settings": {},
        "experts": [{"class": "FMPRating", "settings": {}}],
        "entry_action": entry_action,
        # Pinned so the test does not depend on a local options cache existing.
        "options_cache_db": "unused-options-cache.sqlite",
    }
    cfg = _build_daily_trial_config(backtest_cfg, decoded, None, option_trade_records=False)
    assert cfg["entry_action"]["option_min_one_contract"] is True
    carried = [a for r in cfg["entry_rules"] for a in (r.get("actions") or [])
               if isinstance(a, dict) and a.get("option_min_one_contract") is True]
    assert carried, cfg["entry_rules"]
