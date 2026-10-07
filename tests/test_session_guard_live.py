"""The live SESSION GUARD: an entry / open-positions pass does not fire outside a regular session.

Mirrors the backtest (no bar, no decision): a 15:30 schedule on a 13:00 half day, a weekday holiday
and a weekend are skipped loudly; the calendar being unreadable refuses the pass.
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from ba2_common.core import market_calendar as MC
from ba2_trade_platform.core import JobManager as JM
from ba2_trade_platform.core.types import AnalysisUseCase

NY = ZoneInfo("America/New_York")


def _at(y, m, d, hh, mm):
    return datetime(y, m, d, hh, mm, tzinfo=NY)


# 2025-11-28 is the day after Thanksgiving (13:00 close); 2025-12-25 a weekday holiday.
HALF_DAY = (2025, 11, 28)
HOLIDAY = (2025, 12, 25)
NORMAL = (2025, 12, 3)


@pytest.mark.parametrize("when, ok", [
    (_at(*NORMAL, 9, 30), True), (_at(*NORMAL, 15, 50), True), (_at(*NORMAL, 16, 0), False),
    (_at(*NORMAL, 9, 25), False),
    (_at(*HALF_DAY, 12, 0), True), (_at(*HALF_DAY, 13, 0), False),
    (_at(*HALF_DAY, 15, 30), False), (_at(*HALF_DAY, 15, 50), False),
    (_at(*HOLIDAY, 9, 30), False), (_at(2025, 12, 6, 10, 0), False),
])
def test_regular_session_status(when, ok):
    assert MC.regular_session_status(when)[0] is ok
    if not ok:
        assert MC.regular_session_status(when)[1]


def test_a_naive_instant_is_refused():
    with pytest.raises(ValueError):
        MC.regular_session_status(datetime(2025, 12, 3, 10, 0))


def _jm():
    jm = JM.JobManager.__new__(JM.JobManager)
    jm._symbol_is_crypto = lambda symbol: symbol == "BTC"
    return jm


def _job(expert=7, symbol="SCREENER", subtype=AnalysisUseCase.ENTER_MARKET):
    return SimpleNamespace(args=[expert, symbol, subtype])


def _warnings(monkeypatch):
    seen = []
    monkeypatch.setattr(JM.logger, "warning", lambda m, *a, **k: seen.append(m))
    return seen


def _levels(monkeypatch):
    """[(level, message)] for every warning/error the guard logs."""
    seen = []
    monkeypatch.setattr(JM.logger, "warning", lambda m, *a, **k: seen.append(("WARNING", m)))
    monkeypatch.setattr(JM.logger, "error", lambda m, *a, **k: seen.append(("ERROR", m)))
    return seen


def test_normal_day_runs_and_logs_nothing(monkeypatch):
    seen = _warnings(monkeypatch)
    assert _jm()._session_guard_allows(_job(), _at(*NORMAL, 9, 30)) is True
    assert seen == []


@pytest.mark.parametrize("day, hh, mm, needle", [
    (HALF_DAY, 15, 30, "early close"), (HALF_DAY, 15, 50, "early close"),
    (HOLIDAY, 9, 30, "holiday"), ((2025, 12, 6), 9, 30, "weekend"),
])
def test_skipped_with_one_loud_line_per_expert_subtype_day(monkeypatch, day, hh, mm, needle):
    seen = _warnings(monkeypatch)
    jm = _jm()
    when = _at(*day, hh, mm)
    assert jm._session_guard_allows(_job(), when) is False
    assert jm._session_guard_allows(_job(), when) is False          # same key: no second line
    assert jm._session_guard_allows(_job(subtype=AnalysisUseCase.OPEN_POSITIONS), when) is False
    assert len(seen) == 2 and all("SESSION GUARD" in m and needle in m for m in seen)
    assert all("scheduled_session_guard_enabled" in m for m in seen)       # names the kill switch
    assert "ENTER_MARKET" in seen[0].upper() or "enter_market" in seen[0]


def test_an_unreadable_calendar_refuses_the_pass_as_an_ERROR(monkeypatch):
    seen = _levels(monkeypatch)

    def boom(instant):
        raise MC.MarketCalendarUnavailable("no pandas_market_calendars")
    monkeypatch.setattr(MC, "regular_session_status", boom)
    assert _jm()._session_guard_allows(_job(), _at(*NORMAL, 10, 0)) is False
    assert seen[0][0] == "ERROR" and "session check failed" in seen[0][1]
    assert "MarketCalendarUnavailable" in seen[0][1]


def test_a_naive_fire_instant_is_refused_loudly_not_crashing_the_dispatch(monkeypatch):
    seen = _levels(monkeypatch)
    assert _jm()._session_guard_allows(_job(), datetime(2025, 12, 3, 9, 30)) is False
    assert seen[0][0] == "ERROR"


def test_a_crypto_instrument_is_refused_not_assumed_nyse(monkeypatch):
    seen = _levels(monkeypatch)
    assert _jm()._session_guard_allows(_job(symbol="BTC"), _at(*NORMAL, 10, 0)) is False
    assert seen[0][0] == "ERROR" and "crypto" in seen[0][1]


@pytest.mark.parametrize("day, hh, mm, level", [
    (HOLIDAY, 9, 30, "WARNING"), ((2025, 12, 6), 9, 30, "WARNING"),       # holiday, weekend
    (HALF_DAY, 15, 30, "WARNING"), (HALF_DAY, 13, 0, "WARNING"),          # after the early close
    (NORMAL, 8, 0, "ERROR"), (NORMAL, 16, 0, "ERROR"), (NORMAL, 17, 30, "ERROR"),  # normal day
])
def test_the_skip_level_separates_a_routine_closed_day_from_an_incident(monkeypatch, day, hh, mm, level):
    seen = _levels(monkeypatch)
    assert _jm()._session_guard_allows(_job(), _at(*day, hh, mm)) is False
    assert [lv for lv, _ in seen] == [level]


def test_a_skipped_group_submits_nothing_and_parks_nothing(monkeypatch):
    """Entry AND open-positions both due on a half day at 15:30: neither reaches the experts, no
    run is registered, so no OPEN_POSITIONS pass can be parked waiting for an entry pass."""
    when = _at(*HALF_DAY, 15, 30)
    jm = _jm()
    jm._dispatch_lock = threading.Lock()
    jm._lock = threading.RLock()
    jm._dispatched_slots = set()
    trigger = SimpleNamespace(get_next_fire_time=lambda prev, now: when)
    jobs = {f"expert_7_{n}": SimpleNamespace(args=[7, s, st], trigger=trigger, next_run_time=when)
            for n, s, st in (("a", "SCREENER", AnalysisUseCase.ENTER_MARKET),
                             ("b", "OPEN_POSITIONS", AnalysisUseCase.OPEN_POSITIONS))}
    jm._scheduled_jobs = jobs
    jm._session_guard_enabled = lambda announce=False: True
    registered, sealed, executed = [], [], []
    queue = SimpleNamespace(_expert_priority=SimpleNamespace(
        register=lambda runs: registered.append(list(runs)), seal=lambda ids: sealed.append(list(ids))))
    monkeypatch.setattr(JM, "get_worker_queue", lambda: queue)
    monkeypatch.setattr(JM, "get_instance", lambda *a, **k: pytest.fail("no expert may be loaded"))
    monkeypatch.setattr(jm, "_execute_scheduled_analysis", lambda *a, **k: executed.append(a),
                        raising=False)
    _warnings(monkeypatch)
    jm._execute_scheduled_group(7, "SCREENER", AnalysisUseCase.ENTER_MARKET, scheduled_for=when)
    assert executed == [] and registered == [[]] and sealed == [[]]
