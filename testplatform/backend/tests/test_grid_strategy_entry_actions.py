"""Grid-launcher strategy rework (ba2test_launcher) on the UNIFIED RULE MODEL: S1/S6/S7 carry
an entry-time TP/SL bracket as GA on/off-TOGGLEABLE actions on their entry TradeRules, S4 is
merged into S1 (disabled), and S1's bracket mirrors the live "high conviction" ruleset
(target-anchored TP + entry SL) replicated per entry rule (per live OR branch).

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


def _bracket_actions(strat):
    """{action_id: action} across every entry rule's adjust actions (id may repeat across
    rules — per-branch replication — so this keeps the first occurrence for value asserts)."""
    out = {}
    for rule in (strat.entry_rules or []):
        for a in (rule.get("actions") or []):
            aid = a.get("id")
            if aid and aid not in out:
                out[aid] = a
    return out


def test_s1_has_high_conviction_entry_bracket_toggleable():
    s1 = _L._build_strategy("S1", "S1-FMPRating", "FMPRating")
    ea = _bracket_actions(s1)
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
    # PER-RULE replication (live's per-tier bracket shape): every entry rule carries its own
    # copy of the bracket alongside its buy action.
    for rule in s1.entry_rules:
        kinds = [a["action_type"] for a in rule["actions"]]
        assert kinds[0] == "buy"
        assert "adjust_take_profit" in kinds and "adjust_stop_loss" in kinds


def test_s1_entry_bracket_emits_ga_toggle_and_value_genes():
    s1 = _L._build_strategy("S1", "S1-FMPRating", "FMPRating")
    space = collect_param_space(s1)
    # Per-rule bracket genes: each entry rule (one per live OR branch) exposes its own
    # value + on/off genes for the TP (a1) and SL (a2) actions.
    rids = [r["id"] for r in s1.entry_rules]
    assert rids, "S1 must carry entry rules"
    for rid in rids:
        assert f"entry:{rid}:a1:action_value" in space  # TP value optimizable
        assert f"entry:{rid}:a1:enabled" in space       # TP on/off optimizable
        assert f"entry:{rid}:a2:action_value" in space  # SL value optimizable
        assert f"entry:{rid}:a2:enabled" in space       # SL on/off optimizable
    # GA turning ONE rule's entry TP off drops that action from that rule only; the open
    # action is never droppable.
    rid = rids[0]
    decoded = decode_params(s1, {f"entry:{rid}:a1:enabled": 0})
    rule = next(r for r in decoded["entry_rules"] if r["id"] == rid)
    kinds = [a["action_type"] for a in rule["actions"]]
    assert "adjust_take_profit" not in kinds
    assert kinds[0] == "buy" and "adjust_stop_loss" in kinds


def test_s6_has_toggleable_entry_tp_sl():
    ea = _bracket_actions(_L._build_strategy("S6", "S6", ""))
    adjust = {a["action_type"]: a for a in ea.values()
              if str(a.get("action_type", "")).startswith("adjust_")}
    assert "adjust_take_profit" in adjust and "adjust_stop_loss" in adjust
    assert all(a["toggle_optimize"] is True for a in adjust.values())


def test_s7_is_faithful_replica_no_entry_bracket_floor_stop_last():
    """S7 (rebuilt): FAITHFUL replica of the archived 186% winner — schedule-evaluated
    exit-condition TP/SL, NO entry-time bracket (the first S7's lossy translation), and the
    always-matching floor stop LAST so it can't shadow the other exit rules under the
    engine's first-match semantics."""
    s7 = _L._build_strategy("S7", "S7", "")
    # no entry bracket: entry rules carry ONLY the open action
    for rule in s7.entry_rules:
        assert [a["action_type"] for a in rule["actions"]] == ["buy"]
    ids = [r["id"] for r in s7.exit_rules]
    assert ids[-1] == "exit_stoploss"          # floor fallback last
    assert "exit_takeprofit" in ids            # the winner's +32% ceiling, as a CLOSE
    assert ids.index("exit_bearish") < ids.index("exit_belock")  # closes before ratchets


def test_s4_is_structure_native_multi_action_trailing():
    """S4 (reborn): explores the unified model's exclusive capabilities — multi-action tier
    rules (SL ratchet + TP extension in ONE rule) and a continue_processing TP-follow that
    doesn't shadow the ladder. Floor stop last + toggleable."""
    s4 = _L._build_strategy("S4", "S4", "FMPRating")
    ids = [r["id"] for r in s4.exit_rules]
    assert ids == ["exit_bearish", "exit_downgrade", "tp_follow",
                   "tier3", "tier2", "tier1", "exit_stoploss"]
    tier3 = next(r for r in s4.exit_rules if r["id"] == "tier3")
    kinds = [a["action_type"] for a in tier3["actions"]]
    assert kinds == ["adjust_stop_loss", "adjust_take_profit"]  # MULTI-ACTION rule
    tp_follow = next(r for r in s4.exit_rules if r["id"] == "tp_follow")
    assert tp_follow["continue_processing"] is True  # fires AND falls through to the ladder
    assert tp_follow["actions"][0]["reference_value"] == "expert_target_price"


def test_s1_gets_a_larger_population_factor():
    assert _L._STRATEGY_POP_FACTOR.get("S1", 1.0) > 1.0
