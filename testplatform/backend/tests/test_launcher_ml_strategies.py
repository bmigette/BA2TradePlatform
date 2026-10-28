"""MLForecast launcher pieces of the ML exploration v2 stage-2 plan (Tasks 5.1, 5.2).

* MLR, the rotation strategy of the selection track: bullish + flat entry, an ALWAYS-on bearish exit, a toggleable
  ``days_opened >= rebalance`` time exit (5..20 step 5), no take-profit, MLForecast only; ``--ml-rank-top-n`` turns the
  ``rank_top_n`` gene into a choice (MLR only).
* ``--ml-hold-from-horizon``: MLH / MLF's ``days_opened`` gene is {d-1, d, d+1} with d = round(H x 7/5) - 1.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "testplatform"))

import ba2test_launcher as L  # noqa: E402

_ML = "MLForecast"


def _nodes(rules):
    """Every dict in a rule list, depth first."""
    out = []

    def walk(n):
        if isinstance(n, list):
            for x in n:
                walk(x)
        elif isinstance(n, dict):
            out.append(n)
            for v in n.values():
                walk(v)

    walk(rules)
    return out


def _node(rules, node_id):
    hits = [n for n in _nodes(rules) if n.get("id") == node_id]
    assert len(hits) == 1, (node_id, len(hits))
    return hits[0]


def _rule(rules, rule_id):
    hits = [r for r in rules if r.get("id") == rule_id]
    assert len(hits) == 1, (rule_id, len(hits))
    return hits[0]


# --------------------------------------------------------------------------- #
# Task 5.1: MLR
# --------------------------------------------------------------------------- #
def test_mlr_is_registered_and_mlforecast_only():
    assert L._STRATEGY_BUILDERS["MLR"] is L._build_strategy_MLR
    assert L._EXPERT_ONLY_STRATEGIES["MLR"] == _ML
    L._refuse_ds_only_strategy("optimize", "MLR", _ML)
    with pytest.raises(SystemExit, match="MLR is for MLForecast only|strategy MLR is for MLForecast only"):
        L._refuse_ds_only_strategy("optimize", "MLR", "DeterministicScorer")


def test_mlr_rules():
    strat = L._build_strategy("MLR", "mlr-test", _ML)
    # entry: bullish and flat, a plain buy (no take-profit / stop bracket anywhere)
    assert len(strat.entry_rules) == 1
    entry = strat.entry_rules[0]
    assert {n["field"] for n in _nodes(entry["conditions"]) if "field" in n} == {"bullish", "has_no_position"}
    assert [a["action_type"] for a in entry["actions"]] == ["buy"]
    all_actions = [a["action_type"] for r in strat.entry_rules + strat.exit_rules for a in r["actions"]]
    assert all_actions == ["buy", "close", "close"]
    assert not any(t in a for a in all_actions for t in ("take_profit", "stop_loss"))
    # exits: the bearish signal is ALWAYS on (no toggle), the time exit is a toggleable gene 5..20 step 5
    assert [r["id"] for r in strat.exit_rules] == ["mlr_exit_signal", "mlr_exit_time"]
    sig = _rule(strat.exit_rules, "mlr_exit_signal")
    assert not sig.get("toggle_optimize") and not sig.get("toggleOptimize")
    assert _node(sig["conditions"], "mlr_bear")["field"] == "bearish"
    assert not _node(sig["conditions"], "mlr_bear")["optimize"]
    time_rule = _rule(strat.exit_rules, "mlr_exit_time")
    assert time_rule["toggle_optimize"] is True
    days = _node(time_rule["conditions"], "mlr_days")
    assert days["field"] == "days_opened" and days["op"] == ">="
    assert days["optimize"] is True
    assert (days["value_min"], days["value_max"], days["value_step"]) == (5, 20, 5)


def test_rank_top_n_gene_defaults_to_off():
    gene = L._EXPERT_OPT[_ML]["expert_params"]["rank_top_n"]
    assert gene == {"optimize": True, "type": "choice", "choices": [0]}


def test_ml_rank_top_n_flag_sets_the_choices_for_mlr_only():
    spec = L._EXPERT_OPT[_ML]
    assert L._apply_ml_rank_top_n(spec, None, _ML, "MLR", "optimize") is spec
    out = L._apply_ml_rank_top_n(spec, "3,5", _ML, "MLR", "optimize")
    assert out["expert_params"]["rank_top_n"] == {"optimize": True, "type": "choice", "choices": [3, 5]}
    assert L._EXPERT_OPT[_ML]["expert_params"]["rank_top_n"]["choices"] == [0]      # the shared spec is untouched
    assert out["expert_params"]["horizon"] == spec["expert_params"]["horizon"]
    for strat in ("MLH", "MLF", "S1"):
        with pytest.raises(SystemExit, match="only applies to MLForecast with strategy MLR"):
            L._apply_ml_rank_top_n(spec, "3", _ML, strat, "optimize")
    with pytest.raises(SystemExit, match="only applies to MLForecast with strategy MLR"):
        L._apply_ml_rank_top_n(spec, "3", "DeterministicScorer", "MLR", "optimize")
    for bad in ("0", "3,3", "-1,5", "a", ",", "2.5"):
        with pytest.raises(SystemExit, match="--ml-rank-top-n needs"):
            L._apply_ml_rank_top_n(spec, bad, _ML, "MLR", "optimize")


def _parse(argv):
    """Run the REAL ``main`` up to the optimize command and return the parsed args."""
    captured = {}
    original = L._cmd_optimize
    L._cmd_optimize = lambda args: (captured.__setitem__("args", args), 0)[1]
    cwd = os.getcwd()
    try:
        assert L.main(list(argv)) == 0
    finally:
        os.chdir(cwd)
        L._cmd_optimize = original
    return captured["args"]


def test_cli_parses_the_ml_flags():
    base = ["optimize", "--expert", _ML, "--universe", "AAPL", "--start", "2024-03-01", "--end", "2024-04-01"]
    args = _parse(base + ["--strategy", "MLR", "--ml-rank-top-n", "3,5"])
    assert args.ml_rank_top_n == "3,5" and args.ml_hold_from_horizon is False
    args = _parse(base + ["--strategy", "MLH", "--ml-hold-from-horizon", "--ml-model-keys", "a"])
    assert args.ml_hold_from_horizon is True and args.ml_rank_top_n is None


# --------------------------------------------------------------------------- #
# Task 5.2: --ml-hold-from-horizon
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("horizon,expected", [
    (1, (0, 0, 1)),        # round(1.4) - 1 = 0: {-1, 0, 1} clipped at 0 -> 0..1
    (3, (2, 3, 4)),        # round(4.2) - 1 = 3
    (5, (5, 6, 7)),        # 7 - 1 = 6
    (10, (12, 13, 14)),    # 14 - 1 = 13
    (20, (26, 27, 28)),    # 28 - 1 = 27
])
def test_hold_days_formula_values(horizon, expected):
    assert L.ml_hold_days_range(horizon) == expected


@pytest.mark.parametrize("bad", [0, -1, 2.5, True, "5", None])
def test_hold_days_formula_rejects_a_bad_horizon(bad):
    with pytest.raises(ValueError, match="horizon"):
        L.ml_hold_days_range(bad)


@pytest.mark.parametrize("kind", ["MLH", "MLF"])
def test_hold_range_rewrites_the_days_gene(kind):
    default = L._build_strategy(kind, "t", _ML)
    d = _node(default.exit_rules, "mlh_days")
    assert (d["value_min"], d["value_max"], d["value_step"]) == (0, 6, 1)          # flag off: the 0..6 gene
    assert L._apply_ml_hold_range(default, None) is default
    strat = L._apply_ml_hold_range(L._build_strategy(kind, "t", _ML), L.ml_hold_days_range(5))
    d = _node(strat.exit_rules, "mlh_days")
    for lo, hi, step in (("value_min", "value_max", "value_step"), ("valueMin", "valueMax", "valueStep")):
        assert (d[lo], d[hi], d[step]) == (5.0, 7.0, 1.0)
    assert d["value"] == 6.0 and d["optimize"] is True and d["op"] == ">="
    # nothing else changed: the signal exit stays a toggle, the bearish leaf stays
    assert _rule(strat.exit_rules, "mlh_exit_signal")["toggle_optimize"] is True


class _Meta:
    def __init__(self, horizons_by_key):
        self.h = horizons_by_key

    def install(self, monkeypatch):
        from ba2_common.core.ml_forecast import store
        import importlib
        mod = importlib.import_module("ba2_experts.MLForecast")
        monkeypatch.setattr(mod, "resolve_model_key", lambda name: name)
        monkeypatch.setattr(store, "read_meta", lambda key, *a, **k: {"horizons": self.h[key]})


def test_hold_flag_off_is_none(monkeypatch):
    assert L._ml_hold_from_horizon_range(False, None, _ML, "MLH", "optimize") is None


def test_hold_flag_reads_the_stores_horizon(monkeypatch):
    _Meta({"a": [5], "b": [5]}).install(monkeypatch)
    assert L._ml_hold_from_horizon_range(True, "a,b", _ML, "MLH", "optimize") == (5, 6, 7)
    assert L._ml_hold_from_horizon_range(True, "a", _ML, "MLF", "optimize") == (5, 6, 7)
    _Meta({"c": [20]}).install(monkeypatch)
    assert L._ml_hold_from_horizon_range(True, "c", _ML, "MLH", "optimize") == (26, 27, 28)


def test_hold_flag_refuses_mixed_horizons(monkeypatch):
    _Meta({"a": [5], "b": [10]}).install(monkeypatch)
    with pytest.raises(SystemExit, match="do not share one horizon"):
        L._ml_hold_from_horizon_range(True, "a,b", _ML, "MLH", "optimize")


def test_hold_flag_refuses_a_multi_horizon_store_and_missing_keys(monkeypatch):
    _Meta({"a": [5, 10, 20]}).install(monkeypatch)
    with pytest.raises(SystemExit, match="single-horizon"):
        L._ml_hold_from_horizon_range(True, "a", _ML, "MLH", "optimize")
    with pytest.raises(SystemExit, match="needs --ml-model-keys"):
        L._ml_hold_from_horizon_range(True, None, _ML, "MLH", "optimize")


def test_hold_flag_refuses_other_strategies_and_experts(monkeypatch):
    _Meta({"a": [5]}).install(monkeypatch)
    for expert, strat in ((_ML, "MLR"), (_ML, "S1"), ("DeterministicScorer", "MLH")):
        with pytest.raises(SystemExit, match="only applies to MLForecast with strategy MLH or MLF"):
            L._ml_hold_from_horizon_range(True, "a", expert, strat, "optimize")
