"""``_option_entry_rule`` must expose the price-vs-analyst-target conditions as toggleable,
optimizable gates (see docs/plans/2026-07-21-options-price-target-conditions.md), and the
existing bullish/bearish rating gate must itself become toggleable so the GA can rely on price
positioning alone.
"""
import importlib.util
import os
import sys

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # testplatform/backend
_launcher = os.path.normpath(os.path.join(_root, "..", "ba2test_launcher.py"))
if _root not in sys.path:
    sys.path.insert(0, _root)
_spec = importlib.util.spec_from_file_location("ba2test_launcher", _launcher)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


def _find_cond(rule, cond_id):
    for c in rule["conditions"]["conditions"]:
        if c["id"] == cond_id:
            return c
    raise AssertionError(f"condition {cond_id} not found in rule {rule['id']}")


def test_signal_gate_is_toggleable():
    rule = mod._option_entry_rule("O_LC")
    signal = _find_cond(rule, "o_lc-signal")
    assert signal["field"] == "bullish"
    assert signal["toggle_optimize"] is True


def test_price_target_gates_present_and_optimizable_with_correct_directions():
    """Four gates, each independently toggleable, so the GA can reach all three non-trivial
    price-positioning patterns (below-low-only, above-high-only, within-range) - not just one
    of them. See the plan's "Design reference" section for why a single op-per-field wiring
    (an earlier, buggy draft of this plan) can't do this: op is never part of the gene space,
    only value and enabled are, so a fixed op only ever reaches ONE direction."""
    rule = mod._option_entry_rule("O_LC")

    low_below = _find_cond(rule, "o_lc-price_low_below")
    assert low_below["field"] == "price_vs_target_low_percent"
    assert low_below["op"] == "<"
    assert low_below["toggle_optimize"] is True
    assert low_below["optimize"] is True
    assert low_below["value_min"] < 0 < low_below["value_max"]

    high_above = _find_cond(rule, "o_lc-price_high_above")
    assert high_above["field"] == "price_vs_target_high_percent"
    assert high_above["op"] == ">"
    assert high_above["toggle_optimize"] is True

    low_above = _find_cond(rule, "o_lc-price_low_above")
    assert low_above["field"] == "price_vs_target_low_percent"
    assert low_above["op"] == ">"
    assert low_above["toggle_optimize"] is True

    high_below = _find_cond(rule, "o_lc-price_high_below")
    assert high_below["field"] == "price_vs_target_high_percent"
    assert high_below["op"] == "<"
    assert high_below["toggle_optimize"] is True


def test_bearish_member_gets_bearish_signal_field():
    rule = mod._option_entry_rule("O_LP")
    signal = _find_cond(rule, "o_lp-signal")
    assert signal["field"] == "bearish"


def test_every_pure_option_member_gets_all_four_price_target_gates():
    for member in mod._OPTION_STRATS:
        rule = mod._option_entry_rule(member)
        m = member.lower()
        _find_cond(rule, f"{m}-price_low_below")
        _find_cond(rule, f"{m}-price_high_above")
        _find_cond(rule, f"{m}-price_low_above")
        _find_cond(rule, f"{m}-price_high_below")
