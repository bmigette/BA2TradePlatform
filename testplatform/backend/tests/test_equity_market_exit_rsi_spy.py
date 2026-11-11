"""``--market-exit rsi`` / ``spy``: two exit kinds that read registries independent of the market-condition profile.

* ``rsi``: ``<prefix>-pb-exit-rsi`` closes a long when the pullback RSI (period searched through the same leaf-level
  field gene the entry leaf has) has recovered ABOVE 50..90 step 5 (default 70); a short mirrors (below, 10..50,
  default 30). Stops processing.
* ``spy``: ``<prefix>-spy-stop`` adjusts the stop from the open price (-2..0, the ``-mkt-stop`` band) while SPY is below
  its 200-day (a short: above); continues processing. Requires ``--benchmark-regime-gates``.
* Both are OFF by default behind a rule-level toggle gene, carry NO market master/profile marker, need no
  ``--market-condition-profile``, and drop their genes from the memo key when toggled off.
* With ``--regime-split-entry`` the spy stop is scoped by an ``entry_tag`` leaf to the positions the SPY-up section
  opened (the engine-level proof is ``tests/backtest/test_market_exit_spy_stop_engine.py``).
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from datetime import date, datetime, timezone
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # testplatform/backend
_LAUNCHER = os.path.normpath(os.path.join(_ROOT, "..", "ba2test_launcher.py"))
_TOOLS = os.path.normpath(os.path.join(_ROOT, "..", "..", "tools"))
for _p in (_ROOT, _TOOLS, os.path.dirname(os.path.abspath(__file__))):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from ba2_common.core import TradeConditions  # noqa: E402
from ba2_common.core.market_condition_templates import (  # noqa: E402
    ALL_MARKET_EXIT_KINDS, PULLBACK_RSI_FIELDS, RSI_EXIT_RULE_SUFFIX, SPY_STOP_RULE_SUFFIX, SPY_UP_ENTRY_TAG,
    independent_exit_rules)
from ba2_common.core.rule_builders import triggers_from_condition_tree  # noqa: E402
from ba2_common.core.rule_models import normalize_trade_rules  # noqa: E402
from ba2_common.core.TradeConditions import create_condition  # noqa: E402

from app.services.strategy_param_space import collect_param_space, decode_params  # noqa: E402

_spec = importlib.util.spec_from_file_location("ba2test_launcher_mx_rsi_spy", _LAUNCHER)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)

EQUITY = sorted(mod._EQUITY_MARKET_CONDITION_STRATEGIES)
PROFILES = ("ohlcv-v1", "ta-structure-v1")
OLD_KINDS = ("exit", "stop", "tp")


@pytest.fixture
def run(monkeypatch):
    """Set the launcher's run-level globals the way ``optimize`` would, then build strategies."""
    def configure(kinds=(), *, profiles=PROFILES, bench=True, split=None, pullback=False, mode="searched"):
        monkeypatch.setattr(mod, "_MARKET_CONDITION_PROFILES", tuple(profiles))
        monkeypatch.setattr(mod, "_MARKET_CONDITION_MODE", mode)
        monkeypatch.setattr(mod, "_BENCHMARK_REGIME_GATES", bench)
        monkeypatch.setattr(mod, "_PULLBACK_GATES", pullback)
        monkeypatch.setattr(mod, "_REGIME_SPLIT_ENTRY", split)
        monkeypatch.setattr(mod, "_MARKET_EXIT_KINDS_SELECTED", tuple(kinds))
    configure()
    return configure


def _built(kind, expert="FMPRating"):
    return mod._build_strategy(kind, f"eq-mx-{kind}", expert)


def _genes(kind):
    return set(collect_param_space(_built(kind)))


def _dump(strategy):
    return json.dumps([strategy.entry_rules, strategy.exit_rules], sort_keys=True, default=str)


def _rule(strategy, suffix):
    found = [r for r in strategy.exit_rules if str(r["id"]).endswith(suffix)]
    assert len(found) <= 1
    return found[0] if found else None


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


# ------------------------------------------------------------------ 1. absent = unchanged


@pytest.mark.parametrize("kind", EQUITY)
def test_the_new_kinds_leave_the_old_ones_untouched(kind, run):
    run(OLD_KINDS)
    base, base_genes = _dump(_built(kind)), _genes(kind)
    run(OLD_KINDS + ("rsi", "spy"))
    both = _built(kind)
    assert _genes(kind) >= base_genes
    # every pre-existing rule is still there, byte for byte, in the same relative order
    old_exit = json.loads(base)[1]
    kept = [r for r in json.loads(json.dumps(both.exit_rules, default=str))
            if not str(r["id"]).endswith((RSI_EXIT_RULE_SUFFIX, SPY_STOP_RULE_SUFFIX))]
    assert kept == old_exit
    assert json.loads(json.dumps(both.entry_rules, default=str)) == json.loads(base)[0]


@pytest.mark.parametrize("kind", EQUITY)
def test_no_kinds_is_todays_strategy(kind, run):
    run(())
    plain = _dump(_built(kind))
    run(OLD_KINDS)
    assert _dump(_built(kind)) != plain or kind is None      # the old kinds change it...
    run(())
    assert _dump(_built(kind)) == plain                      # ...and leave no state behind


def test_the_old_kinds_are_exactly_exit_stop_tp():
    from ba2_common.core.market_condition_templates import MARKET_EXIT_KINDS
    assert MARKET_EXIT_KINDS == ("exit", "stop", "tp")
    assert ALL_MARKET_EXIT_KINDS == ("exit", "stop", "tp", "rsi", "spy")


# ------------------------------------------------------------------ 2. what each kind adds


@pytest.mark.parametrize("kind", EQUITY)
def test_rsi_adds_exactly_its_three_genes_and_its_rule(kind, run):
    p = kind.lower()
    run(OLD_KINDS)
    base = _genes(kind)
    run(OLD_KINDS + ("rsi",))
    added = _genes(kind) - base
    rid = f"{p}-pb-exit-rsi"
    assert added == {f"exit:{rid}:enabled", f"cond:{rid}-rsi:value", f"cond:{rid}-rsi:field"}
    rule = _rule(_built(kind), RSI_EXIT_RULE_SUFFIX)
    assert rule["enabled"] is False and rule["toggle_optimize"] is True      # OFF by default behind the toggle
    assert rule["continue_processing"] is False
    assert [a["action_type"] for a in rule["actions"]] == ["close"]


def test_spy_adds_its_two_genes_where_it_survives(run):
    run(OLD_KINDS)
    base = _genes("S6")
    run(OLD_KINDS + ("spy",))
    assert _genes("S6") - base == {"exit:s6-spy-stop:enabled", "exit:s6-spy-stop:a0:action_value"}
    rule = _rule(_built("S6"), SPY_STOP_RULE_SUFFIX)
    assert rule["enabled"] is False and rule["toggle_optimize"] is True
    assert rule["continue_processing"] is True
    (action,) = rule["actions"]
    assert action["action_type"] == "adjust_stop_loss" and action["reference_value"] == "order_open_price"
    assert (action["action_value_min"], action["action_value_max"], action["action_value_step"]) == (-2.0, 0.0, 1.0)


@pytest.mark.parametrize("kind", [k for k in EQUITY if k != "S6"])
def test_spy_is_dropped_where_a_catch_all_adjusts_the_stop_every_bar(kind, run, capsys):
    """Same rule as the market stop (``_insert_equity_market_exit_rules``): a pass keeps only its LAST stop-loss
    action and the always-matching floor stop is last, so the rule would be a searched-but-dead gene."""
    run(OLD_KINDS)
    base = _genes(kind)
    run(OLD_KINDS + ("rsi", "spy"))
    assert not [g for g in _genes(kind) - base if "spy-stop" in g]
    assert _rule(_built(kind), SPY_STOP_RULE_SUFFIX) is None
    assert "'spy'" in capsys.readouterr().out                      # said, not silent


@pytest.mark.parametrize("kind", [k for k in EQUITY if k != "S6"])
def test_spy_alone_where_it_is_dropped_is_refused(kind, run):
    run(("spy",))
    with pytest.raises(ValueError, match="adds no rule"):
        _built(kind)


def test_gene_counts_s1_and_s2(run):
    """The numbers the delivery reports (all flags: both profiles, SPY regime gates)."""
    counts = {}
    for kinds in (OLD_KINDS, OLD_KINDS + ("rsi", "spy")):
        run(kinds)
        counts[kinds] = {k: len(_genes(k)) for k in ("S1", "S2")}
    for k in ("S1", "S2"):
        assert counts[OLD_KINDS + ("rsi", "spy")][k] - counts[OLD_KINDS][k] == 3      # rsi 3; spy dropped on S1/S2


def test_independent_kinds_need_no_profile_and_no_master_marker(run):
    run(("rsi",), profiles=(), bench=False)
    s = _built("S2")
    rule = _rule(s, RSI_EXIT_RULE_SUFFIX)
    assert rule and "market_master_gene" not in rule and "market_profile_gene" not in rule
    genes = collect_param_space(s)
    assert "market:enabled" not in genes and "market:profile" not in genes
    run(("rsi", "exit"), profiles=PROFILES)
    s = _built("S2")
    assert _rule(s, RSI_EXIT_RULE_SUFFIX) and "market_master_gene" not in _rule(s, RSI_EXIT_RULE_SUFFIX)
    assert any(r.get("market_master_gene") for r in s.exit_rules)       # the profile kinds still carry it
    run(("rsi",), profiles=PROFILES, mode="all-off")                    # the control arm: rsi is independent
    assert _rule(_built("S2"), RSI_EXIT_RULE_SUFFIX)


def test_search_market_profile_does_not_mark_the_independent_rules(run, monkeypatch):
    monkeypatch.setattr(mod, "_SEARCH_MARKET_PROFILE", True)
    run(OLD_KINDS + ("rsi", "spy"))
    s = _built("S6")
    for suffix in (RSI_EXIT_RULE_SUFFIX, SPY_STOP_RULE_SUFFIX):
        assert "market_profile_gene" not in _rule(s, suffix) and "market_master_gene" not in _rule(s, suffix)
    assert _rule(s, "-mkt-stop")["market_profile_gene"] is True


# ------------------------------------------------------------------ 3. requirements, loudly


def test_spy_without_benchmark_regime_gates_exits(run):
    run((), bench=False)
    with pytest.raises(SystemExit) as e:
        mod._resolve_market_exit_kinds("spy", "optimize")
    assert "--benchmark-regime-gates" in str(e.value)
    run((), bench=True)
    assert mod._resolve_market_exit_kinds("spy", "optimize") == ("spy",)


def test_rsi_alone_needs_no_profile_but_the_old_kinds_still_do(run):
    run((), profiles=(), bench=False)
    assert mod._resolve_market_exit_kinds("rsi", "optimize") == ("rsi",)
    for raw in ("exit", "stop,rsi", "tp"):
        with pytest.raises(SystemExit) as e:
            mod._resolve_market_exit_kinds(raw, "optimize")
        assert "--market-condition-profile" in str(e.value)


def test_old_kinds_still_refuse_the_all_off_control(run):
    run((), mode="all-off")
    with pytest.raises(SystemExit):
        mod._resolve_market_exit_kinds("exit,rsi", "optimize")


@pytest.mark.parametrize("raw", ["bogus", "rsi,rsi", "exit,stop,tp,rsi,spy,x"])
def test_unknown_or_repeated_kinds_exit(raw, run):
    with pytest.raises(SystemExit) as e:
        mod._resolve_market_exit_kinds(raw, "optimize")
    assert "exit,stop,tp,rsi,spy" in str(e.value)


def test_kinds_are_canonically_ordered(run):
    assert mod._resolve_market_exit_kinds("spy,rsi,tp,exit", "optimize") == ("exit", "tp", "rsi", "spy")


def test_check_refuses_non_equity_and_a_strategy_without_the_rules(run):
    run(("rsi",), profiles=())
    built = _built("S2")
    with pytest.raises(SystemExit):
        mod._check_market_exit_independent("optimize", built, "O_LC")
    with pytest.raises(SystemExit):
        mod._check_market_exit_independent("optimize", SimpleNamespace(exit_rules=[]), "S2")
    mod._check_market_exit_independent("optimize", built, "S2")                # fine
    run(())
    mod._check_market_exit_independent("optimize", object(), "O_LC")           # no kinds: a no-op


def test_a_rule_toggled_on_is_a_real_rule_and_off_is_absent(run):
    run(("rsi",), profiles=())
    s = _built("S2")
    rid = "s2-pb-exit-rsi"
    on = decode_params(s, {f"exit:{rid}:enabled": 1, f"cond:{rid}-rsi:value": 80.0, f"cond:{rid}-rsi:field": "pullback_rsi_4"})
    (rule,) = [r for r in on["exit_rules"] if r["id"] == rid]
    assert "enabled" not in rule and "market_master_gene" not in rule
    (leaf,) = _leaves(rule["conditions"])
    assert (leaf["field"], leaf["op"], leaf["value"]) == ("pullback_rsi_4", ">", 80.0)
    assert not [k for k in leaf if k in ("field_optimize", "field_choices", "fieldOptimize", "fieldChoices")]
    for flat in ({}, {f"exit:{rid}:enabled": 0, f"cond:{rid}-rsi:value": 80.0}):
        off = decode_params(s, flat)
        assert rid not in [r["id"] for r in off["exit_rules"]]


# ------------------------------------------------------------------ 4. long vs short


def test_long_and_short_templates_mirror():
    ids = lambda rules: {r["id"]: r for r in rules}                                           # noqa: E731
    lg = ids(independent_exit_rules("x", "long", ("rsi", "spy")))
    sh = ids(independent_exit_rules("x", "short", ("rsi", "spy")))
    (l_rsi,), (s_rsi,) = _leaves(lg["x-pb-exit-rsi"]["conditions"]), _leaves(sh["x-pb-exit-rsi"]["conditions"])
    assert (l_rsi["op"], l_rsi["value"], l_rsi["value_min"], l_rsi["value_max"], l_rsi["value_step"]) == (">", 70.0, 50.0, 90.0, 5.0)
    assert (s_rsi["op"], s_rsi["value"], s_rsi["value_min"], s_rsi["value_max"], s_rsi["value_step"]) == ("<", 30.0, 10.0, 50.0, 5.0)
    for leaf in (l_rsi, s_rsi):
        assert leaf["field"] == PULLBACK_RSI_FIELDS[0] and leaf["field_choices"] == list(PULLBACK_RSI_FIELDS)
        assert leaf["field_optimize"] is True and not leaf.get("mode_optimize") and leaf["mode"] != "off"
    (l_spy,), (s_spy,) = _leaves(lg["x-spy-stop"]["conditions"]), _leaves(sh["x-spy-stop"]["conditions"])
    assert (l_spy["field"], l_spy["mode"]) == ("spy_trend_regime", "below_200")
    assert (s_spy["field"], s_spy["mode"]) == ("spy_trend_regime", "above_200")
    assert lg["x-spy-stop"]["actions"] == sh["x-spy-stop"]["actions"]                    # percent is direction-relative


def test_templates_refuse_bad_input():
    with pytest.raises(ValueError):
        independent_exit_rules("x", "flat", ("rsi",))
    with pytest.raises(ValueError):
        independent_exit_rules("x", "long", ("exit",))
    with pytest.raises(ValueError):
        independent_exit_rules("x", "long", ("rsi", "rsi"))


# ------------------------------------------------------------------ 5. rule evaluation


SIM_BAR = date(2021, 9, 15)


class _Provider:
    def __init__(self, df):
        self.df = df

    def get_ohlcv_data(self, symbol, start_date=None, end_date=None, interval="1d", **kw):
        df = self.df
        if end_date is not None:
            df = df[df["Date"] <= pd.Timestamp(end_date).tz_localize(None)]
        return df.reset_index(drop=True)


class _Account:
    id = 1
    _as_of_date = staticmethod(lambda: SIM_BAR)

    def get_instrument_current_price(self, symbol, *a, **k):
        return 100.0


def _closes(last_moves):
    """A long flat-ish history that ends with ``last_moves`` (so the short RSI is steered)."""
    base = 100.0 + np.cumsum(np.random.default_rng(3).normal(0, 0.5, 200))
    return np.concatenate([base, base[-1] + np.cumsum(last_moves)])


def _rsi_leaf_fires(monkeypatch, closes, period, threshold):
    idx = pd.bdate_range(end=pd.Timestamp(SIM_BAR), periods=len(closes))
    df = pd.DataFrame({"Date": idx, "Open": closes, "High": closes, "Low": closes, "Close": closes, "Volume": 1e6})
    monkeypatch.setattr(TradeConditions, "_provider_resolver", lambda c, n, **kw: _Provider(df), raising=False)
    rule = independent_exit_rules("s2", "long", ("rsi",))[0]
    flat = {"exit:s2-pb-exit-rsi:enabled": 1, "cond:s2-pb-exit-rsi-rsi:value": threshold,
            "cond:s2-pb-exit-rsi-rsi:field": f"pullback_rsi_{period}"}
    strat = SimpleNamespace(entry_rules=[], exit_rules=normalize_trade_rules([rule]))
    (decoded,) = decode_params(strat, flat)["exit_rules"]
    (leaf,) = _leaves(decoded["conditions"])
    from ba2_common.core.rule_builders import FIELD_EVENT
    rec = SimpleNamespace(created_at=datetime(2021, 9, 15, tzinfo=timezone.utc), instance_id=1, symbol="AAPL", data={})
    cond = create_condition(FIELD_EVENT[leaf["field"]], _Account(), "AAPL", rec,
                            operator_str=leaf["op"], value=leaf["value"])
    return cond.evaluate(), cond.calculated_value


def test_rsi_above_the_threshold_closes_and_below_does_not(monkeypatch):
    closes = _closes([1.0, 1.0, 1.0, 1.0, 1.0, 1.0])               # a run of gains: RSI(3) near 100
    fired, value = _rsi_leaf_fires(monkeypatch, closes, 3, 70.0)
    assert value > 90 and fired is True
    fired, _ = _rsi_leaf_fires(monkeypatch, closes, 3, 90.0 + 1e-9 if value <= 90 else value + 1.0)
    assert fired is False
    down = _closes([-1.0, -1.0, -1.0, -1.0, -1.0, -1.0])           # a run of losses: RSI(3) near 0
    fired, value = _rsi_leaf_fires(monkeypatch, down, 3, 70.0)
    assert value < 10 and fired is False


def test_the_rule_converts_to_a_close_with_one_rsi_trigger():
    rule = independent_exit_rules("s2", "long", ("rsi",))[0]
    strat = SimpleNamespace(entry_rules=[], exit_rules=normalize_trade_rules([rule]))
    (decoded,) = decode_params(strat, {"exit:s2-pb-exit-rsi:enabled": 1,
                                       "cond:s2-pb-exit-rsi-rsi:value": 70.0,
                                       "cond:s2-pb-exit-rsi-rsi:field": "pullback_rsi_2"})["exit_rules"]
    triggers = triggers_from_condition_tree(decoded["conditions"])
    assert len(triggers) == 1 and "pullback_rsi_2" in json.dumps(triggers, default=str)


# ------------------------------------------------------------------ 6. memo key


def test_memo_key_drops_the_genes_of_a_rule_toggled_off(run):
    from app.services.strategy_optimization_handler import canonical_trial_params, mode_anchor_index

    run(("rsi", "spy"))
    s = _built("S6")
    anchors = mode_anchor_index(s)
    off = {"exit:s6-pb-exit-rsi:enabled": 0, "exit:s6-spy-stop:enabled": 0, "other": 1}
    keys = set()
    for field in PULLBACK_RSI_FIELDS:
        for thr in (50.0, 90.0):
            for pct in (-2.0, 0.0):
                keys.add(tuple(sorted(canonical_trial_params(anchors, {
                    **off, "cond:s6-pb-exit-rsi-rsi:field": field, "cond:s6-pb-exit-rsi-rsi:value": thr,
                    "exit:s6-spy-stop:a0:action_value": pct}).items())))
    assert len(keys) == 1 and dict(next(iter(keys))) == off
    on = {**off, "exit:s6-pb-exit-rsi:enabled": 1, "exit:s6-spy-stop:enabled": 1}
    assert len({tuple(sorted(canonical_trial_params(anchors, {
        **on, "cond:s6-pb-exit-rsi-rsi:field": f, "cond:s6-pb-exit-rsi-rsi:value": 70.0,
        "exit:s6-spy-stop:a0:action_value": -1.0}).items())) for f in PULLBACK_RSI_FIELDS}) == 4
    # toggled on keeps its genes; a genome with neither rule is returned as is (identity)
    plain = {"model:x": 1.0, "exit:s6-mkt-stop:enabled": 0, "exit:s6-mkt-stop:a0:action_value": -1.0}
    assert canonical_trial_params(anchors, plain) is plain


# ------------------------------------------------------------------ 7. the split interaction


def test_split_tags_the_two_sections_only_with_spy(run):
    run(OLD_KINDS + ("rsi",), split="context")
    s = _built("S6")
    assert not [a for r in s.entry_rules for a in r["actions"] if a.get("entry_tag")]    # unchanged without spy
    run(OLD_KINDS + ("rsi", "spy"), split="context")
    s = _built("S6")
    tags = {r["name"].rsplit(" [", 1)[1]: {a.get("entry_tag") for a in r["actions"] if a["action_type"] == "buy"}
            for r in s.entry_rules}
    assert tags == {"SPY up]": {1}, "SPY down]": {2}}
    assert SPY_UP_ENTRY_TAG == 1


def test_split_scopes_the_spy_stop_to_the_up_section(run):
    run(OLD_KINDS + ("spy",), split="context")
    rule = _rule(_built("S6"), SPY_STOP_RULE_SUFFIX)
    tag = [x for x in _leaves(rule["conditions"]) if x["field"] == "entry_tag"]
    assert len(tag) == 1 and tag[0]["value"] == float(SPY_UP_ENTRY_TAG)
    run(OLD_KINDS + ("spy",), split=None)
    assert not [x for x in _leaves(_rule(_built("S6"), SPY_STOP_RULE_SUFFIX)["conditions"]) if x["field"] == "entry_tag"]


def test_the_split_spy_rule_survives_decode_and_converts(run):
    run(("spy",), split="context", profiles=())
    s = _built("S6")
    out = decode_params(s, {"exit:s6-spy-stop:enabled": 1, "exit:s6-spy-stop:a0:action_value": -1.0})
    (rule,) = [r for r in out["exit_rules"] if r["id"].endswith(SPY_STOP_RULE_SUFFIX)]
    fields = {x["field"] for x in _leaves(rule["conditions"])}
    assert fields == {"spy_trend_regime", "entry_tag"}
    assert len(triggers_from_condition_tree(rule["conditions"])) == 2       # neither leaf silently dropped
    assert rule["actions"][0]["action_value"] == -1.0
