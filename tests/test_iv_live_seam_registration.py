"""Live host wiring of the iv-v1 Alpha Vantage chain reader (2026-11-15): registered, SILENT, inert.

Prod (8081) pulls this code with no iv gate anywhere. What changes for such an instance must be
nothing: the registration reads no setting, opens no file, makes no call and logs nothing at INFO
or above; an expert without a chain profile never builds (or even asks for) a chain reader.
"""
from __future__ import annotations

import logging

from ba2_common.core.market_condition_iv_live import clear_live_chain_readers, live_chain_reader_factory


def test_the_registration_is_silent_and_registers_the_alphavantage_reader(caplog, monkeypatch):
    import requests

    from ba2_trade_platform.core import seam_wiring
    from ba2_providers.market_conditions.av_live_chain import make_reader

    def no_network(*a, **k):
        raise AssertionError("the registration must not make a request")
    monkeypatch.setattr(requests, "get", no_network)
    logged = []
    for level in ("info", "warning", "error"):
        monkeypatch.setattr(seam_wiring.logger, level, lambda msg, *a, _l=level, **k: logged.append((_l, msg)))
    clear_live_chain_readers()
    try:
        seam_wiring._register_live_chain_readers()
        assert logged == []
        assert live_chain_reader_factory("alphavantage") is make_reader
        from ba2_trade_platform.modules.dataproviders.options.iv_chain_prefetch import make_tastytrade_chain_reader
        assert live_chain_reader_factory("tastytrade") is make_tastytrade_chain_reader
        assert live_chain_reader_factory("thetadata") is None
    finally:
        clear_live_chain_readers()


def test_an_expert_without_a_chain_profile_never_reaches_the_chain_seam(monkeypatch):
    from ba2_common.core import instance_resolver
    from ba2_common.core import market_condition_iv_live as ivl
    from ba2_common.core import market_condition_live as live

    class _E:
        settings = {}

    monkeypatch.setattr(instance_resolver, "get_instance_resolver",
                        lambda: type("R", (), {"get_expert_instance": lambda self, i: _E()})())
    asked = []
    monkeypatch.setattr(ivl, "live_chain_reader_factory", lambda src: asked.append(src))
    pir = live.PerInstanceMarketConditionResolver()
    assert pir.resolver_for(7) is None
    assert asked == [] and pir._resolvers == {}


def test_an_import_error_of_the_reader_does_not_abort_startup_and_iv_gates_refuse(caplog, monkeypatch):
    import sys

    from ba2_common.core.market_condition_readers import LiveUnavailableMarketConditionReader
    from ba2_trade_platform.core import seam_wiring

    monkeypatch.setitem(sys.modules, "ba2_providers.market_conditions.av_live_chain", None)   # ImportError
    warned = []
    monkeypatch.setattr(seam_wiring.logger, "warning", lambda msg, *a, **k: warned.append(str(msg)))
    clear_live_chain_readers()
    try:
        seam_wiring._register_live_chain_readers()
        assert len(warned) == 1 and "could not be imported" in warned[0]
        reader = live_chain_reader_factory("alphavantage")("iv-v1", cache_root=None, source_profile="x")
        assert isinstance(reader, LiveUnavailableMarketConditionReader)
        assert "could not be imported" in reader.reason
    finally:
        clear_live_chain_readers()


class _FakeScheduler:
    def __init__(self):
        self.added, self.removed = [], []

    def add_job(self, **kw):
        self.added.append(kw["id"])
        return object()

    def remove_job(self, job_id):
        self.removed.append(job_id)


def _bare_job_manager():
    from ba2_trade_platform.core.JobManager import JobManager

    jm = JobManager.__new__(JobManager)
    jm._scheduler = _FakeScheduler()
    jm._scheduled_jobs = {}
    return jm


def test_no_prefetch_job_and_no_log_without_an_active_iv_gate(caplog, monkeypatch):
    from ba2_trade_platform.modules.dataproviders.options import iv_chain_prefetch as ivp

    monkeypatch.setattr(ivp, "has_active_iv_gates", lambda: False)
    from ba2_trade_platform.core import JobManager as jm_mod

    logged = []
    for level in ("info", "warning", "error"):
        monkeypatch.setattr(jm_mod.logger, level, lambda msg, *a, _l=level, **k: logged.append((_l, msg)))
    jm = _bare_job_manager()
    jm._schedule_iv_chain_prefetch_job()
    assert jm._scheduler.added == [] and jm._scheduled_jobs == {}
    assert logged == []


def test_the_prefetch_job_is_scheduled_with_a_gate_and_removed_after_the_last_one(monkeypatch):
    from ba2_trade_platform.core.JobManager import IV_CHAIN_PREFETCH_JOB_ID
    from ba2_trade_platform.modules.dataproviders.options import iv_chain_prefetch as ivp

    jm = _bare_job_manager()
    monkeypatch.setattr(ivp, "has_active_iv_gates", lambda: True)
    monkeypatch.setattr(ivp, "configured_source_value", lambda: "alphavantage")
    jm._schedule_iv_chain_prefetch_job()
    assert jm._scheduler.added == [IV_CHAIN_PREFETCH_JOB_ID] and IV_CHAIN_PREFETCH_JOB_ID in jm._scheduled_jobs
    monkeypatch.setattr(ivp, "has_active_iv_gates", lambda: False)
    jm._schedule_iv_chain_prefetch_job()
    assert jm._scheduler.removed == [IV_CHAIN_PREFETCH_JOB_ID] and jm._scheduled_jobs == {}


def test_a_fresh_installation_has_no_active_iv_gate():
    """The real check against the test DB (no iv-v1 setting row): the cheap pre-filter stops it."""
    from ba2_trade_platform.modules.dataproviders.options import iv_chain_prefetch as ivp

    assert ivp.has_active_iv_gates() is False
    assert ivp.run_prefetch_once() == {"skipped": "no active iv-v1 gate"}


# --------------------------------------------------------------------------- re-review 2026-11-15
def test_job_manager_stop_sets_the_prefetch_stop_flag_and_start_clears_it(monkeypatch):
    """Item 1: stop()/shutdown() halt a running prefetch at its next check; a restart re-arms it."""
    from ba2_providers.market_conditions import av_live_chain as avl
    from ba2_trade_platform.core import JobManager as jm_mod

    avl.clear_stop()
    jm_mod._iv_prefetch_stop(True)
    assert avl._STOP.is_set()
    jm_mod._iv_prefetch_stop(False)
    assert not avl._STOP.is_set()
    jm = _bare_job_manager()
    jm._running = True
    jm._lock = __import__("threading").Lock()
    for name in ("_stop_account_refresh_watchdog", "_stop_all_live_experts", "_stop_control_thread"):
        monkeypatch.setattr(jm, name, lambda: None)
    jm._scheduler.remove_all_jobs = lambda: None
    jm.stop()
    assert avl._STOP.is_set()
    avl.clear_stop()


def test_a_restart_inside_the_window_runs_the_prefetch_once(monkeypatch):
    """Item 2: catch-up after a restart past 06:30 and before the deadline."""
    import threading

    from ba2_trade_platform.core.JobManager import IV_CHAIN_PREFETCH_JOB_ID
    from ba2_trade_platform.modules.dataproviders.options import iv_chain_prefetch as ivp

    ran = threading.Event()
    jm = _bare_job_manager()
    monkeypatch.setattr(jm, "_execute_iv_chain_prefetch", ran.set)
    monkeypatch.setattr(ivp, "in_window", lambda now=None: False)
    jm._scheduled_jobs[IV_CHAIN_PREFETCH_JOB_ID] = object()
    jm._iv_chain_prefetch_catchup_if_due()
    assert not ran.wait(0.2)
    monkeypatch.setattr(ivp, "in_window", lambda now=None: True)
    jm._iv_chain_prefetch_catchup_if_due()
    assert ran.wait(2.0)
    ran.clear()
    jm._scheduled_jobs.clear()                    # no active gate -> no job -> no catch-up
    jm._iv_chain_prefetch_catchup_if_due()
    assert not ran.wait(0.2)


def test_the_prefetch_window(monkeypatch):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from ba2_trade_platform.modules.dataproviders.options import iv_chain_prefetch as ivp

    ny = ZoneInfo("America/New_York")
    assert ivp.in_prefetch_window(datetime(2025, 11, 17, 7, 0, tzinfo=ny))          # Monday 07:00
    assert not ivp.in_prefetch_window(datetime(2025, 11, 17, 6, 0, tzinfo=ny))
    assert not ivp.in_prefetch_window(datetime(2025, 11, 17, 9, 50, tzinfo=ny))
    assert not ivp.in_prefetch_window(datetime(2025, 11, 16, 7, 0, tzinfo=ny))      # Sunday


def test_a_single_expert_refresh_reschedules_the_prefetch(monkeypatch):
    """Item 2: adding/enabling/disabling one expert (the single-expert refresh branch) re-evaluates
    the prefetch job."""
    import inspect

    from ba2_trade_platform.core.JobManager import JobManager

    src = inspect.getsource(JobManager._refresh_expert_schedules_sync)
    single = src[:src.index("            else:\n                # Stop all live experts before full refresh")]
    assert "self._schedule_iv_chain_prefetch_job()" in single
