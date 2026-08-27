"""``_option_entry_rule``'s bullish/bearish rating gate must be toggleable, so the GA can rely
on the other entry gates alone rather than always requiring the expert's direction call.

SCOPE NARROWED 2026-08-27. This file used to also pin the four ``price_vs_target_*`` gates
(``test_price_target_gates_present_and_optimizable_with_correct_directions`` and
``test_every_pure_option_member_gets_all_four_price_target_gates``, both removed with them).
Those gates were deleted -- not weakened -- because ``PriceVsTargetLow/HighCondition`` reads
``expert_recommendation.data["FMPRating"][...]`` and only FMPRating writes that key, so under
any other expert all four failed CLOSED. They are replaced by the single expert-independent
``-exp_profit`` gate; that they must STAY gone is asserted in
test_option_grid_foundations.py::test_no_structure_gates_on_the_analyst_target_range.
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


def test_bearish_member_gets_bearish_signal_field():
    rule = mod._option_entry_rule("O_LP")
    signal = _find_cond(rule, "o_lp-signal")
    assert signal["field"] == "bearish"


def test_every_pure_option_member_gets_the_expected_profit_gate():
    """The replacement for ``test_every_pure_option_member_gets_all_four_price_target_gates``.

    Same shape of guarantee -- every member carries the signal-strength gate, none is left with
    no signal gate at all -- on the field every expert can actually answer.
    """
    for member in mod._OPTION_STRATS:
        gate = _find_cond(mod._option_entry_rule(member), f"{member.lower()}-exp_profit")
        assert gate["field"] == "expected_profit_target_percent"
        assert gate["op"] == ">"
        assert gate["optimize"] is True
        assert gate["toggle_optimize"] is True
