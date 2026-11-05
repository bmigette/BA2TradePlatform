"""Free-form content is scrubbed on the way out: credential-like keys at any depth, token-shaped strings."""
import json

import pytest

from ba2_trade_platform.services import accounts as accounts_svc
from ba2_trade_platform.services import experts as experts_svc
from ba2_trade_platform.services._scrub import REDACTED, is_secret_key, mask_setting, scrub
from tests.api_helpers import PW_KEY, SK, FakeAccount, seed_secrets, userinfo_url, v1_client

pytestmark = pytest.mark.api_db


# --- the key rule: word boundaries, not substrings --------------------------------------------------------

@pytest.mark.parametrize("key", [
    "api_key", "openai_api_key", "apiKey", "apikey", "secret", "client_secret", "access_token", "flex_token",
    "password", "login_password", "passphrase", "pin", "account_pin", "auth", "authorization", "Authorization",
    "credentials", "bearer", "cookie", "session-cookie", "webhook_url", "sentry_dsn", "private_key", "tls_cert",
    "signature", "hmac", "client_id", "clientId", "Private_Key", "oauth_secret",
])
def test_credential_keys_are_secret(key):
    assert is_secret_key(key), key


@pytest.mark.parametrize("key", [
    "spinoff", "mapping", "shipping_address", "max_tokens", "pinned_symbols", "passthrough", "monkey",
    "keyboard", "min_confidence", "paper_account", "overalloc_min_res_pct", "description", "symbol",
    "base_url", "endpoint", "api_host", "hostname", "auth_url", "uri",
])
def test_innocent_keys_are_not_secret(key):
    assert not is_secret_key(key), key


def test_scrub_redacts_secret_keys_at_any_depth_but_not_numbers_or_masks():
    out = scrub({"a": {"password": "p", "list": [{"api_key": "k", "n": 1}], "token_count": 5},
                 "api_key": {"set": True}, "pin": None, "ok": "fine"})
    assert out["a"]["password"] == REDACTED
    assert out["a"]["list"][0]["api_key"] == REDACTED and out["a"]["list"][0]["n"] == 1
    assert out["a"]["token_count"] == 5                  # numbers under a credential-ish key stay numbers
    assert out["api_key"] == {"set": True}                # an already-masked setting is kept
    assert out["pin"] is None and out["ok"] == "fine"


@pytest.mark.parametrize("text,gone", [
    (SK + "ant-api03-ABCDEFGH1234567890", "ABCDEFGH1234567890"),
    ("header Authorization: Bearer abc.def-123456 end", "abc.def-123456"),
    ("GET /x?api_key=KEYVALUE99&b=1", "KEYVALUE99"),
    ("GET /x?token=TOKVALUE77", "TOKVALUE77"),
    (userinfo_url("host.example/p", "PASSWORD123"), "PASSWORD123"),
    ('{"client_secret": "JSONSECRET"}', "JSONSECRET"),
    ("{'" + PW_KEY + "': 'PYSECRET'}", "PYSECRET"),
])
def test_scrub_redacts_token_shaped_strings(text, gone):
    out = scrub({"text": text})["text"]
    assert gone not in out and REDACTED in out


def test_scrub_keeps_ordinary_text_and_urls_without_userinfo():
    text = "AAPL rose 3% on https://example.com/news?id=7 with key takeaways"
    assert scrub(text) == text


def test_url_setting_masks_only_the_userinfo():
    assert mask_setting("base_url", userinfo_url("broker.example/v2", "SECRETPW"), None) == \
        "https://u:[redacted]@broker.example/v2"
    assert mask_setting("api_key", "abc", None) == {"set": True}
    assert mask_setting("api_key", "", None) == {"set": False}
    assert mask_setting("harmless", "x", {"secret": True}) == {"set": True}


# --- end to end: seeded secrets must not appear in ANY GET body ---------------------------------------------

def _all_get_bodies(ids):
    c = v1_client()
    paths = ["/accounts", f"/accounts/{ids['account']}", "/experts", f"/experts/{ids['expert']}",
             "/transactions", f"/transactions/{ids['transaction']}", "/orders", "/analyses",
             f"/analyses/{ids['analysis']}", f"/analyses/{ids['analysis']}?include_text=true&include_state=true",
             "/recommendations", "/risk-manager/runs", f"/risk-manager/runs/{ids['smart_ref']}",
             f"/risk-manager/runs/{ids['rm_ref']}", "/activity", "/ping"]
    for path in paths:
        r = c.get(path)
        assert r.status_code == 200, (path, r.status_code, r.text[:200])
        yield path, r.text


def test_no_get_body_contains_a_seeded_secret(monkeypatch):
    ids, secrets = seed_secrets(monkeypatch, experts_svc, accounts_svc)
    for path, body in _all_get_bodies(ids):
        for secret in secrets:
            assert secret not in body, (path, secret)


def test_the_seeded_secrets_are_really_in_the_database(monkeypatch):
    """Guard against a vacuous grep: the same seeds ARE stored raw (so only the scrub hides them)."""
    from sqlmodel import select
    from ba2_trade_platform.core.db import get_db
    from ba2_trade_platform.core.models import ActivityLog, AnalysisOutput
    ids, secrets = seed_secrets(monkeypatch, experts_svc, accounts_svc)
    with get_db() as s:
        stored = " ".join(str(a.description) + str(a.data) for a in s.exec(select(ActivityLog)).all())
        stored += " ".join(o.text for o in s.exec(select(AnalysisOutput)).all())
    assert sum(secret in stored for secret in secrets) >= 3


def test_scrub_applies_to_the_response_even_when_a_service_forgets():
    """The filter sits on the app, not in the services: a route returning raw secrets is still scrubbed."""
    from fastapi import APIRouter

    from ba2_trade_platform.api.app import create_v1_app
    from tests.api_helpers import client_for
    app = create_v1_app()
    router = APIRouter()

    @router.get("/leaky")
    def leaky():
        return {"note": "key=LEAKED123", "client_secret": "LEAKED456", "nested": [{"x": SK + "LEAKED7890123"}]}
    app.include_router(router)
    body = client_for(app).get("/leaky").text
    assert "LEAKED" not in body and json.loads(body)["client_secret"] == REDACTED
    assert FakeAccount  # keep the import used
