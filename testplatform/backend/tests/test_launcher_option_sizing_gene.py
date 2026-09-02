"""``option_sizing`` must be a SEARCHED gene (OPT-C6).

Position size (% of equity per structure) is bounded, symbol-comparable and exactly the same
category of knob as %OTM / DTE / wing width -- and ``_collect_action_genes`` emitted nothing
for it, so it was a per-structure constant the GA could not touch.

It also gates the fitness work: ``contracts x max_loss`` IS ``option_sizing`` % of equity by
construction, so any return-on-collateral measure divides by a constant while sizing is frozen
and degenerates back into plain return.

Measured BEFORE this file existed::

    authored option_sizing per structure: {'O_LC': 5.0, 'O_VERT': 5.0, 'O_SSTG': 20.0,
      'O_SSTD': 20.0, 'O_IC': 20.0, 'O_JL': 20.0, 'O_BF': 8.0, 'O_RS': 15.0, 'O_LP': 5.0,
      'O_BULLCS': 5.0, 'O_BEARCS': 15.0, 'O_BULLPS': 15.0, 'O_CSP': 20.0, 'O_STRD': 5.0,
      'O_STRG': 5.0}
    distinct values: [5.0, 8.0, 15.0, 20.0]
    option_sizing GENES across all 19 built option strategies: 0
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

from app.services.strategy_param_space import (  # noqa: E402
    collect_param_space,
    decode_params,
)

_SINGLES = sorted(mod._OPTION_STRATS)


def _build(kind):
    if kind in mod._OPTION_GROUPS:
        return mod._build_strategy_option_group(kind)
    return mod._build_strategy_option(kind)


@pytest.mark.parametrize("kind", _SINGLES)
def test_every_pure_option_structure_searches_its_size(kind):
    space = collect_param_space(_build(kind))
    key = f"entry:{kind.lower()}-entry:a0:option_sizing"
    assert key in space, (
        f"{kind}: position size is not in the emitted parameter space, so it is a constant "
        f"the GA cannot touch: {sorted(space)}"
    )


@pytest.mark.parametrize("kind", sorted(mod._OPTION_GROUPS))
def test_every_group_member_sizes_independently(kind):
    space = collect_param_space(_build(kind))
    for member in mod._OPTION_GROUPS[kind]:
        assert f"entry:{member.lower()}-entry:a0:option_sizing" in space


@pytest.mark.parametrize("kind", _SINGLES)
def test_the_band_brackets_the_authored_size_and_actually_varies(kind):
    """A band that does not contain the authored value silently RE-SIZES every structure; a
    degenerate band (min == max) is a gene that cannot move."""
    space = collect_param_space(_build(kind))
    spec = space[f"entry:{kind.lower()}-entry:a0:option_sizing"]
    authored = mod._OPTION_STRATS[kind]["option_sizing"]
    assert spec["min"] < spec["max"], f"{kind}: degenerate sizing band {spec}"
    assert spec["min"] <= authored <= spec["max"], (
        f"{kind}: authored size {authored} is outside the searched band {spec}"
    )
    assert spec["step"] > 0
    levels = int((spec["max"] - spec["min"]) / spec["step"]) + 1
    assert levels >= 5, f"{kind}: only {levels} sizing levels"


#: The convex-harvest grid's two members (plan Task 13) are the ONE deliberate exception to
#: the >=1.0% floor below: design 2026-08-31-convex-harvest-grid-design.md §2 states the
#: per-ticket floor as 0.5% OF SLEEVE, not 1% -- "many small tickets, no single ticket may
#: dominate ex-ante" is a NARROWER bet than any grid-1/grid-2 structure's "a real 1-contract
#: bet" reasoning ever assumed. Excluded from the shared-floor test below and re-pinned at its
#: OWN floor immediately after it.
_CONVEX_MEMBERS = {"O_CONVEXC", "O_CONVEXP"}


@pytest.mark.parametrize("kind", [k for k in _SINGLES if k not in _CONVEX_MEMBERS])
def test_the_band_never_reaches_zero_or_the_whole_account(kind):
    """A 0% size is a structure that cannot open (a zero-trade genome dressed as a real one);
    an unbounded one bets the account on a single structure.

    The ceiling was 40% until 2026-08-27. It moved to 50% because that is the smallest number at
    which a FULL-NOTIONAL structure can open on a $100 name: a cash-secured put at spot $100
    reserves strike*100 = $10,000, i.e. exactly 50% of the grid's $20k account. At 40% the
    full-notional kinds (O_CSP/O_JL/O_RS) topped out at spot $60 and were unopenable on most of a
    large-cap universe. It stays BOUNDED -- 50% is half the account on one structure, which is the
    most a cash-secured put can ever need; anything above it is not sizing, it is leverage."""
    spec = collect_param_space(_build(kind))[f"entry:{kind.lower()}-entry:a0:option_sizing"]
    assert spec["min"] >= 1.0, f"{kind}: sizing can go to {spec['min']}%"
    assert spec["max"] <= 50.0, f"{kind}: sizing can reach {spec['max']}% of equity"


@pytest.mark.parametrize("kind", sorted(_CONVEX_MEMBERS))
def test_the_convex_members_band_never_reaches_zero_or_past_the_designs_2_percent_ceiling(kind):
    """The convex-harvest grid's OWN floor/ceiling (design §2: 0.5-2.0% of sleeve) -- narrower
    than every other structure's band on purpose, not a relaxation of the zero-size guard
    above: 0.5% still cannot open a zero-trade genome, and 2.0% is far inside the shared 50%
    leverage ceiling."""
    spec = collect_param_space(_build(kind))[f"entry:{kind.lower()}-entry:a0:option_sizing"]
    assert spec["min"] == 0.5, f"{kind}: expected the design's 0.5% floor, got {spec['min']}%"
    assert spec["max"] == 2.0, f"{kind}: expected the design's 2.0% ceiling, got {spec['max']}%"


def test_the_bands_differ_by_structure_class():
    """One shared window would either starve the credit structures or let the debit ones bet
    the account -- 20% of equity in a defined-risk condor and in a long call are not the same
    risk."""
    bands = {k: tuple(collect_param_space(_build(k))[
        f"entry:{k.lower()}-entry:a0:option_sizing"][f] for f in ("min", "max"))
        for k in _SINGLES}
    assert len(set(bands.values())) > 1, f"every structure searches the same band: {bands}"
    assert bands["O_LC"] != bands["O_IC"]


def test_the_gene_decodes_onto_the_action():
    s = _build("O_LC")
    decoded = decode_params(s, {"entry:o_lc-entry:a0:option_sizing": 7.0})
    assert decoded["entry_rules"][0]["actions"][0]["option_sizing"] == 7.0
    # Source untouched.
    assert s.entry_rules[0]["actions"][0]["option_sizing"] == 5.0


def test_the_decoded_size_reaches_the_seeded_action_config():
    """The gene must survive the rule -> EventAction conversion the engine actually seeds from
    (``rule_builders.action_from_rule``), or it is a knob the simulation never sees. The
    launcher's option strategies seed via ``seed_entry_ruleset_from_rules`` (the unified
    ``entry_rules`` path), so the DECODED action -- not the run-level ``entry_action`` flag --
    is what becomes the open action."""
    from ba2_common.core.rule_builders import action_from_rule

    s = _build("O_IC")
    decoded = decode_params(s, {"entry:o_ic-entry:a0:option_sizing": 12.5})
    action = decoded["entry_rules"][0]["actions"][0]
    cfg = action_from_rule(action)["act"]
    assert cfg["sizing"] == 12.5, (
        f"option_sizing did not reach the action config the evaluator reads: {cfg}"
    )


def test_the_equity_overlays_get_no_sizing_gene():
    """O_CC / O_PP size off the HELD share count (one contract per 100 shares), not
    option_sizing, so a sizing gene there would be inert."""
    for kind, rid in (("O_CC", "cc_sell"), ("O_PP", "pp_buy")):
        space = collect_param_space(mod._STRATEGY_BUILDERS[kind](kind))
        assert f"exit:{rid}:a0:option_sizing" not in space


def test_the_band_table_is_total_over_the_authored_sizes():
    """A structure whose authored size has no band would keep a frozen size while every
    sibling searched one -- the import-time guard must actually fire."""
    authored = {c["option_sizing"] for c in mod._OPTION_STRATS.values()
                if c.get("option_sizing") is not None}
    assert authored <= set(mod._OPTION_SIZING_BANDS)
    with pytest.raises(KeyError):
        mod._apply_option_sizing_gene({"option_sizing": 12.34})
