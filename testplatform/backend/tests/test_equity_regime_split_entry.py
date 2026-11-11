"""``--regime-split-entry``: ONE equity ruleset with a SPY-above-200 section and a SPY-below-200 section.

Every initial-entry rule becomes UP (``spy-trend`` pinned ``above_200``) then DOWN (pinned ``below_200``); the DOWN
copy owns ``-dn`` genes for the leaves the scope separates (context < entry < all). Flag off = today's strategy and
gene space; the flag needs ``--benchmark-regime-gates``.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # testplatform/backend
_LAUNCHER = os.path.normpath(os.path.join(_ROOT, "..", "ba2test_launcher.py"))
_TOOLS = os.path.normpath(os.path.join(_ROOT, "..", "..", "tools"))
for _p in (_ROOT, _TOOLS, os.path.dirname(os.path.abspath(__file__))):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from ba2_common.core import benchmark_regime as br  # noqa: E402
from ba2_common.core.market_condition_rules import _unresolved_reason  # noqa: E402
from ba2_common.core.rule_builders import triggers_from_condition_tree  # noqa: E402
from ba2_common.core.rule_models import MODE_OFF  # noqa: E402
from ba2_common.core.TradeConditions import SpyTrendRegimeCondition  # noqa: E402

from app.services.strategy_param_space import (  # noqa: E402
    authored_genome, collect_param_space, decode_params)

import run_screener_capband_matrix as M  # noqa: E402
from test_matrix_walk_forward import _flag, _plain_argv, env  # noqa: E402,F401

_spec = importlib.util.spec_from_file_location("ba2test_launcher_regime_split", _LAUNCHER)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)

EQUITY = sorted(mod._EQUITY_MARKET_CONDITION_STRATEGIES)
SCOPES = ("context", "entry", "all")
TREND_ABOVE = float(br.TREND_MODE_CODES["above_200"])
TREND_BELOW = float(br.TREND_MODE_CODES["below_200"])


@pytest.fixture
def gates(monkeypatch):
    monkeypatch.setattr(mod, "_BENCHMARK_REGIME_GATES", True)
    monkeypatch.setattr(mod, "_PULLBACK_GATES", True)
    monkeypatch.setattr(mod, "_REGIME_SPLIT_ENTRY", None)


def _built(kind, scope=None, expert="FMPRating", monkeypatch=None):
    mod._REGIME_SPLIT_ENTRY = scope
    try:
        return mod._build_strategy(kind, f"eq-rs-{kind}", expert)
    finally:
        mod._REGIME_SPLIT_ENTRY = None


def _genes(kind, scope=None, expert="FMPRating"):
    return set(collect_param_space(_built(kind, scope, expert)))


def _dump(strategy):
    return json.dumps([strategy.entry_rules, strategy.exit_rules], sort_keys=True, default=str)


def _leaves(node, out=None):
    out = [] if out is None else out
    if isinstance(node, list):
        for x in node:
            _leaves(x, out)
    elif isinstance(node, dict):
        if node.get("field"):
            out.append(node)
        _leaves(node.get("conditions"), out)
    return out


def _trend(rule):
    (leaf,) = [x for x in _leaves(rule["conditions"]) if x["field"] == "spy_trend_regime"]
    return leaf


def _rules_with_tree(strategy):
    return [r for r in strategy.entry_rules if isinstance(r.get("conditions"), dict) and r["conditions"].get("conditions")]


# ---------------------------------------------------------------- 1. flag off


@pytest.mark.parametrize("kind", EQUITY)
def test_flag_off_is_identical_and_leaks_no_state(kind, gates):
    base = _dump(_built(kind))
    base_genes = _genes(kind)
    assert _dump(_built(kind, "context")) != base
    assert _dump(_built(kind)) == base
    assert _genes(kind) == base_genes
    assert not [g for g in base_genes if "-dn" in g]
    assert not [r for r in _built(kind).entry_rules if "gene_rule_id" in r or r["id"].endswith("-dn")]


def test_resolve_none_is_off(gates):
    assert mod._resolve_regime_split_entry(None, "optimize") is None
    assert mod._REGIME_SPLIT_ENTRY is None


# ---------------------------------------------------------------- 2. needs the SPY gates


@pytest.mark.parametrize("scope", SCOPES)
def test_flag_without_benchmark_regime_gates_exits_loudly(scope, monkeypatch):
    monkeypatch.setattr(mod, "_BENCHMARK_REGIME_GATES", False)
    monkeypatch.setattr(mod, "_REGIME_SPLIT_ENTRY", None)
    with pytest.raises(SystemExit) as e:
        mod._resolve_regime_split_entry(scope, "optimize")
    assert "--benchmark-regime-gates" in str(e.value)
    assert mod._REGIME_SPLIT_ENTRY is None


def test_unknown_scope_exits(gates):
    with pytest.raises(SystemExit):
        mod._resolve_regime_split_entry("everything", "optimize")


def test_check_refuses_non_equity_and_a_strategy_with_no_down_rule(gates, monkeypatch):
    from types import SimpleNamespace
    split = _built("S2", "context")
    monkeypatch.setattr(mod, "_REGIME_SPLIT_ENTRY", "context")
    with pytest.raises(SystemExit):
        mod._check_regime_split_entry("optimize", split, "O_LC")
    with pytest.raises(SystemExit):
        mod._check_regime_split_entry("optimize", SimpleNamespace(entry_rules=[]), "S2")
    mod._check_regime_split_entry("optimize", split, "S2")      # fine
    monkeypatch.setattr(mod, "_REGIME_SPLIT_ENTRY", None)
    mod._check_regime_split_entry("optimize", object(), "O_LC")                  # flag off: no-op


def test_apply_records_the_scope_only_when_on(gates, monkeypatch):
    block = {}
    mod._apply_regime_split(block)
    assert block == {}
    monkeypatch.setattr(mod, "_REGIME_SPLIT_ENTRY", "entry")
    mod._apply_regime_split(block)
    assert block == {"regime_split_entry": "entry"}


# ---------------------------------------------------------------- 3. structure and gene sets per scope


def _new(kind, scope):
    return _genes(kind, scope) - _genes(kind)


def _dn(genes):
    return {g for g in genes if "-dn" in g}


@pytest.mark.parametrize("kind,n_in", [("S1", 3), ("S2", 1)])
@pytest.mark.parametrize("scope", SCOPES)
def test_rules_are_doubled_up_then_down_with_the_trend_pinned(kind, n_in, scope, gates):
    before = _rules_with_tree(_built(kind))
    after = _built(kind, scope).entry_rules
    assert len(before) == n_in and len(after) == 2 * n_in
    for i, rule in enumerate(before):
        up, down = after[2 * i], after[2 * i + 1]
        assert up["id"] == rule["id"] and down["id"] == f"{rule['id']}-dn"
        assert up["name"].endswith(" [SPY up]") and down["name"].endswith(" [SPY down]")
        for r, want in ((up, TREND_ABOVE), (down, TREND_BELOW)):
            leaf = _trend(r)
            assert leaf["op"] == "==" and leaf["value"] == want
            assert not leaf.get("mode_optimize") and not leaf.get("mode_choices")
            assert _unresolved_reason(leaf) == ""
    assert not [g for g in _genes(kind, scope) if "spy-trend" in g]
    assert "cond:%s-spy-trend:mode" % kind.lower() in _genes(kind)        # it WAS a gene before
    # every rule still converts to a trigger on the SPY trend (no silent drop of a pinned leaf)
    for r in after:
        events = {t["event_type"] for t in triggers_from_condition_tree(r["conditions"]).values()}
        assert any("spy_trend" in str(e).lower() for e in events)


def test_gene_sets_per_scope_s2(gates):
    k = "s2"
    ctx = {f"cond:{k}-dn-pb-rsi:{x}" for x in ("mode", "value", "field")} | {
        f"cond:{k}-dn-pb-below-high:{x}" for x in ("mode", "value")} | {
        f"cond:{k}-dn-pb-vs-sma200:{x}" for x in ("mode", "value")} | {f"cond:{k}-dn-spy-vol:mode"}
    sig = {f"cond:{c}-dn:{x}" for c in ("gate_confidence", "gate_expected_profit", "gate_days_since_close",
                                         "gate_days_since_profit", "gate_days_since_loss") for x in ("value", "enabled")}
    assert _new("S2", "context") == ctx
    assert _new("S2", "entry") == ctx | sig
    assert _new("S2", "all") == ctx | sig            # S2's entry rule has no optimizable action or toggle of its own


def test_gene_sets_per_scope_s1_tiers_share_dn_ids(gates):
    ctx = _new("S1", "context")
    assert ctx == {"cond:s1-dn-pb-rsi:mode", "cond:s1-dn-pb-rsi:value", "cond:s1-dn-pb-rsi:field",
                   "cond:s1-dn-pb-below-high:mode", "cond:s1-dn-pb-below-high:value",
                   "cond:s1-dn-pb-vs-sma200:mode", "cond:s1-dn-pb-vs-sma200:value", "cond:s1-dn-spy-vol:mode"}
    entry, full = _new("S1", "entry"), _new("S1", "all")
    sig = entry - ctx
    assert sig and all(g.startswith("cond:u") and "-dn:" in g for g in sig)
    assert ctx < entry < full
    own = full - entry
    assert own and all(g.startswith("entry:buy-") and "-dn:" in g for g in own)
    assert {f"entry:buy-{n}-dn:enabled" for n in (1, 2, 3)} <= own
    assert {f"entry:buy-{n}-dn:a{a}:action_value" for n in (1, 2, 3) for a in (1, 2)} <= own
    # context / entry never fork the rule or its action genes
    assert not [g for g in entry if g.startswith("entry:")]


@pytest.mark.parametrize("kind", EQUITY)
def test_scopes_nest_and_only_the_trend_gene_disappears(kind, gates):
    base = _genes(kind)
    c, e, a = (_genes(kind, s) for s in SCOPES)
    assert base - c == {f"cond:{kind.lower()}-spy-trend:mode"} == base - e == base - a
    assert _dn(c) < _dn(e) <= _dn(a)
    assert (c - base) <= _dn(c) and (e - base) <= _dn(e) and (a - base) <= _dn(a)


def test_market_condition_leaves_get_their_own_down_genes(gates, monkeypatch):
    monkeypatch.setattr(mod, "_MARKET_CONDITION_PROFILES", ("ohlcv-v1",))
    base = _genes("S2")
    ctx = _genes("S2", "context")
    market = {g for g in base if "-market-" in g}
    assert market
    assert {g.replace("s2-market-", "s2-dn-market-") for g in market} <= ctx


def test_exit_rules_are_untouched(gates):
    assert _built("S1", "all").exit_rules == _built("S1").exit_rules


def test_confidence_clamp_reaches_the_down_copies(gates, monkeypatch):
    monkeypatch.setitem(mod._EXPERT_CONFIDENCE_CEILING, "FMPRating", 40.0)
    for scope in ("entry", "all"):
        strat = _built("S1", scope)
        conf = [x for r in strat.entry_rules for x in _leaves(r["conditions"]) if x["field"] == "confidence"]
        assert len(conf) == 6 and any(x["id"].endswith("-dn") for x in conf)
        for x in conf:
            assert x["value_max"] <= 40.0 and x["value"] <= 40.0


# ---------------------------------------------------------------- 4. decode


def _flat(kind, scope):
    strat = _built(kind, scope)
    space = collect_param_space(strat)
    return strat, authored_genome(strat, space)


def test_authored_genome_covers_every_dn_gene(gates):
    for kind in ("S1", "S2"):
        for scope in SCOPES:
            strat, flat = _flat(kind, scope)
            assert set(flat) >= {g for g in collect_param_space(strat) if g.split(":", 1)[0] in ("cond", "entry", "exit")}


def test_decode_gives_up_and_down_their_own_thresholds_s2(gates):
    strat, flat = _flat("S2", "entry")
    flat.update({"cond:gate_confidence:value": 55.0, "cond:gate_confidence-dn:value": 70.0,
                 "cond:s2-pb-rsi:mode": "below", "cond:s2-pb-rsi:value": 10.0,
                 "cond:s2-dn-pb-rsi:mode": "below", "cond:s2-dn-pb-rsi:value": 30.0,
                 "cond:s2-pb-vs-sma200:mode": MODE_OFF, "cond:s2-dn-pb-vs-sma200:mode": "above",
                 "cond:s2-dn-pb-vs-sma200:value": -5.0})
    up, down = decode_params(strat, flat)["entry_rules"]
    by_up = {x["id"]: x for x in _leaves(up["conditions"])}
    by_dn = {x["id"]: x for x in _leaves(down["conditions"])}
    assert by_up["gate_confidence"]["value"] == 55.0 and by_dn["gate_confidence-dn"]["value"] == 70.0
    assert by_up["s2-pb-rsi"]["value"] == 10.0 and by_dn["s2-dn-pb-rsi"]["value"] == 30.0
    assert "s2-pb-vs-sma200" not in by_up                       # off in UP ...
    assert by_dn["s2-dn-pb-vs-sma200"]["value"] == -5.0 and by_dn["s2-dn-pb-vs-sma200"]["op"] == ">"   # ... on in DOWN
    assert by_up["s2-spy-trend"]["value"] == TREND_ABOVE and by_dn["s2-dn-spy-trend"]["value"] == TREND_BELOW
    for leaf in list(by_up.values()) + list(by_dn.values()):
        assert _unresolved_reason(leaf) == ""
    assert "gene_rule_id" not in up and "gene_rule_id" not in down


def test_shared_action_genes_drive_both_sections_and_all_scope_splits_them(gates):
    strat, flat = _flat("S1", "entry")
    flat["entry:buy-1:a1:action_value"] = -3.0
    up, down = decode_params(strat, flat)["entry_rules"][:2]
    assert [a.get("action_value") for a in up["actions"][1:2]] == [-3.0] == [a.get("action_value") for a in down["actions"][1:2]]
    flat["entry:buy-1:enabled"] = 0
    assert [r["id"] for r in decode_params(strat, flat)["entry_rules"]] == ["buy-2", "buy-2-dn", "buy-3", "buy-3-dn"]

    strat, flat = _flat("S1", "all")
    flat["entry:buy-1:a1:action_value"] = -3.0
    flat["entry:buy-1-dn:a1:action_value"] = -9.0
    up, down = decode_params(strat, flat)["entry_rules"][:2]
    assert up["actions"][1]["action_value"] == -3.0 and down["actions"][1]["action_value"] == -9.0
    flat["entry:buy-1-dn:enabled"] = 0
    assert [r["id"] for r in decode_params(strat, flat)["entry_rules"]][:3] == ["buy-1", "buy-2", "buy-2-dn"]


# ---------------------------------------------------------------- 5. evaluation


@pytest.fixture
def _regime():
    br.reset_benchmark_regime()
    yield
    br.reset_benchmark_regime()


def _fires(rule, state):
    br.set_benchmark_regime(None if state is None else br.BenchmarkRegime(state, "normal"))
    leaf = _trend(rule)
    return SpyTrendRegimeCondition(account=None, instrument_name="AAPL", expert_recommendation=None,
                                   operator_str=leaf["op"], value=float(leaf["value"])).evaluate()


@pytest.mark.parametrize("kind", ["S1", "S2"])
@pytest.mark.parametrize("scope", SCOPES)
def test_only_the_matching_section_can_fire(kind, scope, gates, _regime):
    strat, flat = _flat(kind, scope)
    rules = decode_params(strat, flat)["entry_rules"]
    assert len(rules) % 2 == 0
    for i in range(0, len(rules), 2):
        up, down = rules[i], rules[i + 1]
        for state in ("up", "pullback"):                       # above the 200-day
            assert (_fires(up, state), _fires(down, state)) == (True, False)
        for state in ("bear_rally", "down"):                   # below it
            assert (_fires(up, state), _fires(down, state)) == (False, True)
        assert (_fires(up, None), _fires(down, None)) == (False, False)      # unknown: neither


# ---------------------------------------------------------------- 6. memo key


def test_memo_key_drops_the_inert_value_and_field_of_an_off_down_gene(gates):
    from app.services.strategy_optimization_handler import canonical_trial_params, mode_anchor_index

    strat = _built("S2", "context")
    anchors = mode_anchor_index(strat)
    assert "s2-dn-pb-rsi" in anchors and "s2-pb-rsi" in anchors and "s2-dn-spy-trend" not in anchors
    base = {f"cond:s2-dn-{s}:mode": MODE_OFF for s in ("pb-rsi", "pb-below-high", "pb-vs-sma200")}

    def key(extra, tag="dn"):
        return tuple(sorted(canonical_trial_params(anchors, {**base, **extra}).items()))

    off = {key({"cond:s2-dn-pb-rsi:field": f, "cond:s2-dn-pb-rsi:value": v})
           for f in ("pullback_rsi_2", "pullback_rsi_5") for v in (5.0, 35.0)}
    assert len(off) == 1
    assert "cond:s2-dn-pb-rsi:field" not in dict(next(iter(off)))
    on = {key({"cond:s2-dn-pb-rsi:mode": "below", "cond:s2-dn-pb-rsi:field": f, "cond:s2-dn-pb-rsi:value": v})
          for f in ("pullback_rsi_2", "pullback_rsi_5") for v in (5.0, 35.0)}
    assert len(on) == 4
    # the UP twin is judged on its own mode, not its DOWN twin's
    up_on = {**base, "cond:s2-pb-rsi:mode": "below", "cond:s2-pb-rsi:value": 5.0}
    other = {**up_on, "cond:s2-pb-rsi:value": 35.0}
    assert canonical_trial_params(anchors, up_on) != canonical_trial_params(anchors, other)


# ---------------------------------------------------------------- the matrix flag


def _argv(env, *extra, suffix="-goal2027-spyreg-regsplit"):
    argv = _plain_argv(env, *extra)
    return [f"--name-suffix={suffix}" if a.startswith("--name-suffix=") else a for a in argv]


def test_matrix_flag_needs_the_spy_gates_and_the_suffix(env, monkeypatch):
    monkeypatch.setattr(sys, "argv", _argv(env, "--regime-split-entry", "context"))
    with pytest.raises(SystemExit):
        M.main()
    monkeypatch.setattr(sys, "argv", _argv(env, "--benchmark-regime-gates", "--regime-split-entry", "context",
                                           suffix="-goal2027-spyreg"))
    with pytest.raises(SystemExit):
        M.main()


def test_matrix_flag_is_forwarded_and_changes_the_job_name(env, monkeypatch):
    monkeypatch.setattr(sys, "argv", _argv(env, "--benchmark-regime-gates", suffix="-goal2027-spyreg-regsplit"))
    assert M.main() == 0
    plain = list(env.calls[-1])
    assert "--regime-split-entry" not in plain
    monkeypatch.setattr(sys, "argv", _argv(env, "--benchmark-regime-gates", "--regime-split-entry", "entry"))
    assert M.main() == 0
    split = env.calls[-1]
    assert _flag(split, "--regime-split-entry") == "entry"
    assert _flag(split, "--name") != _flag(plain, "--name")
    monkeypatch.setattr(sys, "argv", _argv(env, "--benchmark-regime-gates", "--regime-split-entry", "all"))
    assert M.main() == 0
    assert _flag(env.calls[-1], "--name") != _flag(split, "--name")
