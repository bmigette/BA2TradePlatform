"""``--benchmark-regime-gates``: the opt-in SPY trend/vol entry gates on the pure option structures.

Pins, in the order the failures would bite: flag off = the rule and the genes every existing run
has (no state leaks between a flag-on and a flag-off build in one process); flag on = exactly two
leaves per member, LAST in the entry AND list (nothing moves); the genes are categorical mode genes
in persisted code order; the GA decode resolves them with NO market-condition profile and NO
manifest; refused for any key whose builders emit no such leaves; the backtest block of the run
records the gate only when on.
"""
from __future__ import annotations

import importlib.util
import inspect
import json
import os
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # testplatform/backend
_LAUNCHER = os.path.normpath(os.path.join(_ROOT, "..", "ba2test_launcher.py"))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from ba2_common.core.rule_models import MODE_OFF  # noqa: E402

from app.services.strategy_param_space import collect_param_space, decode_params  # noqa: E402

_spec = importlib.util.spec_from_file_location("ba2test_launcher_bench", _LAUNCHER)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)

TREND = ["off", "up", "pullback", "bear_rally", "down", "above_200", "below_200"]
VOL = ["off", "calm", "normal", "stressed", "not_stressed", "not_calm"]
KINDS = ["O_LC", "O_BULLPS", "O_CSP", "O_IC", "O_WHEEL", "OS1"]
FIELDS = ("spy_trend_regime", "spy_vol_regime")


@pytest.fixture
def gates_on(monkeypatch):
    monkeypatch.setattr(mod, "_BENCHMARK_REGIME_GATES", True)


def _built(kind):
    return mod._build_strategy(kind, f"br-{kind}", "FMPRating")


def _entry_ids(strategy):
    trees = [r for r in (strategy.entry_rules or [])
             if isinstance(r.get("conditions"), dict) and r["conditions"].get("conditions")]
    return [[c.get("id") for c in t["conditions"]["conditions"]] for t in trees]


def _spy_genes(strategy):
    return sorted(g for g in collect_param_space(strategy) if "-spy-" in g)


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


@pytest.mark.parametrize("kind", KINDS)
def test_flag_off_rule_is_identical_and_leaks_no_state(kind, monkeypatch):
    baseline = json.dumps(_built(kind).entry_rules, sort_keys=True, default=str)
    monkeypatch.setattr(mod, "_BENCHMARK_REGIME_GATES", True)
    gated = json.dumps(_built(kind).entry_rules, sort_keys=True, default=str)
    assert gated != baseline
    monkeypatch.setattr(mod, "_BENCHMARK_REGIME_GATES", False)
    assert json.dumps(_built(kind).entry_rules, sort_keys=True, default=str) == baseline
    assert _spy_genes(_built(kind)) == []


@pytest.mark.parametrize("kind", ["O_LC", "O_CSP", "O_IC", "O_BULLPS"])
def test_flag_on_adds_exactly_two_leaves_last(kind, monkeypatch):
    baseline = _entry_ids(_built(kind))
    monkeypatch.setattr(mod, "_BENCHMARK_REGIME_GATES", True)
    after = _entry_ids(_built(kind))
    p = kind.lower()
    assert [ids[-2:] for ids in after] == [[f"{p}-spy-trend", f"{p}-spy-vol"]] * len(after)
    assert [ids[:-2] for ids in after] == baseline


def test_market_condition_gates_stay_before_the_spy_gates(monkeypatch):
    monkeypatch.setattr(mod, "_MARKET_CONDITION_PROFILES", ("ohlcv-v1",))
    monkeypatch.setattr(mod, "_BENCHMARK_REGIME_GATES", True)
    ids, = _entry_ids(_built("O_LC"))
    assert ids[-5:] == ["o_lc-market-slope", "o_lc-market-adx", "o_lc-market-rv",
                        "o_lc-spy-trend", "o_lc-spy-vol"]


def test_group_members_get_their_own_distinct_genes(gates_on):
    genes = _spy_genes(_built("OS1"))
    assert genes and len(genes) == len(set(genes))
    members = {g.split(":")[1].rsplit("-spy-", 1)[0] for g in genes}
    assert len(members) > 1
    for m in members:
        assert f"cond:{m}-spy-trend:mode" in genes and f"cond:{m}-spy-vol:mode" in genes


def test_gene_names_and_choices(gates_on):
    genes = collect_param_space(_built("O_CSP"))
    assert genes["cond:o_csp-spy-trend:mode"]["choices"] == TREND
    assert genes["cond:o_csp-spy-vol:mode"]["choices"] == VOL
    assert not any("-spy-" in g and g.endswith(":value") for g in genes)


@pytest.mark.parametrize("kind", ["O_LC", "O_CSP", "O_IC", "O_WHEEL"])
def test_gene_count_added_is_two_per_member(kind, monkeypatch):
    before = set(collect_param_space(_built(kind)))
    monkeypatch.setattr(mod, "_BENCHMARK_REGIME_GATES", True)
    after = set(collect_param_space(_built(kind)))
    assert len(after - before) == 2 and not (before - after)


def test_wheel_leaves_are_renamed(gates_on):
    assert [x["id"] for x in _spy_leaves(_built("O_WHEEL").entry_rules)] == [
        "o_wheel-spy-trend", "o_wheel-spy-vol"]
    assert _spy_genes(_built("O_CSP")) == ["cond:o_csp-spy-trend:mode", "cond:o_csp-spy-vol:mode"]


def test_gates_off_strips_them(gates_on, monkeypatch):
    monkeypatch.setattr(mod, "_OPTION_GATES_OFF", True)
    assert _spy_genes(_built("O_LC")) == []
    assert _spy_leaves(_built("O_LC").entry_rules) == []


def test_decode_off_removes_leaf_and_non_off_resolves_to_code(gates_on):
    strat = _built("O_CSP")
    genome = {"cond:o_csp-spy-trend:mode": "above_200", "cond:o_csp-spy-vol:mode": MODE_OFF}
    decoded = decode_params(strat, genome)
    leaves = _spy_leaves(decoded["entry_rules"])
    assert [x["field"] for x in leaves] == ["spy_trend_regime"]      # the vol leaf is gone
    leaf = leaves[0]
    assert leaf["op"] == "==" and float(leaf["value"]) == 5.0 and leaf["mode"] == "above_200"


@pytest.mark.parametrize("kind", ["S1", "O_CC", "O_PP", "O_STK"])
def test_refused_for_non_pure_option_kinds(kind, gates_on):
    with pytest.raises(SystemExit) as e:
        mod._apply_benchmark_regime("optimize", {"ohlcv_history_source": "fmp"},
                                    _built("O_LC"), kind)
    assert kind in str(e.value)


def test_refused_without_a_history_source(gates_on):
    with pytest.raises(SystemExit):
        mod._apply_benchmark_regime("optimize", {}, _built("O_LC"), "O_LC")


def test_apply_records_block_only_when_on(monkeypatch, capsys):
    block = {"ohlcv_history_source": "alpaca"}
    assert mod._apply_benchmark_regime("optimize", block, _built("O_LC"), "O_STK") == {}
    assert block == {"ohlcv_history_source": "alpaca"}
    monkeypatch.setattr(mod, "_BENCHMARK_REGIME_GATES", True)
    rec = mod._apply_benchmark_regime("optimize", block, _built("O_LC"), "O_LC")
    assert block["benchmark_regime_gates"] is True
    assert block["benchmark_regime"] == rec
    assert rec["symbol"] == "SPY" and rec["history_source"] == "alpaca"
    assert rec["genes"] == ["cond:o_lc-spy-trend:mode", "cond:o_lc-spy-vol:mode"]
    assert rec["gene_count"] == 2
    assert "alpaca" in capsys.readouterr().out


def test_resolve_sets_and_resets_the_global(monkeypatch):
    monkeypatch.setattr(mod, "_BENCHMARK_REGIME_GATES", False)
    assert mod._resolve_benchmark_regime_gates(True, "optimize") is True
    assert mod._BENCHMARK_REGIME_GATES is True
    assert mod._resolve_benchmark_regime_gates(None, "optimize") is False
    assert mod._BENCHMARK_REGIME_GATES is False


def test_cli_flag_exists_default_off_and_both_commands_wire_it():
    import argparse

    p = argparse.ArgumentParser()
    mod._add_market_condition_args(p)
    assert p.parse_args([]).benchmark_regime_gates is False
    assert p.parse_args(["--benchmark-regime-gates"]).benchmark_regime_gates is True
    for fn in (mod._cmd_optimize, mod._cmd_optimize_batch):
        src = inspect.getsource(fn)
        assert src.count("_resolve_benchmark_regime_gates(") == 1, fn.__name__
        assert src.count("_apply_benchmark_regime(") == 1, fn.__name__
