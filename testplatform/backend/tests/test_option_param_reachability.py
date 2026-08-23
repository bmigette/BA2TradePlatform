"""Every option structure's tunable knobs must be REACHABLE by the GA (2026-08-23).

The structure audit found four holes in the grid, all silent (a frozen parameter looks
exactly like a searched one that converged):

  * O_IC / O_JL / O_RS / O_BF declared no ``option_dte_optimize``, so their DTE window was
    pinned at 25-45 forever while every other structure searched it.
  * O_BF additionally declared no ``option_strike_param_optimize``, so its BODY was frozen
    at 0.0% OTM (always ATM) — the one knob that decides what the butterfly is a bet on.
  * O_CC / O_PP build their overlay action as an inline literal instead of going through
    ``_option_entry_action_for``, so they were the only two option paths with NO
    ``option_min_volume`` floor at all — free to select contracts the fill engine's
    participation cap would then refuse.

These assert the WIRING end to end (strategy config -> gene space), not the declarations, so
a key that exists but never becomes a gene still fails.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "testplatform"))

import ba2test_launcher as L                                              # noqa: E402
from app.services.strategy_param_space import collect_param_space        # noqa: E402

ALL_OPTION_KINDS = sorted(L._OPTION_STRATS)
# The straddles select ATM by construction (OpenStraddleAction / OpenShortStraddleAction
# ignore strike_param entirely), so a strike gene there would be a decoy.
ATM_BY_CONSTRUCTION = {"O_STRD", "O_SSTD"}
WING_STRUCTURES = {"O_IC", "O_JL", "O_BF", "O_RS"}


def _genes(kind):
    return set(collect_param_space(L._build_strategy_option(kind)))


def _suffixes(kind, suffix):
    return {g for g in _genes(kind) if g.endswith(suffix)}


@pytest.mark.parametrize("kind", ALL_OPTION_KINDS)
def test_every_structure_can_search_its_dte_window(kind):
    assert _suffixes(kind, ":option_dte"), f"{kind} DTE is frozen at its hardcoded window"


@pytest.mark.parametrize("kind", sorted(set(ALL_OPTION_KINDS) - ATM_BY_CONSTRUCTION))
def test_every_non_atm_structure_can_search_its_strike(kind):
    assert _suffixes(kind, ":option_delta"), f"{kind} strike_param is frozen"


@pytest.mark.parametrize("kind", sorted(WING_STRUCTURES))
def test_every_winged_structure_can_search_its_wing_width(kind):
    assert _suffixes(kind, ":option_wing_width"), f"{kind} wing width is frozen"


@pytest.mark.parametrize("kind", sorted(ATM_BY_CONSTRUCTION))
def test_the_atm_structures_still_expose_no_strike_gene(kind):
    """Guard against 'fixing' this one by adding a gene the action throws away."""
    assert not _suffixes(kind, ":option_delta")


# --------------------------------------------------------------------------- #
# O_CC / O_PP: equity entry + an option OVERLAY action built outside
# _option_entry_action_for.
# --------------------------------------------------------------------------- #
def _overlay_actions(kind):
    build = {"O_CC": L._build_strategy_covered_call, "O_PP": L._build_strategy_protective_put}
    strat = build[kind](kind)
    return [a for r in (strat.exit_rules or []) for a in (r.get("actions") or [])
            if str(a.get("action_type", "")) in ("sell_covered_call", "buy_protective_put")]


@pytest.mark.parametrize("kind", ["O_CC", "O_PP"])
def test_the_equity_overlays_carry_the_same_min_volume_floor(kind):
    acts = _overlay_actions(kind)
    assert acts, f"{kind} has no option overlay action"
    for a in acts:
        assert a.get("option_min_volume") == L._OPTION_MIN_VOLUME_DEFAULT, (
            f"{kind}'s overlay would select contracts the fill engine cannot fill")


@pytest.mark.parametrize("kind", ["O_CC", "O_PP"])
def test_the_equity_overlays_are_searchable_too(kind):
    build = {"O_CC": L._build_strategy_covered_call, "O_PP": L._build_strategy_protective_put}
    genes = set(collect_param_space(build[kind](kind)))
    assert {g for g in genes if g.endswith(":option_delta")}, f"{kind} overlay strike frozen"
    assert {g for g in genes if g.endswith(":option_dte")}, f"{kind} overlay DTE frozen"
