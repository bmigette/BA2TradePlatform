"""Tests for POST /api/reload (ba2_trade_platform/ui/api_routes.py) — the DB reload callback
that drops cached expert/account instances (+ their settings caches) so a live process picks
up out-of-band DB edits without a restart.

The router is mounted on a throwaway FastAPI app (not the full NiceGUI ``app``, which needs
``ui.run()`` to bind) — ``include_router`` is all TestClient needs to exercise the routes.
"""
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ba2_trade_platform.core.AccountInstanceCache import AccountInstanceCache
from ba2_trade_platform.core.ExpertInstanceCache import ExpertInstanceCache
from ba2_trade_platform.ui import api_routes
from tests.factories import create_account_definition, create_expert_instance


def _client():
    app = FastAPI()
    app.include_router(api_routes.router)
    return TestClient(app)


def test_reload_scoped_to_expert_invalidates_only_that_instance():
    acct = create_account_definition()
    inst_a = create_expert_instance(account_id=acct.id, expert="MockExpert")
    inst_b = create_expert_instance(account_id=acct.id, expert="MockExpert")

    # Populate the cache directly (bypassing the plugin-class lookup in get_expert_instance_from_id)
    ExpertInstanceCache._cache[inst_a.id] = object()
    ExpertInstanceCache._cache[inst_b.id] = object()

    r = _client().post("/api/reload", json={"expert_instance_id": inst_a.id})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ok"
    assert body["expertsReloaded"] == [inst_a.id]
    assert body["accountsReloaded"] == []

    assert inst_a.id not in ExpertInstanceCache._cache
    assert inst_b.id in ExpertInstanceCache._cache  # untouched
    del ExpertInstanceCache._cache[inst_b.id]


def test_reload_scoped_to_account_invalidates_only_that_instance():
    acct_a = create_account_definition()
    acct_b = create_account_definition()
    AccountInstanceCache._cache[acct_a.id] = object()
    AccountInstanceCache._cache[acct_b.id] = object()

    r = _client().post("/api/reload", json={"account_id": acct_a.id})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["accountsReloaded"] == [acct_a.id]
    assert body["expertsReloaded"] == []

    assert acct_a.id not in AccountInstanceCache._cache
    assert acct_b.id in AccountInstanceCache._cache
    del AccountInstanceCache._cache[acct_b.id]


def test_reload_with_no_scope_clears_everything():
    acct = create_account_definition()
    inst = create_expert_instance(account_id=acct.id, expert="MockExpert")
    ExpertInstanceCache._cache[inst.id] = object()
    AccountInstanceCache._cache[acct.id] = object()

    r = _client().post("/api/reload", json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert inst.id in body["expertsReloaded"]
    assert body["accountsReloaded"] == "all"

    assert ExpertInstanceCache._cache == {}
    assert AccountInstanceCache._cache == {}


def test_reload_with_empty_body_defaults_to_clear_everything():
    """The Pydantic default (both fields None) behaves the same as an explicit {}."""
    ExpertInstanceCache._cache[999] = object()
    r = _client().post("/api/reload")
    assert r.status_code == 200, r.text
    assert ExpertInstanceCache._cache == {}


def test_reload_response_reports_schedule_refresh_queued():
    r = _client().post("/api/reload", json={})
    assert r.status_code == 200, r.text
    # JobManager isn't started in this test process, but refresh_expert_schedules() itself
    # must not raise (it early-returns if not running) -- the endpoint should still report
    # the queue attempt succeeded (no exception raised while queuing).
    assert r.json()["schedulesRefreshQueued"] is True


def _job_manager_on_main_thread(monkeypatch):
    """A REAL JobManager, marked running but WITHOUT spinning its actual background control
    thread: this test suite's shared engine is ``sqlite:///:memory:`` with the SQLAlchemy
    default pool, which hands each THREAD its own separate (and therefore empty, un-migrated)
    in-memory database -- a real control thread would see "no such table: expertinstance" for
    rows this test just wrote on the main thread. Draining the control queue synchronously (see
    _drain_control_queue below) exercises the exact same queue-message + _refresh_expert_
    schedules_sync code path the real thread runs, just on the connection this test's writes are
    actually visible on. Swapped in as the module-level singleton so get_job_manager() inside
    the endpoint resolves to it.

    Spies on _schedule_expert_jobs instead of asserting real APScheduler jobs got created: a
    bare MockExpert instance has no execution_schedule_enter_market/enabled-instruments settings
    configured, so _schedule_expert_jobs would legitimately no-op past that point. What this
    proves is the DISCOVERY step -- was the expert instance looked up and handed to the
    scheduling routine at all -- which is exactly what "a new expert wasn't picked up without a
    restart" would fail at.
    """
    import ba2_trade_platform.core.JobManager as job_manager_module

    jm = job_manager_module.JobManager()
    seen_ids = []
    monkeypatch.setattr(jm, "_schedule_expert_jobs", lambda ei: seen_ids.append(ei.id))
    jm._running = True  # skip .start(): no control thread, no initial _schedule_all_expert_jobs
    monkeypatch.setattr(job_manager_module, "_job_manager_instance", jm)
    return jm, seen_ids


def _drain_control_queue(jm):
    """Synchronously process every message currently queued, mirroring
    JobManager._control_thread_worker's REFRESH_EXPERT_SCHEDULES branch -- on the CALLING
    thread, so it sees the same DB connection the test's writes went through."""
    import queue as _queue
    while True:
        try:
            message = jm._control_queue.get_nowait()
        except _queue.Empty:
            return
        jm._refresh_expert_schedules_sync(message.expert_instance_id)
        jm._control_queue.task_done()


def test_reload_scoped_discovers_expert_instance_created_after_startup(monkeypatch):
    """An ExpertInstance row created AFTER the JobManager already started (out-of-band DB
    insert, e.g. via the DB editor or another process) must be picked up by a SCOPED reload --
    proving "load a new expert" works, not just "refresh a known one"."""
    jm, seen_ids = _job_manager_on_main_thread(monkeypatch)

    acct = create_account_definition()
    new_inst = create_expert_instance(account_id=acct.id, expert="MockExpert")
    assert new_inst.id not in seen_ids  # JobManager started before this row existed

    r = _client().post("/api/reload", json={"expert_instance_id": new_inst.id})
    assert r.status_code == 200, r.text

    _drain_control_queue(jm)
    assert new_inst.id in seen_ids, "new expert instance was never scheduled after a scoped reload"


def test_reload_with_no_scope_discovers_expert_instance_created_after_startup(monkeypatch):
    """Same guarantee via the full (no-scope) reload path, which re-queries ALL enabled
    ExpertInstance rows from the DB (_schedule_all_expert_jobs -> get_all_instances) rather than
    walking any stale in-memory list -- so it picks up new rows for free."""
    jm, seen_ids = _job_manager_on_main_thread(monkeypatch)

    acct = create_account_definition()
    new_inst = create_expert_instance(account_id=acct.id, expert="MockExpert")
    assert new_inst.id not in seen_ids

    r = _client().post("/api/reload", json={})
    assert r.status_code == 200, r.text

    _drain_control_queue(jm)
    assert new_inst.id in seen_ids, "new expert instance was never scheduled after a full reload"
