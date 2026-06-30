"""Task D1 — launcher option strategy builders + entry_action carrying.

The launcher (``testplatform/ba2test_launcher.py``) is a top-level script, not an
importable package module, so we load it by file path. We assert:
  * all 10 option/equity strategy keys are registered in ``_STRATEGY_BUILDERS``,
  * ``_option_entry_action_for`` emits the right option action config (incl.
    ``option_strike_param`` and, for wing structures, an optimizable wing range),
  * building O_IC carries an entry_action with the iron-condor action + wing range,
  * building O_CC carries a ``sell_covered_call`` overlay in exit_conditions,
  * ``_build_strategy`` dispatches every O_* key to a Strategy without error.
"""
import importlib.util
import os
import sys

# Load the launcher module by path (it lives at testplatform/ba2test_launcher.py).
_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # testplatform/backend
_launcher = os.path.normpath(os.path.join(_root, "..", "ba2test_launcher.py"))
# The launcher imports `app.*`; ensure the backend dir is importable.
if _root not in sys.path:
    sys.path.insert(0, _root)
_spec = importlib.util.spec_from_file_location("ba2test_launcher", _launcher)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


_ALL_KEYS = ["O_LC", "O_CC", "O_VERT", "O_STK", "O_SSTG", "O_SSTD",
             "O_IC", "O_JL", "O_BF", "O_RS"]
_PURE_OPTION_KEYS = ["O_LC", "O_VERT", "O_SSTG", "O_SSTD", "O_IC", "O_JL", "O_BF", "O_RS"]


def test_option_strategy_keys_registered():
    for k in _ALL_KEYS:
        assert k in mod._STRATEGY_BUILDERS, f"{k} missing from _STRATEGY_BUILDERS"


def test_short_strangle_builder_emits_entry_action():
    entry = mod._option_entry_action_for("O_SSTG")
    assert entry["action_type"] == "open_short_strangle"
    assert "option_strike_param" in entry


def test_iron_condor_carries_entry_action_with_wing_range():
    strat = mod._build_strategy("O_IC", "O_IC", "FMPRating")
    ea = getattr(strat, "entry_action", None)
    assert ea is not None, "O_IC strategy must carry an entry_action"
    assert ea["action_type"] == "open_iron_condor"
    # Wing structures expose an optimizable wing-width range.
    assert ea.get("option_wing_width_optimize") is True
    assert "option_wing_width_min" in ea and "option_wing_width_max" in ea


def test_covered_call_has_overlay_rule():
    strat = mod._build_strategy("O_CC", "O_CC", "FMPRating")
    # O_CC is an equity entry with a covered-call OPEN_POSITIONS overlay (no entry_action).
    assert getattr(strat, "entry_action", None) is None
    actions = [r.get("action_type") for r in (strat.exit_conditions or [])]
    assert "sell_covered_call" in actions, f"expected sell_covered_call overlay; got {actions}"


def test_dispatch_returns_strategy_for_every_option_key():
    for k in _ALL_KEYS:
        strat = mod._build_strategy(k, k, "FMPRating")
        assert strat is not None
        assert strat.name == k


def test_pure_option_keys_all_carry_entry_action():
    for k in _PURE_OPTION_KEYS:
        strat = mod._build_strategy(k, k, "FMPRating")
        ea = getattr(strat, "entry_action", None)
        assert ea is not None, f"{k} must carry an entry_action"
        assert "action_type" in ea
