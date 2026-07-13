"""Tests for POST /api/run-schedule (ba2_trade_platform/ui/api_routes.py) — the API
equivalent of the Scheduled Jobs page's "Run Now" button: fires a currently-registered
schedule immediately instead of waiting for its next cron occurrence.
"""
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ba2_trade_platform.core.types import AnalysisUseCase
from ba2_trade_platform.ui import api_routes


def _client():
    app = FastAPI()
    app.include_router(api_routes.router)
    return TestClient(app)


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
