"""The equity grid driver's per-expert strategy plan and the ATR grid's strategy selection.

``--strategy-plan`` replaces ``--strategies`` per expert (goal2027atr keeps only the strategies
that reached each expert's pooled goal2020 top 5), and ``--no-budget-overrides`` makes every job
use the grid's own population/generations. Without either flag the driver is unchanged.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

_ROOT = os.path.join(os.path.dirname(__file__), "..", "..", "..")
sys.path.insert(0, os.path.join(_ROOT, "tools"))
sys.path.insert(0, _ROOT)

import run_screener_capband_matrix as M  # noqa: E402
from tools.strategy_research.atr_grid import select_strategies as SEL  # noqa: E402

PLAN = {"experts": {"FMPRating": ["S1", "S6"], "FMPEarningsDrift": ["S5"],
                    "FMPInsiderClusterBuy": ["S1"], "DeterministicScorer": ["S2", "S6"]}}


@pytest.fixture
def captured(monkeypatch, tmp_path):
    calls: list[list[str]] = []
    monkeypatch.setattr(subprocess, "run",
                        lambda cmd, **kw: calls.append(list(cmd)) or SimpleNamespace(returncode=0))
    monkeypatch.setenv("DB_FILE", str(tmp_path / "no-such.db"))
    return calls


def _plan_file(tmp_path, plan=PLAN):
    p = tmp_path / "plan.json"
    p.write_text(json.dumps(plan), encoding="utf-8")
    return str(p)


def _flag(cmd, flag):
    return cmd[cmd.index(flag) + 1] if flag in cmd else None


def test_the_plan_replaces_the_strategy_list_per_expert(tmp_path):
    plan = M.load_strategy_plan(_plan_file(tmp_path))
    jobs = list(M._jobs(["mid"], ["S1", "S2", "S3"], False, frozenset({"FactorRanker"}),
                        "-x", plan))
    got = sorted((e, s) for _n, e, s, _b in jobs)
    assert got == sorted([("FMPRating", "S1"), ("FMPRating", "S6"),
                          ("FMPEarningsDrift", "S5"), ("FMPInsiderClusterBuy", "S1"),
                          ("DeterministicScorer", "S2"), ("DeterministicScorer", "S6")])


def test_without_a_plan_the_jobs_are_unchanged():
    old = list(M._jobs(["mid"], ["S1", "S2"], False, frozenset(), "-x"))
    assert old == list(M._jobs(["mid"], ["S1", "S2"], False, frozenset(), "-x", None))
    assert {(e, s) for _n, e, s, _b in old if s} == {
        (e, s) for e in M._CLASSIC for s in ("S1", "S2")}


@pytest.mark.parametrize("plan, message", [
    ({"experts": {"FMPRating": ["S1"]}}, "no strategy list"),
    ({"experts": {**PLAN["experts"], "FMPRating": []}}, "empty strategy list"),
    ({"experts": {**PLAN["experts"], "FMPRating": ["S9"]}}, "unknown or repeated"),
    ({"experts": {**PLAN["experts"], "FMPRating": ["S1", "S1"]}}, "unknown or repeated"),
    ({"experts": {**PLAN["experts"], "NoSuchExpert": ["S1"]}}, "unknown expert"),
])
def test_a_bad_plan_is_refused(tmp_path, plan, message):
    with pytest.raises(SystemExit, match=message):
        M.load_strategy_plan(_plan_file(tmp_path, plan))


def test_a_skipped_expert_needs_no_entry(tmp_path):
    plan = {"experts": {k: v for k, v in PLAN["experts"].items() if k != "FMPRating"}}
    assert "FMPRating" not in M.load_strategy_plan(_plan_file(tmp_path, plan),
                                                  frozenset({"FMPRating"}))


def test_no_budget_overrides_gives_every_job_the_grid_budget(captured, monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "argv", [
        "run_screener_capband_matrix.py", "--bands", "mid", "--skip-experts", "FactorRanker",
        "--strategy-plan", _plan_file(tmp_path), "--population", "120", "--generations", "30",
        "--no-budget-overrides"])
    assert M.main() == 0
    assert len(captured) == 6
    assert {(_flag(c, "--population"), _flag(c, "--generations")) for c in captured} == {
        ("120", "30")}


def test_the_overrides_still_apply_by_default(captured, monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "argv", [
        "run_screener_capband_matrix.py", "--bands", "mid", "--skip-experts", "FactorRanker",
        "--strategy-plan", _plan_file(tmp_path), "--population", "120", "--generations", "30"])
    assert M.main() == 0
    pops = {(_flag(c, "--expert"), _flag(c, "--strategy")): _flag(c, "--population")
            for c in captured}
    assert pops[("FMPRating", "S1")] == "140"       # S1 override
    assert pops[("FMPRating", "S6")] == "130"       # FMPRating bonus (+10)
    assert pops[("DeterministicScorer", "S2")] == "120"


# --------------------------------------------------------------------------- the selection
def test_parse_job_keeps_only_canonical_goal2020_strategy_jobs():
    assert SEL.parse_job("scr-mid-FMPRating-S6-goal2020-risk_atr-from2022") == (
        "FMPRating", "mid", "S6", "risk_atr")
    assert SEL.parse_job("scr-small-DeterministicScorer-S2-goal2020-notional-ds") == (
        "DeterministicScorer", "small", "S2", "notional")
    assert SEL.parse_job("sen-S5-goal2020-risk_atr") == (
        "FMPSenateTraderWeight", "all", "S5", "risk_atr")
    assert SEL.parse_job("scr-mid-FMPRating-S6-goal2020-risk_atr-probe") is None
    assert SEL.parse_job("scr-mid-FactorRanker-goal2020-riskatr") is None


def test_car_is_annualised_and_a_wipeout_floors_at_minus_100():
    assert SEL.car_pct(100.0, "2020-01-01", "2022-01-01") == pytest.approx(41.4, abs=0.1)
    assert SEL.car_pct(-100.0, "2020-01-01", "2021-01-01") == -100.0


def test_select_keeps_a_strategy_in_any_rankings_top_n():
    def row(i, strat, fit, car):
        return {"backtest_id": i, "expert": "E", "band": "mid", "mode": "risk_atr",
                "strategy": strat, "fitness": fit, "car": car, "calmar": 0.0,
                "max_drawdown": -10.0}
    rows = [row(1, "S1", 9, 1), row(2, "S1", 8, 2), row(3, "S2", 1, 30),
            row(4, "S3", 2, 0.5), row(5, "S7", 0, 0)]
    out = SEL.select(rows, top_n=2, rankings=["fitness", "car"])
    assert out["experts"]["E"] == ["S1", "S2"]          # S2 only by CAR
    assert out["evidence"]["E"]["dropped"] == ["S3", "S7"]
