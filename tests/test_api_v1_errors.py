"""The /api/v1 error envelope: {"error", "message", "detail"} for service, HTTP, validation and crash errors."""
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import BaseModel

from ba2_trade_platform.api.errors import install_error_handlers
from ba2_trade_platform.services import errors as se
import pytest


pytestmark = pytest.mark.api_db


class Body(BaseModel):
    n: int


def _client() -> TestClient:
    app = FastAPI()
    install_error_handlers(app)

    @app.get("/nf")
    def nf():
        raise se.NotFound("no such thing", {"id": 7})

    @app.get("/broker")
    def broker():
        raise se.BrokerRefused("40310000 wash trade", {"code": 40310000})

    @app.get("/http")
    def http():
        raise HTTPException(status_code=403, detail="nope")

    @app.get("/coded")
    def coded():
        raise HTTPException(status_code=403, detail={"error": "remote_disabled", "message": "off"})

    @app.get("/crash")
    def crash():
        raise RuntimeError("secret internals")

    @app.post("/v")
    def v(body: Body):
        return body

    return TestClient(app, raise_server_exceptions=False)


def test_service_errors_use_the_envelope():
    r = _client().get("/nf")
    assert r.status_code == 404
    assert r.json() == {"error": "not_found", "message": "no such thing", "detail": {"id": 7}}
    r = _client().get("/broker")
    assert r.status_code == 502 and r.json()["error"] == "broker_refused"
    assert r.json()["message"] == "40310000 wash trade"


def test_http_exceptions_are_enveloped_with_a_code_per_status():
    r = _client().get("/http")
    assert r.status_code == 403
    assert r.json() == {"error": "forbidden", "message": "nope", "detail": {}}


def test_http_exception_with_its_own_error_code_keeps_it():
    r = _client().get("/coded")
    assert r.status_code == 403
    assert r.json() == {"error": "remote_disabled", "message": "off", "detail": {}}


def test_validation_errors_are_422_invalid_with_field_errors():
    r = _client().post("/v", json={"n": "x"})
    assert r.status_code == 422
    body = r.json()
    assert body["error"] == "invalid" and body["detail"]["errors"][0]["loc"][-1] == "n"


def test_unhandled_exception_is_500_internal_without_leaking_the_message():
    r = _client().get("/crash")
    assert r.status_code == 500
    assert r.json() == {"error": "internal", "message": "Internal error (see the platform log)", "detail": {}}


def test_validation_envelope_drops_input_and_ctx():
    r = _client().post("/v", json={"n": "SECRET-VALUE"})
    err = r.json()["detail"]["errors"][0]
    assert "input" not in err and "ctx" not in err and "SECRET-VALUE" not in r.text
    assert err["loc"][-1] == "n" and "msg" in err and "type" in err


def test_500_logs_the_exception_class_name_not_its_message(monkeypatch):
    from ba2_trade_platform.api import errors as api_errors
    lines = []

    class _Rec:
        @staticmethod
        def error(msg, *a, **k):
            lines.append(msg)
    monkeypatch.setattr(api_errors, "logger", _Rec)
    _client().get("/crash")
    assert lines and "RuntimeError" in lines[0] and "secret internals" not in lines[0]


def test_413_is_enveloped_with_its_own_code():
    app = FastAPI()
    install_error_handlers(app)

    @app.get("/big")
    def big():
        raise HTTPException(status_code=413, detail="too big")
    r = TestClient(app).get("/big")
    assert r.status_code == 413 and r.json()["error"] == "payload_too_large"
