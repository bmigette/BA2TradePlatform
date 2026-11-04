"""Audit trail of API writes: one INFO log line and one ActivityLog row (source 'api'), token last 4 only."""
import json

from fastapi import APIRouter, Depends, FastAPI, HTTPException

from ba2_trade_platform.api.audit import AuditedRoute
from ba2_trade_platform.api.auth import require_access
from tests.api_helpers import TOKEN, client_for, set_api_token
import pytest


pytestmark = pytest.mark.api_db


def _app() -> FastAPI:
    app = FastAPI(dependencies=[Depends(require_access)])
    router = APIRouter(route_class=AuditedRoute)

    @router.post("/act")
    def act(payload: dict):
        return {"done": True}

    @router.post("/boom")
    def boom():
        raise HTTPException(status_code=409, detail="busy")

    @router.get("/read")
    def read():
        return {}

    app.include_router(router)
    return app


def test_write_is_audited_with_token_tail_only(monkeypatch):
    set_api_token()
    from ba2_trade_platform.api import audit
    captured = []
    monkeypatch.setattr(audit, "log_activity", lambda *a, **k: captured.append((a, k)))
    messages = []
    class _Rec:     # the platform logger does not propagate to root, so caplog cannot see it
        info = staticmethod(messages.append)
        warning = staticmethod(messages.append)
    monkeypatch.setattr(audit, "logger", _Rec)
    r = client_for(_app(), token=TOKEN).post("/act", json={"x": 1})
    assert r.status_code == 200
    (args, kwargs), = captured
    data = kwargs["data"]
    assert data["source"] == "api" and data["path"] == "/act" and data["status"] == 200
    assert data["token_tail"] == "abcd" and data["client"] == "127.0.0.1"
    assert json.loads(data["arguments"]) == {"x": 1}
    assert TOKEN not in json.dumps(data)
    lines = [m for m in messages if "API write" in m]
    assert lines and "abcd" in lines[0] and TOKEN not in lines[0]


def test_failed_write_is_audited_with_its_status(monkeypatch):
    set_api_token()
    from ba2_trade_platform.api import audit
    captured = []
    monkeypatch.setattr(audit, "log_activity", lambda *a, **k: captured.append(k["data"]))
    assert client_for(_app(), token=TOKEN).post("/boom").status_code == 409
    assert captured[0]["status"] == 409


def test_reads_and_refused_writes_write_no_activity_row(monkeypatch):
    set_api_token()
    from ba2_trade_platform.api import audit
    captured = []
    monkeypatch.setattr(audit, "log_activity", lambda *a, **k: captured.append(k["data"]))
    client_for(_app()).get("/read")
    assert client_for(_app()).post("/act", json={}).status_code == 401   # no bearer
    assert captured == []
