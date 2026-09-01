"""Delta must be a SEARCHED strike-selection method, and the genes must be named for what
they carry (OPT-C3).

``percent_otm`` was the only strike gene in all 16 option grids and it is volatility-BLIND:
5 % OTM on a 15-vol utility and on a 90-vol biotech are not the same proposition. Delta is
normalised across symbols, implemented in ``option_selector._pick_by``, backtest-supported and
the LIVE default -- and was never searched. Worse, the gene the GA did search was NAMED
``option_delta`` while carrying percent-OTM values.

Measured BEFORE this file existed::

    strike methods used by all 15 grid entries: ['percent_otm']
    gene NAMED option_delta: ['entry:o_lc-entry:a0:option_delta'] -> {'type': 'float', 'min': 0.0, 'max': 8.0, 'step': 2.0}
    decodes to option_strike_param = 6.0 with option_strike_method = percent_otm
      i.e. the gene called option_delta carries a PERCENT-OTM value
    genes mentioning a real delta / strike method anywhere: []
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

from ba2_common.core.types import honours_strike_method  # noqa: E402
from app.services.strategy_param_space import (  # noqa: E402
    collect_param_space,
    decode_params,
)

_SINGLES = sorted(mod._OPTION_STRATS)
_DELTA_CAPABLE = sorted(k for k in _SINGLES
                        if honours_strike_method(mod._OPTION_STRATS[k]["action_type"]))
_DELTA_BLIND = sorted(set(_SINGLES) - set(_DELTA_CAPABLE))


def _build(kind):
    if kind in mod._OPTION_GROUPS:
        return mod._build_strategy_option_group(kind)
    return mod._build_strategy_option(kind)


def _entry_action(strategy, idx=0):
    return strategy.entry_rules[idx]["actions"][0]


# ---------------------------------------------------------------------------
# 1. Naming -- the gene must be called what it carries
# ---------------------------------------------------------------------------
def test_the_percent_otm_gene_is_no_longer_called_option_delta():
    space = collect_param_space(_build("O_LC"))
    assert "entry:o_lc-entry:a0:option_strike_param" in space
    assert "entry:o_lc-entry:a0:option_delta" not in space, (
        "the percent-OTM gene is still emitted under the name of a different quantity"
    )
    spec = space["entry:o_lc-entry:a0:option_strike_param"]
    assert (spec["min"], spec["max"]) == (0.0, 8.0)  # a PERCENT range, as before


def test_the_delta_gene_carries_an_actual_delta():
    space = collect_param_space(_build("O_LC"))
    spec = space["entry:o_lc-entry:a0:option_strike_delta"]
    assert 0.0 < spec["min"] < spec["max"] <= 1.0, (
        f"option_strike_delta searches {spec['min']}..{spec['max']}, which is not a delta"
    )


def test_the_legacy_option_delta_key_still_decodes_as_the_percent_param():
    """A persisted best-params blob (or a warm start from an older optimization) carries the
    OLD name for the percent param. Re-reading it as a delta would turn 6 (% OTM) into a
    6-delta target -- the deepest ITM contract on the chain."""
    s = _build("O_LC")
    decoded = decode_params(s, {"entry:o_lc-entry:a0:option_delta": 6.0})
    action = decoded["entry_rules"][0]["actions"][0]
    assert action["option_strike_method"] == "percent_otm"
    assert action["option_strike_param"] == 6.0
    # The source strategy is never mutated.
    assert _entry_action(s)["option_strike_param"] == 2.0


# ---------------------------------------------------------------------------
# 2. The method itself is searched -- but only where the builder honours it
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("kind", _DELTA_CAPABLE)
def test_delta_capable_structures_search_the_strike_method(kind):
    space = collect_param_space(_build(kind))
    key = f"entry:{kind.lower()}-entry:a0:option_strike_method"
    assert key in space, (
        f"{kind} ({mod._OPTION_STRATS[kind]['action_type']}) honours strike_method but the "
        f"method is not in the searched space: {sorted(space)}"
    )
    assert space[key]["type"] == "choice"
    assert "delta" in space[key]["choices"] and "percent_otm" in space[key]["choices"]


@pytest.mark.parametrize("kind", _DELTA_BLIND)
def test_structures_whose_builder_ignores_strike_method_get_no_method_gene(kind):
    """Eight of the nineteen builders hard-code ``method="percent_otm"`` (OPT-S2). A method
    gene there would be search budget spent on a dimension the simulation cannot see -- the
    exact defect this track exists to remove."""
    space = collect_param_space(_build(kind))
    m = kind.lower()
    assert f"entry:{m}-entry:a0:option_strike_method" not in space
    assert f"entry:{m}-entry:a0:option_strike_delta" not in space


def test_both_halves_of_the_split_are_non_empty():
    """Otherwise the two tests above are vacuous."""
    assert _DELTA_CAPABLE and _DELTA_BLIND, (_DELTA_CAPABLE, _DELTA_BLIND)


def test_group_members_get_the_gene_individually():
    space = collect_param_space(_build("OS1"))
    for member in mod._OPTION_GROUPS["OS1"]:
        m = member.lower()
        capable = honours_strike_method(mod._OPTION_STRATS[member]["action_type"])
        assert (f"entry:{m}-entry:a0:option_strike_method" in space) is capable


def test_the_equity_overlays_search_it_too():
    """O_CC's covered call and O_PP's protective put both honour strike_method and were the
    grid's least-searched option legs."""
    for kind, rid in (("O_CC", "cc_sell"), ("O_PP", "pp_buy")):
        s = _build(kind) if kind in mod._OPTION_STRATS else mod._STRATEGY_BUILDERS[kind](kind)
        space = collect_param_space(s)
        assert f"exit:{rid}:a0:option_strike_method" in space, sorted(space)
        assert f"exit:{rid}:a0:option_strike_delta" in space


# ---------------------------------------------------------------------------
# 3. Decode: the param that lands on the action must match the chosen method
# ---------------------------------------------------------------------------
def test_choosing_delta_writes_the_delta_not_the_percent():
    s = _build("O_LC")
    decoded = decode_params(s, {
        "entry:o_lc-entry:a0:option_strike_method": "delta",
        "entry:o_lc-entry:a0:option_strike_param": 8.0,
        "entry:o_lc-entry:a0:option_strike_delta": 0.25,
    })
    action = decoded["entry_rules"][0]["actions"][0]
    assert action["option_strike_method"] == "delta"
    assert action["option_strike_param"] == 0.25, (
        "the percent value reached the selector under a delta method: |delta| nearest 8.0 is "
        "the deepest ITM contract on the chain"
    )


def test_choosing_percent_otm_writes_the_percent_not_the_delta():
    s = _build("O_LC")
    decoded = decode_params(s, {
        "entry:o_lc-entry:a0:option_strike_method": "percent_otm",
        "entry:o_lc-entry:a0:option_strike_param": 8.0,
        "entry:o_lc-entry:a0:option_strike_delta": 0.25,
    })
    action = decoded["entry_rules"][0]["actions"][0]
    assert action["option_strike_method"] == "percent_otm"
    assert action["option_strike_param"] == 8.0, (
        "a 0.25 delta reached the selector as 0.25 % OTM -- effectively at the money"
    )


def test_an_authored_delta_method_without_a_delta_gene_still_uses_a_delta():
    """The method gene may pick delta while the delta itself is not being searched."""
    s = _build("O_LC")
    decoded = decode_params(s, {
        "entry:o_lc-entry:a0:option_strike_method": "delta",
        "entry:o_lc-entry:a0:option_strike_param": 8.0,
    })
    action = decoded["entry_rules"][0]["actions"][0]
    assert action["option_strike_param"] == mod._OPTION_DELTA_RANGE["option_strike_delta"]


def test_a_delta_choice_with_no_delta_anywhere_is_rejected_not_guessed():
    """Unknown is never a substitute: without a delta-scaled parameter the percent range would
    silently become the delta target."""
    import types as _t

    action = {"action_type": "buy_call", "option_strike_method": "percent_otm",
              "option_strike_param": 2.0,
              "option_strike_param_optimize": True, "option_strike_param_min": 0.0,
              "option_strike_param_max": 8.0, "option_strike_param_step": 2.0,
              "option_strike_method_optimize": True,
              "option_strike_method_choices": ["percent_otm", "delta"]}
    strategy = _t.SimpleNamespace(
        entry_rules=[{"id": "r", "actions": [action], "conditions": {}}], exit_rules=[])
    with pytest.raises(ValueError, match="delta"):
        collect_param_space(strategy)


# ---------------------------------------------------------------------------
# 4. The un-decoded template must be unchanged (plain backtests, hand-seeded runs)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("kind", _SINGLES)
def test_the_authored_method_and_param_are_untouched(kind):
    action = _entry_action(_build(kind))
    assert action["option_strike_method"] == "percent_otm"
    assert action["option_strike_param"] == mod._OPTION_STRATS[kind]["option_strike_param"]
