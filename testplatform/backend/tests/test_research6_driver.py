"""Research driver contract tests; all execution/database/cache fixtures are isolated."""
from copy import deepcopy
import importlib
import itertools
import json
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from tools.strategy_research import profiles as P, runtime as R
from tools.strategy_research import run_goal2020_followups as D
from app.services.strategy_param_space import collect_param_space, decode_params


def space(job):
    return collect_param_space(SimpleNamespace(**job["strategy"]), job["optimization_config"]["expert_params"])


def grid(job):
    axes = space(job)
    values = [[s["min"] + i * s["step"] for i in range(round((s["max"] - s["min"]) / s["step"]) + 1)]
              for s in axes.values()]
    return [dict(zip(axes, values)) for values in itertools.product(*values)]


def one(family, variant):
    return next(j for j in P.build_manifest(families=[family])["jobs"] if j["variant"] == variant)


def test_ten_families_thirty_five_jobs_and_193_concrete_candidates():
    jobs = P.build_manifest()["jobs"]
    assert len(jobs) == 35
    assert {j["family"] for j in jobs} == set(P.FAMILIES)
    assert len([j for j in jobs if j["variant"] == "control"]) == 6
    assert sum(len(grid(j)) for j in jobs) == 193
    for job in jobs:
        D.verify_job(job)
        bt = job["optimization_config"]["backtest"]
        assert bt["initial_capital"] == bt["account_settings"]["starting_cash"] == 10000
        assert bt["account_settings"]["equity_cap"] == 10000
        assert bt["run_schedule_override"]["times"] == ["09:30"]
        days = [k for k, v in bt["run_schedule_override"]["days"].items() if v]
        assert days == (["monday", "tuesday", "wednesday", "thursday", "friday"]
                        if job["family"] in ("pullback", "etf_trend") else ["monday"])
        assert sum(bt["manage_schedule_override"]["days"].values()) == 5
        assert not any(k.startswith(("schedule:", "screener:")) or k.endswith(":enabled") for k in space(job))
        settings = bt["experts"][0]["settings"]
        assert settings["use_atr_stop"] is False and settings["regime_overlay_enabled"] is False
        assert settings["execution_schedule_enter_market"] == bt["run_schedule_override"]
        if "screener_opt" in bt:
            for key, value in bt["screener_opt"]["base_settings"].items():
                assert settings["screener_" + key] == value
        assert bt["start_date"] == ("2022-01-01" if job["family"] in ("small_rating", "analyst_targets") else "2020-01-01")


@pytest.mark.parametrize("family", P.DEPLOYED_FAMILIES)
def test_control_decode_cannot_reenable_old_grid_flags(family):
    job = one(family, "control")
    decoded = decode_params(SimpleNamespace(**job["strategy"]), grid(job)[0])
    assert decoded["entry_rules"] == job["strategy"]["entry_rules"]
    assert decoded["exit_rules"] == job["strategy"]["exit_rules"]
    assert len(grid(job)) == 1
    for rule in decoded["entry_rules"]:
        fields = {n["field"] for n in P.walk(rule["conditions"]) if "field" in n}
        assert {"bullish", "has_no_position"} <= fields
        assert rule["continue_processing"] is False
        assert any(a["action_type"] == "buy" for a in rule["actions"])


def test_saved_action_values_survive_normalization():
    from ba2_common.core.rule_models import TradeRule
    rules = one("large_ds", "control")["strategy"]["entry_rules"]
    actions = TradeRule.model_validate(rules[0]).to_canonical_dict()["actions"]
    assert [a["action_value"] for a in actions[1:]] == [12.0, -8.0]


@pytest.mark.parametrize("family,values", [("mid_insider", [60, 90, 120]),
    ("small_earnings", [60, 90, 120]), ("mid_ds", [15, 20, 25, 30]),
    ("small_rating", [60, 90, 120])])
def test_timeouts_decode_and_are_reachable_before_floor_stop(family, values):
    job = one(family, "timeout")
    holds = []
    for params in grid(job):
        decoded = decode_params(SimpleNamespace(**job["strategy"]), params)
        rules = decoded["exit_rules"]
        matches = [(i, n) for i, r in enumerate(rules) for n in P.walk(r["conditions"])
                   if "field" in n and n["field"] == "days_opened"]
        assert len(matches) == 1
        index, node = matches[0]
        holds.append(node["value"])
        assert rules[index]["actions"] == [{"action_type": "close"}]
        floor = [i for i, r in enumerate(rules) if any(a["action_type"] == "adjust_stop_loss" for a in r["actions"])]
        assert not floor or index < min(floor)
    assert holds == values


def test_quality_ratios_are_coupled_and_source_is_not_mutated():
    before = P.load_baselines()
    jobs = P.build_manifest(families=["large_ds"])["jobs"][1:]
    for job, ratio in zip(jobs, [(0.7, 0.3), (0.5, 0.5), (0.3, 0.7)]):
        s = job["optimization_config"]["backtest"]["experts"][0]["settings"]
        assert (s["w_technical"], s["w_fundamental"]) == ratio
        assert s["w_analyst"] == s["w_earnings"] == s["tw_rsi"] == s["tw_don"] == 0
        assert s["fw_value"] == s["fw_growth"] == 0
        assert {p["model:mom_lookback_days"] for p in grid(job)} == {126, 252}
        assert job["strategy"] == before["large_ds"]["strategy"]
    assert P.load_baselines() == before


def test_independent_experiments_do_not_cross_unrelated_genes():
    fresh = one("mid_insider", "signal_freshness")
    assert set(space(fresh)) == {"model:lookback_days"}
    assert [n["value"] for n in P.walk(fresh["strategy"]) if n.get("field") == "days_opened"] == [210]
    offsets = one("mid_earnings", "target_offset")
    assert [p["entry:buy-2:a1:action_value"] for p in grid(offsets)] == [-14, -12, -10, -8]
    for variant, values in [("common_near", [-14, -4]), ("common_wide", [8, -18])]:
        for rule in one("small_rating", variant)["strategy"]["entry_rules"]:
            assert [a["action_value"] for a in rule["actions"][1:]] == values


def test_fingerprints_distinguish_cap_costs_dates_and_search():
    base = P.build_manifest()
    assert base == P.build_manifest()
    for kwargs in [dict(equity_cap=None), dict(spread_bps=0), dict(start="2021-01-01"), dict(search="genetic")]:
        new = P.build_manifest(**kwargs)
        assert new["jobs"][1]["fingerprint"] != base["jobs"][1]["fingerprint"]
    bt = P.build_manifest(equity_cap=None, spread_bps=0)["jobs"][0]["optimization_config"]["backtest"]
    assert bt["account_settings"]["equity_cap"] is None
    assert bt["account_settings"]["spread_bps"] == 0


@pytest.mark.parametrize("kwargs", [dict(equity=-1), dict(equity=float("nan")), dict(equity_cap=-1),
    dict(start="2027-01-01"), dict(end="2021-01-01"), dict(families=[]),
    dict(families=["mid_ds", "mid_ds"]), dict(parallel=0), dict(workers=["remote227"])])
def test_invalid_campaign_fails_before_launch(kwargs):
    with pytest.raises(ValueError):
        P.build_manifest(**kwargs)


def test_preview_never_opens_db_or_launches(monkeypatch, tmp_path):
    def forbidden(*args, **kwargs):
        pytest.fail("Preview attempted execution or DB access")
    monkeypatch.setattr(sqlite3, "connect", forbidden)
    monkeypatch.setattr(D.subprocess, "run", forbidden)
    assert D.main(["--dry-run", "--output-dir", str(tmp_path)]) == 0
    assert len(json.loads((tmp_path / "manifest.json").read_text())["jobs"]) == 35


def test_bad_or_live_database_refused(tmp_path):
    with pytest.raises(ValueError):
        R.check_database(tmp_path / "absent.sqlite")
    live = tmp_path / "live.sqlite"
    with sqlite3.connect(live) as db:
        db.execute("CREATE TABLE accountinstance(id INTEGER)")
    with pytest.raises(ValueError, match="Not a BA2 backtest"):
        R.check_database(live)


def test_sequential_driver_stops_on_first_child_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(D, "check_database", lambda p: p)
    calls = []
    def fail(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=9)
    monkeypatch.setattr(D.subprocess, "run", fail)
    args = SimpleNamespace(db_file=tmp_path / "test.sqlite", cache_dir=tmp_path, resume=False)
    assert D.run_jobs(P.build_manifest()["jobs"][:2], tmp_path, args) == 9
    assert len(calls) == 1


def test_os_lock_prevents_duplicate_job(tmp_path):
    with R.job_lock(tmp_path / "one.lock"):
        with pytest.raises(RuntimeError):
            with R.job_lock(tmp_path / "one.lock"):
                pytest.fail("Second runner acquired lock")
    with R.job_lock(tmp_path / "one.lock"):
        pass


def test_equal_fitness_neighbours_are_preserved():
    a = {"params": {"x": 1}, "fitness": 0.5}
    b = {"params": {"x": 2}, "fitness": 0.5}
    assert len(R.ranked_results([a, a, b], 5)) == 2
    with pytest.raises(RuntimeError):
        R.ranked_results([{"params": {}, "fitness": float("nan")}], 5)


def test_preflight_reads_actual_screen_and_refuses_missing_files(monkeypatch, tmp_path):
    import pandas as pd
    from ba2_providers.screener import metric_store as ms
    job = one("large_ds", "control")
    job["optimization_config"]["backtest"]["screener_opt"]["store"] = str(tmp_path)
    frame = pd.DataFrame({"date": ["2020-01-02", "2025-12-31"]})
    monkeypatch.setattr(ms, "load_store", lambda _: frame)
    monkeypatch.setattr(ms, "screened_symbol_union", lambda *args: ["AAA"])
    with pytest.raises(ValueError, match="no symbols silently removed"):
        R.preflight(job, tmp_path)
    for interval in ("1d", "5min"):
        pd.DataFrame({"Date": pd.to_datetime(["2018-01-01", "2020-01-02", "2025-12-31"], utc=True)}).to_parquet(tmp_path / f"AAA_{interval}.parquet")
    monkeypatch.setattr(R, "code_signature", lambda: "source-version")
    ready = R.preflight(job, tmp_path)
    assert ready["optimization_config"]["backtest"]["enabled_instruments"] == ["AAA"]
    assert ready["preflight"]["coverage"]["5min"]["covered_pct"] == 100
    D.verify_job(ready)
    assert ready["name"] != job["name"]


def test_execution_persists_once_and_recovers_partial_top_rows(monkeypatch, gate_engine):
    """Use the real ORM and config decoder; replace only the expensive backtest work."""
    from sqlalchemy.orm import sessionmaker
    from app.models import database as database_module
    from app.models.strategy_optimization import StrategyOptimization
    from app.models.backtest import Backtest
    from app.services import strategy_optimization_handler as handler
    from app.services import strategy_fitness as fitness
    R.add_source_paths()
    import ba2test_launcher as launcher
    factory = sessionmaker(bind=gate_engine)
    monkeypatch.setattr(database_module, "SessionLocal", factory)
    monkeypatch.setattr(launcher, "_enter_backend", lambda: None)
    monkeypatch.setattr(handler, "_build_hoisted_state", lambda block: {})
    searches, reruns = [], []
    job = one("mid_ds", "timeout")
    job["name"] += "-isolated-persistence"
    block = job["optimization_config"]["backtest"]
    block["backtest_id"] = "isolated"
    block["enabled_instruments"] = ["AAPL"]
    job["save_top"] = 2

    def search(task_id, payload):
        searches.append(payload["optimization_id"])
        with factory() as db:
            opt = db.get(StrategyOptimization, payload["optimization_id"])
            opt.all_results = [{"params": params, "fitness": 0.5} for params in grid(job)]
            opt.best_params, opt.best_fitness = opt.all_results[0]["params"], 0.5
            opt.status = "completed"
            db.commit()
        return {"status": "completed"}

    def rerun(config):
        reruns.append(config)
        return {"ok": True, "results": {"total_trades": 1, "winning_trades": 1, "losing_trades": 0,
            "win_rate": 100.0, "total_return": 1.0, "sharpe_ratio": 1.0, "max_drawdown": 0.0,
            "profit_factor": 1.0, "avg_trade_duration": 5.0, "final_equity": 10100.0,
            "equity_curve": [], "drawdown_curve": [], "trades": []}}

    monkeypatch.setattr(handler, "handle_strategy_optimization", search)
    monkeypatch.setattr(handler, "_persist_trial_worker", rerun)
    monkeypatch.setattr(fitness, "compute_fitness", lambda *args: 0.5)
    path = Path(gate_engine.url.database)
    first = R.execute_ready(job, path)
    second = R.execute_ready(job, path)
    assert first == second
    assert len(searches) == 1 and len(reruns) == 2
    assert all(c["account_settings"]["equity_cap"] == 10000 for c in reruns)
    assert all(c["run_schedule_override"] == block["run_schedule_override"] for c in reruns)
    with factory() as db:
        bt = db.get(Backtest, first["backtest_ids"][1])
        bt.status = "failed"
        db.commit()
    recovered = R.execute_ready(job, path)
    assert recovered == first
    assert len(searches) == 1 and len(reruns) == 3
    with factory() as db:
        assert db.query(Backtest).filter_by(optimization_id=first["optimization_id"]).count() == 2


def test_generated_settings_are_recognized_by_actual_experts():
    for job in P.build_manifest()["jobs"]:
        expert = getattr(importlib.import_module("ba2_experts." + job["expert"]), job["expert"])
        known = set(expert.get_merged_settings_definitions())
        config = job["optimization_config"]
        assert set(config["backtest"]["experts"][0]["settings"]) <= known
        assert set(config["expert_params"]) <= known
