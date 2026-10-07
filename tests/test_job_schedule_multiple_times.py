"""The live schedule fires at EVERY configured time (audit item 21, 2026-10-07).

``JobManager._parse_schedule`` used ``times[0]`` only (a TODO since 2025-09) while the settings UI
accepts several times and the backtest honours them all. One scheduler job per (expert, symbol,
subtype) is kept -- its trigger is an ``OrTrigger`` when there is more than one time -- so job ids stay
unique and the removal / refresh paths are untouched.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.combining import OrTrigger
from apscheduler.triggers.cron import CronTrigger

from ba2_trade_platform.core.JobManager import JobManager, parse_schedule_times

NY = ZoneInfo("America/New_York")
ALL_DAYS = {d: True for d in ("monday", "tuesday", "wednesday", "thursday", "friday",
                              "saturday", "sunday")}


def _jm() -> JobManager:
    return JobManager.__new__(JobManager)


def _fires(trigger, start: datetime, n: int):
    """The next ``n`` fire times from ``start``."""
    out, previous, now = [], None, start
    for _ in range(n):
        nxt = trigger.get_next_fire_time(previous, now)
        if nxt is None:
            break
        out.append(nxt)
        previous, now = nxt, nxt + timedelta(seconds=1)
    return out


def test_several_times_all_fire_on_the_enabled_days():
    schedule = {"days": {**{d: False for d in ALL_DAYS}, "monday": True, "wednesday": True},
                "times": ["15:00", "09:30"], "time_basis": "market"}
    trigger = _jm()._parse_schedule(schedule)
    assert isinstance(trigger, OrTrigger)
    start = datetime(2026, 10, 5, 0, 0, tzinfo=NY)                     # a Monday
    fires = [f.astimezone(NY) for f in _fires(trigger, start, 5)]
    assert [(f.strftime("%a %H:%M")) for f in fires] == [
        "Mon 09:30", "Mon 15:00", "Wed 09:30", "Wed 15:00", "Mon 09:30"]


def test_a_single_time_keeps_the_plain_cron_trigger():
    trigger = _jm()._parse_schedule({"days": {"monday": True}, "times": ["09:30"]})
    assert isinstance(trigger, CronTrigger)


def test_duplicate_and_unordered_times_collapse():
    assert parse_schedule_times(["15:00", "09:30", "9:30", "15:00"]) == [(9, 30), (15, 0)]


@pytest.mark.parametrize("bad", ["24:00", "09:60", "nine", "09:30:00x"])
def test_an_unparseable_time_yields_no_trigger_not_a_partial_one(bad):
    assert _jm()._parse_schedule({"days": {"monday": True}, "times": ["09:30", bad]}) is None


def test_monthly_schedules_every_time_too():
    trigger = _jm()._parse_schedule({"frequency": "monthly", "ordinal": 1, "weekday": "monday",
                                     "times": ["09:30", "15:30"], "time_basis": "market"})
    assert isinstance(trigger, OrTrigger)
    start = datetime(2026, 10, 1, 0, 0, tzinfo=NY)
    fires = [f.astimezone(NY) for f in _fires(trigger, start, 2)]
    assert [f.strftime("%Y-%m-%d %H:%M") for f in fires] == ["2026-10-05 09:30", "2026-10-05 15:30"]


# ---- a weekday missing from ``days``: Mon-Fri enabled, Sat/Sun disabled (the UI's declared default) ----
def test_a_missing_weekday_key_follows_the_ui_default_weekdays_on_weekend_off():
    """An absent Monday-Friday key is ENABLED and an absent Saturday/Sunday key is DISABLED -- what
    the settings UI shows for an absent day (``settings.py`` ``_load_schedule_config``) and what the
    backtest sees (it has no weekend bars). Explicit values always win."""
    schedule = {"days": {"monday": True, "tuesday": False}, "times": ["09:30"]}
    trigger = _jm()._parse_schedule(schedule)
    live_days = sorted({f.weekday() for f in _fires(trigger, datetime(2026, 10, 5, 0, 0), 30)})
    assert live_days == [0, 2, 3, 4]                       # Mon, Wed, Thu, Fri; Tue explicit False; no weekend
    every = _jm()._parse_schedule({"days": {}, "times": ["09:30"]})
    assert sorted({f.weekday() for f in _fires(every, datetime(2026, 10, 5, 0, 0), 30)}) == [0, 1, 2, 3, 4]


def test_an_explicit_weekend_true_still_fires_on_the_weekend():
    trigger = _jm()._parse_schedule({"days": {"saturday": True, "monday": False}, "times": ["09:30"]})
    assert sorted({f.weekday() for f in _fires(trigger, datetime(2026, 10, 5, 0, 0), 30)}) == [1, 2, 3, 4, 5]


@pytest.fixture
def jm_errors(monkeypatch):
    import ba2_trade_platform.core.JobManager as module
    seen = []
    monkeypatch.setattr(module.logger, "error", lambda msg, *a, **k: seen.append(str(msg)))
    return seen


def test_an_unknown_day_key_refuses_the_whole_schedule_with_one_loud_error(jm_errors):
    """A typo such as "wensday" is never ignored. The schedule is REFUSED (no trigger) rather than
    run on the valid days: the typo day would otherwise fall to its absent-key default, trading on a
    day the owner may have meant to switch off."""
    schedule = {"days": {**ALL_DAYS, "wensday": False}, "times": ["09:30"]}
    assert _jm()._parse_schedule(schedule, context="expert instance 5, setting "
                                                    "execution_schedule_enter_market") is None
    assert len(jm_errors) == 1
    assert "wensday" in jm_errors[0] and "expert instance 5" in jm_errors[0]
    assert "execution_schedule_enter_market" in jm_errors[0]


def test_a_job_with_an_unknown_day_key_is_not_created(manager, jm_errors):
    manager._create_scheduled_job(_expert(5), "AAPL", {"days": {"mondey": True}, "times": ["09:30"]},
                                  "enter_market")
    assert manager._scheduler.get_jobs() == []
    assert any("mondey" in m and "expert instance 5" in m and "execution_schedule_enter_market" in m
               for m in jm_errors)


def test_all_listed_weekdays_false_still_schedules_nothing():
    assert _jm()._parse_schedule({"days": dict.fromkeys(ALL_DAYS, False), "times": ["09:30"]}) is None


# ---- the job-level paths: unique ids, removal and re-adding on a settings change -------------------
@pytest.fixture
def manager():
    jm = _jm()
    jm._scheduler = BackgroundScheduler(executors={"default": ThreadPoolExecutor(1),
                                                   "experts": ThreadPoolExecutor(1)})
    jm._scheduler.start(paused=True)
    jm._scheduled_jobs = {}
    yield jm
    jm._scheduler.shutdown(wait=False)


def _expert(i):
    return SimpleNamespace(id=i)


def test_multiple_times_make_one_job_per_symbol_and_subtype_with_unique_ids(manager):
    schedule = {"days": dict(ALL_DAYS), "times": ["09:30", "12:00", "15:30"]}
    for symbol in ("AAPL", "MSFT"):
        manager._create_scheduled_job(_expert(5), symbol, schedule, "enter_market")
        manager._create_scheduled_job(_expert(5), symbol, schedule, "open_positions")
    ids = [j.id for j in manager._scheduler.get_jobs()]
    assert len(ids) == len(set(ids)) == 4
    assert set(ids) == set(manager._scheduled_jobs)
    assert all(isinstance(j.trigger, OrTrigger) for j in manager._scheduler.get_jobs())


def test_changing_the_times_replaces_the_job_and_removal_by_expert_prefix_still_works(manager):
    manager._create_scheduled_job(_expert(5), "AAPL", {"days": dict(ALL_DAYS), "times": ["09:30", "15:30"]},
                                  "enter_market")
    manager._create_scheduled_job(_expert(51), "AAPL", {"days": dict(ALL_DAYS), "times": ["10:00"]},
                                  "enter_market")
    # a settings change re-adds the same id: replaced, not duplicated
    manager._create_scheduled_job(_expert(5), "AAPL", {"days": dict(ALL_DAYS), "times": ["11:00"]},
                                  "enter_market")
    jobs = {j.id: j for j in manager._scheduler.get_jobs()}
    assert len(jobs) == 2 and isinstance(jobs["expert_5_symbol_AAPL_subtype_enter_market"].trigger,
                                         CronTrigger)
    # the refresh path removes by the "expert_<id>_" prefix: expert 5 only, never expert 51
    for job_id in [i for i in list(manager._scheduled_jobs) if i.startswith("expert_5_")]:
        manager._scheduler.remove_job(job_id)
        del manager._scheduled_jobs[job_id]
    assert [j.id for j in manager._scheduler.get_jobs()] == ["expert_51_symbol_AAPL_subtype_enter_market"]


def test_the_cohort_dispatcher_finds_a_multi_time_job_due_at_each_of_its_times():
    """``_execute_scheduled_group`` picks its due jobs with ``trigger.get_next_fire_time(None, fire)
    == fire``: an OrTrigger must answer that for EACH of its times, or the later one never dispatches."""
    trigger = _jm()._parse_schedule({"days": dict(ALL_DAYS), "times": ["09:30", "15:30"],
                                     "time_basis": "market"})
    for hhmm in ((9, 30), (15, 30)):
        fire = datetime(2026, 10, 6, *hhmm, tzinfo=NY)
        assert trigger.get_next_fire_time(None, fire) == fire
