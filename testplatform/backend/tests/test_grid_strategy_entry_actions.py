"""Grid-launcher strategy rework (ba2test_launcher): S1/S6/S7 carry an entry-time TP/SL bracket
that is GA on/off-TOGGLEABLE, S4 is merged into S1 (disabled), and S1's entry bracket mirrors the
live "high conviction" ruleset (target-anchored TP + entry SL).

The launcher is a top-level script, not a package module, so it is loaded by file path (same as
test_option_strategy_builders.py).
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
_L = importlib.util.module_from_spec(_spec)
sys.modules["ba2test_launcher"] = _L
_spec.loader.exec_module(_L)

from app.services.strategy_param_space import collect_param_space, decode_params  # noqa: E402


def _entry(strat):
    return {a["id"]: a for a in (getattr(strat, "entry_actions", None) or [])}


def test_s1_has_high_conviction_entry_bracket_toggleable():
    s1 = _L._build_strategy("S1", "S1-FMPRating", "FMPRating")
    ea = _entry(s1)
    # target-anchored TP (merged from S4) + entry SL, matching live ruleset 10 dominant-tier values
    assert ea["s1_tp_target"]["action_type"] == "adjust_take_profit"
    assert ea["s1_tp_target"]["reference_value"] == "expert_target_price"
    assert ea["s1_tp_target"]["action_value"] == -5.0
    assert ea["s1_sl_entry"]["action_type"] == "adjust_stop_loss"
    assert ea["s1_sl_entry"]["reference_value"] == "order_open_price"
    assert ea["s1_sl_entry"]["action_value"] == -8.0
    # both GA on/off-toggleable
    assert ea["s1_tp_target"]["toggle_optimize"] is True
    assert ea["s1_sl_entry"]["toggle_optimize"] is True


def test_s1_entry_bracket_emits_ga_toggle_and_value_genes():
    s1 = _L._build_strategy("S1", "S1-FMPRating", "FMPRating")
    space = collect_param_space(s1)
    for gid in ("s1_tp_target", "s1_sl_entry"):
        assert f"entry:{gid}:action_value" in space  # value optimizable
        assert f"entry:{gid}:enabled" in space       # on/off optimizable
    # GA turning the entry TP off drops it from decoded entry_rules
    decoded = decode_params(s1, {"entry:s1_tp_target:enabled": 0})
    assert not any(r["id"] == "s1_tp_target" for r in decoded["entry_rules"])
    assert any(r["id"] == "s1_sl_entry" for r in decoded["entry_rules"])


def test_s6_and_s7_have_toggleable_entry_tp_sl():
    for kind in ("S6", "S7"):
        ea = _entry(_L._build_strategy(kind, kind, ""))
        types = {a["action_type"] for a in ea.values()}
        assert "adjust_take_profit" in types and "adjust_stop_loss" in types, kind
        assert all(a["toggle_optimize"] is True for a in ea.values()), kind


def test_s4_is_merged_into_s1_and_disabled():
    assert "S4" not in _L._STRATEGY_BUILDERS
    with pytest.raises(SystemExit) as ei:
        _L._build_strategy("S4", "S4", "FMPRating")
    assert "merged into S1" in str(ei.value)


def test_s1_gets_a_larger_population_factor():
    assert _L._STRATEGY_POP_FACTOR.get("S1", 1.0) > 1.0
