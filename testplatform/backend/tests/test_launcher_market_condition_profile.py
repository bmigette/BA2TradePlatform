"""``--market-condition-profile``: the opt-in market-condition entry gates (plan Task 8).

What this pins, in the order the failures would bite:

* profile ``none`` emits the PRE-CHANGE rule BYTE FOR BYTE. The literal below was produced from
  ``git show HEAD:testplatform/ba2test_launcher.py`` (commit 4fd71c55, the docs commit this task
  branched from) loaded as a throwaway module and dumped with ``json.dumps(..., indent=4)``. It is
  the whole contract of an opt-in feature: the goal2020 archive, its 135 optimizations, their
  labels and the 26 forward-test deployments must stay comparable with runs launched after this.
* with a profile on, every permitted structure collects exactly the genes the contract names, on
  the INITIAL-ENTRY tree only, with ids that do not collide between structures.
* every new leaf REACHES THE ENGINE (``triggers_from_condition_tree``), because a leaf the engine
  drops is a gate the GA keeps scoring and the run never applies -- the whitelist trap.
* the all-off control decodes back to profile ``none``'s tree, so Task 9's compatibility gate has
  something to stand on.
* the launch-time refusals: an unknown profile, a profile without a manifest, and a manifest that
  does not cover the run's universe.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from datetime import date

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # testplatform/backend
_LAUNCHER = os.path.normpath(os.path.join(_ROOT, "..", "ba2test_launcher.py"))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from ba2_common.core.market_conditions import (  # noqa: E402
    PROFILES,
    FieldSpec,
    ProfileSpec,
    registered_profile,
)
from ba2_common.core.rule_models import MODE_OFF, NUMERIC_MODE_CHOICES  # noqa: E402

from app.services.strategy_param_space import collect_param_space, decode_params  # noqa: E402

_spec = importlib.util.spec_from_file_location("ba2test_launcher_mc", _LAUNCHER)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)

#: The 16 permitted discovery structures (tools/run_options_matrix.py::_DISCOVERY_STRATEGIES).
PERMITTED = ["O_LC", "O_LP", "O_VERT", "O_BULLCS", "O_BULLPS", "O_BEARCS", "O_BF",
             "O_IC", "O_JL", "O_RS", "O_CSP", "O_STRD", "O_STRG",
             "O_CC", "O_PP", "O_WHEEL"]

#: ``_option_entry_rule("O_LC")`` as the code before this change produced it. See the module
#: docstring for how it was obtained; it is a PIN, never regenerate it to make a test pass.
#:
#: AMENDED ONCE, 2026-09-19, for the ``-signal`` leaf ONLY: the direction gate became a
#: ``rec_direction`` numeric leaf with a MODE gene so the GA can also pick the CONTRARIAN
#: direction (the flag leaf could only be switched off). That is a deliberate change to the
#: authored rule, not profile drift, and it is behaviour-preserving at the authored default
#: (``> 0`` on a HOLD-centred scale IS the old ``bullish`` flag -- see
#: test_launcher_option_entry_rule.py). Every other leaf, and the leaf ORDER, is untouched,
#: which is what this pin exists to protect: profile ``none`` must still append NOTHING.
#:
#: AMENDED 2026-09-25 (plan 2026-09-24 Task 9), the entry action's ``option_entry_cross`` band
#: ONLY: 0.0..1.0 step 0.25 authored at 0.0 -> 0.75..1.0 step 0.05 authored at 0.75 (passive
#: limits went unfilled under next-open fills). A deliberate gene-range change, not profile drift.
#:
#: AMENDED 2026-09-25 (plan 2026-09-24 Task 12), two gate RANGES only, values untouched:
#: ``shared-rel_volume`` value_max 3.0 -> 1.5, and ``o_lc-iv_rv`` (a DEBIT member) value_min
#: 0.8 -> 1.0. Deliberate gene-range changes, not profile drift.
PROFILE_NONE_O_LC = json.loads(r"""
{
    "id": "o_lc-entry",
    "name": "O_LC-entry",
    "conditions": {
        "id": "o_lc-root",
        "type": "AND",
        "conditions": [
            {
                "id": "o_lc-signal",
                "field": "rec_direction",
                "field_type": "numeric",
                "op": ">",
                "value": 0.0,
                "optimize": false,
                "value_min": 0.0,
                "value_max": 0.0,
                "value_step": 1.0,
                "mode_optimize": true,
                "mode_choices": ["off", "below", "above"]
            },
            {
                "id": "o_lc-flat",
                "field": "has_no_position",
                "field_type": "flag"
            },
            {
                "id": "shared-gate_confidence",
                "field": "confidence",
                "op": ">",
                "value": 50,
                "optimize": true,
                "value_min": 40,
                "value_max": 75,
                "value_step": 5,
                "toggle_optimize": true
            },
            {
                "id": "o_lc-iv_rank",
                "field": "iv_rank",
                "op": "<",
                "value": 30.0,
                "optimize": true,
                "value_min": 10.0,
                "value_max": 60.0,
                "value_step": 5.0,
                "toggle_optimize": true
            },
            {
                "id": "shared-rel_volume",
                "field": "relative_volume",
                "op": ">",
                "optimize": true,
                "toggle_optimize": true,
                "value": 0.5,
                "value_min": 0.5,
                "value_max": 1.5,
                "value_step": 0.25
            },
            {
                "id": "o_lc-iv_rv",
                "field": "iv_to_realized_vol",
                "op": "<",
                "value": 1.6,
                "optimize": true,
                "toggle_optimize": true,
                "value_min": 1.0,
                "value_max": 1.6,
                "value_step": 0.1
            },
            {
                "id": "o_lc-exp_profit",
                "field": "expected_profit_target_percent",
                "op": ">",
                "optimize": true,
                "toggle_optimize": true,
                "value": 5.0,
                "value_min": 2.0,
                "value_max": 20.0,
                "value_step": 2.0
            }
        ]
    },
    "actions": [
        {
            "action_type": "buy_call",
            "option_strike_method": "percent_otm",
            "option_strike_param": 2.0,
            "option_dte_min": 25,
            "option_dte_max": 45,
            "option_sizing": 5.0,
            "option_strike_param_optimize": true,
            "option_strike_param_min": 0.0,
            "option_strike_param_max": 8.0,
            "option_strike_param_step": 2.0,
            "option_dte_optimize": true,
            "option_dte_min_range": 20,
            "option_dte_max_range": 60,
            "option_dte_step": 5,
            "option_min_volume": 25,
            "option_strike_method_optimize": true,
            "option_strike_method_choices": [
                "percent_otm",
                "delta"
            ],
            "option_strike_delta": 0.3,
            "option_strike_delta_optimize": true,
            "option_strike_delta_min": 0.05,
            "option_strike_delta_max": 0.5,
            "option_strike_delta_step": 0.05,
            "option_sizing_optimize": true,
            "option_sizing_min": 1.0,
            "option_sizing_max": 10.0,
            "option_sizing_step": 1.0,
            "option_entry_cross": 0.75,
            "option_entry_cross_optimize": true,
            "option_entry_cross_min": 0.75,
            "option_entry_cross_max": 1.0,
            "option_entry_cross_step": 0.05,
            "option_selection_half": "debit",
            "option_w_premium_optimize": true,
            "option_w_premium_min": -2.0,
            "option_w_premium_max": 2.0,
            "option_w_premium_step": 0.5,
            "option_w_iv_optimize": true,
            "option_w_iv_min": -2.0,
            "option_w_iv_max": 2.0,
            "option_w_iv_step": 0.5,
            "option_w_rvol_optimize": true,
            "option_w_rvol_min": 0.0,
            "option_w_rvol_max": 2.0,
            "option_w_rvol_step": 0.5
        }
    ],
    "continue_processing": false
}
""")

#: ``_build_strategy("O_CC", ...).entry_rules`` as the code before this change produced it --
#: the OVERLAY half of the byte-identity contract (the gates go on the S2 STOCK entry there,
#: spliced into the returned strategy, so `none` has to leave that splice invisible). Same
#: provenance as PROFILE_NONE_O_LC; a PIN, never regenerated to make a test pass.
PROFILE_NONE_O_CC_ENTRY = json.loads(r"""
[
    {
        "id": "buy",
        "name": "enter-buy",
        "conditions": {
            "id": "root",
            "operator": "AND",
            "type": "AND",
            "conditions": [
                {
                    "id": "buy-bullish",
                    "field": "bullish",
                    "fieldType": "flag",
                    "field_type": "flag",
                    "comparison": "is_true",
                    "op": "is_true",
                    "optimizeEnabled": false,
                    "optimize": false
                },
                {
                    "id": "buy-flat",
                    "field": "has_no_position",
                    "fieldType": "flag",
                    "field_type": "flag",
                    "comparison": "is_true",
                    "op": "is_true",
                    "optimizeEnabled": false,
                    "optimize": false
                },
                {
                    "id": "gate_confidence",
                    "field": "confidence",
                    "fieldType": "numeric",
                    "field_type": "numeric",
                    "comparison": ">",
                    "op": ">",
                    "optimizeEnabled": true,
                    "optimize": true,
                    "value": 50.0,
                    "valueMin": 40.0,
                    "value_min": 40.0,
                    "valueMax": 80.0,
                    "value_max": 80.0,
                    "valueStep": 5.0,
                    "value_step": 5.0,
                    "toggleOptimize": true,
                    "toggle_optimize": true
                },
                {
                    "id": "gate_expected_profit",
                    "field": "expected_profit",
                    "fieldType": "numeric",
                    "field_type": "numeric",
                    "comparison": ">",
                    "op": ">",
                    "optimizeEnabled": true,
                    "optimize": true,
                    "value": 3.0,
                    "valueMin": 0.0,
                    "value_min": 0.0,
                    "valueMax": 15.0,
                    "value_max": 15.0,
                    "valueStep": 1.0,
                    "value_step": 1.0,
                    "toggleOptimize": true,
                    "toggle_optimize": true
                },
                {
                    "id": "gate_days_since_close",
                    "field": "days_since_last_close",
                    "fieldType": "numeric",
                    "field_type": "numeric",
                    "comparison": ">",
                    "op": ">",
                    "optimizeEnabled": true,
                    "optimize": true,
                    "value": 0.0,
                    "valueMin": 0.0,
                    "value_min": 0.0,
                    "valueMax": 30.0,
                    "value_max": 30.0,
                    "valueStep": 5.0,
                    "value_step": 5.0,
                    "toggleOptimize": true,
                    "toggle_optimize": true
                },
                {
                    "id": "gate_days_since_profit",
                    "field": "days_since_last_profitable_close",
                    "fieldType": "numeric",
                    "field_type": "numeric",
                    "comparison": ">",
                    "op": ">",
                    "optimizeEnabled": true,
                    "optimize": true,
                    "value": 0.0,
                    "valueMin": 0.0,
                    "value_min": 0.0,
                    "valueMax": 30.0,
                    "value_max": 30.0,
                    "valueStep": 5.0,
                    "value_step": 5.0,
                    "toggleOptimize": true,
                    "toggle_optimize": true
                },
                {
                    "id": "gate_days_since_loss",
                    "field": "days_since_last_losing_close",
                    "fieldType": "numeric",
                    "field_type": "numeric",
                    "comparison": ">",
                    "op": ">",
                    "optimizeEnabled": true,
                    "optimize": true,
                    "value": 0.0,
                    "valueMin": 0.0,
                    "value_min": 0.0,
                    "valueMax": 60.0,
                    "value_max": 60.0,
                    "valueStep": 10.0,
                    "value_step": 10.0,
                    "toggleOptimize": true,
                    "toggle_optimize": true
                }
            ]
        },
        "actions": [
            {
                "action": "buy",
                "action_type": "buy",
                "lot_size": 100
            }
        ],
        "continueProcessing": false,
        "continue_processing": false
    }
]
""")


#: A throwaway SECOND profile, for the multi-profile paths. Its field/short cannot collide with
#: a registered one (``register_profile`` refuses that), so it is not a copy of the real
#: ``structure_state``: the categorical CONTRACT is now tested against the real registry field
#: (Task 10 registered ``ta-structure-v1``), and this exists only to make "two profiles" real.
CATEGORICAL = ProfileSpec(
    name="test-categorical-v1", calc_version="test-categorical-v1/calc-1",
    fields=(FieldSpec(name="t_structure_state", kind="categorical", short="t-structure-state",
                      searched=True, codes={"bear": 2, "bull": 1}, ui_name="Structure state"),),
)


@pytest.fixture
def profile_on(monkeypatch):
    """Run the body with ``ohlcv-v1`` selected, then restore the module default (off)."""
    monkeypatch.setattr(mod, "_MARKET_CONDITION_PROFILES", ("ohlcv-v1",))
    yield "ohlcv-v1"


def _entry_tree(strategy):
    trees = [r for r in (strategy.entry_rules or [])
             if isinstance(r.get("conditions"), dict) and r["conditions"].get("conditions")]
    assert len(trees) == 1, [r.get("id") for r in (strategy.entry_rules or [])]
    return trees[0]["conditions"]


#: The decision window the published test snapshot serves (its two feature rows are
#: 2025-06-27 and 2025-06-30, the prior sessions of these two decision dates).
MC_START, MC_END = "2025-06-30", "2025-07-01"


def _built(kind: str):
    return mod._build_strategy(kind, f"mc-{kind}", "FMPRating")


def _market_ids(node) -> list:
    """Every ``*-market-*`` leaf id anywhere under ``node`` (a dict, list or rule list)."""
    out: list = []

    def walk(n):
        if isinstance(n, list):
            for x in n:
                walk(x)
            return
        if not isinstance(n, dict):
            return
        cid = n.get("id")
        if isinstance(cid, str) and "-market-" in cid:
            out.append(cid)
        for v in n.values():
            walk(v)

    walk(node)
    return out


# --------------------------------------------------------------------------- profile none
def test_profile_none_reproduces_the_pre_change_option_entry_rule_byte_for_byte():
    assert mod._MARKET_CONDITION_PROFILES == (), "the default must be OFF"
    assert mod._option_entry_rule("O_LC") == PROFILE_NONE_O_LC


def test_profile_none_reproduces_the_pre_change_overlay_entry_rules_byte_for_byte():
    """O_CC is the other shape: its entry comes from the shared S2 builder and the gates are
    SPLICED into the returned strategy, so `none` must leave no trace of that splice."""
    assert mod._MARKET_CONDITION_PROFILES == ()
    assert mod._build_strategy("O_CC", "mc-O_CC", "FMPRating").entry_rules == PROFILE_NONE_O_CC_ENTRY


def test_profile_none_adds_no_leaf_and_no_gene_to_any_permitted_structure():
    for kind in PERMITTED:
        strat = _built(kind)
        assert _market_ids(strat.entry_rules) == [], kind
        assert _market_ids(strat.exit_rules) == [], kind
        assert [g for g in collect_param_space(strat) if "-market-" in g] == [], kind


# --------------------------------------------------------------------------- the gate leaves
def test_the_gate_leaves_are_built_from_the_registry(profile_on):
    leaves = mod._market_condition_gates("o_lc")
    specs = [f for f in PROFILES["ohlcv-v1"].fields if f.searched]
    assert len(leaves) == len(specs) == 3
    for leaf, spec in zip(leaves, specs):
        assert leaf["id"] == f"o_lc-market-{spec.short}"
        assert leaf["field"] == spec.name
        assert leaf["mode_optimize"] is True
        assert leaf["mode_choices"] == list(NUMERIC_MODE_CHOICES)
        assert leaf["op"] == spec.anchor_op and leaf["value"] == spec.anchor_value
        assert (leaf["value_min"], leaf["value_max"], leaf["value_step"]) == (
            spec.value_min, spec.value_max, spec.value_step)
        assert leaf["optimize"] is True
        # 'off' already removes the leaf; the two disable controls together are refused
        # downstream, so the template must never author both.
        assert "toggle_optimize" not in leaf


def test_every_authored_operator_is_one_the_condition_class_accepts(profile_on):
    """Launcher and engine cannot drift: the op comes from the generated class's own set."""
    from ba2_common.core.TradeConditions import market_condition_condition_class

    for leaf in mod._market_condition_gates("o_lc"):
        allowed = market_condition_condition_class(leaf["field"]).ALLOWED_OPERATORS
        assert leaf["op"] in allowed, (leaf["field"], leaf["op"], sorted(allowed))


def test_a_categorical_field_emits_a_mode_gene_and_no_threshold(monkeypatch):
    """Against the REAL registry field (``ta-structure-v1``'s ``structure_state``), not a
    throwaway spec: Task 3 wrote the categorical rules before a categorical field existed."""
    monkeypatch.setattr(mod, "_MARKET_CONDITION_PROFILES", ("ta-structure-v1",))
    leaves = mod._market_condition_gates("o_lc")
    assert len(leaves) == 5                       # the five searched ta-structure fields
    leaf, = [x for x in leaves if x["field"] == "structure_state"]
    assert leaf["id"] == "o_lc-market-structure"
    assert leaf["op"] == "=="
    assert leaf["mode_choices"] == [MODE_OFF, "bull", "bear"]  # ascending CODE, not alphabet
    assert "none" not in leaf["mode_choices"]     # stored as code 0, never selectable
    assert "value" not in leaf and "value_min" not in leaf and "optimize" not in leaf
    space = {}
    from app.services.strategy_param_space import _walk_condition_nodes

    _walk_condition_nodes({"id": "root", "type": "AND", "conditions": [leaf]}, space)
    assert sorted(space) == ["cond:o_lc-market-structure:mode"]
    assert space["cond:o_lc-market-structure:mode"]["choices"] == [MODE_OFF, "bull", "bear"]


# --------------------------------------------------------------------------- placement + genes
@pytest.mark.parametrize("kind", PERMITTED)
def test_each_permitted_structure_collects_exactly_six_more_genes(kind, profile_on):
    strat = _built(kind)
    prefix = kind.lower()
    genes = sorted(g for g in collect_param_space(strat) if "-market-" in g)
    assert genes == sorted([
        f"cond:{prefix}-market-{short}:{kind_}"
        for short in ("slope", "adx", "rv") for kind_ in ("mode", "value")
    ])


@pytest.mark.parametrize("kind", PERMITTED)
def test_the_gates_sit_on_the_initial_entry_tree_and_never_on_an_exit_rule(kind, profile_on):
    strat = _built(kind)
    tree = _entry_tree(strat)
    ids = [c.get("id") for c in tree["conditions"]]
    assert ids[-3:] == [f"{kind.lower()}-market-{s}" for s in ("slope", "adx", "rv")]
    # Design section 6: no gate may sit where it could delay an exit, a reduction or a
    # protective-order adjustment.
    assert _market_ids(strat.exit_rules) == [], kind


def test_the_overlays_gate_the_stock_entry_not_the_overlay_rule(profile_on):
    for kind in ("O_CC", "O_PP"):
        strat = _built(kind)
        tree = _entry_tree(strat)
        assert [c.get("id") for c in tree["conditions"]][:2] == ["buy-bullish", "buy-flat"]
        assert _market_ids(tree)[-3:] == [f"{kind.lower()}-market-{s}"
                                          for s in ("slope", "adx", "rv")]
        overlay = [r for r in strat.exit_rules if r.get("id") in ("cc_sell", "pp_buy")]
        assert overlay, kind
        assert _market_ids(overlay) == []


def test_the_wheel_gets_wheel_specific_ids_even_though_its_entry_is_the_csps(profile_on):
    strat = _built("O_WHEEL")
    ids = _market_ids(strat.entry_rules)
    assert ids == ["o_wheel-market-slope", "o_wheel-market-adx", "o_wheel-market-rv"]
    assert not any(i.startswith("o_csp-market-") for i in _market_ids(strat.entry_rules))


# --------------------------------------------------------------------------- reaching the engine
@pytest.mark.parametrize("kind", ["O_LC", "O_IC", "O_CC", "O_WHEEL"])
def test_every_new_leaf_reaches_the_engine(kind, profile_on, monkeypatch):
    """A leaf the engine DROPS is a gate the GA keeps scoring and the run never applies.

    The warnings are recorded through the module's own logger object: the ba2_common logger does
    not propagate to the root logger, so a caplog assertion here would pass on an empty string
    whatever happened.
    """
    from ba2_common.core import rule_builders
    from ba2_common.core.rule_builders import triggers_from_condition_tree

    warnings: list = []
    monkeypatch.setattr(rule_builders.logger, "warning",
                        lambda msg, *a: warnings.append(msg % a if a else msg))
    before = triggers_from_condition_tree(_entry_tree(_build_without_profile(kind)))
    after = triggers_from_condition_tree(_entry_tree(_built(kind)))
    assert len(after) == len(before) + 3, (kind, sorted(before), sorted(after))
    assert warnings == []
    events = {t["event_type"] for t in after.values()}
    for spec in PROFILES["ohlcv-v1"].fields:
        assert spec.name in events, (kind, spec.name)


def _build_without_profile(kind: str):
    saved = mod._MARKET_CONDITION_PROFILES
    mod._MARKET_CONDITION_PROFILES = ()
    try:
        return _built(kind)
    finally:
        mod._MARKET_CONDITION_PROFILES = saved


# --------------------------------------------------------------------------- the all-off control
def _market_mode_genes(space) -> list:
    """The MARKET-CONDITION mode genes of a collected space.

    A gene-level filter, not "every ``:mode`` gene", and the distinction became load-bearing on
    2026-09-19: the option DIRECTION gate is now a mode leaf too (``<m>-signal`` on
    ``rec_direction``, off/below/above). That gene is not part of the market-condition profile
    and exists under profile ``none`` as well, so switching it off would not build the control
    -- it would build a DIFFERENT strategy (one that enters in both directions) and the
    comparison below would fail for a reason that has nothing to do with the profile. The
    control holds it at its authored mode on both sides, which is exactly what "the same run
    with the market gates off" means.
    """
    return [g for g in space if g.endswith(":mode") and "-market-" in g]


@pytest.mark.parametrize("kind", PERMITTED)
def test_the_all_off_control_decodes_to_the_profile_none_tree(kind, profile_on):
    """Explicit ``mode=off`` on every MARKET gene must leave the SAME tree profile ``none``
    builds -- for every permitted structure, since Task 9's compatibility gate compares a whole
    frozen run and one structure's stray leaf would move its orders."""
    gated = _built(kind)
    space = collect_param_space(gated)
    market_modes = _market_mode_genes(space)
    assert len(market_modes) == 3, (kind, market_modes)
    genome = {g: (MODE_OFF if g in market_modes else _authored(gated, g))
              for g in space if g.startswith("cond:") or g.startswith("entry:")}
    decoded = decode_params(gated, {k: v for k, v in genome.items() if v is not None})
    plain = decode_params(_build_without_profile(kind), {})
    assert _market_ids(decoded["entry_rules"]) == []
    assert decoded["entry_rules"] == plain["entry_rules"]
    assert decoded["exit_rules"] == plain["exit_rules"]


@pytest.mark.parametrize("kind", PERMITTED)
def test_the_control_holds_the_direction_gate_and_only_the_market_gates_go_off(kind, profile_on):
    """The exclusion above, pinned rather than left implicit.

    Two halves. (1) The market mode genes the control switches off are EXACTLY the three the
    profile added -- turning a fourth one off would be a different strategy, not a control.
    (2) The direction mode gene, where the structure has one, is present in BOTH the gated and
    the profile-``none`` space, which is why holding it at its authored mode is the honest
    comparison. The overlays (O_CC / O_PP, whose entry is the shared equity builder's) and the
    non-directional structures have no direction mode gene at all, so the set is empty for
    them -- asserted here so this test still says something for every kind.
    """
    gated_space = collect_param_space(_built(kind))
    plain_space = collect_param_space(_build_without_profile(kind))
    market = set(_market_mode_genes(gated_space))
    assert market == {f"cond:{kind.lower()}-market-{s}:mode" for s in ("slope", "adx", "rv")}
    other_gated = {g for g in gated_space if g.endswith(":mode")} - market
    other_plain = {g for g in plain_space if g.endswith(":mode")}
    assert other_gated == other_plain, kind
    assert not [g for g in plain_space if "-market-" in g], kind


def _authored(strategy, gene):
    """The template's own value for a ``cond:<id>:value`` gene (None for anything else)."""
    if not gene.endswith(":value"):
        return None
    cid = gene[len("cond:"):-len(":value")]
    found = []

    def walk(n):
        if isinstance(n, list):
            for x in n:
                walk(x)
            return
        if isinstance(n, dict):
            if n.get("id") == cid and "value" in n:
                found.append(n["value"])
            for v in n.values():
                walk(v)

    walk(strategy.entry_rules)
    return found[0] if found else None


# --------------------------------------------------------------------------- smoke mode
def test_gates_off_removes_the_market_leaves_too(profile_on):
    """``mode_optimize`` marks a strategy opinion exactly as ``toggle_optimize`` does."""
    rule = mod._option_entry_rule("O_LC", gates_off=True)
    ids = [c["id"] for c in rule["conditions"]["conditions"]]
    assert ids == ["o_lc-flat"], ids


@pytest.mark.parametrize("kind", PERMITTED)
def test_gates_off_leaves_no_market_gate_and_no_market_gene_anywhere(kind, profile_on,
                                                                     monkeypatch):
    """EVERY permitted structure, because the two overlays do not share the pure-option path:
    their entry comes from the S2 builder, so the smoke filter inside ``_option_entry_rule``
    never sees their leaves and ``--gates-off`` left 6 market genes searching on O_CC/O_PP."""
    monkeypatch.setattr(mod, "_OPTION_GATES_OFF", True)
    strat = _built(kind)
    assert _market_ids(strat.entry_rules) == [], kind
    assert _market_ids(strat.exit_rules) == [], kind
    assert [g for g in collect_param_space(strat) if "-market-" in g] == [], kind


# --------------------------------------------------------------------------- CLI validation
def test_an_unknown_profile_is_refused_at_launch(monkeypatch):
    monkeypatch.setattr(mod, "_MARKET_CONDITION_PROFILES", ())
    with pytest.raises(SystemExit, match="unknown market-condition profile"):
        mod._resolve_market_condition_profiles("bogus", "optimize")
    with pytest.raises(SystemExit, match="mixes 'none'"):
        mod._resolve_market_condition_profiles("none,ohlcv-v1", "optimize")
    assert mod._resolve_market_condition_profiles("none", "optimize") == ()
    assert mod._resolve_market_condition_profiles(None, "optimize") == ()
    assert mod._resolve_market_condition_profiles("ohlcv-v1", "optimize") == ("ohlcv-v1",)


def test_more_than_one_profile_is_accepted_now_that_the_seam_reads_a_list(monkeypatch):
    """Task 10 widened ``install_backtest_market_conditions`` to one reader per profile behind a
    composite, so a comma list is no longer a job that would die per trial."""
    with registered_profile(CATEGORICAL):
        assert mod._resolve_market_condition_profiles(
            "ohlcv-v1,test-categorical-v1", "optimize") == ("ohlcv-v1", "test-categorical-v1")
        # ... and a repeat is collapsed, not counted twice.
        assert mod._resolve_market_condition_profiles(
            "ohlcv-v1,ohlcv-v1", "optimize") == ("ohlcv-v1",)
    monkeypatch.setattr(mod, "_MARKET_CONDITION_PROFILES", ())


def test_one_manifest_per_profile_is_required_and_matched(monkeypatch):
    monkeypatch.setattr(mod, "_MARKET_CONDITION_PROFILES", ("ohlcv-v1", "ta-structure-v1"))
    monkeypatch.setattr(mod, "_MARKET_CONDITION_MANIFESTS", {})
    # bare digests are matched POSITIONALLY to the profile list
    assert mod._resolve_market_condition_manifests("d1,d2", "optimize") == {
        "ohlcv-v1": "d1", "ta-structure-v1": "d2"}
    # ... and the counts must agree, or the pin would silently be for the wrong profile
    with pytest.raises(SystemExit, match="Give one digest per profile"):
        mod._resolve_market_condition_manifests("d1", "optimize")
    # profile=digest is explicit and order-free
    assert mod._resolve_market_condition_manifests(
        "ta-structure-v1=d2,ohlcv-v1=d1", "optimize") == {"ta-structure-v1": "d2", "ohlcv-v1": "d1"}
    with pytest.raises(SystemExit, match="does not select"):
        mod._resolve_market_condition_manifests("nope-v1=d1,ohlcv-v1=d2", "optimize")
    with pytest.raises(SystemExit, match="mixes bare digests"):
        mod._resolve_market_condition_manifests("d1,ohlcv-v1=d2", "optimize")
    monkeypatch.setattr(mod, "_MARKET_CONDITION_PROFILES", ("ohlcv-v1",))
    assert mod._resolve_market_condition_manifests("abc123", "optimize") == {"ohlcv-v1": "abc123"}


def test_the_optimize_flags_exist_with_the_documented_defaults():
    import argparse

    p = argparse.ArgumentParser()
    mod._add_market_condition_args(p)
    args = p.parse_args([])
    assert args.market_condition_profile == "none"
    assert args.market_condition_manifest is None
    args = p.parse_args(["--market-condition-profile", "ohlcv-v1",
                         "--market-condition-manifest", "abc123"])
    assert (args.market_condition_profile, args.market_condition_manifest) == ("ohlcv-v1", "abc123")


def test_an_optimize_with_a_profile_and_no_manifest_is_refused(profile_on, monkeypatch):
    monkeypatch.setattr(mod, "_MARKET_CONDITION_MANIFESTS", {})
    with pytest.raises(SystemExit, match="needs a --market-condition-manifest digest OF ITS OWN"):
        mod._apply_market_conditions(
            "optimize", {"enabled_instruments": ["AAA"], "start_date": MC_START,
                         "end_date": MC_END}, _built("O_LC"))


def test_a_manifest_without_a_profile_is_refused_rather_than_ignored(monkeypatch):
    """Ignoring it would run an UNGATED grid from a command line that says otherwise -- and the
    driver folds the manifest into the job-name digest, so it would read as gated afterwards."""
    monkeypatch.setattr(mod, "_MARKET_CONDITION_PROFILES", ())
    monkeypatch.setattr(mod, "_MARKET_CONDITION_MANIFESTS", {"": "abc123"})
    with pytest.raises(SystemExit, match="without --market-condition-profile"):
        mod._apply_market_conditions(
            "optimize", {"enabled_instruments": ["AAA"], "start_date": MC_START,
                         "end_date": MC_END}, _built("O_LC"))


# --------------------------------------------------------------------------- the pinned snapshot
def _publish_manifest(root, symbols=("AAA", "BBB")):
    """A tiny published snapshot over ``symbols`` (the VALUES do not matter here -- what is being
    pinned is the launcher's coverage check and what it records)."""
    from ba2_common.core.market_condition_store import MarketConditionStore
    from ba2_common.core.market_conditions import STATUS_INSUFFICIENT_HISTORY

    profile = PROFILES["ohlcv-v1"]
    fields = [f.name for f in profile.fields]
    # FEATURE sessions, built the way the warmup builds them for the window MC_START..MC_END:
    # ``[prior(first decision), last decision]`` (BT/live parity plan 2026-09-22 A3) -- a live
    # decision on S reads prior(S), a backtest BAR D reads D itself, and these three rows serve
    # both over the window every test here launches over (a pin is validated against the run's
    # sessions too, F1). Before the parity fix the fixture was the two rows 06-27, 06-30: the
    # backtest's last bar 07-01 then read 06-30.
    sessions = [date(2025, 6, 27), date(2025, 6, 30), date(2025, 7, 1)]
    store = MarketConditionStore(root)
    objects = []
    for symbol in symbols:
        rows = [{"session": s, "values": [None] * len(fields),
                 "status": [STATUS_INSUFFICIENT_HISTORY] * len(fields),
                 "reasons": ["published row"] * len(fields),
                 "window_digest": "sha256:" + "0" * 64, "raw_shard_ref": "",
                 "raw_row_lo": 0, "raw_row_hi": 0} for s in sessions]
        # One feature object per (symbol, calendar month) -- the store's sharding rule.
        for month in sorted({(r["session"].year, r["session"].month) for r in rows}):
            entry, _ = store.write_feature_object(
                profile, symbol,
                [r for r in rows if (r["session"].year, r["session"].month) == month])
            objects.append(entry)
    manifest = store.make_manifest(
        profile, source_profile="fmp-daily-split-adjusted-v1", timing_policy="prior_session_v1",
        objects=objects, raw_objects=[],
        coverage={s: {"rows": len(sessions)} for s in symbols}, universe=list(symbols),
        sessions=sessions, window_start=date.fromisoformat(MC_START),
        window_end=date.fromisoformat(MC_END))
    return store, store.write_manifest(manifest)


@pytest.fixture
def snapshot(tmp_path, monkeypatch):
    import ba2_common.config as bc

    store, digest = _publish_manifest(tmp_path / "cache")
    monkeypatch.setattr(bc, "CACHE_FOLDER", str(store.cache_root))
    return digest


def test_a_manifest_that_does_not_cover_the_universe_is_refused_naming_the_symbols(
        profile_on, snapshot, monkeypatch):
    monkeypatch.setattr(mod, "_MARKET_CONDITION_MANIFESTS", {"ohlcv-v1": snapshot})
    with pytest.raises(SystemExit) as e:
        mod._apply_market_conditions(
            "optimize", {"enabled_instruments": ["AAA", "ZZZ", "QQQ"], "start_date": MC_START,
                         "end_date": MC_END}, _built("O_LC"))
    message = str(e.value)
    assert "ZZZ" in message and "QQQ" in message and "AAA" not in message.split("instruments:")[1]
    assert snapshot in message


def test_a_snapshot_warmed_for_another_window_is_refused_before_dispatch(
        profile_on, snapshot, monkeypatch):
    """Review 2026-09-16, F1 -- reproduced against THIS preflight and fixed here.

    The reviewer pinned a 2024-03 snapshot on a 2025 run: every symbol was present, the check
    passed, the launcher wrote the pin, and ``observe()`` then returned None for every decision
    date. Every gate read ``missing_session``, every gated entry was refused, and the GA would
    have scored that suppression as strategy behaviour. The refusal has to happen HERE, at launch,
    as a job configuration error -- a zero-trade fitness is not an answer to it.
    """
    monkeypatch.setattr(mod, "_MARKET_CONDITION_MANIFESTS", {"ohlcv-v1": snapshot})
    block = {"enabled_instruments": ["AAA", "BBB"], "start_date": "2025-01-02",
             "end_date": "2025-12-31",
             "experts": [{"class": "FMPRating", "settings": {}}]}
    with pytest.raises(SystemExit) as e:
        mod._apply_market_conditions("optimize", block, _built("O_LC"))
    message = str(e.value)
    assert "does not serve the feature rows" in message and snapshot in message
    assert "2025-01-02..2025-12-31" in message
    # ... and nothing was written onto the run: a refused launch leaves no half-gated config.
    assert "market_condition_profiles" not in block
    assert "market_condition_profile" not in block["experts"][0]["settings"]


def test_the_window_the_snapshot_was_warmed_for_passes(profile_on, snapshot, monkeypatch):
    """The other half of the same guard: the right window is not refused, so the check cannot be
    "passing" by refusing everything."""
    monkeypatch.setattr(mod, "_MARKET_CONDITION_MANIFESTS", {"ohlcv-v1": snapshot})
    block = _gated_block()
    assert mod._apply_market_conditions("optimize", block, _built("O_LC"))["manifests"] == {
        "ohlcv-v1": snapshot}


def test_a_block_with_no_window_cannot_be_validated_and_is_refused(profile_on, snapshot,
                                                                   monkeypatch):
    """An unvalidated pin is exactly the failure the refusal exists for, so "I could not check"
    is not allowed to read as "it is fine"."""
    monkeypatch.setattr(mod, "_MARKET_CONDITION_MANIFESTS", {"ohlcv-v1": snapshot})
    with pytest.raises(SystemExit, match="no start_date/end_date"):
        mod._apply_market_conditions(
            "optimize", {"enabled_instruments": ["AAA"],
                         "experts": [{"class": "FMPRating", "settings": {}}]}, _built("O_LC"))


def test_the_coverage_check_reads_the_universe_on_the_block_it_is_given(
        profile_on, snapshot, monkeypatch):
    """Coverage is checked against ``enabled_instruments`` AS IT STANDS WHEN THE CHECK RUNS.

    A --screener run replaces that list with the screened candidate union, which is the universe
    the gates are actually asked about; an uncovered symbol in it must refuse the run. (That the
    call really happens after the rewrite is the next test's job -- ordering is not observable
    from a unit call.)
    """
    monkeypatch.setattr(mod, "_MARKET_CONDITION_MANIFESTS", {"ohlcv-v1": snapshot})
    screened = {"enabled_instruments": ["AAA", "ZZZ"],   # what the screener block writes
                "start_date": MC_START, "end_date": MC_END}
    with pytest.raises(SystemExit, match="ZZZ"):
        mod._apply_market_conditions("optimize", screened, _built("O_LC"))


def test_the_optimize_command_records_the_profile_after_every_universe_rewrite():
    """The ORDER inside ``_cmd_optimize``, read from the source.

    The contract is "after EVERY rewrite of ``enabled_instruments``", so the LAST one is what the
    call has to follow -- checking only the first would pass a future block that narrows the
    universe again below it. Both screener blocks (``--screener`` and ``--screener-gate-store``)
    run between the block's construction and this call; the SCREENER one assigns the list.
    """
    src = open(_LAUNCHER, encoding="utf-8").read()
    start = src.index("def _cmd_optimize(args)")
    body = src[start:src.index("def _cmd_optimize_batch(args)")]
    last_rewrite = body.rindex('backtest_block["enabled_instruments"]')
    call = body.index('_apply_market_conditions("optimize"')
    assert last_rewrite < call, ("the market-condition coverage check must run AFTER every "
                                 "rewrite of the run universe")


def test_two_cache_roots_with_the_same_digest_get_their_own_reader(profile_on, tmp_path,
                                                                    monkeypatch):
    """The facts cache is a per-batch read saver, not an identity claim about a digest.

    The same digest names a DIFFERENT file under a different BA2_HOME (it is the hash of the
    manifest, and two hosts can publish the same content), so a process that switches roots --
    a test, a re-pointed run -- must not be served the first root's reader.
    """
    import ba2_common.config as bc

    store_a, digest = _publish_manifest(tmp_path / "a", symbols=("AAA",))
    store_b, digest_b = _publish_manifest(tmp_path / "b", symbols=("AAA",))
    assert digest == digest_b, "same content, same digest -- that is the premise"

    monkeypatch.setattr(bc, "CACHE_FOLDER", str(store_a.cache_root))
    first = mod._market_condition_manifest_facts(digest, "ohlcv-v1")
    assert mod._market_condition_manifest_facts(digest, "ohlcv-v1") is first, "cached per root"

    monkeypatch.setattr(bc, "CACHE_FOLDER", str(store_b.cache_root))
    second = mod._market_condition_manifest_facts(digest, "ohlcv-v1")
    assert second is not first
    assert second["reader"].cache_root == str(store_b.cache_root)
    assert first["reader"].cache_root == str(store_a.cache_root)


def test_the_run_config_records_the_profile_the_manifest_and_the_calc_versions(
        profile_on, snapshot, monkeypatch):
    monkeypatch.setattr(mod, "_MARKET_CONDITION_MANIFESTS", {"ohlcv-v1": snapshot})
    strat = _built("O_LC")
    block = {"enabled_instruments": ["AAA", "BBB"], "start_date": MC_START, "end_date": MC_END,
             "experts": [{"class": "FMPRating", "settings": {}}]}
    recorded = mod._apply_market_conditions("optimize", block, strat)

    # PLURAL: one manifest PER PROFILE, and the provenance read off each is keyed by profile.
    assert block["market_condition_profiles"] == ["ohlcv-v1"]
    assert block["market_condition_manifests"] == {"ohlcv-v1": snapshot}
    # ... and THE EXPERT SETTING, which is what live reads and what the seam resolves from.
    assert block["experts"][0]["settings"]["market_condition_profile"] == "ohlcv-v1"
    assert recorded["manifests"] == {"ohlcv-v1": snapshot}
    assert recorded["calc_versions"] == {"ohlcv-v1": PROFILES["ohlcv-v1"].calc_version}
    facts = recorded["facts"]["ohlcv-v1"]
    assert facts["source_profile"] == "fmp-daily-split-adjusted-v1"
    assert facts["timing_policy"] == "prior_session_v1"
    assert facts["calendar_version"]
    assert [f["name"] for f in recorded["fields"]] == [f.name for f in PROFILES["ohlcv-v1"].fields]
    # FieldSpec.to_dict, never dataclasses.asdict: the private _code_pairs must not leak.
    assert all("_code_pairs" not in f for f in recorded["fields"])
    assert recorded["gene_count"] == 6 and len(recorded["genes"]) == 6
    assert recorded["genes"] == sorted(g for g in collect_param_space(strat) if "-market-" in g)


def test_the_profile_does_not_scale_population_or_generations():
    """Operator decision 2026-09-15: "add genes but keep pop size as is, is already big".

    Asserted where the numbers are actually decided -- the driver's resolved argv -- rather than
    on a dict the code under test never touches.
    """
    import importlib.util as _ilu

    driver_path = os.path.normpath(os.path.join(_ROOT, "..", "..", "tools",
                                                "run_options_matrix.py"))
    spec = _ilu.spec_from_file_location("run_options_matrix_pop", driver_path)
    driver = _ilu.module_from_spec(spec)
    spec.loader.exec_module(driver)
    argv = ["--profile", "discovery", "--strategies", "O_LC", "--experts", "FMPRating",
            "--launcher", _LAUNCHER, "--start", "2020-01-01", "--end", "2025-12-31",
            "--screener-gate-store", "store.parquet", "--max-stock-price", "0"]
    gated = argv + ["--market-condition-profile", "ohlcv-v1",
                    "--market-condition-manifest", "abc123"]

    def budget(a):
        args = driver.resolve_args(driver.build_parser(), a)
        cmd = driver.build_cmd(args, _LAUNCHER, "n", "FMPRating", "O_LC", "AAPL")
        return {flag: cmd[cmd.index(flag) + 1]
                for flag in ("--population", "--generations", "--early-stop")}

    assert budget(argv) == budget(gated) == {"--population": "200", "--generations": "60",
                                             "--early-stop": "8"}


def test_the_persisted_digest_round_trips_into_a_trial_config(profile_on, snapshot, monkeypatch):
    """The stored ``backtest`` block is what every later consumer re-reads (re-runs, robustness
    variants, top-N persist, tools/backtest_parity.py). If the digest is not in it, the seam
    refuses every one of them."""
    from app.services.strategy_optimization_handler import _build_daily_trial_config

    monkeypatch.setattr(mod, "_MARKET_CONDITION_MANIFESTS", {"ohlcv-v1": snapshot})
    strat = _built("O_LC")
    backtest_cfg = {
        "backtest_id": "mc", "start_date": MC_START, "end_date": MC_END,
        "enabled_instruments": ["AAA", "BBB"], "experts": [{"class": "FMPRating", "settings": {}}],
        "initial_capital": 20_000.0, "account_settings": {}, "warmup_days": 0, "seed": 1,
        "entry_action": getattr(strat, "entry_action", None),
    }
    mod._apply_market_conditions("optimize", backtest_cfg, strat)
    # Round-trip through JSON: the persisted optimization_config is a JSON column.
    backtest_cfg = json.loads(json.dumps(backtest_cfg, default=str))
    trial = _build_daily_trial_config(backtest_cfg, decode_params(strat, {}), None, option_trade_records=False)
    assert trial["market_condition_profiles"] == ["ohlcv-v1"]
    assert trial["market_condition_manifests"] == {"ohlcv-v1": snapshot}
    assert trial["_ga_trial"] is True


def test_a_pre_task10_persisted_config_still_round_trips_into_a_trial_config():
    """Every optimization_config persisted BEFORE the seam went plural carries the singular
    ``market_condition_profile``/``_manifest`` pair. Re-running one of those genomes (the parity
    tool, a re-run, a robustness variant, a top-N persist) has to keep working."""
    from app.services.strategy_optimization_handler import _build_daily_trial_config

    legacy = {"backtest_id": "mc", "start_date": "2024-02-01", "end_date": "2024-06-01",
              "enabled_instruments": ["AAA"], "experts": [{"class": "FMPRating", "settings": {}}],
              "initial_capital": 20_000.0, "account_settings": {}, "warmup_days": 0, "seed": 1,
              "market_condition_profile": "ohlcv-v1", "market_condition_manifest": "e" * 64}
    trial = _build_daily_trial_config(json.loads(json.dumps(legacy)), {}, option_trade_records=False)
    assert trial["market_condition_profiles"] == ["ohlcv-v1"]
    assert trial["market_condition_manifests"] == {"ohlcv-v1": "e" * 64}
    assert trial["_ga_trial"] is True


@pytest.mark.parametrize("config,message", [
    # The plural list is EMPTY and the legacy key names a profile: one key says the gates are on
    # and the other says they are off, and the quiet reading is a run that trades UNGATED under
    # the name of a gated one.
    ({"market_condition_profiles": [], "market_condition_profile": "ohlcv-v1"}, "they disagree"),
    # A manifest with no profile at all. The digest folds into the driver's job identity, so the
    # run reads as gated in every listing afterwards while nothing ever opens the snapshot.
    ({"market_condition_manifests": {"ohlcv-v1": "d" * 64}}, "but no profile is"),
    ({"market_condition_profiles": [], "market_condition_manifest": "d" * 64}, "but no profile is"),
])
def test_a_legacy_pin_that_contradicts_the_plural_one_is_refused_not_dropped(config, message):
    """Three shapes that used to resolve to "no profile, no manifest" without a word."""
    from app.services.backtest.seam_wiring import market_condition_pins

    with pytest.raises(ValueError, match=message):
        market_condition_pins(config, required=False)
    with pytest.raises(ValueError, match=message):
        market_condition_pins(config)
    # ... and the shapes that are NOT contradictions still resolve.
    assert market_condition_pins({"market_condition_profile": "none"}) == ([], {})
    assert market_condition_pins({"market_condition_profiles": []}) == ([], {})
    assert market_condition_pins({}, required=False) == ([], {})


# --------------------------------------------------------------------------- ta-structure-v1
def test_the_ta_structure_gate_leaves_carry_the_registry_ranges_and_anchors(monkeypatch):
    monkeypatch.setattr(mod, "_MARKET_CONDITION_PROFILES", ("ta-structure-v1",))
    leaves = {lf["field"]: lf for lf in mod._market_condition_gates("o_lc")}
    searched = [f for f in PROFILES["ta-structure-v1"].fields if f.searched]
    assert set(leaves) == {f.name for f in searched}
    for spec in searched:
        leaf = leaves[spec.name]
        assert leaf["id"] == f"o_lc-market-{spec.short}"
        assert leaf["mode_optimize"] is True
        if spec.kind == "numeric":
            assert leaf["mode_choices"] == list(NUMERIC_MODE_CHOICES)
            assert (leaf["op"], leaf["value"]) == (spec.anchor_op, spec.anchor_value)
            assert (leaf["value_min"], leaf["value_max"], leaf["value_step"]) == (
                spec.value_min, spec.value_max, spec.value_step)
            assert leaf["optimize"] is True
        else:
            assert leaf["mode_choices"] == ["off", *spec.codes]
            assert "value" not in leaf and "optimize" not in leaf


def test_the_profile_adds_nine_genes_per_arm_and_both_profiles_add_fifteen(monkeypatch):
    """Design 3.2: "four numeric and one categorical, NINE genes per arm on top of the six from
    section 3". Counted on the strategy the run stores, not re-derived from the registry."""
    monkeypatch.setattr(mod, "_MARKET_CONDITION_PROFILES", ("ta-structure-v1",))
    strat = _built("O_LC")
    genes = mod._market_condition_gene_names(strat)
    assert len(genes) == 9
    assert genes == sorted(g for g in collect_param_space(strat) if "-market-" in g)

    monkeypatch.setattr(mod, "_MARKET_CONDITION_PROFILES", ("ohlcv-v1", "ta-structure-v1"))
    both = mod._market_condition_gene_names(_built("O_LC"))
    assert len(both) == 15


def test_both_profiles_gate_only_the_initial_entry_tree(monkeypatch):
    monkeypatch.setattr(mod, "_MARKET_CONDITION_PROFILES", ("ohlcv-v1", "ta-structure-v1"))
    strat = _built("O_LC")
    assert len(_market_ids(strat.entry_rules)) == 8          # 3 ohlcv + 5 ta-structure leaves
    assert _market_ids(strat.exit_rules) == []


# ------------------------------------------------- the flag writes the EXPERT SETTING (Task 12)
def _gated_block():
    return {"enabled_instruments": ["AAA", "BBB"], "start_date": MC_START, "end_date": MC_END,
            "experts": [{"class": "FMPRating", "settings": {"sizing_mode": "risk_atr"}}]}


def test_the_flag_writes_the_setting_onto_every_expert_job(profile_on, snapshot, monkeypatch):
    """``--market-condition-profile`` is what the operator types; the SETTING is what the run
    carries, what the backtest seam resolves from and what a deploy of the winning genome puts
    on the live instance. One string, three readers."""
    monkeypatch.setattr(mod, "_MARKET_CONDITION_MANIFESTS", {"ohlcv-v1": snapshot})
    block = _gated_block()
    block["experts"].append({"class": "FMPRatingB", "settings": {}})
    mod._apply_market_conditions("optimize", block, _built("O_LC"))
    assert [spec["settings"]["market_condition_profile"] for spec in block["experts"]] == [
        "ohlcv-v1", "ohlcv-v1"]
    # The settings the spec already carried are untouched.
    assert block["experts"][0]["settings"]["sizing_mode"] == "risk_atr"


def test_the_gate_leaves_are_all_served_by_the_setting_the_job_carries(profile_on, snapshot,
                                                                      monkeypatch):
    """The gates and the data supply cannot disagree, because both are derived from the setting:
    ``_market_condition_gates`` reads it through the same parser the seam and live use."""
    from ba2_common.core.market_condition_rules import (
        assert_market_fields_served, parse_profile_setting,
    )

    monkeypatch.setattr(mod, "_MARKET_CONDITION_MANIFESTS", {"ohlcv-v1": snapshot})
    strat = _built("O_LC")
    block = _gated_block()
    mod._apply_market_conditions("optimize", block, strat)
    setting = block["experts"][0]["settings"]["market_condition_profile"]
    assert_market_fields_served(strat.entry_rules, parse_profile_setting(setting),
                                where="entry rules")


def test_the_gates_are_built_through_the_settings_parser(monkeypatch):
    """Not from the global directly: an unregistered or repeated name is refused with the SAME
    message the seam, the live resolver and the deploy importer produce."""
    monkeypatch.setattr(mod, "_MARKET_CONDITION_PROFILES", ("ohlcv-v1",))
    assert mod._market_condition_setting_value() == "ohlcv-v1"
    assert mod._market_condition_setting_profiles() == ("ohlcv-v1",)
    assert [lf["field"] for lf in mod._market_condition_gates("o_lc")] == [
        f.name for f in PROFILES["ohlcv-v1"].fields if f.searched]

    monkeypatch.setattr(mod, "_MARKET_CONDITION_PROFILES", ("ohlcv-v9",))
    with pytest.raises(ValueError, match="not a registered"):
        mod._market_condition_gates("o_lc")

    monkeypatch.setattr(mod, "_MARKET_CONDITION_PROFILES", ())
    assert mod._market_condition_setting_value() == ""
    assert mod._market_condition_gates("o_lc") == []


def test_a_profile_off_run_writes_no_setting_at_all(monkeypatch):
    """No profile -> the stored config is byte-identical to today's, INCLUDING the expert
    settings: the whole goal2020 archive stays comparable, and a re-run of an ungated genome
    cannot acquire a market-condition setting it never had."""
    monkeypatch.setattr(mod, "_MARKET_CONDITION_PROFILES", ())
    block = _gated_block()
    before = json.loads(json.dumps(block))
    assert mod._apply_market_conditions("optimize", block, _built("O_LC")) == {}
    assert block == before


def test_a_job_with_no_expert_spec_to_carry_the_setting_is_refused(profile_on, snapshot,
                                                                   monkeypatch):
    """A run-level key alone would be gated in the config and UNGATED the moment its genome is
    deployed (a payload carries settings, not that key)."""
    monkeypatch.setattr(mod, "_MARKET_CONDITION_MANIFESTS", {"ohlcv-v1": snapshot})
    with pytest.raises(SystemExit) as e:
        mod._apply_market_conditions(
            "optimize", {"enabled_instruments": ["AAA"], "start_date": MC_START,
                         "end_date": MC_END}, _built("O_LC"))
    assert "market_condition_profile" in str(e.value)


def test_a_setting_naming_a_profile_with_no_manifest_fails_before_dispatch(profile_on,
                                                                          monkeypatch):
    """One message at launch instead of N identical crashed trials (the seam refuses such a
    trial config; this is the same refusal, earlier)."""
    monkeypatch.setattr(mod, "_MARKET_CONDITION_MANIFESTS", {})
    with pytest.raises(SystemExit) as e:
        mod._apply_market_conditions("optimize", _gated_block(), _built("O_LC"))
    assert "needs a --market-condition-manifest digest OF ITS OWN" in str(e.value)


def test_the_setting_and_the_run_config_key_agree_by_construction(profile_on, snapshot,
                                                                  monkeypatch):
    """Both are written, and the seam refuses a config where they differ -- so the redundancy is
    checked rather than trusted."""
    from app.services.backtest.seam_wiring import market_condition_pins

    monkeypatch.setattr(mod, "_MARKET_CONDITION_MANIFESTS", {"ohlcv-v1": snapshot})
    block = _gated_block()
    mod._apply_market_conditions("optimize", block, _built("O_LC"))
    assert market_condition_pins(block) == (["ohlcv-v1"], {"ohlcv-v1": snapshot})


# -------------------------------------------- the flag is OPTIONS-ONLY in this delivery (I5)
def test_the_profile_flag_is_refused_for_a_strategy_that_emits_no_gates(profile_on, snapshot,
                                                                        monkeypatch):
    """``optimize-batch --strategies S1,O_LC --market-condition-profile ...`` produced an S1 job
    labelled gated, held to the OPTION snapshot's coverage, and searching exactly zero market
    genes -- only the option builders emit leaves. Refused by name instead."""
    monkeypatch.setattr(mod, "_MARKET_CONDITION_MANIFESTS", {"ohlcv-v1": snapshot})
    with pytest.raises(SystemExit) as e:
        mod._apply_market_conditions("optimize-batch", _gated_block(), _built("O_LC"), "S1")
    msg = str(e.value)
    assert "options-only" in msg and "'S1'" in msg
    assert "O_LC" in msg                     # the message lists the keys that ARE gated


@pytest.mark.parametrize("kind", ["O_LC", "O_CC", "O_PP", "O_WHEEL"])
def test_every_gate_emitting_key_is_permitted(profile_on, snapshot, monkeypatch, kind):
    monkeypatch.setattr(mod, "_MARKET_CONDITION_MANIFESTS", {"ohlcv-v1": snapshot})
    block = _gated_block()
    mod._apply_market_conditions("optimize", block, _built(kind), kind)
    assert block["experts"][0]["settings"]["market_condition_profile"] == "ohlcv-v1"


def test_the_permitted_set_is_exactly_the_keys_whose_builders_emit_leaves():
    """Pinned against the builders rather than restated: the pure-option structures get their
    leaves from ``_option_entry_rule``, O_CC/O_PP from ``_append_market_condition_gates``, and
    O_STK has no entry gate of its own to hang them on."""
    assert mod._MARKET_CONDITION_STRATEGIES == mod._PURE_OPTION_STRATEGIES | {"O_CC", "O_PP"}
    assert "O_STK" not in mod._MARKET_CONDITION_STRATEGIES
    assert not (mod._MARKET_CONDITION_STRATEGIES & {"S1", "S2", "S5", "S7"})


def test_an_ungated_run_is_not_restricted_by_strategy(monkeypatch):
    """NO IMPACT: with the profile off, every strategy key goes through untouched -- the whole
    equity grid must be unchanged by a flag it does not pass."""
    monkeypatch.setattr(mod, "_MARKET_CONDITION_PROFILES", ())
    monkeypatch.setattr(mod, "_MARKET_CONDITION_MANIFESTS", {})
    block = _gated_block()
    before = json.loads(json.dumps(block))
    assert mod._apply_market_conditions("optimize-batch", block, _built("O_LC"), "S1") == {}
    assert block == before


def test_a_caller_that_names_no_kind_is_still_served(profile_on, snapshot, monkeypatch):
    """``kind`` defaults to '' so an in-code caller that has no strategy key (a test, a one-off
    harness) is not refused -- the restriction is on the two CLI commands, which always know it."""
    monkeypatch.setattr(mod, "_MARKET_CONDITION_MANIFESTS", {"ohlcv-v1": snapshot})
    block = _gated_block()
    mod._apply_market_conditions("optimize", block, _built("O_LC"))
    assert block["market_condition_profiles"] == ["ohlcv-v1"]


def test_both_cli_commands_pass_the_strategy_key():
    """The guard is worth nothing if the commands do not name the strategy."""
    import inspect

    for fn in (mod._cmd_optimize, mod._cmd_optimize_batch):
        calls = [ln for ln in inspect.getsource(fn).splitlines()
                 if "_apply_market_conditions(" in ln]
        assert len(calls) == 1, (fn.__name__, calls)
        assert calls[0].rstrip().endswith("strat, args.strategy)") or \
            calls[0].rstrip().endswith("strat, strat_kind)"), calls[0]
