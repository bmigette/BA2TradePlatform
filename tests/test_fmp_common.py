"""Tests for the shared FMP rate-limit/error handling helper.

FMP returns rate-limit errors as HTTP 200 with a JSON dict body like
``{"Error Message": "Limit Reach."}`` instead of a 429 status. The providers
assumed a list and crashed (dict-slice / [0] KeyError). ``fmp_list_call``
normalizes the result to a list, retries on FMP error dicts with backoff, and
raises ``FMPError`` after logging the raw payload.
"""

import pytest
import requests

from ba2_trade_platform.modules.dataproviders.fmp_common import (
    fmp_list_call,
    fmp_http_get,
    TTLCache,
    FMPError,
)


# ---------------------------------------------------------------------------
# TTLCache: dedupe identical fetches across experts within a short window
# ---------------------------------------------------------------------------
class TestTTLCache:
    def test_caches_within_ttl_and_calls_once(self):
        t = {"now": 1000.0}
        calls = {"n": 0}

        def fn():
            calls["n"] += 1
            return "value"

        c = TTLCache(ttl_seconds=600, clock=lambda: t["now"])
        assert c.get_or_call("AAPL", fn) == "value"
        t["now"] = 1300.0  # within TTL
        assert c.get_or_call("AAPL", fn) == "value"
        assert calls["n"] == 1  # fn called only once

    def test_refetches_after_expiry(self):
        t = {"now": 1000.0}
        calls = {"n": 0}

        def fn():
            calls["n"] += 1
            return calls["n"]

        c = TTLCache(ttl_seconds=600, clock=lambda: t["now"])
        assert c.get_or_call("AAPL", fn) == 1
        t["now"] = 1700.0  # past TTL (600)
        assert c.get_or_call("AAPL", fn) == 2
        assert calls["n"] == 2

    def test_caches_none_value(self):
        calls = {"n": 0}

        def fn():
            calls["n"] += 1
            return None

        c = TTLCache(ttl_seconds=600, clock=lambda: 1000.0)
        assert c.get_or_call("NOCOV", fn) is None
        assert c.get_or_call("NOCOV", fn) is None
        assert calls["n"] == 1  # None is cached, not re-fetched

    def test_keys_are_independent(self):
        c = TTLCache(ttl_seconds=600, clock=lambda: 1000.0)
        assert c.get_or_call("A", lambda: "a") == "a"
        assert c.get_or_call("B", lambda: "b") == "b"
        assert c.get_or_call("A", lambda: "x") == "a"  # A still cached


# ---------------------------------------------------------------------------
# fmp_http_get: HTTP-status-level backoff (real 429 / 5xx), not the 200-error-dict
# ---------------------------------------------------------------------------
class _Resp:
    def __init__(self, status, headers=None):
        self.status_code = status
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(f"{self.status_code} error")


def _getter(responses):
    """Return a fake requests.get yielding the given responses/exceptions in order."""
    it = iter(responses)

    def _get(url, params=None, timeout=None):
        item = next(it)
        if isinstance(item, Exception):
            raise item
        return item

    return _get


class TestFmpHttpGet:
    def test_returns_response_on_200(self):
        resp = _Resp(200)
        sleeps = []
        out = fmp_http_get("u", getter=_getter([resp]), sleep=sleeps.append)
        assert out is resp
        assert sleeps == []  # no retries on success

    def test_retries_on_429_then_succeeds(self):
        ok = _Resp(200)
        sleeps = []
        out = fmp_http_get("u", delays=(5, 15, 30), getter=_getter([_Resp(429), ok]),
                           sleep=sleeps.append)
        assert out is ok
        assert sleeps == [5]  # slept once before the successful retry

    def test_persistent_429_raises_fmperror_after_retries(self):
        sleeps = []
        with pytest.raises(FMPError):
            fmp_http_get("u", symbol="MSFT", endpoint="price-target-consensus",
                         delays=(1, 2, 3),
                         getter=_getter([_Resp(429), _Resp(429), _Resp(429), _Resp(429)]),
                         sleep=sleeps.append)
        assert sleeps == [1, 2, 3]  # one sleep per delay

    def test_retries_on_500(self):
        ok = _Resp(200)
        out = fmp_http_get("u", delays=(1, 2), getter=_getter([_Resp(503), ok]),
                           sleep=lambda s: None)
        assert out is ok

    def test_non_retryable_status_raises_immediately(self):
        sleeps = []
        with pytest.raises(requests.exceptions.HTTPError):
            fmp_http_get("u", delays=(1, 2), getter=_getter([_Resp(401)]),
                         sleep=sleeps.append)
        assert sleeps == []  # 401 is not retried

    def test_respects_retry_after_header(self):
        ok = _Resp(200)
        sleeps = []
        out = fmp_http_get("u", delays=(2, 5),
                           getter=_getter([_Resp(429, {"Retry-After": "10"}), ok]),
                           sleep=sleeps.append)
        assert out is ok
        assert sleeps == [10]  # max(delay=2, Retry-After=10)

    def test_retries_on_connection_error(self):
        ok = _Resp(200)
        sleeps = []
        out = fmp_http_get("u", delays=(1, 2),
                           getter=_getter([requests.exceptions.ConnectionError("down"), ok]),
                           sleep=sleeps.append)
        assert out is ok
        assert sleeps == [1]


def test_list_passes_through():
    data = [{"a": 1}, {"b": 2}]
    assert fmp_list_call(lambda: data, sleep=lambda s: None) == data


def test_none_returns_empty_list():
    assert fmp_list_call(lambda: None, sleep=lambda s: None) == []


def test_empty_list_returns_empty_list():
    assert fmp_list_call(lambda: [], sleep=lambda s: None) == []


def test_error_dict_retried_then_raises():
    delays = (15, 30, 60)
    calls = {"sleeps": []}

    def _sleep(s):
        calls["sleeps"].append(s)

    def _fn():
        return {"Error Message": "Limit Reach."}

    with pytest.raises(FMPError):
        fmp_list_call(_fn, symbol="AAPL", endpoint="income_statement",
                      delays=delays, sleep=_sleep)

    # One sleep per delay (retried len(delays) times before giving up).
    assert calls["sleeps"] == list(delays)


def test_error_dict_alternate_keys_retried():
    for key in ("error", "message"):
        calls = {"sleeps": []}
        with pytest.raises(FMPError):
            fmp_list_call(lambda: {key: "boom"}, delays=(1, 2),
                          sleep=lambda s: calls["sleeps"].append(s))
        assert calls["sleeps"] == [1, 2]


def test_unexpected_dict_raises_immediately_without_retry():
    calls = {"sleeps": []}

    def _sleep(s):
        calls["sleeps"].append(s)

    with pytest.raises(FMPError):
        fmp_list_call(lambda: {"unexpected": "shape"}, symbol="MSFT",
                      endpoint="company_profile", delays=(15, 30, 60), sleep=_sleep)

    # Unexpected (non-error) dict: no retries, sleep never called.
    assert calls["sleeps"] == []


def test_recovers_when_retry_returns_list():
    """If a later attempt returns a valid list, it is returned (no raise)."""
    seq = iter([{"Error Message": "Limit Reach."}, [{"ok": True}]])
    calls = {"sleeps": []}
    out = fmp_list_call(lambda: next(seq), delays=(15, 30, 60),
                        sleep=lambda s: calls["sleeps"].append(s))
    assert out == [{"ok": True}]
    assert calls["sleeps"] == [15]  # slept once before the successful retry
