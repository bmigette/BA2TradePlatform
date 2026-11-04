"""Auth matrix of the management API: {local, remote} x {read, write} x {token unset, set, wrong} x
{api_allow_remote off, on}."""
import pytest
from fastapi import Depends, FastAPI, Request

from ba2_trade_platform.api.auth import (
    ALLOW_REMOTE_KEY, API_TOKEN_KEY, get_allow_remote, get_api_token, require_access,
    rotate_api_token, set_allow_remote,
)
from tests.api_helpers import TOKEN, client_for, set_allow_remote as allow_remote, set_api_token

REMOTE = "192.168.1.9"


def _app() -> FastAPI:
    app = FastAPI(dependencies=[Depends(require_access)])

    @app.get("/r")
    def r():
        return {"ok": True}

    @app.post("/w")
    def w():
        return {"ok": True}

    return app


def test_no_token_set_local_read_open_everything_else_403():
    allow_remote(True)
    assert client_for(_app()).get("/r").status_code == 200
    assert client_for(_app()).post("/w").status_code == 403
    assert client_for(_app(), host=REMOTE).get("/r").status_code == 403
    # Even a presented token cannot open anything when none is set.
    assert client_for(_app(), token="whatever").post("/w").status_code == 403


def test_token_set_write_requires_bearer_everywhere():
    set_api_token()
    allow_remote(True)
    for host in ("127.0.0.1", REMOTE):
        assert client_for(_app(), host=host).post("/w").status_code == 401
        assert client_for(_app(), host=host, token="wrong" * 8).post("/w").status_code == 401
        assert client_for(_app(), host=host, token=TOKEN).post("/w").status_code == 200


def test_token_set_remote_read_requires_bearer_local_read_does_not():
    set_api_token()
    allow_remote(True)
    assert client_for(_app()).get("/r").status_code == 200
    assert client_for(_app(), host="10.0.0.5").get("/r").status_code == 401
    assert client_for(_app(), host="10.0.0.5", token="x" * 44).get("/r").status_code == 401
    assert client_for(_app(), host="10.0.0.5", token=TOKEN).get("/r").status_code == 200


@pytest.mark.parametrize("token_state", ["unset", "valid", "wrong"])
@pytest.mark.parametrize("method,path", [("get", "/r"), ("post", "/w")])
def test_remote_refused_with_remote_disabled_when_setting_off(token_state, method, path):
    if token_state != "unset":
        set_api_token()
    token = {"unset": None, "valid": TOKEN, "wrong": "wrong" * 8}[token_state]
    assert get_allow_remote() is False       # default: a missing row means off
    r = getattr(client_for(_app(), host=REMOTE, token=token), method)(path)
    assert r.status_code == 403
    assert r.json()["detail"]["error"] == "remote_disabled"


def test_remote_disabled_applies_before_the_token_check_and_local_is_unaffected():
    set_api_token()
    assert client_for(_app()).get("/r").status_code == 200
    assert client_for(_app(), token=TOKEN).post("/w").status_code == 200
    assert client_for(_app()).post("/w").status_code == 401


def test_allow_remote_is_read_per_request():
    set_api_token()
    c = client_for(_app(), host=REMOTE, token=TOKEN)
    assert c.get("/r").status_code == 403
    set_allow_remote(True)
    assert c.get("/r").status_code == 200
    set_allow_remote(False)
    assert c.get("/r").status_code == 403
    assert ALLOW_REMOTE_KEY == "api_allow_remote"


def test_ipv6_loopback_counts_as_local():
    assert client_for(_app(), host="::1").get("/r").status_code == 200


def test_malformed_authorization_header_is_401():
    set_api_token()
    allow_remote(True)
    c = client_for(_app(), host="10.0.0.5")
    assert c.get("/r", headers={"Authorization": "Basic abc"}).status_code == 401
    assert c.get("/r", headers={"Authorization": "Bearer"}).status_code == 401


def test_blank_stored_token_counts_as_unset():
    set_api_token("   ")
    assert get_api_token() is None
    assert client_for(_app()).post("/w", headers={"Authorization": "Bearer    "}).status_code == 403


def test_rotate_api_token_stores_and_replaces():
    first = rotate_api_token()
    assert len(first) >= 40 and get_api_token() == first
    second = rotate_api_token()
    assert second != first and get_api_token() == second
    assert API_TOKEN_KEY == "api_token"


def test_write_success_records_token_last_four_only():
    set_api_token()
    app = FastAPI(dependencies=[Depends(require_access)])
    seen = {}

    @app.post("/w")
    def w(request: Request):
        seen["tail"] = request.state.token_tail
        return {}

    client_for(app, token=TOKEN).post("/w")
    assert seen["tail"] == TOKEN[-4:] == "abcd"
