"""Strategies S8 / S9: S6's rules + the SPY-regime split + the RSI close and SPY stop, all implied by the key.

S8 = split scope ``all`` (DOWN section owns its signal thresholds AND its entry toggle / bracket / size genes);
S9 = split scope ``entry`` (own signal thresholds, shared entry toggle / bracket). Both imply ``--market-exit rsi,spy``,
need ``--benchmark-regime-gates``, refuse a contradicting ``--regime-split-entry`` and must never leak into another
strategy built in the same process (optimize-batch builds several).
"""
from __future__ import annotations

import importlib.util
import itertools
import json
import os
import sys
from types import SimpleNamespace

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # testplatform/backend
_LAUNCHER = os.path.normpath(os.path.join(_ROOT, "..", "ba2test_launcher.py"))
_TOOLS = os.path.normpath(os.path.join(_ROOT, "..", "..", "tools"))
for _p in (_ROOT, _TOOLS, os.path.dirname(os.path.abspath(__file__))):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from ba2_common.core.market_condition_templates import (  # noqa: E402
    RSI_EXIT_RULE_SUFFIX, SPY_DOWN_ENTRY_TAG, SPY_STOP_RULE_SUFFIX, SPY_UP_ENTRY_TAG)

from app.services.strategy_param_space import GENE_RULE_ID_MARKER, collect_param_space, decode_params  # noqa: E402

import run_screener_capband_matrix as M  # noqa: E402
from test_matrix_walk_forward import _flag, _plain_argv, env  # noqa: E402,F401

_spec = importlib.util.spec_from_file_location("ba2test_launcher_s8", _LAUNCHER)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)

IMPLIED = {"S8": "all", "S9": "entry"}
PROFILES = ("ohlcv-v1", "ta-structure-v1")
BASE_KINDS = ("exit", "stop", "tp")


@pytest.fixture
def run(monkeypatch):
    """The launcher's run-level globals as ``optimize`` leaves them for the grid's classic jobs (SPY gates, pullback
    gates, the two market profiles, ``--market-exit exit,stop,tp``): NO split flag, NO rsi/spy on the command line."""
    def configure(*, bench=True, split=None, kinds=BASE_KINDS):
        monkeypatch.setattr(mod, "_MARKET_CONDITION_PROFILES", PROFILES)
        monkeypatch.setattr(mod, "_MARKET_CONDITION_MODE", "searched")
        monkeypatch.setattr(mod, "_BENCHMARK_REGIME_GATES", bench)
        monkeypatch.setattr(mod, "_PULLBACK_GATES", True)
        monkeypatch.setattr(mod, "_REGIME_SPLIT_ENTRY", split)
        monkeypatch.setattr(mod, "_MARKET_EXIT_KINDS_SELECTED", tuple(kinds))
    configure()
    return configure


def _built(kind, expert="FMPRating"):
    return mod._build_strategy(kind, f"eq-{kind.lower()}", expert)


def _dump(strategy):
    return json.dumps([strategy.entry_rules, strategy.exit_rules], sort_keys=True, default=str)


def _space(strategy):
    return json.dumps(collect_param_space(strategy), sort_keys=True, default=str)


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


def _up_down(strategy):
    up = [r for r in strategy.entry_rules if str(r.get("name", "")).endswith(mod._REGIME_SPLIT_UP_SUFFIX)]
    down = [r for r in strategy.entry_rules if str(r.get("name", "")).endswith(mod._REGIME_SPLIT_DOWN_SUFFIX)]
    return up, down


def _exit(strategy, suffix):
    found = [r for r in strategy.exit_rules if str(r["id"]).endswith(suffix)]
    assert len(found) == 1, [r["id"] for r in strategy.exit_rules]
    return found[0]


# ------------------------------------------------------------------ 1. the strategies build, with the implied shape


def test_registered_and_admitted_wherever_s6_is():
    for kind in IMPLIED:
        assert kind in mod._STRATEGY_BUILDERS
        assert kind in mod._EQUITY_MARKET_CONDITION_STRATEGIES
        assert kind in mod._MARKET_CONDITION_STRATEGIES
        assert kind in M.SPY_GATE_STRATEGIES
    assert set(mod._IMPLIED_RULE_SHAPE) == set(IMPLIED) == set(M.IMPLIED_SHAPE_STRATEGIES)
    assert {k: v[0] for k, v in mod._IMPLIED_RULE_SHAPE.items()} == IMPLIED
    assert all(v[1] == ("rsi", "spy") for v in mod._IMPLIED_RULE_SHAPE.values())


@pytest.mark.parametrize("kind", sorted(IMPLIED))
def test_builds_with_up_and_down_rules_with_distinct_ids(kind, run):
    s = _built(kind)
    up, down = _up_down(s)
    assert len(up) == len(down) == 1                      # S6 has one entry rule
    assert up[0]["id"] != down[0]["id"] and down[0]["id"] == f"{up[0]['id']}-dn"
    ids = [r["id"] for r in s.entry_rules]
    assert len(set(ids)) == len(ids)
    # mutually exclusive by the pinned SPY trend leaf
    trend = lambda r: [x for x in _leaves(r["conditions"]) if x["field"] == "spy_trend_regime"]  # noqa: E731
    assert len(trend(up[0])) == len(trend(down[0])) == 1
    assert trend(up[0])[0] != trend(down[0])[0]          # pinned to above_200 vs below_200 (and the -dn id)


def test_s8_down_has_its_own_toggle_and_action_genes_and_s9_shares_them(run):
    genes = {k: set(collect_param_space(_built(k))) for k in ("S6", "S8", "S9")}
    s8, s9 = genes["S8"], genes["S9"]
    dn_rule_genes = lambda g, rid: {x for x in g if f"{rid}-dn" in x}  # noqa: E731
    up8, down8 = _up_down(_built("S8"))
    rid = up8[0]["id"]
    # S8 (scope all): the DOWN rule owns an enabled toggle and the entry action genes, under its OWN id
    assert GENE_RULE_ID_MARKER not in down8[0]
    own8 = dn_rule_genes(s8, rid)
    assert any(":enabled" in x for x in own8), own8
    assert any("action_value" in x for x in own8), own8
    # S9 (scope entry): the toggle / action genes are SHARED with the UP twin through gene_rule_id
    up9, down9 = _up_down(_built("S9"))
    assert down9[0][GENE_RULE_ID_MARKER] == up9[0]["id"]
    assert not [x for x in dn_rule_genes(s9, up9[0]["id"]) if ":enabled" in x or "action_value" in x]
    # and the DOWN signal thresholds are separate in both
    assert [x for x in s9 if "-dn" in x]                             # separate DOWN signal thresholds
    assert len(s8) > len(s9) > len(genes["S6"])                      # S8 adds the entry toggle + bracket genes


@pytest.mark.parametrize("kind", sorted(IMPLIED))
def test_has_the_rsi_close_and_the_spy_stop_scoped_to_the_up_section(kind, run):
    s = _built(kind)
    assert _exit(s, RSI_EXIT_RULE_SUFFIX)
    stop = _exit(s, SPY_STOP_RULE_SUFFIX)
    tag = [x for x in _leaves(stop["conditions"]) if x["field"] == "entry_tag"]
    assert len(tag) == 1 and tag[0]["value"] == float(SPY_UP_ENTRY_TAG)
    up, down = _up_down(s)
    tags = lambda r: {a.get("entry_tag") for a in r["actions"] if a["action_type"] == "buy"}  # noqa: E731
    assert tags(up[0]) == {SPY_UP_ENTRY_TAG} and tags(down[0]) == {SPY_DOWN_ENTRY_TAG}
    # the S6 exits are still there (S6 base)
    assert {"exit_bearish", "exit_downgrade", "exit_time"} <= {r["id"] for r in s.exit_rules}


@pytest.mark.parametrize("kind", sorted(IMPLIED))
def test_other_kinds_the_user_passes_still_apply(kind, run):
    run(kinds=())
    bare = {r["id"] for r in _built(kind).exit_rules}
    run(kinds=BASE_KINDS)
    full = {r["id"] for r in _built(kind).exit_rules}
    assert bare < full                                     # exit,stop,tp ADD rules on top of the implied rsi/spy
    assert any(i.endswith(RSI_EXIT_RULE_SUFFIX) for i in bare) and any(i.endswith(SPY_STOP_RULE_SUFFIX) for i in bare)


# ------------------------------------------------------------------ 2. refusals


@pytest.mark.parametrize("kind", sorted(IMPLIED))
def test_refused_without_benchmark_regime_gates(kind, run):
    run(bench=False)
    with pytest.raises(SystemExit) as e:
        _built(kind)
    assert "--benchmark-regime-gates" in str(e.value) and kind in str(e.value)
    _built("S6")                                           # other strategies are not affected


@pytest.mark.parametrize("kind,scope", [(k, s) for k in IMPLIED for s in ("context", "entry", "all") if s != IMPLIED[k]])
def test_refused_with_a_conflicting_regime_split_entry(kind, scope, run):
    run(split=scope)
    with pytest.raises(SystemExit) as e:
        _built(kind)
    assert "--regime-split-entry" in str(e.value)


@pytest.mark.parametrize("kind", sorted(IMPLIED))
def test_its_own_scope_is_accepted_as_redundant(kind, run):
    alone = _dump(_built(kind))
    run(split=IMPLIED[kind])
    assert _dump(_built(kind)) == alone


def test_an_s6_job_in_the_same_command_may_carry_a_split_flag(run):
    run(split="all")
    _built("S8")                                           # redundant for S8 ...
    assert _up_down(_built("S6"))[1]                       # ... and a real split for S6, as before


# ------------------------------------------------------------------ 3. no leak across strategies, any order


def test_no_leak_across_s6_s8_s9_in_any_order(run):
    alone = {k: (_dump(_built(k)), _space(_built(k))) for k in ("S6", "S8", "S9")}
    assert alone["S6"] != alone["S8"] != alone["S9"] != alone["S6"]
    for order in itertools.permutations(("S6", "S8", "S9")):
        run()
        seen = {}
        for k in order:
            s = _built(k)
            seen[k] = (_dump(s), _space(s))
        assert seen == alone, order
        # the globals hold only what the command line said
        assert mod._REGIME_SPLIT_ENTRY is None and mod._MARKET_EXIT_KINDS_SELECTED == BASE_KINDS
    for k in ("S6", "S8", "S9"):                          # and repeating one job after the others
        assert (_dump(_built(k)), _space(_built(k))) == alone[k]


def test_s6_is_unsplit_and_has_no_independent_rules_after_s8(run):
    _built("S8")
    s6 = _built("S6")
    assert not _up_down(s6)[1] and not [r for r in s6.exit_rules if r["id"].endswith(
        (RSI_EXIT_RULE_SUFFIX, SPY_STOP_RULE_SUFFIX))]


# ------------------------------------------------------------------ 4. gene counts


def test_gene_counts(run, capsys):
    counts = {}
    for expert in ("FMPRating", "DeterministicScorer"):
        counts[expert] = {k: len(collect_param_space(_built(k, expert))) for k in ("S6", "S9", "S8")}
        s6, s9, s8 = (counts[expert][k] for k in ("S6", "S9", "S8"))
        assert s6 < s9 < s8, counts
    with capsys.disabled():
        print(f"\nGENE COUNTS (collect_param_space, strategy-level; run-level genes excluded): {counts}")


# ------------------------------------------------------------------ 5. launcher plumbing: checks, record, stored row


@pytest.mark.parametrize("kind", sorted(IMPLIED))
def test_checks_pass_and_the_block_records_the_effective_values(kind, run):
    s = _built(kind)
    mod._check_regime_split_entry("optimize", s, kind)
    mod._check_market_exit_independent("optimize", s, kind)
    block = {}
    mod._apply_regime_split(block, kind)
    assert block == {"regime_split_entry": IMPLIED[kind]}
    other = {}
    mod._apply_regime_split(other, "S6")
    assert other == {}                                     # S6 with no flag: nothing recorded
    assert mod._effective_market_exit_kinds(kind) == ("exit", "stop", "tp", "rsi", "spy")
    assert mod._effective_market_exit_kinds("S6") == BASE_KINDS


def test_checks_refuse_an_s6_built_strategy_labelled_s8(run):
    s6 = _built("S6")                                      # no DOWN rule, no rsi/spy rules
    with pytest.raises(SystemExit):
        mod._check_regime_split_entry("optimize", s6, "S8")
    with pytest.raises(SystemExit):
        mod._check_market_exit_independent("optimize", s6, "S8")


@pytest.mark.parametrize("kind", sorted(IMPLIED))
def test_stored_row_round_trip(kind, run):
    """A stored row carries its rules (the Strategy row) and its genes; a re-run / walk-forward fold decodes them
    and rebuilds the SAME rules from the key alone (no split / rsi / spy on the command line)."""
    s = _built(kind)
    # (a) the persisted form: rules survive JSON, and the gene space of the reloaded rules is identical
    reloaded = SimpleNamespace(**{a: json.loads(json.dumps(getattr(s, a), default=str))
                                  for a in ("entry_rules", "exit_rules")})
    for attr in ("entry_rules", "exit_rules"):
        assert getattr(reloaded, attr) == json.loads(json.dumps(getattr(s, attr), default=str))
    assert set(collect_param_space(reloaded)) == set(collect_param_space(s))
    # (b) a decoded genome keeps the DOWN rule and the implied exits
    out = decode_params(s, {})
    names = [r.get("name", "") for r in out["entry_rules"]]
    assert any(n.endswith(mod._REGIME_SPLIT_DOWN_SUFFIX) for n in names)
    # (c) the block records the effective scope, and rebuilding in a fresh process state (what --rerun /
    # run_walk_forward do: same optimize command, only the kind) gives byte-identical rules and genes
    block = {}
    mod._apply_regime_split(block, kind)
    assert block["regime_split_entry"] == IMPLIED[kind]
    first = (_dump(s), _space(s))
    run()                                                  # fresh globals
    again = _built(kind)
    assert (_dump(again), _space(again)) == first


# ------------------------------------------------------------------ 6. the grid driver


def test_matrix_gives_s8_s9_the_gates_but_never_a_split_flag():
    args = SimpleNamespace(benchmark_regime_gates=True, pullback_gates=True, regime_split_entry="all")
    for kind in IMPLIED:
        assert M.benchmark_regime_tokens(args, kind) == ["--benchmark-regime-gates"]
        assert M.pullback_gate_tokens(args, kind) == ["--pullback-gates"]
        assert M.regime_split_tokens(args, kind) == []
    assert M.regime_split_tokens(args, "S6") == ["--regime-split-entry", "all"]


def test_per_strategy_market_exit_kinds_and_name_tokens():
    args = SimpleNamespace(market_exit="exit,stop,tp", market_exit_per_strategy=True)
    assert M.market_exit_for(args, "S1") == M.market_exit_for(args, "S2") == "exit,stop,tp,rsi"
    assert M.market_exit_for(args, "S6") == "exit,stop,tp,rsi,spy"
    for other in ("S3", "S5", "S7", "S8", "S9", None):
        assert M.market_exit_for(args, other) == "exit,stop,tp"
        assert M.market_exit_name_token(args, other) == ""
    assert M.market_exit_name_token(args, "S1") == "-mx-exit_stop_tp_rsi"
    assert M.market_exit_name_token(args, "S6") == "-mx-exit_stop_tp_rsi_spy"
    off = SimpleNamespace(market_exit="exit,stop,tp", market_exit_per_strategy=False)
    assert M.market_exit_for(off, "S6") == "exit,stop,tp" and M.market_exit_name_token(off, "S6") == ""
    assert M.market_condition_passthrough(args, "S6")[M.market_condition_passthrough(args, "S6").index("--market-exit") + 1] \
        == "exit,stop,tp,rsi,spy"


def _late_argv(env, late, *extra, experts=("DeterministicScorer", "FMPRating")):
    skip = ",".join(e for e in ("FMPRating", "DeterministicScorer", "FMPEarningsDrift",
                                "FMPInsiderClusterBuy", "FactorRanker") if e not in experts)
    return ["run_screener_capband_matrix.py", "--bands", "mid", "--skip-experts", skip, "--strategy-plan", env.plan,
            "--name-suffix=-goal2027-spyreg", "--benchmark-regime-gates", "--late-strategy", late, *extra]


def test_late_strategy_runs_only_its_cells_for_the_plans_experts(env, monkeypatch, tmp_path):
    plan = json.loads(open(env.plan, encoding="utf-8").read())
    plan["late_strategies"] = {"S8": ["DeterministicScorer", "FMPRating"], "S9": ["DeterministicScorer", "FMPRating"]}
    with open(env.plan, "w", encoding="utf-8") as f:
        json.dump(plan, f)
    for late in ("S9", "S8"):
        env.calls.clear()
        monkeypatch.setattr(sys, "argv", _late_argv(env, late, experts=("DeterministicScorer", "FMPRating", "FMPInsiderClusterBuy",
                                                                       "FactorRanker")))
        assert M.main() == 0
        got = sorted((_flag(c, "--expert"), _flag(c, "--strategy")) for c in env.calls)
        assert got == [("DeterministicScorer", late), ("FMPRating", late)], got   # nobody else, nothing else
        for c in env.calls:
            assert "--regime-split-entry" not in c and "--benchmark-regime-gates" in c
            assert "-mx-" not in _flag(c, "--name") and "regsplit" not in _flag(c, "--name")


def test_late_strategy_needs_a_plan_section_and_the_spy_gates(env, monkeypatch):
    monkeypatch.setattr(sys, "argv", _late_argv(env, "S8"))
    with pytest.raises(SystemExit):
        M.main()                                           # the fixture plan has no late_strategies
    monkeypatch.setattr(sys, "argv", [a for a in _late_argv(env, "S8") if a != "--benchmark-regime-gates"])
    with pytest.raises(SystemExit):
        M.main()


def test_the_shipped_plan_lists_s8_s9_for_ds_and_fmprating_only_and_not_in_the_regular_experts():
    path = os.path.normpath(os.path.join(_ROOT, "..", "..", "docs", "strategy_research", "atr_grid", "strategy_plan.json"))
    doc = json.load(open(path, encoding="utf-8"))
    assert doc["late_strategies"] == {"S8": ["DeterministicScorer", "FMPRating"], "S9": ["DeterministicScorer", "FMPRating"]}
    assert not [s for strats in doc["experts"].values() for s in strats if s in IMPLIED]
    assert M.load_late_strategy_experts(path, "S8") == ["DeterministicScorer", "FMPRating"]
    assert M.LATE_STRATEGY_ORDER == ("S9", "S8")


def test_the_grid_script_runs_s9_then_s8_after_every_other_step():
    sh = open(os.path.normpath(os.path.join(_ROOT, "..", "..", "tools", "grid_goal2027.sh")), encoding="utf-8").read()
    line = next(l for l in sh.splitlines() if l.startswith("ALL_STEPS="))
    steps = line.split('"')[1].split()
    late = [i for i, s in enumerate(steps) if s.startswith(("s8-", "s9-"))]
    assert late == list(range(len(steps) - len(late), len(steps)))            # a suffix of the order
    tail = [s[:2] for s in steps[len(steps) - len(late):]]
    assert tail == sorted(tail, reverse=True) == ["s9"] * 6 + ["s8"] * 6      # S9 cells, then S8 cells last
    assert {s.split("-", 1)[1].split("-")[0] for s in steps if s.startswith(("s8-", "s9-"))} == {"fr", "ds"}
