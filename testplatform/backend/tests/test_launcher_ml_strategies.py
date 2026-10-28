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


#: The shipped MLForecast gene space, unchanged by the ranking mode (MLH / MLF / S1 keep their trial sequence).
_PRE_RANKING_GENES = ["model_key", "horizon", "min_edge_pct", "min_prob", "max_uncertainty", "tp_quantile",
                      "sl_quantile", "exit_flip_prob"]


def test_the_shipped_spec_has_no_ranking_gene():
    assert list(L._EXPERT_OPT[_ML]["expert_params"]) == _PRE_RANKING_GENES
    assert "rank_top_n" not in L._EXPERT_OPT[_ML]["fixed_settings"]
    assert L._apply_ml_rank_top_n(L._EXPERT_OPT[_ML], None, _ML, "MLH", "optimize") is L._EXPERT_OPT[_ML]


def test_ml_rank_top_n_flag_adds_the_gene_for_mlr_only():
    spec = L._EXPERT_OPT[_ML]
    out = L._apply_ml_rank_top_n(spec, "3,5", _ML, "MLR", "optimize")
    assert out["expert_params"]["rank_top_n"] == {"optimize": True, "type": "choice", "choices": [3, 5]}
    assert list(out["expert_params"])[:-1] == _PRE_RANKING_GENES            # appended: earlier loci keep their index
    assert "rank_top_n" not in L._EXPERT_OPT[_ML]["expert_params"]          # the shared spec is untouched
    assert out["expert_params"]["horizon"] == spec["expert_params"]["horizon"]
    for strat in ("MLH", "MLF", "S1"):
        with pytest.raises(SystemExit, match="only applies to MLForecast with strategy MLR"):
            L._apply_ml_rank_top_n(spec, "3", _ML, strat, "optimize")
    with pytest.raises(SystemExit, match="only applies to MLForecast with strategy MLR"):
        L._apply_ml_rank_top_n(spec, "3", "DeterministicScorer", "MLR", "optimize")
    for bad in ("0", "3,3", "-1,5", "a", ",", "2.5"):
        with pytest.raises(SystemExit, match="--ml-rank-top-n needs"):
            L._apply_ml_rank_top_n(spec, bad, _ML, "MLR", "optimize")


@pytest.mark.parametrize("raw", [None, ""])
def test_mlr_without_ml_rank_top_n_is_refused(raw):
    with pytest.raises(SystemExit, match="MLR needs --ml-rank-top-n"):
        L._apply_ml_rank_top_n(L._EXPERT_OPT[_ML], raw, _ML, "MLR", "optimize")


def _block(universe=("MSFT", "aapl", "AAPL", "NVDA"), **extra):
    return {"enabled_instruments": list(universe), "experts": [{"class": _ML, "settings": {"enable_sell": False}}],
            **extra}


def test_rank_universe_is_the_jobs_sorted_symbols_for_mlr_only():
    b = _block()
    L._apply_ml_rank_universe("optimize", b, "MLR")
    assert b["experts"][0]["settings"] == {"enable_sell": False, "rank_universe": "AAPL,MSFT,NVDA"}
    for strat in ("MLH", "MLF", "S1"):
        other = _block()
        L._apply_ml_rank_universe("optimize", other, strat)
        assert "rank_universe" not in other["experts"][0]["settings"]
    with pytest.raises(SystemExit, match="cannot run with a screener"):
        L._apply_ml_rank_universe("optimize", _block(screener_opt={"x": 1}), "MLR")
    with pytest.raises(SystemExit, match="non-empty universe"):
        L._apply_ml_rank_universe("optimize", _block(universe=()), "MLR")


def test_rank_universe_is_part_of_the_checkpoint_identity():
    from app.services import strategy_optimization_handler as H
    cfg = lambda u: {"experts": [{"class": _ML, "settings": {"rank_universe": u}}]}   # noqa: E731
    assert H.checkpoint_expert_settings_identity(cfg("")) == {}
    assert H.checkpoint_expert_settings_identity(cfg("AAPL,MSFT")) == {"MLForecast.rank_universe": "AAPL,MSFT"}
    assert H.CHECKPOINT_IDENTITY_EXPERT_SETTINGS["rank_universe"] ==         __import__("ba2_experts.MLForecast", fromlist=["x"]).MLForecast.get_settings_definitions()["rank_universe"]["default"]


def test_optimize_batch_refuses_mlr(monkeypatch):
    import app.services.task_queue as TQ
    monkeypatch.setattr(TQ, "get_task_queue", lambda: pytest.fail("queued"))
    args = _parse_cmd("_cmd_optimize_batch", ["optimize-batch", "--experts", _ML, "--strategies", "MLR",
                                              "--universe", "AAPL", "--start", "2024-03-01", "--end", "2024-04-01"])
    with pytest.raises(SystemExit, match="optimize-batch: strategy MLR"):
        L._cmd_optimize_batch(args)


def _parse_cmd(cmd_attr, argv):
    captured = {}
    original = getattr(L, cmd_attr)
    setattr(L, cmd_attr, lambda args: (captured.__setitem__("args", args), 0)[1])
    cwd = os.getcwd()
    try:
        assert L.main(list(argv)) == 0
    finally:
        os.chdir(cwd)
        setattr(L, cmd_attr, original)
    return captured["args"]


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


# --------------------------------------------------------------------------- #
# The real optimize command -> the persisted run config
# --------------------------------------------------------------------------- #
def _run_optimize(monkeypatch, strategy, extra, universe="MSFT,AAPL,NVDA"):
    import ba2_common.core.db as bdb
    from app.models.database import SessionLocal
    from app.models.strategy_optimization import StrategyOptimization
    import app.services.strategy_optimization_handler as SOH
    monkeypatch.setattr(SOH, "handle_strategy_optimization", lambda task_id, payload: {"status": "completed"})
    monkeypatch.setattr(L, "_persist_top_backtests", lambda *a, **k: 0)
    args = _parse_cmd("_cmd_optimize", ["optimize", "--expert", _ML, "--strategy", strategy, "--universe", universe,
                                        "--start", "2024-03-01", "--end", "2024-04-01", "--population", "2",
                                        "--generations", "1", "--rerun", "--name", f"mlr-e2e-{strategy}-{os.getpid()}",
                                        *extra])
    saved = (os.getcwd(), bdb._db_file)
    try:
        assert L._cmd_optimize(args) == 0
    finally:
        os.chdir(saved[0])
        bdb._db_file = saved[1]
    db = SessionLocal()
    try:
        return db.query(StrategyOptimization).order_by(StrategyOptimization.id.desc()).first().optimization_config
    finally:
        db.close()


def test_mlr_job_persists_the_gene_and_the_universe_as_a_fixed_setting(monkeypatch):
    cfg = _run_optimize(monkeypatch, "MLR", ["--ml-rank-top-n", "3,5"])
    assert cfg["expert_params"]["rank_top_n"] == {"optimize": True, "type": "choice", "choices": [3, 5]}
    settings = cfg["backtest"]["experts"][0]["settings"]
    assert settings["rank_universe"] == "AAPL,MSFT,NVDA" and settings["enable_sell"] is False


def test_mlr_universe_is_the_final_one_after_exclusions(monkeypatch):
    cfg = _run_optimize(monkeypatch, "MLR", ["--ml-rank-top-n", "3", "--exclude-symbols", "NVDA"])
    assert cfg["backtest"]["enabled_instruments"] == ["MSFT", "AAPL"]
    assert cfg["backtest"]["experts"][0]["settings"]["rank_universe"] == "AAPL,MSFT"


@pytest.mark.parametrize("strategy", ["MLH", "MLF", "S1"])
def test_other_strategies_keep_the_gene_space_and_settings(monkeypatch, strategy):
    cfg = _run_optimize(monkeypatch, strategy, [])
    assert "rank_top_n" not in cfg["expert_params"]
    assert list(cfg["expert_params"])[:len(_PRE_RANKING_GENES)] == _PRE_RANKING_GENES
    assert "rank_universe" not in cfg["backtest"]["experts"][0]["settings"]


def test_mlr_job_without_the_flag_is_refused(monkeypatch):
    with pytest.raises(SystemExit, match="MLR needs --ml-rank-top-n"):
        _run_optimize(monkeypatch, "MLR", [])
