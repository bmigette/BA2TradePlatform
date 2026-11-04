"""The /api/v1 sub-app: auth on everything incl. docs, envelope errors, the three maintenance endpoints,
and the legacy /api/* aliases staying alive behind the same auth."""
from fastapi import FastAPI

from ba2_trade_platform.api.app import create_v1_app
from ba2_trade_platform.core.ExpertInstanceCache import ExpertInstanceCache
from ba2_trade_platform.ui import api_routes
from ba2_trade_platform.version import APP_VERSION
from tests.api_helpers import TOKEN, client_for, set_allow_remote, set_api_token


def test_ping_reports_version_to_local_reader_without_token():
    r = client_for(create_v1_app()).get("/ping")
    assert r.status_code == 200 and r.json() == {"status": "ok", "app_version": APP_VERSION}


def test_docs_and_openapi_follow_the_read_rule():
    set_api_token()
    set_allow_remote(True)
    local = client_for(create_v1_app())
    assert local.get("/openapi.json").status_code == 200
    assert "text/html" in local.get("/docs").headers["content-type"]
    remote = client_for(create_v1_app(), host="10.1.1.1")
    assert remote.get("/openapi.json").status_code == 401
    assert remote.get("/docs").status_code == 401
    assert client_for(create_v1_app(), host="10.1.1.1", token=TOKEN).get("/openapi.json").status_code == 200


def test_remote_is_refused_everywhere_while_api_allow_remote_is_off():
    set_api_token()
    remote = client_for(create_v1_app(), host="10.1.1.1", token=TOKEN)
    for path in ("/ping", "/openapi.json", "/docs"):
        r = remote.get(path)
        assert r.status_code == 403 and r.json()["error"] == "remote_disabled", path
    assert remote.post("/reload", json={}).status_code == 403


def test_openapi_lists_the_three_maintenance_posts_and_ping():
    spec = client_for(create_v1_app()).get("/openapi.json").json()
    for path in ("/reload", "/run-schedule", "/process-recommendations"):
        assert "post" in spec["paths"][path]
    assert "get" in spec["paths"]["/ping"]


def test_unknown_route_is_an_enveloped_404():
    r = client_for(create_v1_app()).get("/nope")
    assert r.status_code == 404 and r.json()["error"] == "not_found"


def test_v1_reload_needs_the_write_token_and_works_with_it():
    ExpertInstanceCache._cache[991] = object()
    assert client_for(create_v1_app()).post("/reload", json={}).status_code == 403   # no token set
    set_api_token()
    assert client_for(create_v1_app()).post("/reload", json={}).status_code == 401
    r = client_for(create_v1_app(), token=TOKEN).post("/reload", json={})
    assert r.status_code == 200 and r.json()["status"] == "ok"


def test_mounted_under_api_v1_next_to_the_legacy_aliases():
    set_api_token()
    host = FastAPI()
    host.include_router(api_routes.router)
    host.mount("/api/v1", create_v1_app())
    c = client_for(host, token=TOKEN)
    assert c.get("/api/v1/ping").status_code == 200
    assert c.post("/api/v1/reload", json={}).status_code == 200
    assert c.post("/api/reload", json={}).status_code == 200            # legacy alias, same handler
    # The alias is behind the same auth (design: it also applies to the three existing endpoints).
    assert client_for(host).post("/api/reload", json={}).status_code == 401


def test_legacy_alias_with_no_token_set_is_403():
    host = FastAPI()
    host.include_router(api_routes.router)
    assert client_for(host).post("/api/reload", json={}).status_code == 403


def test_legacy_alias_from_remote_is_refused_while_api_allow_remote_is_off():
    set_api_token()
    host = FastAPI()
    host.include_router(api_routes.router)
    r = client_for(host, host="10.1.1.1", token=TOKEN).post("/api/reload", json={})
    assert r.status_code == 403 and r.json()["detail"]["error"] == "remote_disabled"
    set_allow_remote(True)
    assert client_for(host, host="10.1.1.1", token=TOKEN).post("/api/reload", json={}).status_code == 200
