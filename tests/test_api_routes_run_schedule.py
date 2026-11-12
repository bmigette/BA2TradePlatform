"""Tests for POST /api/run-schedule (ba2_trade_platform/ui/api_routes.py) — the API
equivalent of the Scheduled Jobs page's "Run Now" button: fires a currently-registered
schedule immediately instead of waiting for its next cron occurrence.
"""
from types import SimpleNamespace

import pytest
from fastapi import FastAPI

from ba2_trade_platform.core.types import AnalysisUseCase
from ba2_trade_platform.ui import api_routes
from tests.api_helpers import TOKEN, client_for, set_api_token


pytestmark = pytest.mark.api_db


def _client():
    set_api_token()
    app = FastAPI()
    app.include_router(api_routes.router)
    return client_for(app, token=TOKEN)


def _job_manager_with_scheduled_job(monkeypatch, expert_instance_id, symbol, subtype):
    """A real JobManager instance (not started) with ONE fake scheduled-job entry, matching
    the ``{job_id: apscheduler.job.Job}`` shape ``_scheduled_jobs`` holds in production —
    only the ``.args`` attribute the endpoint reads is faked."""
    import ba2_trade_platform.core.JobManager as job_manager_module

    jm = job_manager_module.JobManager()
    fake_job = SimpleNamespace(args=[expert_instance_id, symbol, subtype])
    jm._scheduled_jobs = {f"expert_{expert_instance_id}_symbol_{symbol}_subtype_{subtype}": fake_job}
    monkeypatch.setattr(job_manager_module, "_job_manager_instance", jm)
    return jm


def test_run_schedule_now_screener_job_routes_through_expansion_task(monkeypatch):
    jm = _job_manager_with_scheduled_job(monkeypatch, 1, "SCREENER", AnalysisUseCase.ENTER_MARKET)

    calls = []

    class _FakeWorkerQueue:
        def submit_instrument_expansion_task(self, **kwargs):
            calls.append(kwargs)
            return "expansion-task-42"

    import ba2_trade_platform.core.JobManager as job_manager_module
    monkeypatch.setattr(job_manager_module, "get_worker_queue", lambda: _FakeWorkerQueue())

    r = _client().post("/api/run-schedule", json={"expert_instance_id": 1, "subtype": "enter_market"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body == {
        "status": "ok", "task_id": "expansion-task-42",
        "expert_instance_id": 1, "symbol": "SCREENER", "subtype": "enter_market",
        "tasks": [{"symbol": "SCREENER", "task_id": "expansion-task-42"}], "skipped": [],
    }
    assert len(calls) == 1
    assert calls[0]["expert_instance_id"] == 1
    assert calls[0]["expansion_type"] == "SCREENER"
    # Manual trigger bypasses balance/transaction checks — see submit_market_analysis's
    # special-symbol branch, which doesn't thread those flags into the expansion task itself
    # (they only matter for regular-symbol submissions), so no assertion needed here beyond
    # the task having been queued at all.


def test_run_schedule_now_no_matching_job_is_404(monkeypatch):
    _job_manager_with_scheduled_job(monkeypatch, 1, "SCREENER", AnalysisUseCase.ENTER_MARKET)

    r = _client().post("/api/run-schedule", json={"expert_instance_id": 999, "subtype": "enter_market"})
    assert r.status_code == 404
    assert "999" in r.json()["detail"]


def test_run_schedule_now_wrong_subtype_is_404(monkeypatch):
    _job_manager_with_scheduled_job(monkeypatch, 1, "SCREENER", AnalysisUseCase.ENTER_MARKET)

    r = _client().post("/api/run-schedule", json={"expert_instance_id": 1, "subtype": "open_positions"})
    assert r.status_code == 404


def test_run_schedule_now_invalid_subtype_is_400(monkeypatch):
    _job_manager_with_scheduled_job(monkeypatch, 1, "SCREENER", AnalysisUseCase.ENTER_MARKET)

    r = _client().post("/api/run-schedule", json={"expert_instance_id": 1, "subtype": "not_a_real_subtype"})
    assert r.status_code == 400


def _static_jobs(monkeypatch, expert_instance_id, symbols, subtype, submit):
    """A JobManager whose expert has one scheduled job PER SYMBOL (a static universe) and whose
    ``submit_market_analysis`` is ``submit`` -- the seam the endpoint calls once per job."""
    import ba2_trade_platform.core.JobManager as job_manager_module

    jm = job_manager_module.JobManager()
    jm._scheduled_jobs = {
        f"expert_{expert_instance_id}_symbol_{s}_subtype_{subtype}":
            SimpleNamespace(args=[expert_instance_id, s, subtype]) for s in symbols}
    # another expert's job and this expert's other subtype must not be fired
    jm._scheduled_jobs["other"] = SimpleNamespace(args=[expert_instance_id + 1, "ZZZ", subtype])
    jm._scheduled_jobs["manage"] = SimpleNamespace(
        args=[expert_instance_id, "OPEN_POSITIONS", AnalysisUseCase.OPEN_POSITIONS])
    monkeypatch.setattr(jm, "submit_market_analysis", submit)
    monkeypatch.setattr(job_manager_module, "_job_manager_instance", jm)
    return jm


def test_run_schedule_now_static_universe_fires_every_symbol(monkeypatch):
    """2026-11-12: a 97-name static expert answered 200 after analysing its FIRST name only."""
    calls = []

    def submit(expert_instance_id, symbol, **kwargs):
        calls.append((expert_instance_id, symbol, kwargs))
        return f"task-{symbol}"

    _static_jobs(monkeypatch, 6, ["AAPL", "MA", "V"], AnalysisUseCase.ENTER_MARKET, submit)
    r = _client().post("/api/run-schedule", json={"expert_instance_id": 6, "subtype": "enter_market"})
    assert r.status_code == 200, r.text
    assert [c[1] for c in calls] == ["AAPL", "MA", "V"]
    assert all(c[0] == 6 and c[2] == {"subtype": AnalysisUseCase.ENTER_MARKET,
                                      "bypass_balance_check": True,
                                      "bypass_transaction_check": True} for c in calls)
    body = r.json()
    assert body["tasks"] == [{"symbol": s, "task_id": f"task-{s}"} for s in ("AAPL", "MA", "V")]
    assert body["skipped"] == []
    assert (body["symbol"], body["task_id"]) == ("AAPL", "task-AAPL")


def test_run_schedule_now_reports_a_refused_symbol_and_still_fires_the_rest(monkeypatch):
    def submit(expert_instance_id, symbol, **kwargs):
        if symbol == "AAPL":
            raise ValueError("existing transaction for AAPL")
        if symbol == "MA":
            return None
        return f"task-{symbol}"

    _static_jobs(monkeypatch, 6, ["AAPL", "MA", "V"], AnalysisUseCase.ENTER_MARKET, submit)
    r = _client().post("/api/run-schedule", json={"expert_instance_id": 6, "subtype": "enter_market"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["tasks"] == [{"symbol": "V", "task_id": "task-V"}]
    assert [s["symbol"] for s in body["skipped"]] == ["AAPL", "MA"]
    assert "existing transaction for AAPL" in body["skipped"][0]["reason"]
    assert (body["symbol"], body["task_id"]) == ("V", "task-V")


def test_run_schedule_now_every_symbol_refused_is_409_naming_each(monkeypatch):
    def submit(expert_instance_id, symbol, **kwargs):
        raise ValueError(f"expert disabled ({symbol})")

    _static_jobs(monkeypatch, 6, ["AAPL", "V"], AnalysisUseCase.ENTER_MARKET, submit)
    r = _client().post("/api/run-schedule", json={"expert_instance_id": 6, "subtype": "enter_market"})
    assert r.status_code == 409
    assert "AAPL" in r.json()["detail"] and "V: expert disabled (V)" in r.json()["detail"]


def test_run_schedule_dry_run_lists_every_symbol_and_submits_nothing(monkeypatch):
    def submit(*a, **k):
        raise AssertionError("a dry run must not submit")

    _static_jobs(monkeypatch, 6, ["AAPL", "V"], AnalysisUseCase.ENTER_MARKET, submit)
    out = api_routes.run_schedule(6, "enter_market", dry_run=True)
    assert out["symbols"] == ["AAPL", "V"] and out["symbol"] == "AAPL" and out["would_submit"] is True
