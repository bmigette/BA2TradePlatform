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


def test_price_vs_target_conditions_present_and_optimizable():
    rule = mod._option_entry_rule("O_LC")
    low = _find_cond(rule, "o_lc-price_low")
    assert low["field"] == "price_vs_target_low_percent"
    assert low["toggle_optimize"] is True
    assert low["optimize"] is True
    assert low["value_min"] < 0 < low["value_max"]

    high = _find_cond(rule, "o_lc-price_high")
    assert high["field"] == "price_vs_target_high_percent"
    assert high["toggle_optimize"] is True


def test_bearish_member_gets_bearish_signal_field():
    rule = mod._option_entry_rule("O_LP")
    signal = _find_cond(rule, "o_lp-signal")
    assert signal["field"] == "bearish"


def test_every_pure_option_member_gets_price_target_conditions():
    for member in mod._OPTION_STRATS:
        rule = mod._option_entry_rule(member)
        m = member.lower()
        _find_cond(rule, f"{m}-price_low")
        _find_cond(rule, f"{m}-price_high")
