"""Idempotency-Key and audit-argument redaction of audited (write) routes."""

import pytest
from fastapi import APIRouter, Depends, FastAPI, HTTPException

from ba2_trade_platform.api import audit, idempotency
from ba2_trade_platform.api.audit import AuditedRoute
from ba2_trade_platform.api.auth import require_access
from tests.api_helpers import PW_KEY, SK, TOKEN, client_for, set_api_token

pytestmark = pytest.mark.api_db


@pytest.fixture(autouse=True)
def fresh_store(monkeypatch):
    monkeypatch.setattr(idempotency, "STORE", idempotency.IdempotencyStore())
    set_api_token()


def _app(calls):
    app = FastAPI(dependencies=[Depends(require_access)])
    router = APIRouter(route_class=AuditedRoute)

    @router.post("/place")
    def place(payload: dict):
        calls.append(payload)
        if payload.get("fail"):
            raise HTTPException(status_code=502, detail="broker said no")
        return {"order": len(calls)}

    app.include_router(router)
    return app


def test_a_repeat_with_the_same_key_returns_the_first_result_without_a_second_call():
    calls = []
    c = client_for(_app(calls), token=TOKEN)
    h = {"Idempotency-Key": "k1"}
    first = c.post("/place", json={"x": 1}, headers=h)
    again = c.post("/place", json={"x": 1}, headers=h)
    assert first.status_code == again.status_code == 200
    assert again.json() == first.json() == {"order": 1}
    assert again.headers["idempotent-replay"] == "true" and "idempotent-replay" not in first.headers
    assert len(calls) == 1


def test_no_key_means_no_dedup():
    calls = []
    c = client_for(_app(calls), token=TOKEN)
    c.post("/place", json={"x": 1})
    c.post("/place", json={"x": 1})
    assert len(calls) == 2


def test_same_key_different_body_is_422_and_not_executed():
    calls = []
    c = client_for(_app(calls), token=TOKEN)
    h = {"Idempotency-Key": "k1"}
    c.post("/place", json={"x": 1}, headers=h)
    r = c.post("/place", json={"x": 2}, headers=h)
    assert r.status_code == 422 and len(calls) == 1


def test_a_failed_attempt_is_not_remembered_so_the_retry_runs():
    calls = []
    c = client_for(_app(calls), token=TOKEN)
    h = {"Idempotency-Key": "k1"}
    assert c.post("/place", json={"fail": True}, headers=h).status_code == 502
    # same key, same body: still a failure that really runs again (nothing stored)
    assert c.post("/place", json={"fail": True}, headers=h).status_code == 502
    assert len(calls) == 2


def test_dry_run_neither_reads_nor_writes_the_store():
    calls = []
    c = client_for(_app(calls), token=TOKEN)
    h = {"Idempotency-Key": "k1"}
    c.post("/place?dry_run=true", json={"x": 1}, headers=h)
    c.post("/place?dry_run=true", json={"x": 1}, headers=h)
    assert len(calls) == 2
    c.post("/place", json={"x": 1}, headers=h)          # the real one still runs
    assert len(calls) == 3


def test_the_key_is_scoped_by_path():
    calls = []
    c = client_for(_app(calls), token=TOKEN)
    h = {"Idempotency-Key": "k1"}
    c.post("/place", json={"x": 1}, headers=h)
    r = c.post("/place?dry_run=1", json={"x": 1}, headers=h)
    assert r.status_code == 200 and len(calls) == 2


def test_store_entries_expire():
    now = [0.0]
    store = idempotency.IdempotencyStore(ttl=600, clock=lambda: now[0])
    assert store.begin("s", "k", "f")[0] == "run"
    store.finish("s", "k", idempotency.Stored(200, b"{}", "application/json"))
    now[0] = 599
    assert store.begin("s", "k", "f")[0] == "replay"
    now[0] = 1300
    assert store.begin("s", "k", "f")[0] == "run"


def test_a_repeat_while_the_first_is_in_flight_is_busy():
    store = idempotency.IdempotencyStore()
    assert store.begin("s", "k", "f")[0] == "run"
    assert store.begin("s", "k", "f")[0] == "busy"          # the first has not finished: it may yet place its order
    store.abort("s", "k")
    assert store.begin("s", "k", "f")[0] == "run"


def test_bad_key_is_422():
    calls = []
    r = client_for(_app(calls), token=TOKEN).post("/place", json={}, headers={"Idempotency-Key": "k" * 300})
    assert r.status_code == 422 and calls == []


def test_audit_arguments_are_redacted(monkeypatch):
    captured = []
    monkeypatch.setattr(audit, "log_activity", lambda *a, **k: captured.append(k["data"]))
    body = {"openai_api_key": SK + "ABCDEF1234567890", PW_KEY: "hunter2hunter2", "note": "use Bearer ZZZTOKEN12345",
            "harmless_name": "valuable-secret-value", "min_confidence": 70}
    c = client_for(_app([]), token=TOKEN)
    assert c.post("/place", json=body).status_code == 200
    arguments = captured[0]["arguments"]
    for leaked in ("ABCDEF1234567890", "hunter2hunter2", "ZZZTOKEN12345"):
        assert leaked not in arguments
    assert "70" in arguments


def test_route_flagged_secret_keys_are_redacted_whatever_their_name(monkeypatch):
    captured = []
    monkeypatch.setattr(audit, "log_activity", lambda *a, **k: captured.append(k["data"]))
    assert "valuable-secret-value" in audit._arguments(b'{"harmless_name": "valuable-secret-value"}')
    assert "valuable-secret-value" not in audit._arguments(b'{"harmless_name": "valuable-secret-value"}',
                                                           {"harmless_name"})
    assert "a=b" in audit._arguments(b"not json a=b")
