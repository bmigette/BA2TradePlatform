"""goal2027 design 2026-11-10 parts 3 and 4: the pullback entry gates on S1-S7, the matrix flag that
carries them, and PullbackReversion's gene space.

Gates: ``--pullback-gates`` appends ``<kind>-pb-rsi`` (period gene) / ``-pb-below-high`` / ``-pb-vs-sma200`` LAST
to every S1-S7 ENTRY tree (S1's three tiers share one set of genes), never to an exit rule; flag off =
byte-identical rules; ``off`` mode reproduces the tree without the leaf; every leaf resolves to a
registered condition (an unmapped field is silently dropped by ``triggers_from_condition_tree``).
"""
from __future__ import annotations

import importlib.util
import itertools
import json
import os
import sys

import numpy as np
import pandas as pd
import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # testplatform/backend
_LAUNCHER = os.path.normpath(os.path.join(_ROOT, "..", "ba2test_launcher.py"))
_TOOLS = os.path.normpath(os.path.join(_ROOT, "..", "..", "tools"))
for _p in (_ROOT, _TOOLS, os.path.dirname(os.path.abspath(__file__))):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from ba2_common.core.rule_builders import triggers_from_condition_tree  # noqa: E402
from ba2_common.core.rule_models import MODE_OFF  # noqa: E402
from ba2_common.core.market_condition_templates import PULLBACK_GATE_SPECS, PULLBACK_RSI_FIELDS  # noqa: E402

from app.services.strategy_param_space import (  # noqa: E402
    _collect_expert, collect_param_space, decode_params)

import run_screener_capband_matrix as M  # noqa: E402
from test_matrix_walk_forward import _flag, _plain_argv, env  # noqa: E402,F401

_spec = importlib.util.spec_from_file_location("ba2test_launcher_pullback", _LAUNCHER)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)

EQUITY = sorted(mod._EQUITY_MARKET_CONDITION_STRATEGIES)
FIELDS = {spec[1] for spec in PULLBACK_GATE_SPECS} | set(PULLBACK_RSI_FIELDS)
SUFFIXES = [spec[0] for spec in PULLBACK_GATE_SPECS]


@pytest.fixture
def gates_on(monkeypatch):
    monkeypatch.setattr(mod, "_PULLBACK_GATES", True)


def _built(kind, expert="FMPRating"):
    return mod._build_strategy(kind, f"eq-pb-{kind}", expert)


def _entry_trees(strategy):
    return [r["conditions"]["conditions"] for r in (strategy.entry_rules or [])
            if isinstance(r.get("conditions"), dict) and r["conditions"].get("conditions")]


def _pb_leaves(node):
    out = []

    def walk(n):
        if isinstance(n, list):
            for x in n:
                walk(x)
        elif isinstance(n, dict):
            if str(n.get("id", "")).find("-pb-") >= 0:
                out.append(n)
            for v in n.values():
                walk(v)

    walk(node)
    return out


def _pb_genes(strategy):
    return sorted(g for g in collect_param_space(strategy) if "-pb-" in g)


def _dump(strategy):
    return json.dumps([strategy.entry_rules, strategy.exit_rules], sort_keys=True, default=str)


@pytest.mark.parametrize("kind", EQUITY)
def test_flag_off_is_byte_identical_and_leaks_no_state(kind, monkeypatch):
    base = _dump(_built(kind))
    monkeypatch.setattr(mod, "_PULLBACK_GATES", True)
    assert _dump(_built(kind)) != base
    monkeypatch.setattr(mod, "_PULLBACK_GATES", False)
    assert _dump(_built(kind)) == base
    assert _pb_genes(_built(kind)) == []


@pytest.mark.parametrize("kind", EQUITY)
def test_every_entry_tree_ends_with_the_six_gates_and_nothing_else_moves(kind, monkeypatch):
    before = [[c.get("id") for c in t] for t in _entry_trees(_built(kind))]
    monkeypatch.setattr(mod, "_PULLBACK_GATES", True)
    after = [[c.get("id") for c in t] for t in _entry_trees(_built(kind))]
    k = kind.lower()
    want = [f"{k}-{s}" for s in SUFFIXES]
    assert after and [ids[-len(want):] for ids in after] == [want] * len(after)
    assert [ids[:-len(want)] for ids in after] == before


@pytest.mark.parametrize("kind", EQUITY)
def test_exit_rules_never_carry_the_gates(kind, gates_on):
    assert _pb_leaves(_built(kind).exit_rules) == []


@pytest.mark.parametrize("kind", EQUITY)
def test_mode_and_value_genes_per_leaf(kind, gates_on):
    genes = collect_param_space(_built(kind))
    k = kind.lower()
    assert SUFFIXES == ["pb-rsi", "pb-below-high", "pb-vs-sma200"]
    assert _pb_genes(_built(kind)) == sorted(
        [f"cond:{k}-{s}:mode" for s in SUFFIXES] + [f"cond:{k}-{s}:value" for s in SUFFIXES]
        + [f"cond:{k}-pb-rsi:field"])
    assert not [g for g in genes if "-pb-rsi" in g and any(g.startswith(f"cond:{k}-pb-rsi{p}:") for p in "2345")]
    assert genes[f"cond:{k}-pb-rsi:field"]["choices"] == list(PULLBACK_RSI_FIELDS)
    for s in SUFFIXES:
        assert genes[f"cond:{k}-{s}:mode"]["choices"] == ["off", "below", "above"]


def test_s1_conviction_tiers_share_one_set_of_genes(gates_on):
    s1 = _built("S1")
    trees = _entry_trees(s1)
    assert len(trees) == 3
    assert len(_pb_leaves(s1.entry_rules)) == len(SUFFIXES) * 3
    assert len(_pb_genes(s1)) == 2 * len(SUFFIXES) + 1   # not 3x (mode+value per leaf, one field gene)


def test_decode_off_reproduces_the_tree_without_the_leaf(monkeypatch):
    plain = _built("S2")
    monkeypatch.setattr(mod, "_PULLBACK_GATES", True)
    gated = _built("S2")
    flat = {f"cond:s2-{s}:mode": MODE_OFF for s in SUFFIXES}
    out = decode_params(gated, flat)
    assert _pb_leaves(out["entry_rules"]) == []
    ids = lambda rules: [[c.get("id") for c in r["conditions"]["conditions"]]           # noqa: E731
                         for r in rules if isinstance(r.get("conditions"), dict) and r["conditions"].get("conditions")]
    assert ids(out["entry_rules"]) == ids(plain.entry_rules)


def test_decode_resolves_a_mode_to_an_operator(gates_on):
    flat = {f"cond:s6-{s}:mode": MODE_OFF for s in SUFFIXES}
    flat["cond:s6-pb-below-high:mode"] = "above"
    flat["cond:s6-pb-below-high:value"] = 8.0
    flat["cond:s6-pb-rsi:mode"] = "below"
    flat["cond:s6-pb-rsi:value"] = 15.0
    flat["cond:s6-pb-rsi:field"] = "pullback_rsi_3"
    out = decode_params(_built("S6"), flat)
    leaves = {x["id"]: x for x in _pb_leaves(out["entry_rules"])}
    assert set(leaves) == {"s6-pb-below-high", "s6-pb-rsi"}
    assert leaves["s6-pb-below-high"]["op"] == ">" and leaves["s6-pb-below-high"]["value"] == 8.0
    rsi = leaves["s6-pb-rsi"]
    assert rsi["op"] == "<" and rsi["value"] == 15.0 and rsi["field"] == "pullback_rsi_3"


@pytest.mark.parametrize("field", PULLBACK_RSI_FIELDS)
def test_each_field_choice_decodes_to_a_concrete_leaf(field, gates_on):
    flat = {f"cond:s2-{s}:mode": MODE_OFF for s in SUFFIXES}
    flat.update({"cond:s2-pb-rsi:mode": "above", "cond:s2-pb-rsi:value": 30.0, "cond:s2-pb-rsi:field": field})
    out = decode_params(_built("S2"), flat)
    (leaf,) = {id(x): x for x in _pb_leaves(out["entry_rules"])}.values()
    assert leaf["field"] == field and leaf["op"] == ">"
    assert not [k for k in leaf if k in ("field_optimize", "field_choices", "fieldOptimize", "fieldChoices", "mode_optimize", "mode_choices", "modeOptimize", "modeChoices")]


def test_a_field_gene_given_as_an_index_resolves_and_off_removes_the_leaf(gates_on):
    flat = {f"cond:s2-{s}:mode": MODE_OFF for s in SUFFIXES}
    flat.update({"cond:s2-pb-rsi:mode": "below", "cond:s2-pb-rsi:value": 10.0, "cond:s2-pb-rsi:field": 2})
    out = decode_params(_built("S2"), flat)
    assert {x["field"] for x in _pb_leaves(out["entry_rules"])} == {"pullback_rsi_4"}
    flat["cond:s2-pb-rsi:mode"] = MODE_OFF
    assert _pb_leaves(decode_params(_built("S2"), flat)["entry_rules"]) == []


@pytest.mark.parametrize("bad", ["pullback_rsi_9", 7, -1, True, None, 2.5])
def test_an_invalid_field_choice_raises(bad, gates_on):
    flat = {f"cond:s2-{s}:mode": MODE_OFF for s in SUFFIXES}
    flat.update({"cond:s2-pb-rsi:mode": "below", "cond:s2-pb-rsi:value": 10.0, "cond:s2-pb-rsi:field": bad})
    with pytest.raises(ValueError):
        decode_params(_built("S2"), flat)


def test_an_undecoded_template_leaf_is_refused_by_the_live_resolver_check(gates_on):
    from ba2_common.core.market_condition_rules import _unresolved_reason

    (leaf,) = [x for x in _pb_leaves(_built("S2").entry_rules) if x["id"].endswith("pb-rsi")][:1]
    leaf = dict(leaf)
    leaf.pop("mode_optimize"), leaf.pop("modeOptimize", None)
    assert "field_choices" in _unresolved_reason(leaf)


def test_memo_key_drops_the_inert_field_gene_of_an_off_leaf(gates_on):
    from app.services.strategy_optimization_handler import canonical_trial_params, mode_anchor_index

    strat = _built("S2")
    anchors = mode_anchor_index(strat)
    base = {f"cond:s2-{s}:mode": MODE_OFF for s in SUFFIXES}
    keys = {tuple(sorted(canonical_trial_params(anchors, {**base, "cond:s2-pb-rsi:field": f}).items()))
            for f in PULLBACK_RSI_FIELDS}
    assert len(keys) == 1
    on = {**base, "cond:s2-pb-rsi:mode": "below"}
    assert len({tuple(sorted(canonical_trial_params(anchors, {**on, "cond:s2-pb-rsi:field": f}).items()))
                for f in PULLBACK_RSI_FIELDS}) == 4


@pytest.mark.parametrize("kind", EQUITY)
def test_every_pullback_leaf_resolves_to_a_trigger(kind, gates_on):
    """No silent drop: with every mode ON, the converted rule carries one trigger per pullback field."""
    k = kind.lower()
    flat = {}
    for s in SUFFIXES:
        flat[f"cond:{k}-{s}:mode"] = "above"
    # every RSI period resolves, one decode per choice
    for rsi_field in PULLBACK_RSI_FIELDS:
        flat[f"cond:{k}-pb-rsi:field"] = rsi_field
        out = decode_params(_built(kind), flat)
        want = (FIELDS - set(PULLBACK_RSI_FIELDS)) | {rsi_field}
        for rule in out["entry_rules"]:
            tree = rule.get("conditions")
            if not (isinstance(tree, dict) and tree.get("conditions")):
                continue
            leaf_fields = {x["field"] for x in _pb_leaves(tree)}
            assert leaf_fields == want
            events = {t["event_type"] for t in triggers_from_condition_tree(tree).values()}
            assert want <= events


def test_gates_follow_the_spy_and_market_gates(monkeypatch):
    monkeypatch.setattr(mod, "_MARKET_CONDITION_PROFILES", ("ohlcv-v1",))
    monkeypatch.setattr(mod, "_BENCHMARK_REGIME_GATES", True)
    monkeypatch.setattr(mod, "_PULLBACK_GATES", True)
    ids = [c.get("id") for c in _entry_trees(_built("S2"))[0]]
    assert ids[-len(SUFFIXES) - 2:-len(SUFFIXES)] == ["s2-spy-trend", "s2-spy-vol"]
    assert ids[-len(SUFFIXES):] == [f"s2-{s}" for s in SUFFIXES]


def test_check_refuses_a_job_that_would_carry_no_gate(gates_on):
    with pytest.raises(SystemExit):
        mod._check_pullback_gates("optimize", _built("S2"), "O_LC")
    mod._check_pullback_gates("optimize", _built("S2"), "S2")          # fine
    from types import SimpleNamespace
    with pytest.raises(SystemExit):
        mod._check_pullback_gates("optimize", SimpleNamespace(entry_rules=[]), "S2")


def test_check_is_a_noop_with_the_flag_off():
    mod._check_pullback_gates("optimize", object(), "O_LC")


# ---------------------------------------------------------------- the matrix flag


def _argv(env, *extra, suffix="-goal2027-pullback"):
    argv = _plain_argv(env, *extra)
    return [f"--name-suffix={suffix}" if a.startswith("--name-suffix=") else a for a in argv]


def test_flag_without_the_pullback_suffix_is_refused(env, monkeypatch):
    monkeypatch.setattr(sys, "argv", _argv(env, "--pullback-gates", suffix="-goal2027"))
    with pytest.raises(SystemExit):
        M.main()


def test_flag_reaches_classic_jobs_only_and_off_is_unchanged(env, monkeypatch):
    monkeypatch.setattr(sys, "argv", _argv(env))
    assert M.main() == 0
    plain = list(env.calls[-1])
    assert "--pullback-gates" not in plain
    monkeypatch.setattr(sys, "argv", _argv(env, "--pullback-gates"))
    assert M.main() == 0
    gated = env.calls[-1]
    assert "--pullback-gates" in gated
    assert _flag(gated, "--name") != _flag(plain, "--name")          # identity differs
    assert [a for a in gated if a != "--pullback-gates"] != plain    # (name digest differs too)


def test_pullback_strategies_adds_the_expert_and_its_absence_adds_nothing(env, monkeypatch):
    skip_all = ["--skip-experts", "FMPRating,DeterministicScorer,FMPEarningsDrift,FMPInsiderClusterBuy,FactorRanker"]
    base = ["run_screener_capband_matrix.py", "--bands", "mid", *skip_all, "--strategy-plan", env.plan,
            "--name-suffix=-goal2027-pullback"]
    before = len(env.calls)
    monkeypatch.setattr(sys, "argv", base)
    assert M.main() == 0
    assert len(env.calls) == before                                   # nothing runs without the flag
    monkeypatch.setattr(sys, "argv", base + ["--pullback-strategies", "S2", "--pullback-gates"])
    assert M.main() == 0
    (cmd,) = env.calls[before:]
    assert _flag(cmd, "--expert") == "PullbackReversion" and _flag(cmd, "--strategy") == "S2"
    assert "--pullback-gates" in cmd
    assert _flag(cmd, "--name").startswith("scr-mid-PullbackReversion-S2-goal2027-pullback")


def test_unknown_pullback_strategy_is_refused(env, monkeypatch):
    monkeypatch.setattr(sys, "argv", _argv(env, "--pullback-strategies", "S9"))
    with pytest.raises(SystemExit):
        M.main()


# ---------------------------------------------------------------- PullbackReversion gene space


def _bars(n, seed):
    rng = np.random.default_rng(seed)
    close = 100 * np.exp(np.cumsum(rng.normal(0.0004, 0.01, n)))
    idx = pd.bdate_range(end="2024-06-14", periods=n, tz="UTC")
    return pd.DataFrame({"Open": close, "High": close * 1.01, "Low": close * 0.99, "Close": close,
                         "Volume": 1e6}, index=idx)


def test_pullback_reversion_gene_space_decodes_to_settings_the_expert_accepts():
    from ba2_experts.PullbackReversion import PullbackReversion, pullback_signal

    spec = mod._EXPERT_OPT["PullbackReversion"]
    genes = _collect_expert(spec["expert_params"])
    assert sorted(genes) == ["model:entry_threshold", "model:exit_mode", "model:rsi_exit",
                             "model:rsi_period", "model:trend_gate"]
    defs = PullbackReversion.get_settings_definitions()
    assert spec["fixed_settings"]["direction"] == "long"
    bars, spy = _bars(400, 1), _bars(400, 2)

    def values(g):
        if g["type"] == "choice":
            return list(g["choices"])
        step = g["step"]
        n = int(round((g["max"] - g["min"]) / step))
        return [g["min"] + i * step for i in range(n + 1)]

    per_gene = {k.split(":", 1)[1]: values(g) for k, g in genes.items()}
    for name, vals in per_gene.items():
        valid = defs[name].get("valid_values")
        if valid:
            assert set(vals) == set(valid)           # the choice list is exactly the expert's own
    base = {"direction": "long", "trend_gate": "sma200", "rsi_period": 2, "entry_threshold": 5.0,
            "exit_mode": "sma5", "rsi_exit": 70.0}
    # every value of every gene, and every pairwise corner, is accepted by the expert's own validation
    for name, vals in per_gene.items():
        for v in vals:
            sig = pullback_signal(bars, {**base, name: v}, spy)
            assert sig["action"] in ("entry", "exit", "none")
    for combo in itertools.product(per_gene["trend_gate"], per_gene["exit_mode"],
                                   (per_gene["rsi_period"][0], per_gene["rsi_period"][-1]),
                                   (per_gene["entry_threshold"][0], per_gene["entry_threshold"][-1])):
        s = {**base, "trend_gate": combo[0], "exit_mode": combo[1], "rsi_period": combo[2],
             "entry_threshold": combo[3]}
        pullback_signal(bars, s, spy)


# ---- daily-entry experts (PullbackReversion): every weekday, no weekday genes -----------------------

def test_daily_entry_expert_has_no_weekday_genes_and_scans_every_weekday():
    import ba2test_launcher as L

    pb = L._EXPERT_OPT["PullbackReversion"]
    fr = L._EXPERT_OPT["FMPRating"]
    assert L._daily_entry(pb) and not L._daily_entry(fr)
    assert L._weekday_schedule_genes(pb) == {}
    assert sorted(L._weekday_schedule_genes(fr)) == sorted(
        f"schedule:{d}" for d in ("monday", "tuesday", "wednesday", "thursday", "friday"))

    monday_only = {"days": {d: d == "monday" for d in
                            ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")},
                   "times": ["10:00"]}
    out = L._entry_run_schedule(pb, monday_only)
    assert out["days"] == {"monday": True, "tuesday": True, "wednesday": True, "thursday": True,
                           "friday": True, "saturday": False, "sunday": False}
    assert out["times"] == ["10:00"]
    assert monday_only["days"]["tuesday"] is False          # input not mutated
    assert L._entry_run_schedule(fr, monday_only) is monday_only


def test_daily_entry_expert_refuses_a_run_without_a_pinned_schedule():
    import ba2test_launcher as L
    import pytest as _pytest

    with _pytest.raises(SystemExit):
        L._entry_run_schedule(L._EXPERT_OPT["PullbackReversion"], None)
    assert L._entry_run_schedule(L._EXPERT_OPT["FMPRating"], None) is None
