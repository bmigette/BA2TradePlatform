"""ba2_mcp.client: query building, headers, error mapping, token hygiene."""
import httpx
import pytest

from tests import mcp_server_support  # noqa: F401  (puts mcp_server on sys.path)

from ba2_mcp.client import ApiCallError, Ba2Client  # noqa: E402

TOKEN = "tok-" + "x" * 30


def _client(handler, token=TOKEN):
    http = httpx.Client(base_url="http://h/api/v1/", transport=httpx.MockTransport(handler))
    return Ba2Client("http://h/api/v1", token, 5.0, http=http)


def test_query_drops_none_lowercases_bools_and_sends_bearer_and_key():
    seen = {}

    def handler(req):
        seen["url"], seen["headers"] = req.url, req.headers
        return httpx.Response(200, json={"ok": 1})
    c = _client(handler)
    assert c.request("GET", "/transactions", query={"a": None, "b": True, "c": False, "n": 3}) == {"ok": 1}
    assert str(seen["url"]) == "http://h/api/v1/transactions?b=true&c=false&n=3"
    assert seen["headers"]["authorization"] == f"Bearer {TOKEN}" and "idempotency-key" not in seen["headers"]
    c.request("POST", "/x", idempotency_key="k1", write=True)
    assert seen["headers"]["idempotency-key"] == "k1"


@pytest.mark.parametrize("status,env,needle", [
    (401, {"error": "unauthorized", "message": "Missing or wrong API token", "detail": {}}, "BA2_API_TOKEN"),
    (403, {"error": "remote_disabled", "message": "off", "detail": {}}, "api_allow_remote"),
    (403, {"error": "forbidden", "message": "no token", "detail": {}}, "Management API"),
    (409, {"error": "conflict", "message": "is CLOSED", "detail": {"id": 4}}, "[conflict]"),
    (502, {"error": "broker_refused", "message": "40310000 wash trade", "detail": {}}, "40310000"),
])
def test_error_envelope_maps_to_text_with_code(status, env, needle):
    c = _client(lambda req: httpx.Response(status, json=env))
    with pytest.raises(ApiCallError) as e:
        c.request("GET", "/x")
    assert needle in str(e.value) and f"[{env['error']}]" in str(e.value) and str(status) in str(e.value)


def test_token_never_appears_in_errors_or_repr():
    c = _client(lambda req: httpx.Response(500, json={"error": "internal", "message": f"echo {TOKEN}", "detail": {}}))
    with pytest.raises(ApiCallError) as e:
        c.request("GET", "/x")
    assert TOKEN not in str(e.value) and TOKEN not in repr(c)


def test_connection_refused_mentions_url_and_nothing_sent():
    def handler(req):
        raise httpx.ConnectError("refused")
    with pytest.raises(ApiCallError) as e:
        _client(handler).request("POST", "/x", write=True)
    assert "BA2_API_URL" in str(e.value) and "Nothing was sent" in str(e.value)


def test_write_timeout_says_maybe_executed_and_read_timeout_does_not():
    def handler(req):
        raise httpx.ReadTimeout("slow")
    c = _client(handler)
    with pytest.raises(ApiCallError) as w:
        c.request("POST", "/x", write=True)
    assert "NOT retried" in str(w.value) and "MAY have been executed" in str(w.value) and "get_transaction" in str(w.value)
    with pytest.raises(ApiCallError) as r:
        c.request("GET", "/x")
    assert "MAY" not in str(r.value)


def test_from_env():
    with pytest.raises(ApiCallError, match="BA2_API_URL"):
        Ba2Client.from_env({})
    c = Ba2Client.from_env({"BA2_API_URL": "http://h/api/v1/", "BA2_API_TOKEN": "t", "BA2_API_TIMEOUT": "7"})
    assert c.base_url == "http://h/api/v1" and c.timeout == 7.0
    assert Ba2Client.from_env({"BA2_API_URL": "http://h/api/v1"}).timeout == 60.0
