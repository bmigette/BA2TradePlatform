"""``value_offset_from``: a leaf's ``value`` gene is a WIDTH above another leaf's threshold.

PROVENANCE. These tests were extracted from
``test_launcher_option_price_gates_never_empty.py`` when the launcher's four
``price_vs_target_*`` option-entry gates were removed on 2026-08-27 (they were FMPRating-only
data and failed CLOSED under any other expert -- see the tombstone comment in
``ba2test_launcher.py``). Those gates were the mechanism's ONLY producer anywhere in the repo,
so deleting that file wholesale would have left ``strategy_param_space._validate_value_offsets``
/ ``_apply_to_tree``'s offset branch and ``rule_models.ConditionLeaf.value_offset_from`` --
all live code -- with zero coverage.

The properties below are therefore re-stated against a HAND-AUTHORED condition tree rather than
a built option strategy, so they no longer depend on any particular grid using the mechanism.

WHY THE MECHANISM EXISTS (kept, because it is the reason the dangling-reference case must
RAISE). Two leaves testing the same field with opposing operators (``x > a`` AND ``x < b``) are
an interval. As two independent absolute genes on one shared grid, roughly half their joint
values put the ceiling at or below the floor -- an empty conjunction that trades nothing for
every symbol on every bar and scores the identical zero-trade sentinel, so selection gets no
gradient from it. Re-parameterising the ceiling as (floor + width >= step) makes every point of
the grid a live interval. Silently treating an unresolvable base as 0 would turn the width back
into an absolute threshold and quietly restore those empty conjunctions.
"""
import copy
import itertools
import os
import sys

import pytest

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # testplatform/backend
if _root not in sys.path:
    sys.path.insert(0, _root)

from app.services.strategy_param_space import (  # noqa: E402
    collect_param_space,
    decode_params,
)

_FLOOR = "band_floor"
_CEIL = "band_ceiling"
_FIELD = "rsi"


def _rule():
    """One rule, one same-field interval: ``rsi > floor AND rsi < floor + width``."""
    return {
        "id": "r1",
        "name": "r1",
        "conditions": {"id": "r1-root", "type": "AND", "conditions": [
            {"id": _FLOOR, "field": _FIELD, "op": ">", "value": -20.0,
             "optimize": True, "toggle_optimize": True,
             "value_min": -20.0, "value_max": 20.0, "value_step": 5.0},
            # The authored ``value`` stays ABSOLUTE (floor + widest width) so an un-decoded
            # template still seeds a valid ruleset; value_min/max/step describe the WIDTH.
            {"id": _CEIL, "field": _FIELD, "op": "<", "value": 25.0,
             "optimize": True, "toggle_optimize": True,
             "value_offset_from": _FLOOR,
             "value_min": 5.0, "value_max": 45.0, "value_step": 5.0},
        ]},
        "actions": [{"action_type": "buy"}],
        "continue_processing": False,
    }


def _strategy(rule=None):
    import types
    return types.SimpleNamespace(entry_rules=[rule or _rule()], exit_rules=[])


def _leaves(rule):
    return {c["id"]: c for c in rule["conditions"]["conditions"] if isinstance(c, dict)}


def _levels(spec):
    out, x = [], spec["min"]
    while x <= spec["max"] + 1e-9:
        out.append(round(x, 6))
        x += spec["step"]
    return out


# ---------------------------------------------------------------------------
# 1. The authored template is itself a live interval
# ---------------------------------------------------------------------------
def test_the_authored_default_is_a_live_interval():
    """No ``enabled`` key means the leaf is ON, so the template's own absolute thresholds must
    already describe a non-empty band -- that is where warm-start begins."""
    leaves = _leaves(_rule())
    assert leaves[_CEIL]["value"] > leaves[_FLOOR]["value"]


def test_the_width_range_is_strictly_positive():
    """``value_min == value_step > 0`` is what makes the interval non-empty at EVERY point of
    the grid; a width range straddling zero is exactly the defect this replaced."""
    ceil = _leaves(_rule())[_CEIL]
    assert 0 < ceil["value_min"] <= ceil["value_max"]


# ---------------------------------------------------------------------------
# 2. The whole grid, on the DECODED tree
# ---------------------------------------------------------------------------
def test_no_decoded_genome_can_empty_the_pair():
    """Exhaustive over the pair's joint gene grid, decoded through ``decode_params`` -- the
    same call the GA makes. Asserted on the DECODED phenotype, not the authored template: a
    template that looks right but decodes wrong is the defect."""
    strategy = _strategy()
    space = collect_param_space(strategy)
    fkey, ckey = f"cond:{_FLOOR}:value", f"cond:{_CEIL}:value"
    assert fkey in space and ckey in space, sorted(space)

    checked = 0
    for fv, cv in itertools.product(_levels(space[fkey]), _levels(space[ckey])):
        decoded = decode_params(strategy, {fkey: fv, ckey: cv})
        leaves = _leaves(decoded["entry_rules"][0])
        lo, hi = leaves[_FLOOR]["value"], leaves[_CEIL]["value"]
        assert hi > lo, (
            f"genome {fkey}={fv}, {ckey}={cv} decodes to the empty conjunction "
            f"{_FIELD} > {lo} AND {_FIELD} < {hi}")
        checked += 1
    assert checked >= 81, f"only {checked} combinations sampled"


def test_the_ceiling_decodes_as_base_plus_width():
    strategy = _strategy()
    decoded = decode_params(strategy, {
        f"cond:{_FLOOR}:value": -10.0,
        f"cond:{_CEIL}:value": 15.0,
    })
    leaves = _leaves(decoded["entry_rules"][0])
    assert leaves[_FLOOR]["value"] == pytest.approx(-10.0)
    assert leaves[_CEIL]["value"] == pytest.approx(5.0)  # -10 + 15


# ---------------------------------------------------------------------------
# 3. Expressiveness must not be lost
# ---------------------------------------------------------------------------
def test_each_bound_keeps_its_own_independent_on_off_gene():
    """Floor-only and ceiling-only patterns stay reachable -- the offset must not fuse the pair
    into one inseparable band."""
    space = collect_param_space(_strategy())
    assert f"cond:{_FLOOR}:enabled" in space
    assert f"cond:{_CEIL}:enabled" in space


def test_dropping_the_floor_leaf_leaves_a_pure_ceiling_gate():
    """A ceiling-only pattern must survive, and the ceiling must still resolve its offset from
    the (now dropped) floor's decoded gene rather than crashing or reverting to a raw width."""
    strategy = _strategy()
    decoded = decode_params(strategy, {
        f"cond:{_FLOOR}:value": -10.0,
        f"cond:{_FLOOR}:enabled": 0,
        f"cond:{_CEIL}:value": 15.0,
    })
    leaves = _leaves(decoded["entry_rules"][0])
    assert _FLOOR not in leaves                                       # floor dropped
    assert leaves[_CEIL]["value"] == pytest.approx(5.0)               # still -10 + 15


def test_a_leaf_with_no_offset_is_untouched():
    """Only the leaf that DECLARES the offset is relative; a plain leaf stays absolute, which
    is what keeps cross-field patterns from being coupled."""
    assert _leaves(_rule())[_FLOOR].get("value_offset_from") is None


# ---------------------------------------------------------------------------
# 4. Plumbing: the key must survive normalisation, and dangle loudly
# ---------------------------------------------------------------------------
def test_value_offset_from_survives_normalize_trade_rules():
    """``ConditionLeaf.to_canonical_dict`` rebuilds the leaf from DECLARED fields only, so an
    undeclared key is dropped by ``normalize_trade_rules`` and the width gene silently reverts
    to an absolute threshold -- reintroducing every empty conjunction with no visible change."""
    from ba2_common.core.rule_models import normalize_trade_rules

    built = normalize_trade_rules([_rule()])
    assert _leaves(built[0])[_CEIL]["value_offset_from"] == _FLOOR


def test_a_dangling_offset_reference_is_rejected_not_defaulted_to_zero():
    """Unknown is never zero: an unresolvable base would turn the width back into an absolute
    threshold, silently restoring the empty conjunctions. It must fail ONCE at gene collection,
    not as N identical crashed trials."""
    rule = _rule()
    _leaves(rule)[_CEIL]["value_offset_from"] = "no-such-leaf"
    with pytest.raises(ValueError, match="value_offset_from"):
        collect_param_space(_strategy(rule))


def test_an_offset_onto_a_leaf_carrying_no_threshold_is_rejected():
    """A base id that EXISTS but has no ``value`` resolves to nothing, which is the same
    unmeasurable case as a missing id and must not silently become zero."""
    rule = _rule()
    leaves = _leaves(rule)
    flagless = {"id": "some-flag", "field": "bullish", "field_type": "flag"}
    rule["conditions"]["conditions"].append(flagless)
    leaves[_CEIL]["value_offset_from"] = "some-flag"
    with pytest.raises(ValueError, match="value_offset_from"):
        collect_param_space(_strategy(rule))


def test_collecting_the_space_does_not_mutate_the_authored_rule():
    rule = _rule()
    before = copy.deepcopy(rule)
    collect_param_space(_strategy(rule))
    assert rule == before
