"""``relative_volume`` and ``iv_to_realized_vol`` must reach the GA's searched space.

A condition that exists and evaluates but is not in the emitted parameter space is exactly the
defect this track is fixing, so these assert on ``collect_param_space`` and on the seeded
trigger, not merely on the built rule.

Semantics (current-bar exclusion, no lookahead, unknown-is-not-1.0) are covered in
``packages/common/tests/test_volume_and_vol_ratio_conditions.py``.
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
_GROUPS = sorted(mod._OPTION_GROUPS)
_GATES = (("rel_volume", "relative_volume"), ("iv_rv", "iv_to_realized_vol"))


def _build(kind):
    if kind in mod._OPTION_GROUPS:
        return mod._build_strategy_option_group(kind)
    return mod._build_strategy_option(kind)


def _leaf(rule, cid):
    for c in rule["conditions"]["conditions"]:
        if c.get("id") == cid:
            return c
    raise AssertionError(f"{cid} not in {[c.get('id') for c in rule['conditions']['conditions']]}")


# ---------------------------------------------------------------------------
# 1. They reach the searched space
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("kind", _SINGLES)
@pytest.mark.parametrize("suffix,field", _GATES)
def test_every_structure_searches_the_gate(kind, suffix, field):
    space = collect_param_space(_build(kind))
    m = kind.lower()
    assert f"cond:{m}-{suffix}:value" in space, (
        f"{kind}: {field} is not in the emitted parameter space: {sorted(space)}")
    assert f"cond:{m}-{suffix}:enabled" in space, (
        f"{kind}: {field} has no ON/OFF gene, and it fails CLOSED where the data is missing")


@pytest.mark.parametrize("kind", _GROUPS)
@pytest.mark.parametrize("suffix,_field", _GATES)
def test_every_group_member_searches_it_independently(kind, suffix, _field):
    space = collect_param_space(_build(kind))
    for member in mod._OPTION_GROUPS[kind]:
        assert f"cond:{member.lower()}-{suffix}:value" in space


@pytest.mark.parametrize("kind", _GROUPS + _SINGLES)
@pytest.mark.parametrize("_suffix,field", _GATES)
def test_the_leaf_becomes_a_trigger(kind, _suffix, field):
    """A field absent from ``FIELD_EVENT`` is silently dropped by
    ``triggers_from_condition_tree`` and the engine never evaluates the gate."""
    from ba2_common.core.rule_builders import triggers_from_condition_tree

    for rule in _build(kind).entry_rules:
        triggers = triggers_from_condition_tree(rule["conditions"])
        assert any(t["event_type"] == field for t in triggers.values()), (
            f"{kind}/{rule['id']}: the {field} gate produced no trigger")


@pytest.mark.parametrize("suffix,_field", _GATES)
def test_the_genes_decode_onto_the_leaf(suffix, _field):
    strategy = _build("O_LC")
    decoded = decode_params(strategy, {f"cond:o_lc-{suffix}:value": 1.25})
    assert _leaf(decoded["entry_rules"][0], f"o_lc-{suffix}")["value"] == 1.25
    off = decode_params(strategy, {f"cond:o_lc-{suffix}:enabled": 0})
    ids = [c.get("id") for c in off["entry_rules"][0]["conditions"]["conditions"]]
    assert f"o_lc-{suffix}" not in ids


# ---------------------------------------------------------------------------
# 2. IV/RV must be able to say opposite things per half
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("kind", _SINGLES)
def test_the_iv_rv_operator_matches_the_premium_direction(kind):
    leaf = _leaf(mod._option_entry_rule(kind), f"{kind.lower()}-iv_rv")
    debit = kind in mod._DEBIT_OPTION_MEMBERS
    assert leaf["op"] == ("<" if debit else ">"), (
        f"{kind} is a {'buyer' if debit else 'seller'} of premium but its IV/RV gate reads "
        f"{leaf['field']} {leaf['op']} {leaf['value']}")


def test_the_two_halves_use_opposite_iv_rv_directions():
    """Derived from the structure's own action_type, not from the classification set, so a
    mis-assignment cannot be self-consistent."""
    long_premium = {"buy_call", "buy_put", "open_bear_put_spread", "open_bull_call_spread",
                    "open_call_butterfly", "open_straddle", "open_strangle"}
    ops = {}
    for kind in _SINGLES:
        op = _leaf(mod._option_entry_rule(kind), f"{kind.lower()}-iv_rv")["op"]
        ops[kind] = op
        at = mod._OPTION_STRATS[kind]["action_type"]
        assert op == ("<" if at in long_premium else ">"), f"{kind} ({at}) -> {op}"
    assert set(ops.values()) == {"<", ">"}


def test_the_iv_rv_window_brackets_parity():
    """Both halves must be able to express "no edge here" (a ratio of 1.0), or the gate is a
    one-sided constant dressed as a search."""
    for kind in _SINGLES:
        spec = collect_param_space(_build(kind))[f"cond:{kind.lower()}-iv_rv:value"]
        assert spec["min"] < 1.0 < spec["max"], (
            f"{kind}: IV/RV searched over {spec['min']}..{spec['max']}, which cannot express "
            f"parity between implied and realised")


# ---------------------------------------------------------------------------
# 3. Relative volume: one direction, sane window
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("kind", _SINGLES)
def test_relative_volume_asks_for_participation(kind):
    """Elevated volume confirms an entry whichever way the premium flows, so unlike iv_rank
    and IV/RV this gate is one-directional on purpose."""
    leaf = _leaf(mod._option_entry_rule(kind), f"{kind.lower()}-rel_volume")
    assert leaf["field"] == "relative_volume"
    assert leaf["op"] == ">"


def test_the_relative_volume_window_spans_quiet_to_unusual():
    spec = collect_param_space(_build("O_LC"))["cond:o_lc-rel_volume:value"]
    assert spec["min"] < 1.0 < spec["max"], (
        f"relative volume searched over {spec['min']}..{spec['max']}: it cannot distinguish "
        f"quiet from busy")
    assert spec["max"] >= 2.0, "cannot demand genuinely unusual activity"
    assert spec["min"] > 0.0, "a 0x threshold is a gate that always passes"


# ---------------------------------------------------------------------------
# 4. No gene may belong to a leaf the engine drops (the closure, on the real strategies)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("kind", _GROUPS + _SINGLES)
def test_no_entry_leaf_is_dropped_at_seeding(kind):
    from ba2_common.core.rule_builders import tree_leaves, triggers_from_condition_tree

    for rule in _build(kind).entry_rules:
        leaves = [leaf["field"] for leaf in tree_leaves(rule["conditions"])]
        triggers = [t["event_type"] for t in
                    triggers_from_condition_tree(rule["conditions"]).values()]
        assert len(triggers) == len(leaves), (
            f"{kind}/{rule['id']}: leaves={leaves} triggers={triggers}")
