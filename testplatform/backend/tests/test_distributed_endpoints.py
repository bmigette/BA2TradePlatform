"""Distributed worker endpoints + self_update gate (HTTP claim/result round-trips the broker)."""
import os

import pytest

from app.services.trial_broker import get_broker


@pytest.fixture(scope="module")
def app_and_token():
    os.environ["BA2_WORKER_TOKEN"] = "unit-tok"
    import app.main as m
    from app.models.database import init_db
    init_db()  # create tables in the isolated test DB (TestClient doesn't fire startup events)
    return m.app, "unit-tok"


def test_register_heartbeat_claim_result(app_and_token):
    app, token = app_and_token
    from starlette.testclient import TestClient
    c = TestClient(app)
    H = {"Authorization": f"Bearer {token}"}

    # auth required
    assert c.post("/api/worker/register", json={"name": "x"}).status_code == 401

    reg = c.post("/api/worker/register", json={"name": "ut-worker"}, headers=H)
    assert reg.status_code == 200
    wid = reg.json()["worker_id"]
    assert "git_commit" in reg.json()["version"]

    hb = c.post("/api/worker/heartbeat", json={"worker_id": wid, "active_jobs": 2}, headers=H)
    assert hb.status_code == 200 and "version" in hb.json()

    # empty queue -> 204
    assert c.post("/api/worker/claim-trial", params={"worker_id": wid}, headers=H).status_code == 204

    # submit a trial to the shared broker, claim it over HTTP, post the result over HTTP.
    broker = get_broker()
    broker.clear()
    tid = broker.submit_one("opt-http", {"hello": "world"}, "sharpe")
    claim = c.post("/api/worker/claim-trial", params={"worker_id": wid}, headers=H)
    assert claim.status_code == 200
    job = claim.json()
    assert job["trial_id"] == tid and job["config"] == {"hello": "world"}

    rr = c.post("/api/worker/trial-result", json={
        "trial_id": tid, "ok": True, "fitness": 3.5, "trades": 7,
    }, headers=H)
    assert rr.status_code == 200 and rr.json()["accepted"] is True

    ready = broker.wait_ready({tid}, timeout=1.0)
    assert ready[tid]["fitness"] == 3.5 and ready[tid]["trades"] == 7
    broker.clear()


def test_admin_version_endpoint(app_and_token):
    app, _ = app_and_token
    from starlette.testclient import TestClient
    os.environ["BA2_ADMIN_TOKEN"] = "admin-tok"
    c = TestClient(app)
    assert c.get("/api/admin/version").status_code == 401
    r = c.get("/api/admin/version", headers={"Authorization": "Bearer admin-tok"})
    assert r.status_code == 200
    body = r.json()
    assert "app_version" in body and "git_commit" in body and "root" in body


def test_self_update_helpers():
    from app.services import self_update
    root = self_update.resolve_repo_root()
    assert (root / ".git").exists()
    info = self_update.get_version_info(root)
    assert info["app_version"] != "unknown"
    # restart command for a direct uvicorn launch (no launcher in sys.modules path here)
    import sys
    saved = sys.argv
    try:
        sys.argv = ["/venv/bin/uvicorn", "app.main:app", "--port", "8000"]
        sys.modules.pop("ba2test_launcher", None)
        cmd = self_update.build_restart_command()
        assert cmd[1:3] == ["-m", "uvicorn"] and "app.main:app" in cmd
    finally:
        sys.argv = saved
