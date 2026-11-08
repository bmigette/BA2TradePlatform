"""Senate/congress fetch path: no API key in any log or exception, retries via the shared getter, the
page-cap is explicit, a transient failure never lands in the cache as a partial/empty answer."""
import logging

import pytest
import requests

from ba2_experts.expert_mixins import FMPCongressTradingMixin
from ba2_providers import fmp_common
from ba2_providers.fmp_common import set_ttl_frozen

KEY = "SECRETKEY123456"


class _Host(FMPCongressTradingMixin):
    def __init__(self):
        self._api_key = KEY
        self.logger = logging.getLogger("senate_hardening")


class _Resp:
    def __init__(self, status, payload=None, url="https://financialmodelingprep.com/stable/house-latest"):
        self.status_code = status
        self._payload = payload if payload is not None else []
        self.headers = {}
        self.url = url
        self.content = b"[]"

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(
                f"{self.status_code} Client Error: Bad Request for url: {self.url}?apikey={KEY}&page=101",
                response=self)


@pytest.fixture(autouse=True)
def _live_and_fast(monkeypatch):
    set_ttl_frozen(False)
    monkeypatch.setattr("time.sleep", lambda _s: None)
    monkeypatch.setattr(fmp_common, "_gate_wait", lambda _s: None)
    monkeypatch.setattr(fmp_common, "_gate_arm", lambda _d: None)
    yield


def _route_through_real_getter(monkeypatch, getter):
    """Make the mixin use the REAL fmp_http_get, with a fake transport underneath."""
    real = fmp_common.fmp_http_get
    monkeypatch.setattr("ba2_providers.fmp_common.fmp_http_get",
                        lambda url, params=None, **kw: real(url, params, getter=getter, delays=(0, 0, 0), **kw))


def test_http_get_redacts_the_key_in_http_errors():
    with pytest.raises(requests.exceptions.HTTPError) as ei:
        fmp_common.fmp_http_get("u", {"apikey": KEY}, getter=lambda url, params, timeout: _Resp(400))
    assert KEY not in str(ei.value)
    assert ei.value.response.status_code == 400


def test_page_cap_is_explicit_and_leaks_no_key(monkeypatch, caplog):
    def getter(url, params, timeout):
        if params["page"] >= 3:
            return _Resp(400)
        return _Resp(200, [{"symbol": "A", "disclosureDate": f"2024-0{params['page'] + 1}-01"}])
    _route_through_real_getter(monkeypatch, getter)
    with caplog.at_level(logging.DEBUG):
        rows = _Host()._fetch_congress_trades("house", symbol=None, full_history=True)
    assert len(rows) == 3
    text = caplog.text
    assert KEY not in text
    assert "HARD PAGE CAP" in text and "INCOMPLETE" in text and "2024-01-01" in text


def test_pagination_retries_a_5xx_through_the_shared_getter(monkeypatch):
    seen = {"n": 0}

    def getter(url, params, timeout):
        seen["n"] += 1
        if params["page"] == 0 and seen["n"] == 1:
            return _Resp(503)
        return _Resp(200, [{"symbol": "A"}] if params["page"] == 0 else [])
    _route_through_real_getter(monkeypatch, getter)
    rows = _Host()._fetch_congress_trades("house", symbol=None, full_history=True)
    assert rows == [{"symbol": "A"}]
    assert seen["n"] == 3  # 503, retry page 0 OK, page 1 empty


def test_exhausted_retries_raise_and_do_not_return_a_partial_list(monkeypatch, caplog):
    def getter(url, params, timeout):
        return _Resp(200, [{"symbol": "A"}]) if params["page"] < 2 else _Resp(503)
    _route_through_real_getter(monkeypatch, getter)
    with caplog.at_level(logging.DEBUG), pytest.raises(ValueError) as ei:
        _Host()._fetch_congress_trades("house", symbol=None, full_history=True, raise_on_error=True)
    assert KEY not in str(ei.value) and KEY not in caplog.text


def test_connection_error_text_is_redacted(monkeypatch, caplog):
    def getter(url, params, timeout):
        raise requests.exceptions.ConnectionError(f"HTTPSConnectionPool: url: {url}?apikey={KEY}&page=1")
    _route_through_real_getter(monkeypatch, getter)
    with caplog.at_level(logging.DEBUG):
        out = _Host()._fetch_congress_trades("senate", symbol="RF")
    assert out is None
    assert KEY not in caplog.text


def test_unknown_symbol_400_is_a_known_empty_answer(monkeypatch):
    _route_through_real_getter(monkeypatch, lambda url, params, timeout: _Resp(400, url="x"))
    assert _Host()._fetch_congress_trades("senate", symbol="9201") == []


def test_auth_error_is_not_a_known_empty_answer(monkeypatch):
    _route_through_real_getter(monkeypatch, lambda url, params, timeout: _Resp(401, url="x"))
    assert _Host()._fetch_congress_trades("senate", symbol="AAPL") is None
