"""Daily corporate-actions refresh job of the Alpaca history cache (plan 2026-10-24-history-source-task11, Task 6)."""
import threading
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from apscheduler.schedulers.background import BackgroundScheduler

from ba2_trade_platform.core import JobManager as JM
from ba2_trade_platform.core import account_history

JOB_ID = "history_corporate_actions_job"


@pytest.fixture
def jm(monkeypatch):
    j = JM.JobManager.__new__(JM.JobManager)
    j._scheduler = BackgroundScheduler()
    j._scheduler.start(paused=True)
    j._running = True
    j._scheduled_jobs = {}
    j._lock = threading.Lock()
    yield j
    j._scheduler.shutdown(wait=False)


def stub_accounts(monkeypatch, accounts):
    monkeypatch.setattr(account_history, "alpaca_history_accounts", lambda: accounts)
    monkeypatch.setattr(account_history, "history_job_is_fresh", lambda now=None: True)
    monkeypatch.setattr(account_history, "build_age_problem", lambda today=None: None)


def test_job_scheduled_only_if_an_account_is_alpaca(jm, monkeypatch):
    stub_accounts(monkeypatch, [])
    jm._schedule_history_corporate_actions_job()
    assert jm._scheduler.get_job(JOB_ID) is None
    stub_accounts(monkeypatch, [{"id": 1, "api_key": "k", "api_secret": "s"}])
    jm._schedule_history_corporate_actions_job()
    job = jm._scheduler.get_job(JOB_ID)
    assert job is not None
    f = {x.name: str(x) for x in job.trigger.fields}
    assert f["hour"] == "8" and f["minute"] == "30" and f["day_of_week"] == "mon-fri"
    assert str(job.trigger.timezone) == "America/New_York"


def test_scheduling_logs_an_error_when_the_build_is_older_than_the_backfill_limit(jm, monkeypatch):
    stub_accounts(monkeypatch, [{"id": 1, "api_key": "k", "api_secret": "s"}])
    monkeypatch.setattr(account_history, "build_age_problem", lambda today=None: "build ends 2026-10-12, 40 sessions ago")
    errors = []
    monkeypatch.setattr(JM.logger, "error", lambda m, *a, **k: errors.append(m))
    jm._schedule_history_corporate_actions_job()
    assert any("40 sessions ago" in m for m in errors)


def test_job_removed_when_the_last_alpaca_account_goes(jm, monkeypatch):
    stub_accounts(monkeypatch, [{"id": 1, "api_key": "k", "api_secret": "s"}])
    jm._schedule_history_corporate_actions_job()
    stub_accounts(monkeypatch, [])
    jm._schedule_history_corporate_actions_job()
    assert jm._scheduler.get_job(JOB_ID) is None and JOB_ID not in jm._scheduled_jobs


def test_job_survives_refresh_scheduled_jobs(jm, monkeypatch):
    stub_accounts(monkeypatch, [{"id": 1, "api_key": "k", "api_secret": "s"}])
    for name in ("_schedule_account_refresh_job", "_schedule_iv_snapshot_job", "_schedule_fred_preopen_job",
                 "_schedule_iv_warm_job", "_schedule_warm_jobs", "_schedule_all_expert_jobs"):
        monkeypatch.setattr(jm, name, lambda: None, raising=False)
    jm._schedule_system_jobs()
    assert jm._scheduler.get_job(JOB_ID) is not None
    jm._remove_scheduled_job = JM.JobManager._remove_scheduled_job.__get__(jm)
    jm.refresh_scheduled_jobs()
    assert jm._scheduler.get_job(JOB_ID) is not None


def test_failure_retries_three_times_then_errors(jm, monkeypatch):
    errors = []
    monkeypatch.setattr(JM.logger, "error", lambda m, *a, **k: errors.append(m))
    monkeypatch.setattr(account_history, "run_corporate_actions_refresh",
                        lambda now=None: {"errors": [{"symbol": "AAA", "error": "HTTP 500"}], "applied": []})
    for attempt in range(3):
        jm._execute_history_corporate_actions_refresh(attempt)
        retry = jm._scheduler.get_job(f"{JOB_ID}_retry_{attempt}")
        assert retry is not None, attempt
    jm._execute_history_corporate_actions_refresh(3)
    assert jm._scheduler.get_job(f"{JOB_ID}_retry_3") is None
    assert any("giving up" in m for m in errors)


def test_exception_is_an_error_and_retried(jm, monkeypatch):
    def boom(now=None):
        raise RuntimeError("alpaca down")
    monkeypatch.setattr(account_history, "run_corporate_actions_refresh", boom)
    jm._execute_history_corporate_actions_refresh(0)
    assert jm._scheduler.get_job(f"{JOB_ID}_retry_0") is not None


def test_clean_run_schedules_no_retry(jm, monkeypatch):
    monkeypatch.setattr(account_history, "run_corporate_actions_refresh", lambda now=None: {"errors": [], "applied": []})
    jm._execute_history_corporate_actions_refresh(0)
    assert jm._scheduler.get_job(f"{JOB_ID}_retry_0") is None


def test_stale_status_at_scheduling_time_arms_a_catchup_run(jm, monkeypatch):
    monkeypatch.setattr(account_history, "alpaca_history_accounts", lambda: [{"id": 1, "api_key": "k", "api_secret": "s"}])
    monkeypatch.setattr(account_history, "history_job_is_fresh", lambda now=None: False)
    jm._schedule_history_corporate_actions_job()
    assert jm._scheduler.get_job(f"{JOB_ID}_catchup") is not None
