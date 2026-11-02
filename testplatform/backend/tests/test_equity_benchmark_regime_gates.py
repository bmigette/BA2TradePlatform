"""goal2027 design 2.3: the SPY regime entry gates on the classic equity strategies S1-S7.

The option side (``test_launcher_benchmark_regime_gates.py``) built the gate: the classifier, the two
condition classes, the backtest calendar, the live publish and the deploy path, all strategy-agnostic.
This pins the equity side: ``--benchmark-regime-gates`` appends ``<kind>-spy-trend`` / ``<kind>-spy-vol``
LAST to every S1-S7 ENTRY tree (S1's three conviction tiers share one pair of genes), never to an exit
rule; flag off = byte-identical rules; the genes stay independent of the market-profile gene; SQM and a
bypass job are refused; a decoded genome exports the gate to the live ruleset.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # testplatform/backend
_LAUNCHER = os.path.normpath(os.path.join(_ROOT, "..", "ba2test_launcher.py"))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from ba2_common.core.rule_models import MODE_OFF  # noqa: E402
from ba2_common.core.rules_convert import trade_rules_to_live_export  # noqa: E402

from app.services.strategy_param_space import (  # noqa: E402
    collect_param_space, decode_params, market_profile_members, neutralise_market_profile_genes)

_spec = importlib.util.spec_from_file_location("ba2test_launcher_eq_bench", _LAUNCHER)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)

TREND = ["off", "up", "pullback", "bear_rally", "down", "above_200", "below_200"]
VOL = ["off", "calm", "normal", "stressed", "not_stressed", "not_calm"]
EQUITY = sorted(mod._EQUITY_MARKET_CONDITION_STRATEGIES)
FIELDS = ("spy_trend_regime", "spy_vol_regime")


@pytest.fixture
def gates_on(monkeypatch):
    monkeypatch.setattr(mod, "_BENCHMARK_REGIME_GATES", True)


def _built(kind, expert="FMPRating"):
    return mod._build_strategy(kind, f"eq-br-{kind}", expert)


def _entry_trees(strategy):
    return [r["conditions"]["conditions"] for r in (strategy.entry_rules or [])
            if isinstance(r.get("conditions"), dict) and r["conditions"].get("conditions")]


def _spy_leaves(node):
    out = []

    def walk(n):
        if isinstance(n, list):
            for x in n:
                walk(x)
        elif isinstance(n, dict):
            if n.get("field") in FIELDS:
                out.append(n)
            for v in n.values():
                walk(v)

    walk(node)
    return out


def _spy_genes(strategy):
    return sorted(g for g in collect_param_space(strategy) if "-spy-" in g)


@pytest.mark.parametrize("kind", EQUITY)
def test_flag_off_is_byte_identical_and_leaks_no_state(kind, monkeypatch):
    base = json.dumps([_built(kind).entry_rules, _built(kind).exit_rules], sort_keys=True, default=str)
    monkeypatch.setattr(mod, "_BENCHMARK_REGIME_GATES", True)
    assert json.dumps([_built(kind).entry_rules, _built(kind).exit_rules], sort_keys=True, default=str) != base
    monkeypatch.setattr(mod, "_BENCHMARK_REGIME_GATES", False)
    assert json.dumps([_built(kind).entry_rules, _built(kind).exit_rules], sort_keys=True, default=str) == base
    assert _spy_genes(_built(kind)) == []


@pytest.mark.parametrize("kind", EQUITY)
def test_every_entry_tree_ends_with_the_two_gates_and_nothing_else_moves(kind, monkeypatch):
    before = [[c.get("id") for c in t] for t in _entry_trees(_built(kind))]
    monkeypatch.setattr(mod, "_BENCHMARK_REGIME_GATES", True)
    after = [[c.get("id") for c in t] for t in _entry_trees(_built(kind))]
    k = kind.lower()
    assert after and [ids[-2:] for ids in after] == [[f"{k}-spy-trend", f"{k}-spy-vol"]] * len(after)
    assert [ids[:-2] for ids in after] == before


@pytest.mark.parametrize("kind", EQUITY)
def test_exit_rules_never_carry_the_gate(kind, gates_on):
    assert _spy_leaves(_built(kind).exit_rules) == []


@pytest.mark.parametrize("kind", EQUITY)
def test_two_categorical_genes_per_strategy(kind, gates_on):
    genes = collect_param_space(_built(kind))
    k = kind.lower()
    assert _spy_genes(_built(kind)) == [f"cond:{k}-spy-trend:mode", f"cond:{k}-spy-vol:mode"]
    assert genes[f"cond:{k}-spy-trend:mode"]["choices"] == TREND
    assert genes[f"cond:{k}-spy-vol:mode"]["choices"] == VOL


def test_s1_conviction_tiers_share_one_pair_of_genes(gates_on):
    s1 = _built("S1")
    assert len(_entry_trees(s1)) > 1
    assert len(_spy_leaves(s1.entry_rules)) == 2 * len(_entry_trees(s1))
    assert _spy_genes(s1) == ["cond:s1-spy-trend:mode", "cond:s1-spy-vol:mode"]


def test_gates_follow_the_market_condition_gates(monkeypatch):
    monkeypatch.setattr(mod, "_MARKET_CONDITION_PROFILES", ("ohlcv-v1",))
    monkeypatch.setattr(mod, "_BENCHMARK_REGIME_GATES", True)
    ids = [c.get("id") for c in _entry_trees(_built("S2"))[0]]
    assert ids[-2:] == ["s2-spy-trend", "s2-spy-vol"]
    assert any("-market-" in i for i in ids[:-2])


def test_the_market_profile_gene_never_switches_the_spy_gates(monkeypatch):
    monkeypatch.setattr(mod, "_MARKET_CONDITION_PROFILES", ("ohlcv-v1", "ta-structure-v1"))
    monkeypatch.setattr(mod, "_SEARCH_MARKET_PROFILE", True)
    monkeypatch.setattr(mod, "_BENCHMARK_REGIME_GATES", True)
    strat = _built("S2")
    members = market_profile_members(strat)
    assert not any("-spy-" in leaf for leaf in members["leaf_profile"])
    genome = {"market:profile": "off", "cond:s2-spy-trend:mode": "up"}
    assert neutralise_market_profile_genes(genome, members)["cond:s2-spy-trend:mode"] == "up"


def test_decode_resolves_a_mode_to_its_code_and_off_drops_the_leaf(gates_on):
    out = decode_params(_built("S6"), {"cond:s6-spy-trend:mode": "down", "cond:s6-spy-vol:mode": MODE_OFF})
    leaves = _spy_leaves(out["entry_rules"])
    assert [x["field"] for x in leaves] == ["spy_trend_regime"]
    assert leaves[0]["op"] == "==" and float(leaves[0]["value"]) == 4.0


def test_a_decoded_equity_gate_exports_to_the_live_enter_ruleset(gates_on):
    out = decode_params(_built("S2"), {"cond:s2-spy-trend:mode": "above_200", "cond:s2-spy-vol:mode": "calm"})
    export = trade_rules_to_live_export(out["entry_rules"], out["exit_rules"], name="eq-gated")
    enter = [t for rs in export["rulesets"] if rs["subtype"] == "enter_market"
             for rule in rs["rules"] for t in rule["triggers"].values()]
    fields = json.dumps(enter)
    assert "spy_trend_regime" in fields and "spy_vol_regime" in fields
    exits = [rs for rs in export["rulesets"] if rs["subtype"] != "enter_market"]
    assert "spy_trend_regime" not in json.dumps(exits)


@pytest.mark.parametrize("kind", EQUITY)
def test_apply_accepts_the_equity_strategies(kind, gates_on):
    block = {"ohlcv_history_source": "alpaca"}
    rec = mod._apply_benchmark_regime("optimize", block, _built(kind), kind)
    assert block["benchmark_regime_gates"] is True and rec["gene_count"] == 2


def test_apply_refuses_sqm(gates_on):
    with pytest.raises(SystemExit) as e:
        mod._apply_benchmark_regime("optimize", {"ohlcv_history_source": "alpaca"},
                                    mod._build_strategy("SQM", "eq-br-sqm", "DeterministicScorer"), "SQM")
    assert "SQM" in str(e.value)


def test_apply_refuses_a_job_whose_strategy_carries_no_gate(gates_on):
    """A bypass expert (FactorRanker) builds the minimal strategy: the flag would search nothing."""
    with pytest.raises(SystemExit) as e:
        mod._apply_benchmark_regime("optimize", {"ohlcv_history_source": "alpaca"},
                                    mod._build_strategy_minimal("eq-br-min"), "S1")
    assert "no SPY regime" in str(e.value)


def test_the_matrix_driver_gates_exactly_the_launchers_equity_strategies():
    tools = os.path.normpath(os.path.join(_ROOT, "..", "..", "tools"))
    if tools not in sys.path:
        sys.path.insert(0, tools)
    import run_screener_capband_matrix as M

    assert M.SPY_GATE_STRATEGIES == frozenset(mod._EQUITY_MARKET_CONDITION_STRATEGIES)
