"""Invariants over the whole /api/v1 route table."""
import pytest

from ba2_trade_platform.api.app import create_v1_app
from tests.api_helpers import TOKEN, client_for, set_allow_remote, set_api_token

pytestmark = pytest.mark.api_db

_PARAMS = {"{account_id}": "1", "{transaction_id}": "1", "{expert_id}": "1", "{analysis_id}": "1",
           "{ref}": "rm-1"}


def _routes():
    app = create_v1_app()
    return [(m, r.path) for r in app.routes if hasattr(r, "methods") for m in r.methods
            if r.path not in ("/openapi.json", "/docs", "/docs/oauth2-redirect")]


def _concrete(path: str) -> str:
    for placeholder, value in _PARAMS.items():
        path = path.replace(placeholder, value)
    assert "{" not in path, f"route {path} has a path parameter the guard test does not know"
    return path


def test_every_route_is_refused_with_remote_disabled_while_the_setting_is_off():
    set_api_token()
    c = client_for(create_v1_app(), host="10.9.9.9", token=TOKEN)
    for method, path in _routes():
        r = c.request(method, _concrete(path))
        assert r.status_code == 403 and r.json()["error"] == "remote_disabled", (method, path)


def test_every_route_is_refused_to_a_remote_caller_without_token():
    set_api_token()
    set_allow_remote(True)
    c = client_for(create_v1_app(), host="10.9.9.9")
    for method, path in _routes():
        r = c.request(method, _concrete(path))
        assert r.status_code == 401, (method, path, r.status_code)


def test_every_route_is_refused_when_no_token_is_set_and_caller_is_remote():
    set_allow_remote(True)
    c = client_for(create_v1_app(), host="10.9.9.9")
    for method, path in _routes():
        assert c.request(method, _concrete(path)).status_code == 403


def test_every_route_needs_a_token_for_a_local_caller_with_a_foreign_host_header():
    set_api_token()
    c = client_for(create_v1_app())
    for method, path in _routes():
        assert c.request(method, _concrete(path), headers={"Host": "evil.example"}).status_code == 401, (method, path)


def test_read_routes_are_get_only_in_phase_2_except_maintenance_posts():
    posts = {path for method, path in _routes() if method == "POST"}
    assert posts == {"/reload", "/run-schedule", "/process-recommendations"}
    assert not any(m in ("PUT", "PATCH", "DELETE") for m, _ in _routes())


def test_token_opens_every_get_for_a_remote_caller():
    set_api_token()
    set_allow_remote(True)
    c = client_for(create_v1_app(), host="10.9.9.9", token=TOKEN)
    assert c.get("/ping").status_code == 200
    assert c.get("/transactions").status_code == 200
    assert c.get("/activity").status_code == 200


# --- review fixes: no route can hide in a Mount; every GET is secret-free -----------------------------------

#: Route-table entries that are not APIRoutes and are allowed anyway (none today). A Mount hides its routes from
#: every other test in this file, so adding one must be a deliberate edit HERE.
ALLOWED_NON_API_ROUTES: frozenset = frozenset()


def test_every_route_is_an_api_route_or_explicitly_allowed():
    from fastapi.routing import APIRoute
    for r in create_v1_app().routes:
        assert isinstance(r, APIRoute) or getattr(r, "path", None) in ALLOWED_NON_API_ROUTES, (
            f"{type(r).__name__} at {getattr(r, 'path', '?')} is not an APIRoute: its routes would escape the "
            f"auth/guard tests")


def test_no_get_route_body_contains_a_seeded_secret(monkeypatch):
    """Walk the real route table (not a hand-kept list), so a new GET router is covered the day it lands."""
    from ba2_trade_platform.services import accounts as accounts_svc
    from ba2_trade_platform.services import experts as experts_svc
    from tests.api_helpers import seed_secrets
    ids, secrets = seed_secrets(monkeypatch, experts_svc, accounts_svc)
    values = {"{account_id}": ids["account"], "{transaction_id}": ids["transaction"],
              "{expert_id}": ids["expert"], "{analysis_id}": ids["analysis"]}
    c = client_for(create_v1_app())
    seen = 0
    for method, path in _routes():
        if method != "GET":
            continue
        concrete = [path]
        for placeholder, value in values.items():
            concrete = [p.replace(placeholder, str(value)) for p in concrete]
        if "{ref}" in path:
            concrete = [p.replace("{ref}", ref) for p in concrete for ref in (ids["rm_ref"], ids["smart_ref"])]
        for p in concrete:
            assert "{" not in p, f"unknown path parameter in {p}"
            for suffix in ("", "?include_text=true&include_state=true"):
                r = c.get(p + suffix) if not (suffix and "?" in p) else c.get(p)
                assert r.status_code == 200, (p, r.status_code, r.text[:200])
                for secret in secrets:
                    assert secret not in r.text, (p + suffix, secret)
                seen += 1
    assert seen >= 15
