"""``iv_rank`` must be a SEARCHED entry gene, and the two halves must ask opposite things.

`iv_rank` is the one genuinely bounded (0-100), symbol-comparable option quantity the platform
owns. It is fully implemented and registered in every condition registry -- and no grid built a
leaf for it. Measured across all 19 built option strategies BEFORE this file existed::

    19 option strategies: 256 condition leaves, 499 genes
    iv_rank LEAVES in any built option strategy: 0
    iv_rank GENES in any emitted parameter space: 0

    O_LC param space keys:
        cond:dte:value
        cond:o_lc-gate_confidence:enabled
        cond:o_lc-gate_confidence:value
        cond:o_lc-price_high_above:enabled
        cond:o_lc-price_high_above:value
        cond:o_lc-price_high_below:enabled
        cond:o_lc-price_high_below:value
        cond:o_lc-price_low_above:enabled
        cond:o_lc-price_low_above:value
        cond:o_lc-price_low_below:enabled
        cond:o_lc-price_low_below:value
        cond:o_lc-signal:enabled
        cond:td:value
        cond:tp:value
        entry:o_lc-entry:a0:option_delta
        entry:o_lc-entry:a0:option_dte
        exit:opt_dte:enabled
        exit:opt_time:enabled
        exit:opt_tp:enabled

A condition that EVALUATES but is not in the searched space is exactly the defect, so these
tests assert on the emitted PARAMETER SPACE, not merely on the built rule.
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
# 1. It reaches the searched space -- on EVERY member, single and grouped
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("kind", _SINGLES)
def test_every_single_option_strategy_searches_an_iv_rank_threshold(kind):
    space = collect_param_space(_build(kind))
    m = kind.lower()
    assert f"cond:{m}-iv_rank:value" in space, (
        f"{kind}: iv_rank is not in the emitted parameter space, so the GA never searches it: "
        f"{sorted(space)}"
    )
    assert f"cond:{m}-iv_rank:enabled" in space, (
        f"{kind}: the iv_rank gate has no ON/OFF gene, so the search cannot switch it off "
        f"where no IV history exists -- and it fails CLOSED, i.e. it would trade nothing"
    )


@pytest.mark.parametrize("kind", _GROUPS)
def test_every_group_member_gets_its_own_iv_rank_gene(kind):
    space = collect_param_space(_build(kind))
    for member in mod._OPTION_GROUPS[kind]:
        assert f"cond:{member.lower()}-iv_rank:value" in space, (
            f"{kind}: member {member} has no iv_rank gene, so the group's structures cannot "
            f"disagree about volatility"
        )


def test_the_searched_range_stays_inside_the_fields_own_0_100_ceiling():
    """OPT-C10: the only range generator that ever touched iv_rank (the generic +/-50%-of-value
    fallback) put about a fifth of its levels ABOVE 100 -- outside the percentile's domain,
    where no value can ever satisfy a '>' gate."""
    for kind in _SINGLES:
        space = collect_param_space(_build(kind))
        spec = space[f"cond:{kind.lower()}-iv_rank:value"]
        assert 0.0 <= spec["min"] <= spec["max"] <= 100.0, (
            f"{kind}: iv_rank searched over {spec['min']}..{spec['max']}, outside 0-100"
        )


# ---------------------------------------------------------------------------
# 2. The two halves must ask for OPPOSITE things (OPT-C4)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("kind", _SINGLES)
def test_the_operator_matches_the_structures_premium_direction(kind):
    """A premium SELLER wants IV rank high, a BUYER wants it low. The GA never searches an
    operator, so one shared direction would gate one of the two halves on the opposite of its
    thesis."""
    leaf = _leaf(mod._option_entry_rule(kind), f"{kind.lower()}-iv_rank")
    debit = kind in mod._DEBIT_OPTION_MEMBERS
    assert leaf["op"] == ("<" if debit else ">"), (
        f"{kind} is a {'DEBIT (long premium)' if debit else 'CREDIT (short premium)'} "
        f"structure but its iv_rank gate reads {leaf['field']} {leaf['op']} {leaf['value']}"
    )


def test_the_two_halves_are_both_non_empty_and_disjoint_in_direction():
    """Guards the mutation that classifies EVERY member into one half: the test above would
    still pass if _DEBIT_OPTION_MEMBERS were the whole set (or empty), because it reads the
    same set it is checking against."""
    ops = {k: _leaf(mod._option_entry_rule(k), f"{k.lower()}-iv_rank")["op"] for k in _SINGLES}
    assert set(ops.values()) == {"<", ">"}, (
        f"both halves share one direction: {sorted(set(ops.values()))}"
    )
    # Derived from the structure's own action_type, NOT from _DEBIT_OPTION_MEMBERS -- reading
    # the classification back would make any mis-assignment self-consistent and invisible.
    long_premium = {"buy_call", "buy_put", "open_bear_put_spread", "open_bull_call_spread",
                    "open_call_butterfly", "open_straddle", "open_strangle"}
    for kind, op in ops.items():
        at = mod._OPTION_STRATS[kind]["action_type"]
        assert op == ("<" if at in long_premium else ">"), (
            f"{kind} ({at}) is {'long' if at in long_premium else 'short'} premium but its "
            f"iv_rank gate is {op}"
        )


def test_the_credit_half_can_demand_a_genuinely_high_rank():
    """A ceiling-only window (e.g. 10..60) would leave the seller unable to express its thesis
    even with the right operator."""
    credit = sorted(mod._CREDIT_OPTION_MEMBERS)
    assert credit
    for kind in credit:
        spec = collect_param_space(_build(kind))[f"cond:{kind.lower()}-iv_rank:value"]
        assert spec["max"] >= 60.0, f"{kind}: seller can only demand iv_rank > {spec['max']}"


def test_the_debit_half_can_demand_a_genuinely_low_rank():
    for kind in sorted(mod._DEBIT_OPTION_MEMBERS):
        spec = collect_param_space(_build(kind))[f"cond:{kind.lower()}-iv_rank:value"]
        assert spec["min"] <= 20.0, f"{kind}: buyer can only require iv_rank < {spec['min']}"


# ---------------------------------------------------------------------------
# 3. It reaches the ENGINE, and decodes
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("kind", _GROUPS + _SINGLES)
def test_the_iv_rank_leaf_becomes_a_trigger(kind):
    """A leaf whose field is absent from ``FIELD_EVENT`` is silently dropped by
    ``triggers_from_condition_tree`` and the engine never evaluates the gate."""
    from ba2_common.core.rule_builders import triggers_from_condition_tree

    strategy = _build(kind)
    for rule in strategy.entry_rules:
        triggers = triggers_from_condition_tree(rule["conditions"])
        assert any(t["event_type"] == "iv_rank" for t in triggers.values()), (
            f"{kind}/{rule['id']}: the iv_rank gate produced no trigger"
        )


def test_the_iv_rank_gene_decodes_onto_the_leaf():
    strategy = _build("O_LC")
    decoded = decode_params(strategy, {"cond:o_lc-iv_rank:value": 45.0})
    assert _leaf(decoded["entry_rules"][0], "o_lc-iv_rank")["value"] == 45.0


def test_switching_the_gate_off_removes_the_leaf_entirely():
    """The escape hatch that makes the fail-closed behaviour survivable on a cache with no IV."""
    strategy = _build("O_LC")
    decoded = decode_params(strategy, {"cond:o_lc-iv_rank:enabled": 0})
    ids = [c.get("id") for c in decoded["entry_rules"][0]["conditions"]["conditions"]]
    assert "o_lc-iv_rank" not in ids


# ---------------------------------------------------------------------------
# 4. Unknown is never a pass
# ---------------------------------------------------------------------------
def test_an_unavailable_rank_refuses_the_entry_for_every_operator():
    """``get_iv_rank`` returns None (never 0.0) below its minimum sample count. Both halves'
    operators must read that as REFUSE -- a '<' gate treating a missing rank as 0 would admit
    every trade in exactly the half that wants cheap vol."""
    from ba2_common.core.TradeConditions import IVRankCondition

    class _Acct:
        def get_iv_rank(self, *a, **kw):
            return None

    # An options-capable account is required by the condition's isinstance check.
    from ba2_common.core.interfaces.OptionsAccountInterface import OptionsAccountInterface
    assert hasattr(OptionsAccountInterface, "get_iv_rank")

    for op in ("<", ">", "<=", ">=", "==", "!="):
        cond = IVRankCondition(_Acct(), "AAPL", None, op, 30.0)
        assert cond.evaluate() is False, f"iv_rank {op} 30 admitted an UNKNOWN rank"
        assert cond.get_calculated_value() is None
