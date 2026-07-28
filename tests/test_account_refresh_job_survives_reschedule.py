"""Regression: a full expert-schedule refresh must not destroy the account refresh job.

2026-07-23 incident. ``refresh_expert_schedules(None)`` -> ``_refresh_expert_schedules_sync(None)``
called ``scheduler.remove_all_jobs()`` and then re-scheduled ONLY the expert jobs, so the
``account_refresh_job`` interval job was silently deleted and never re-added. Account /
order / transaction reconciliation then stopped for 4 days while the process stayed alive
and kept trading:

    18:24:39  Executing scheduled account refresh   (completed 18:24:44 -- healthy)
    18:28:34  Refreshing expert schedules for expert all experts  <- remove_all_jobs()
    18:29:39  (next account refresh never fired, and never fired again)

Fallout: 46 orders froze in PENDING_NEW, and 4 transactions (DDOG/FPS/LEGN/HIHO) stayed
OPENED after their stop-loss orders had already FILLED, so the platform booked positions
that no longer existed at the broker.
"""

import threading
from datetime import datetime, timedelta
from unittest.mock import patch

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from ba2_trade_platform.core.JobManager import JobManager

ACCOUNT_JOB_ID = "account_refresh_job"


def _make_jobmanager():
    """A JobManager with a real (paused) scheduler but no DB / live-expert side effects."""
    jm = JobManager.__new__(JobManager)
    jm._scheduler = BackgroundScheduler()
    jm._scheduler.start(paused=True)
    jm._running = True
    jm._scheduled_jobs = {}
    jm._lock = threading.Lock()
    jm._live_experts = {}
    # Mirror the watchdog attributes real __init__ sets (we bypassed it via __new__).
    jm._last_account_refresh_completed = None
    jm._watchdog_thread = None
    jm._watchdog_running = False
    # Neutralise everything that would need the DB or real expert threads.
    jm._stop_all_live_experts = lambda: None
    jm._stop_live_expert = lambda _id: None
    jm._schedule_all_expert_jobs = lambda: None
    jm._schedule_expert_analysis = lambda _id: None
    return jm


def _install_account_refresh_stub(jm, calls):
    """Stand in for _schedule_account_refresh_job without touching AppSetting / DB."""

    def _schedule():
        calls.append(1)
        job = jm._scheduler.add_job(
            func=lambda: None,
            trigger=IntervalTrigger(minutes=5),
            id=ACCOUNT_JOB_ID,
            name="Account Refresh Job",
            replace_existing=True,
        )
        jm._scheduled_jobs[ACCOUNT_JOB_ID] = job

    jm._schedule_account_refresh_job = _schedule
    return _schedule


def test_full_schedule_refresh_preserves_account_refresh_job():
    """The UI 'refresh all schedules' path must leave reconciliation scheduled."""
    jm = _make_jobmanager()
    calls = []
    schedule_account_refresh = _install_account_refresh_stub(jm, calls)
    try:
        schedule_account_refresh()  # startup state: job is scheduled
        calls.clear()
        assert jm._scheduler.get_job(ACCOUNT_JOB_ID) is not None

        jm._refresh_expert_schedules_sync(None)

        assert jm._scheduler.get_job(ACCOUNT_JOB_ID) is not None, (
            "account_refresh_job was destroyed by a full expert-schedule refresh -- "
            "account/order/transaction reconciliation silently stops"
        )
    finally:
        jm._scheduler.shutdown(wait=False)


def test_single_expert_refresh_preserves_account_refresh_job():
    """The per-expert path only removes ``expert_<id>_*`` jobs; guard against regression."""
    jm = _make_jobmanager()
    calls = []
    schedule_account_refresh = _install_account_refresh_stub(jm, calls)
    try:
        schedule_account_refresh()
        assert jm._scheduler.get_job(ACCOUNT_JOB_ID) is not None

        jm._refresh_expert_schedules_sync(7)

        assert jm._scheduler.get_job(ACCOUNT_JOB_ID) is not None
    finally:
        jm._scheduler.shutdown(wait=False)


def test_refresh_scheduled_jobs_preserves_account_refresh_job():
    """``refresh_scheduled_jobs()`` walks _scheduled_jobs, which holds the account job too."""
    jm = _make_jobmanager()
    calls = []
    schedule_account_refresh = _install_account_refresh_stub(jm, calls)
    jm._remove_scheduled_job = lambda job_id: (
        jm._scheduler.remove_job(job_id), jm._scheduled_jobs.pop(job_id, None)
    )
    try:
        schedule_account_refresh()
        assert jm._scheduler.get_job(ACCOUNT_JOB_ID) is not None

        jm.refresh_scheduled_jobs()

        assert jm._scheduler.get_job(ACCOUNT_JOB_ID) is not None, (
            "refresh_scheduled_jobs() dropped the account refresh job"
        )
    finally:
        jm._scheduler.shutdown(wait=False)


# ----------------------------------------------------------------------------
# Watchdog
# ----------------------------------------------------------------------------

def test_watchdog_reports_unhealthy_when_job_missing():
    jm = _make_jobmanager()
    _install_account_refresh_stub(jm, [])
    try:
        jm._last_account_refresh_completed = datetime.now()
        healthy, job_present, _since, _threshold = jm.account_refresh_health()
        assert (healthy, job_present) == (False, False)
    finally:
        jm._scheduler.shutdown(wait=False)


def test_watchdog_reports_unhealthy_when_refresh_is_stale():
    """Job still registered but nothing has completed for far too long."""
    jm = _make_jobmanager()
    schedule_account_refresh = _install_account_refresh_stub(jm, [])
    try:
        schedule_account_refresh()
        jm._last_account_refresh_completed = datetime.now() - timedelta(hours=4)

        healthy, job_present, since, threshold = jm.account_refresh_health()

        assert job_present is True
        assert healthy is False
        assert since > threshold
    finally:
        jm._scheduler.shutdown(wait=False)


def test_watchdog_reports_healthy_after_a_recent_refresh():
    jm = _make_jobmanager()
    schedule_account_refresh = _install_account_refresh_stub(jm, [])
    try:
        schedule_account_refresh()
        jm._last_account_refresh_completed = datetime.now()

        healthy, job_present, _since, _threshold = jm.account_refresh_health()

        assert (healthy, job_present) == (True, True)
    finally:
        jm._scheduler.shutdown(wait=False)


def test_watchdog_rearms_a_missing_account_refresh_job():
    """One watchdog pass must restore the job -- detection alone is not enough."""
    jm = _make_jobmanager()
    calls = []
    _install_account_refresh_stub(jm, calls)
    immediate = []
    jm.execute_account_refresh_immediately = lambda: immediate.append(1)
    try:
        assert jm._scheduler.get_job(ACCOUNT_JOB_ID) is None  # job lost

        # Run exactly one pass of the loop body.
        jm._watchdog_running = True

        def _stop_after_first_pass(_seconds):
            jm._watchdog_running = False

        with patch("ba2_trade_platform.core.JobManager.time.sleep", _stop_after_first_pass):
            jm._account_refresh_watchdog_loop(poll_seconds=1)

        assert jm._scheduler.get_job(ACCOUNT_JOB_ID) is not None, "watchdog did not re-arm the job"
        assert calls, "_schedule_account_refresh_job was never called"
        assert immediate, "watchdog should also kick an immediate catch-up refresh"
    finally:
        jm._scheduler.shutdown(wait=False)
