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
                "field": "bullish",
                "field_type": "flag",
                "toggle_optimize": true
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
                "value_max": 3.0,
                "value_step": 0.25
            },
            {
                "id": "o_lc-iv_rv",
                "field": "iv_to_realized_vol",
                "op": "<",
                "value": 1.6,
                "optimize": true,
                "toggle_optimize": true,
                "value_min": 0.8,
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
            "option_entry_cross": 0.0,
            "option_entry_cross_optimize": true,
            "option_entry_cross_min": 0.0,
            "option_entry_cross_max": 1.0,
            "option_entry_cross_step": 0.25,
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

#: A throwaway CATEGORICAL profile: the only way to exercise the categorical branch before Task 10
#: registers ``ta-structure-v1``. Codes are deliberately NOT in alphabetical order, so the
#: "ascending CODE order" contract is actually tested.
CATEGORICAL = ProfileSpec(
    name="test-categorical-v1", calc_version="test-categorical-v1/calc-1",
    fields=(FieldSpec(name="structure_state", kind="categorical", short="structure-state",
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
    with registered_profile(CATEGORICAL):
        from ba2_common.core import rule_builders

        rule_builders.register_market_condition_field_events()
        monkeypatch.setattr(mod, "_MARKET_CONDITION_PROFILES", ("test-categorical-v1",))
        leaf, = mod._market_condition_gates("o_lc")
        assert leaf["id"] == "o_lc-market-structure-state"
        assert leaf["op"] == "=="
        assert leaf["mode_choices"] == [MODE_OFF, "bull", "bear"]  # ascending CODE, not alphabet
        assert "value" not in leaf and "value_min" not in leaf and "optimize" not in leaf
        space = {}
        from app.services.strategy_param_space import _walk_condition_nodes

        _walk_condition_nodes({"id": "root", "type": "AND", "conditions": [leaf]}, space)
        assert sorted(space) == ["cond:o_lc-market-structure-state:mode"]
        assert space["cond:o_lc-market-structure-state:mode"]["choices"] == [MODE_OFF, "bull", "bear"]


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
def test_the_all_off_control_decodes_to_the_profile_none_tree(profile_on):
    """Three explicit ``mode=off`` genes must leave the SAME tree profile ``none`` builds."""
    gated = _built("O_LC")
    space = collect_param_space(gated)
    genome = {g: (MODE_OFF if g.endswith(":mode") else _authored(gated, g))
              for g in space if g.startswith("cond:") or g.startswith("entry:")}
    decoded = decode_params(gated, {k: v for k, v in genome.items() if v is not None})
    plain = decode_params(_build_without_profile("O_LC"), {})
    assert _market_ids(decoded["entry_rules"]) == []
    assert decoded["entry_rules"] == plain["entry_rules"]


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


def test_more_than_one_profile_is_refused_while_the_trial_seam_pins_one(monkeypatch):
    with registered_profile(CATEGORICAL):
        with pytest.raises(SystemExit, match="takes ONE profile today"):
            mod._resolve_market_condition_profiles("ohlcv-v1,test-categorical-v1", "optimize")


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
    monkeypatch.setattr(mod, "_MARKET_CONDITION_MANIFEST", None)
    with pytest.raises(SystemExit, match="needs --market-condition-manifest"):
        mod._apply_market_conditions("optimize", None,
                                     {"enabled_instruments": ["AAA"]}, _built("O_LC"))


# --------------------------------------------------------------------------- the pinned snapshot
def _publish_manifest(root, symbols=("AAA", "BBB")):
    """A tiny published snapshot over ``symbols`` (the VALUES do not matter here -- what is being
    pinned is the launcher's coverage check and what it records)."""
    from ba2_common.core.market_condition_store import MarketConditionStore
    from ba2_common.core.market_conditions import STATUS_INSUFFICIENT_HISTORY

    profile = PROFILES["ohlcv-v1"]
    fields = [f.name for f in profile.fields]
    sessions = [date(2025, 6, 27), date(2025, 6, 30)]
    store = MarketConditionStore(root)
    objects = []
    for symbol in symbols:
        rows = [{"session": s, "values": [None] * len(fields),
                 "status": [STATUS_INSUFFICIENT_HISTORY] * len(fields),
                 "reasons": ["published row"] * len(fields),
                 "window_digest": "sha256:" + "0" * 64, "raw_shard_ref": "",
                 "raw_row_lo": 0, "raw_row_hi": 0} for s in sessions]
        entry, _ = store.write_feature_object(profile, symbol, rows)
        objects.append(entry)
    manifest = store.make_manifest(
        profile, source_profile="fmp-daily-split-adjusted-v1", timing_policy="prior_session_v1",
        objects=objects, raw_objects=[],
        coverage={s: {"rows": len(sessions)} for s in symbols}, universe=list(symbols),
        sessions=sessions, window_start=sessions[0], window_end=sessions[-1])
    return store, store.write_manifest(manifest)


@pytest.fixture
def snapshot(tmp_path, monkeypatch):
    import ba2_common.config as bc

    store, digest = _publish_manifest(tmp_path / "cache")
    monkeypatch.setattr(bc, "CACHE_FOLDER", str(store.cache_root))
    return digest


def test_a_manifest_that_does_not_cover_the_universe_is_refused_naming_the_symbols(
        profile_on, snapshot, monkeypatch):
    monkeypatch.setattr(mod, "_MARKET_CONDITION_MANIFEST", snapshot)
    with pytest.raises(SystemExit) as e:
        mod._apply_market_conditions(
            "optimize", None, {"enabled_instruments": ["AAA", "ZZZ", "QQQ"]}, _built("O_LC"))
    message = str(e.value)
    assert "ZZZ" in message and "QQQ" in message and "AAA" not in message.split("instruments:")[1]
    assert snapshot in message


def test_the_run_config_records_the_profile_the_manifest_and_the_calc_versions(
        profile_on, snapshot, monkeypatch):
    monkeypatch.setattr(mod, "_MARKET_CONDITION_MANIFEST", snapshot)
    strat = _built("O_LC")
    block = {"enabled_instruments": ["AAA", "BBB"]}
    recorded = mod._apply_market_conditions("optimize", None, block, strat)

    assert block["market_condition_profile"] == "ohlcv-v1"
    assert block["market_condition_manifest"] == snapshot
    assert recorded["calc_versions"] == {"ohlcv-v1": PROFILES["ohlcv-v1"].calc_version}
    assert recorded["source_profile"] == "fmp-daily-split-adjusted-v1"
    assert recorded["timing_policy"] == "prior_session_v1"
    assert recorded["calendar_version"]
    assert [f["name"] for f in recorded["fields"]] == [f.name for f in PROFILES["ohlcv-v1"].fields]
    # FieldSpec.to_dict, never dataclasses.asdict: the private _code_pairs must not leak.
    assert all("_code_pairs" not in f for f in recorded["fields"])
    assert recorded["gene_count"] == 6 and len(recorded["genes"]) == 6
    assert recorded["genes"] == sorted(g for g in collect_param_space(strat) if "-market-" in g)


def test_the_profile_does_not_scale_population_or_generations(profile_on, snapshot, monkeypatch):
    """Operator decision 2026-09-15: add genes, keep the population as it is."""
    monkeypatch.setattr(mod, "_MARKET_CONDITION_MANIFEST", snapshot)
    block = {"enabled_instruments": ["AAA", "BBB"]}
    cfg = {"populationSize": 200, "generations": 60, "earlyStoppingGenerations": 8,
           "backtest": block}
    mod._apply_market_conditions("optimize", None, block, _built("O_LC"))
    assert (cfg["populationSize"], cfg["generations"], cfg["earlyStoppingGenerations"]) == (
        200, 60, 8)


def test_the_persisted_digest_round_trips_into_a_trial_config(profile_on, snapshot, monkeypatch):
    """The stored ``backtest`` block is what every later consumer re-reads (re-runs, robustness
    variants, top-N persist, tools/backtest_parity.py). If the digest is not in it, the seam
    refuses every one of them."""
    from app.services.strategy_optimization_handler import _build_daily_trial_config

    monkeypatch.setattr(mod, "_MARKET_CONDITION_MANIFEST", snapshot)
    strat = _built("O_LC")
    backtest_cfg = {
        "backtest_id": "mc", "start_date": "2024-02-01", "end_date": "2024-06-01",
        "enabled_instruments": ["AAA", "BBB"], "experts": [{"class": "FMPRating", "settings": {}}],
        "initial_capital": 20_000.0, "account_settings": {}, "warmup_days": 0, "seed": 1,
        "entry_action": getattr(strat, "entry_action", None),
    }
    mod._apply_market_conditions("optimize", None, backtest_cfg, strat)
    # Round-trip through JSON: the persisted optimization_config is a JSON column.
    backtest_cfg = json.loads(json.dumps(backtest_cfg, default=str))
    trial = _build_daily_trial_config(backtest_cfg, decode_params(strat, {}), None)
    assert trial["market_condition_profile"] == "ohlcv-v1"
    assert trial["market_condition_manifest"] == snapshot
    assert trial["_ga_trial"] is True
