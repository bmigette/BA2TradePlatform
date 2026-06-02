"""Tests for the shared FMP rate-limit/error handling helper.

FMP returns rate-limit errors as HTTP 200 with a JSON dict body like
``{"Error Message": "Limit Reach."}`` instead of a 429 status. The providers
assumed a list and crashed (dict-slice / [0] KeyError). ``fmp_list_call``
normalizes the result to a list, retries on FMP error dicts with backoff, and
raises ``FMPError`` after logging the raw payload.
"""

import pytest

from ba2_trade_platform.modules.dataproviders.fmp_common import (
    fmp_list_call,
    FMPError,
)


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
