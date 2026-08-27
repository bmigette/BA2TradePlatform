"""Task D1 — launcher option strategy builders + entry_action carrying.

The launcher (``testplatform/ba2test_launcher.py``) is a top-level script, not an
importable package module, so we load it by file path. We assert:
  * all 11 option/equity strategy keys are registered in ``_STRATEGY_BUILDERS``,
  * ``_option_entry_action_for`` emits the right option action config (incl.
    ``option_strike_param`` and, for wing structures, an optimizable wing range),
  * building O_IC carries an entry_action with the iron-condor action + wing range,
  * building O_CC carries a ``sell_covered_call`` overlay in exit_conditions,
  * ``_build_strategy`` dispatches every O_* key to a Strategy without error.
"""
import importlib.util
import os
import sys

import pytest


@pytest.fixture(autouse=True)
def _allow_unrunnable_wheel(monkeypatch):
    """O_WHEEL refuses to BUILD unless this override is set.

    Deliberate: the backtest liquidates assigned stock at the next bar's open, AFTER the manage
    pass has written a covered call against it, so every wheel position it opens is a naked short
    call. Tests in this file enumerate and build EVERY registered key, so they need the
    engine-development override. The refusal itself is asserted in
    test_option_grid_foundations.py::test_o_wheel_refuses_to_build_by_default.
    """
    monkeypatch.setenv("BA2_ALLOW_UNRUNNABLE_WHEEL", "1")


# Load the launcher module by path (it lives at testplatform/ba2test_launcher.py).
_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # testplatform/backend
_launcher = os.path.normpath(os.path.join(_root, "..", "ba2test_launcher.py"))
# The launcher imports `app.*`; ensure the backend dir is importable.
if _root not in sys.path:
    sys.path.insert(0, _root)
_spec = importlib.util.spec_from_file_location("ba2test_launcher", _launcher)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


_ALL_KEYS = ["O_LC", "O_CC", "O_VERT", "O_STK", "O_SSTG", "O_SSTD", "O_WHEEL",
             "O_IC", "O_JL", "O_BF", "O_RS",
             "O_BULLCS", "O_BEARCS", "O_CSP", "O_STRD", "O_STRG", "O_PP"]
# O_WHEEL is pure-option by entry (it sells a put) even though it grows an equity leg on
# assignment -- _PURE_OPTION_STRATEGIES includes it, and _resolve_fitness gives it the
# option metric on that basis.
_PURE_OPTION_KEYS = ["O_LC", "O_VERT", "O_SSTG", "O_SSTD", "O_IC", "O_JL", "O_BF", "O_RS", "O_WHEEL",
                     "O_BULLCS", "O_BEARCS", "O_CSP", "O_STRD", "O_STRG"]


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
    actions = [a.get("action_type")
               for r in (strat.exit_rules or [])
               for a in (r.get("actions") or [])]
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


# --- Long put (O_LP) + grouped families (OS1/OS2/OS3) ------------------------------------
def test_long_put_registered_and_gates_bearish():
    strat = mod._build_strategy("O_LP", "O_LP", "FMPRating")
    ea = getattr(strat, "entry_action", None)
    assert ea is not None and ea["action_type"] == "buy_put"
    fields = [c.get("field") for c in strat.entry_rules[0]["conditions"]["conditions"]]
    assert "bearish" in fields, f"O_LP entry must gate on the bearish signal; got {fields}"


def test_option_groups_registered_and_build():
    for kind, members in mod._OPTION_GROUPS.items():
        assert kind in mod._STRATEGY_BUILDERS, f"{kind} missing from _STRATEGY_BUILDERS"
        strat = mod._build_strategy(kind, kind, "FMPRating")
        # One toggleable entry rule per member, ids prefixed by the member key.
        rids = [r.get("id") for r in strat.entry_rules]
        assert rids == [f"{m.lower()}-entry" for m in members]
        assert all(r.get("toggle_optimize") for r in strat.entry_rules), \
            "every group member rule must be GA-toggleable"
        # Each rule's action is that member's own option action.
        for m, r in zip(members, strat.entry_rules):
            assert r["actions"][0]["action_type"] == mod._OPTION_STRATS[m]["action_type"]
        # entry_action (the engine's option-path flag) is option-typed.
        ea = getattr(strat, "entry_action", None)
        assert ea is not None and "action_type" in ea


def test_group_param_space_keys_genes_per_member():
    from app.services.strategy_param_space import collect_param_space

    strat = mod._build_strategy("OS1", "OS1", "FMPRating")
    space = collect_param_space(strat)
    for m in mod._OPTION_GROUPS["OS1"]:
        assert f"entry:{m.lower()}-entry:enabled" in space, f"missing toggle gene for {m}"


def test_group_decode_drops_disabled_members():
    from app.services.strategy_param_space import decode_params

    strat = mod._build_strategy("OS1", "OS1", "FMPRating")
    members = mod._OPTION_GROUPS["OS1"]
    keep = members[1]  # keep only the second member
    flat = {f"entry:{m.lower()}-entry:enabled": (1 if m == keep else 0) for m in members}
    decoded = decode_params(strat, flat)
    rids = [r.get("id") for r in (decoded.get("entry_rules") or [])]
    assert rids == [f"{keep.lower()}-entry"], f"expected only {keep} to survive; got {rids}"


# --- New structure types: bull/bear call spreads, cash-secured put, long straddle/strangle,
# protective put (OS1/OS2/OS3/OS4 extensions + the O_PP equity+overlay hybrid) -----------------

def test_bull_call_spread_builder_emits_entry_action():
    entry = mod._option_entry_action_for("O_BULLCS")
    assert entry["action_type"] == "open_bull_call_spread"
    assert "option_strike_param" in entry


def test_bear_call_spread_builder_emits_entry_action():
    entry = mod._option_entry_action_for("O_BEARCS")
    assert entry["action_type"] == "open_bear_call_spread"
    assert "option_strike_param" in entry


def test_cash_secured_put_builder_emits_entry_action():
    entry = mod._option_entry_action_for("O_CSP")
    assert entry["action_type"] == "sell_cash_secured_put"
    assert "option_strike_param" in entry


def test_long_straddle_builder_emits_entry_action():
    entry = mod._option_entry_action_for("O_STRD")
    assert entry["action_type"] == "open_straddle"


def test_long_strangle_builder_emits_entry_action():
    entry = mod._option_entry_action_for("O_STRG")
    assert entry["action_type"] == "open_strangle"
    assert "option_strike_param" in entry


def test_bear_call_spread_gates_bearish():
    """O_BEARCS is a directional-bearish credit structure -- must gate like O_LP, not the
    default bullish gate every other original key uses."""
    strat = mod._build_strategy("O_BEARCS", "O_BEARCS", "FMPRating")
    fields = [c.get("field") for c in strat.entry_rules[0]["conditions"]["conditions"]]
    assert "bearish" in fields, f"O_BEARCS entry must gate on the bearish signal; got {fields}"


def test_protective_put_registered_and_has_overlay_rule():
    """O_PP mirrors O_CC's shape: equity entry (no entry_action) + a buy_protective_put
    OPEN_POSITIONS overlay."""
    strat = mod._build_strategy("O_PP", "O_PP", "FMPRating")
    assert getattr(strat, "entry_action", None) is None
    actions = [a.get("action_type")
               for r in (strat.exit_rules or [])
               for a in (r.get("actions") or [])]
    assert "buy_protective_put" in actions, f"expected buy_protective_put overlay; got {actions}"


def test_os4_is_non_directional_volatility_group():
    """OS4 groups the two non-directional (long vol) structures."""
    assert mod._OPTION_GROUPS["OS4"] == ["O_STRD", "O_STRG"]


def test_every_pure_option_action_type_is_unique_across_groups():
    """Sanity: no two _OPTION_STRATS keys should collide on the same underlying action_type
    (that would mean a structure got accidentally duplicated instead of a new one added)."""
    action_types = [v["action_type"] for v in mod._OPTION_STRATS.values()]
    assert len(action_types) == len(set(action_types)), \
        f"duplicate action_type in _OPTION_STRATS: {action_types}"


# --------------------------------------------------------------------------- #
# $20k affordability + overlay wiring (2026-07-25)
# --------------------------------------------------------------------------- #
def test_option_overlay_strategies_size_the_equity_entry_in_round_lots():
    """Regression: O_CC/O_PP must buy in 100-share lots so the overlay can actually fire.

    SellCoveredCall/BuyProtectivePut size as floor(held_shares/100). At $20k the RM buys
    3-85 shares, never 100, so both overlays no-opped and BOTH jobs silently degenerated to
    the plain S2 equity baseline -- v8 shipped byte-identical O_CC and O_PP top-5 rows with
    ZERO trades carrying a contract_symbol."""
    for kind in ("O_CC", "O_PP"):
        strat = mod._build_strategy(kind, kind, "FMPRating")
        lots = [a.get("lot_size")
                for r in (strat.entry_rules or [])
                for a in (r.get("actions") or [])
                if a.get("action_type") == "buy"]
        assert lots and all(l == 100 for l in lots), \
            f"{kind} equity entry must carry lot_size=100; got {lots}"


def test_plain_equity_control_is_not_lot_constrained():
    """O_STK is the plain-equity CONTROL — constraining it to round lots would change the
    baseline the option jobs are measured against."""
    strat = mod._build_strategy("O_STK", "O_STK", "FMPRating")
    lots = [a.get("lot_size")
            for r in (strat.entry_rules or [])
            for a in (r.get("actions") or [])]
    assert all(l is None for l in lots), f"O_STK must stay odd-lot; got {lots}"


def test_covered_call_and_protective_put_are_not_the_same_strategy():
    """They differ only in the overlay action; v8 shipped them producing identical results."""
    cc = mod._build_strategy("O_CC", "O_CC", "FMPRating")
    pp = mod._build_strategy("O_PP", "O_PP", "FMPRating")
    cc_actions = {a.get("action_type") for r in (cc.exit_rules or []) for a in (r.get("actions") or [])}
    pp_actions = {a.get("action_type") for r in (pp.exit_rules or []) for a in (r.get("actions") or [])}
    assert "sell_covered_call" in cc_actions and "sell_covered_call" not in pp_actions
    assert "buy_protective_put" in pp_actions and "buy_protective_put" not in cc_actions


def test_full_notional_structures_excluded_from_default_groups_at_20k():
    """CSP / jade lizard / put ratio spread reserve 18k-30k PER CONTRACT on this large-cap
    universe (strike-scaled notional), i.e. the whole $20k account or more. They stay
    defined + standalone-runnable, just out of the default grouped search."""
    grouped = {m for members in mod._OPTION_GROUPS.values() for m in members}
    for kind in ("O_CSP", "O_JL", "O_RS"):
        assert kind not in grouped, f"{kind} is unaffordable at $20k and must not be grouped"
        assert kind in mod._OPTION_STRATS, f"{kind} must remain runnable standalone"
    # ...and no group may be left empty by the filter.
    assert all(members for members in mod._OPTION_GROUPS.values())
