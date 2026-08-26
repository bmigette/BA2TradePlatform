"""No genome may make an option entry rule's price gates a logically EMPTY conjunction (OPT-C5).

The four price-vs-analyst-target gates are two PAIRS, each testing one field with opposing
operators -- i.e. an interval. Authored as two independent absolute thresholds on a shared
-20..+20 step-5 grid, roughly half of each pair's joint values put the ceiling at or below the
floor. That conjunction is empty for EVERY symbol on EVERY bar, so the trial trades nothing and
scores the identical zero-trade sentinel: selection gets no gradient from a quarter of the
search space, and the waste is centred exactly on the authored all-zero default that warm-start
begins from.

Measured on the built ``O_LC`` rule BEFORE the fix, over all 16 toggle combinations x 9^4
threshold values::

    o_lc-price_low_below price_vs_target_low_percent < default 0.0 range -20.0 20.0 5.0
    o_lc-price_high_above price_vs_target_high_percent > default 0.0 range -20.0 20.0 5.0
    o_lc-price_low_above price_vs_target_low_percent > default 0.0 range -20.0 20.0 5.0
    o_lc-price_high_below price_vs_target_high_percent < default 0.0 range -20.0 20.0 5.0
    sampled sub-space: 104976  guaranteed-empty: 27135  = 25.8%
    authored default (all four ON, all values 0.0): low% < 0.0 AND low% > 0.0 -> EMPTY

This file asserts the property on the DECODED phenotype (what the engine actually receives),
not on the authored template -- a template that looks right but decodes wrong is the defect.
"""
import importlib.util
import itertools
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

#: (field, floor id suffix, ceiling id suffix) for each same-field interval.
_PAIRS = (
    ("price_vs_target_low_percent", "price_low_above", "price_low_below"),
    ("price_vs_target_high_percent", "price_high_above", "price_high_below"),
)


def _leaves(rule):
    return {c["id"]: c for c in rule["conditions"]["conditions"] if isinstance(c, dict)}


def _levels(spec):
    out, x = [], spec["min"]
    while x <= spec["max"] + 1e-9:
        out.append(round(x, 6))
        x += spec["step"]
    return out


def _strategy(rule):
    import types
    return types.SimpleNamespace(entry_rules=[rule], exit_rules=[])


# ---------------------------------------------------------------------------
# 1. The authored default -- where warm-start begins
# ---------------------------------------------------------------------------
def test_the_authored_default_is_a_live_interval_on_every_member():
    """Every gate is ON in the authored template (no ``enabled`` key means present), so the
    template's own thresholds must already describe a non-empty band."""
    for member in sorted(mod._PURE_OPTION_STRATEGIES & set(mod._OPTION_STRATS)):
        rule = mod._option_entry_rule(member)
        leaves = _leaves(rule)
        m = member.lower()
        for field, floor_id, ceil_id in _PAIRS:
            floor = leaves[f"{m}-{floor_id}"]
            ceil = leaves[f"{m}-{ceil_id}"]
            assert floor["field"] == ceil["field"] == field
            assert floor["op"] == ">" and ceil["op"] == "<"
            assert ceil["value"] > floor["value"], (
                f"{member}: the authored default is an EMPTY conjunction on {field} "
                f"({field} > {floor['value']} AND {field} < {ceil['value']})"
            )


# ---------------------------------------------------------------------------
# 2. The whole grid, on the DECODED rule
# ---------------------------------------------------------------------------
def test_no_decoded_genome_can_empty_a_same_field_pair():
    """Exhaustive over both pairs' joint gene grid (and the ON/OFF genes), decoded through
    ``decode_params`` -- the same call the GA makes."""
    rule = mod._option_entry_rule("O_LC")
    strategy = _strategy(rule)
    space = collect_param_space(strategy)

    for field, floor_id, ceil_id in _PAIRS:
        fkey = f"cond:o_lc-{floor_id}:value"
        ckey = f"cond:o_lc-{ceil_id}:value"
        assert fkey in space and ckey in space, (
            f"{field}'s floor/width genes are missing from the emitted parameter space: "
            f"{sorted(space)}"
        )
        checked = 0
        for fv, cv in itertools.product(_levels(space[fkey]), _levels(space[ckey])):
            decoded = decode_params(strategy, {fkey: fv, ckey: cv})
            leaves = _leaves(decoded["entry_rules"][0])
            lo = leaves[f"o_lc-{floor_id}"]["value"]
            hi = leaves[f"o_lc-{ceil_id}"]["value"]
            assert hi > lo, (
                f"genome {fkey}={fv}, {ckey}={cv} decodes to the empty conjunction "
                f"{field} > {lo} AND {field} < {hi}"
            )
            checked += 1
        assert checked >= 81, f"only {checked} combinations sampled for {field}"


# ---------------------------------------------------------------------------
# 3. Expressiveness must not be lost
# ---------------------------------------------------------------------------
def test_each_bound_keeps_its_own_independent_on_off_gene():
    """Floor-only and ceiling-only patterns stay reachable -- the fix must not fuse the pair
    into one inseparable band."""
    rule = mod._option_entry_rule("O_LC")
    space = collect_param_space(_strategy(rule))
    for _field, floor_id, ceil_id in _PAIRS:
        assert f"cond:o_lc-{floor_id}:enabled" in space
        assert f"cond:o_lc-{ceil_id}:enabled" in space


def test_dropping_the_floor_leaf_leaves_a_pure_ceiling_gate():
    """"Still below the low estimate" (ceiling only) must survive, and the ceiling must still
    resolve its offset from the (now dropped) floor's decoded gene rather than crashing or
    silently reverting to a raw width."""
    rule = mod._option_entry_rule("O_LC")
    strategy = _strategy(rule)
    decoded = decode_params(strategy, {
        "cond:o_lc-price_low_above:value": -10.0,
        "cond:o_lc-price_low_above:enabled": 0,
        "cond:o_lc-price_low_below:value": 15.0,
    })
    leaves = _leaves(decoded["entry_rules"][0])
    assert "o_lc-price_low_above" not in leaves          # floor dropped
    assert leaves["o_lc-price_low_below"]["value"] == pytest.approx(5.0)  # -10 + 15


def test_the_cross_field_inside_the_range_pattern_is_untouched():
    """"Inside the analyst range" is a LOW floor + a HIGH ceiling: two different fields, so
    the offset re-parameterisation must not couple them."""
    rule = mod._option_entry_rule("O_LC")
    leaves = _leaves(rule)
    assert leaves["o_lc-price_low_above"].get("value_offset_from") is None
    assert leaves["o_lc-price_high_below"]["value_offset_from"] == "o_lc-price_high_above"


# ---------------------------------------------------------------------------
# 4. OPT-C12 -- the high-target window must contain the universe
# ---------------------------------------------------------------------------
def test_the_high_target_floor_window_brackets_the_universe_median():
    """``price_vs_target_high_percent`` has a ~-31 % median, so the old shared -20..+20 window
    put ~77 % of symbols outside the searchable range and both high gates were inert on them."""
    leaves = _leaves(mod._option_entry_rule("O_LC"))
    hi = leaves["o_lc-price_high_above"]
    assert hi["value_min"] <= -31.0 <= hi["value_max"], (
        f"the high-target floor searches {hi['value_min']}..{hi['value_max']}, which does not "
        f"contain the -31% universe median -- the gate cannot change any evaluation"
    )


def test_the_low_target_floor_window_is_unchanged():
    """The review flagged only the HIGH line; silently moving the LOW one too would make every
    prior low-gate result incomparable for no evidenced reason."""
    lo = _leaves(mod._option_entry_rule("O_LC"))["o_lc-price_low_above"]
    assert (lo["value_min"], lo["value_max"], lo["value_step"]) == (-20.0, 20.0, 5.0)


# ---------------------------------------------------------------------------
# 5. Plumbing: the offset key must survive normalisation, and dangle loudly
# ---------------------------------------------------------------------------
def test_value_offset_from_survives_normalize_trade_rules():
    """``ConditionLeaf.to_canonical_dict`` rebuilds the leaf from DECLARED fields only, so an
    undeclared key is dropped by ``normalize_trade_rules`` and the width gene silently reverts
    to an absolute threshold -- reintroducing every empty conjunction with no visible change."""
    from ba2_common.core.rule_models import normalize_trade_rules

    built = normalize_trade_rules([mod._option_entry_rule("O_LC")])
    leaves = _leaves(built[0])
    assert leaves["o_lc-price_low_below"]["value_offset_from"] == "o_lc-price_low_above"
    assert leaves["o_lc-price_high_below"]["value_offset_from"] == "o_lc-price_high_above"


def test_the_built_strategy_keeps_the_offsets_end_to_end():
    """The real builder path (``_build_strategy_option``), not the raw rule dict."""
    s = mod._build_strategy_option("O_LC")
    leaves = _leaves(s.entry_rules[0])
    assert leaves["o_lc-price_low_below"]["value_offset_from"] == "o_lc-price_low_above"


def test_a_dangling_offset_reference_is_rejected_not_defaulted_to_zero():
    """Unknown is never zero: an unresolvable base would turn the width back into an absolute
    threshold, silently restoring the empty conjunctions."""
    rule = mod._option_entry_rule("O_LC")
    _leaves(rule)["o_lc-price_low_below"]["value_offset_from"] = "no-such-leaf"
    with pytest.raises(ValueError, match="value_offset_from"):
        collect_param_space(_strategy(rule))
