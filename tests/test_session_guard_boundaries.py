"""Session guard: boundaries around the open, DST weekends, the kill switch and the UI warnings.

THE FIRE INSTANT is APScheduler's scheduled run time (``ScheduledExpertExecutor`` passes
``run_times[-1]`` as ``scheduled_for``), never ``now()`` at callback entry: a callback delayed by a
busy pool still carries the scheduled instant, computed by the trigger's own timezone, so a prod
schedule (market basis, 09:30) reaches the guard as exactly 09:30:00.000 New York.
"""
from __future__ import annotations

import threading
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from apscheduler.triggers.cron import CronTrigger

from ba2_trade_platform.core import JobManager as JM
from ba2_trade_platform.core.types import AnalysisUseCase

NY = ZoneInfo("America/New_York")
DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
PROD_SCHEDULE = {"days": {d: d not in ("saturday", "sunday") for d in DAYS},
                 "times": ["09:30"], "time_basis": "market"}
HALF_DAY = (2025, 11, 28)
NORMAL = (2025, 12, 3)


def _at(y, m, d, hh, mm):
    return datetime(y, m, d, hh, mm, tzinfo=NY)


def _jm():
    jm = JM.JobManager.__new__(JM.JobManager)
    jm._symbol_is_crypto = lambda symbol: False
    return jm


def _job(expert=7, symbol="SCREENER", subtype=AnalysisUseCase.ENTER_MARKET):
    return SimpleNamespace(args=[expert, symbol, subtype])


def _levels(monkeypatch):
    seen = []
    monkeypatch.setattr(JM.logger, "warning", lambda m, *a, **k: seen.append(("WARNING", m)))
    monkeypatch.setattr(JM.logger, "error", lambda m, *a, **k: seen.append(("ERROR", m)))
    return seen


@pytest.mark.parametrize("label, day", [
    ("normal Wednesday", NORMAL),
    ("Monday after the US DST start", (2026, 3, 9)),
    ("Monday after the EU DST start", (2026, 3, 30)),
    ("Monday after the US DST end", (2026, 11, 2)),
    ("Monday after the EU DST end", (2026, 10, 26)),
    ("half day", HALF_DAY),
])
def test_the_prod_schedule_fires_inside_the_session_at_exactly_0930(monkeypatch, label, day):
    seen = _levels(monkeypatch)
    trigger = _jm()._parse_schedule(PROD_SCHEDULE)
    fire = trigger.get_next_fire_time(None, _at(*day, 0, 0) - timedelta(hours=4))
    assert fire.astimezone(NY) == _at(*day, 9, 30), label
    assert fire.astimezone(NY).strftime("%H:%M:%S.%f") == "09:30:00.000000"
    jm = _jm()
    assert jm._session_guard_allows(_job(), fire) is True, label
    for later in (timedelta(microseconds=1), timedelta(seconds=1), timedelta(minutes=10)):
        assert jm._session_guard_allows(_job(), fire + later) is True      # a delayed callback
    assert seen == []


def test_the_open_is_inclusive_and_one_microsecond_earlier_is_not(monkeypatch):
    _levels(monkeypatch)
    jm = _jm()
    assert jm._session_guard_allows(_job(), _at(*NORMAL, 9, 30)) is True
    assert jm._session_guard_allows(_job(), _at(*NORMAL, 9, 30) - timedelta(microseconds=1)) is False


def test_the_instant_is_judged_in_new_york_whatever_timezone_it_arrives_in(monkeypatch):
    _levels(monkeypatch)
    inst = _at(*NORMAL, 9, 30)
    for tz in ("UTC", "Europe/Paris", "America/New_York", "Asia/Tokyo"):
        assert _jm()._session_guard_allows(_job(), inst.astimezone(ZoneInfo(tz))) is True, tz


@pytest.mark.parametrize("label, day, hh, mm, ok", [
    ("Paris 09:30, 6 h gap", NORMAL, 9, 30, False),
    ("Paris 09:30, 5 h gap (US DST, EU not yet)", (2026, 3, 16), 9, 30, False),
    ("Paris 15:30, 6 h gap", NORMAL, 15, 30, True),
    ("Paris 15:30, 5 h gap", (2026, 3, 16), 15, 30, True),
    ("Paris 14:30, 5 h gap", (2026, 3, 16), 14, 30, True),
    ("Paris 14:30, 6 h gap", NORMAL, 14, 30, False),
])
def test_a_local_basis_schedule_on_a_paris_machine(monkeypatch, label, day, hh, mm, ok):
    paris = ZoneInfo("Europe/Paris")
    trigger = CronTrigger(hour=hh, minute=mm, day_of_week="0,1,2,3,4", timezone=paris)
    fire = trigger.get_next_fire_time(None, datetime(*day, 0, 5, tzinfo=paris))
    seen = _levels(monkeypatch)
    assert _jm()._session_guard_allows(_job(), fire) is ok, label
    if not ok:
        assert seen and seen[0][0] == "ERROR"       # a normal trading day: an incident


class _Session:
    def __init__(self, value):
        self.value = value

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def exec(self, stmt):
        return SimpleNamespace(first=lambda: None if self.value is None
                               else SimpleNamespace(value_str=self.value))


def test_the_kill_switch_default_is_declared_on_and_a_stored_false_turns_it_off(monkeypatch):
    from ba2_trade_platform.core.app_settings import APP_SETTINGS_DEFINITIONS, app_setting_default
    assert APP_SETTINGS_DEFINITIONS[JM.SESSION_GUARD_SETTING]["type"] == "bool"
    assert app_setting_default(JM.SESSION_GUARD_SETTING) is True
    monkeypatch.setattr(JM, "get_db", lambda: SimpleNamespace(bind=None))
    infos, warns, errs = [], [], []
    monkeypatch.setattr(JM.logger, "info", lambda m, *a, **k: infos.append(m))
    monkeypatch.setattr(JM.logger, "warning", lambda m, *a, **k: warns.append(m))
    monkeypatch.setattr(JM.logger, "error", lambda m, *a, **k: errs.append(m))
    for stored, expected in ((None, True), ("", True), ("true", True), ("false", False),
                             ("0", False), ("maybe", True)):
        monkeypatch.setattr(JM, "Session", lambda bind, v=stored: _Session(v))
        infos.clear(), warns.clear(), errs.clear()
        assert _jm()._session_guard_enabled(announce=True) is expected, stored
        if stored == "maybe":
            assert errs and "keeping the guard ON" in errs[0]
        elif expected:
            assert infos and "ENABLED" in infos[0] and JM.SESSION_GUARD_SETTING in infos[0]
        else:
            assert warns and "DISABLED" in warns[0] and "NO session check" in warns[0]


def test_a_disabled_guard_lets_the_pass_through_and_says_so(monkeypatch):
    when = _at(2025, 12, 25, 9, 30)                        # a holiday
    jm = _jm()
    jm._dispatch_lock, jm._lock, jm._dispatched_slots = threading.Lock(), threading.RLock(), set()
    trigger = SimpleNamespace(get_next_fire_time=lambda prev, now: when)
    job = SimpleNamespace(args=[7, "SCREENER", AnalysisUseCase.ENTER_MARKET], trigger=trigger,
                          next_run_time=when)
    jm._scheduled_jobs = {"expert_7_a": job}
    jm._session_guard_enabled = lambda announce=False: False
    warns = []
    monkeypatch.setattr(JM.logger, "warning", lambda m, *a, **k: warns.append(m))
    queue = SimpleNamespace(_expert_priority=SimpleNamespace(register=lambda runs: None,
                                                             seal=lambda ids: None))
    monkeypatch.setattr(JM, "get_worker_queue", lambda: queue)
    monkeypatch.setattr(JM, "get_instance", lambda *a, **k: None)
    jm._execute_scheduled_group(7, "SCREENER", AnalysisUseCase.ENTER_MARKET, scheduled_for=when)
    assert any("DISABLED by app setting 'scheduled_session_guard_enabled'" in m for m in warns)


def test_the_ui_warns_outside_the_session_and_too_close_to_the_close():
    from ba2_common.core.schedule_genes import schedule_time_warnings
    assert schedule_time_warnings(["09:30", "10:00", "15:45"], "market") == []
    out = schedule_time_warnings(["09:29", "16:00", "17:30", "08:00"], "market")
    assert len(out) == 4 and all("this pass will be skipped: the market is closed" in w for w in out)
    late = schedule_time_warnings(["15:50", "15:59"], "market")
    assert len(late) == 2 and all("too close to the close to finish" in w for w in late)
    loc = schedule_time_warnings(["09:30"], "local", ZoneInfo("Europe/Paris"))
    assert len(loc) == 1 and "this pass will be skipped" in loc[0] and "local" in loc[0]
    with pytest.raises(ValueError):
        schedule_time_warnings(["09:30"], "local")
