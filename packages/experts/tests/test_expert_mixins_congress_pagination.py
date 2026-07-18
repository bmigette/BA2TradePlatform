"""Tests for FMPCongressTradingMixin._fetch_congress_trades' pagination-depth fix
(2026-07-18).

BUG: the unscoped (``symbol=None``) ``{chamber}-latest`` fetch used to grab ONLY
page 0 (~1000 rows, roughly the most recent 4 months of disclosures) -- invisible to
any backtest walking further back than that. Confirmed in practice: a 2023-2026 GA
matrix grid for FMPSenateTraderWeight's basket mode scored ``trades=0, fitness=-1e9``
for every individual across every generation because ``_gather_all`` depends entirely
on this unscoped fetch.

These tests exercise ``_fetch_congress_trades`` directly (mocking ``fmp_http_get``)
rather than going through a real network call or the disk cache, proving:
1. ``full_history=False`` (default) makes exactly ONE request (page 0) -- BYTE-IDENTICAL
   to the pre-fix behavior, so every existing caller (FMPSenateTraderCopy's live path)
   is unaffected unless it explicitly opts in.
2. ``full_history=True`` paginates ``page=0..N`` until an empty page (end of feed),
   concatenating every row.
3. ``full_history=True`` respects ``max_pages`` as a safety cap even if the feed never
   empties.
4. ``full_history=True`` is a no-op when a real ``symbol`` is given (the per-symbol
   ``{chamber}-trades`` endpoint was never paginated and doesn't need to be -- it
   already returns a symbol's FULL history in one call).
5. Shallow and deep fetches are cached under DIFFERENT (namespace, key) pairs, so a
   stale shallow cache entry can never silently satisfy a deep caller (or vice versa)
   -- the exact "wrong answer" the task's design doc calls out as re-introducing this
   same bug behind a cache hit.
"""
import logging

from ba2_experts.expert_mixins import FMPCongressTradingMixin
from ba2_providers.fmp_common import set_ttl_frozen

_LOG = logging.getLogger("test_congress_pagination")


class _MixinHost(FMPCongressTradingMixin):
    """Bare instance carrying just what _fetch_congress_trades needs."""

    def __init__(self, api_key: str = "test-key"):
        self._api_key = api_key
        self.logger = _LOG


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


def setup_function(_fn):
    # Defensive: keep the (thread-local) TTL-freeze flag unset for these tests so
    # fmp_history_disk_cached takes the live "always call fetch_fn" passthrough path
    # (its own semantics are covered by packages/providers/tests; these tests are
    # about the pagination LOOP, not the disk-cache layer).
    set_ttl_frozen(False)


def test_default_shallow_fetch_makes_exactly_one_page0_request(monkeypatch):
    calls = []

    def fake_get(url, params, **kwargs):
        calls.append(dict(params))
        return _FakeResponse([{"symbol": "AAPL", "transactionDate": "2026-06-01"}])

    monkeypatch.setattr("ba2_providers.fmp_common.fmp_http_get", fake_get)

    e = _MixinHost()
    data = e._fetch_congress_trades("senate", symbol=None)  # full_history defaults False

    assert len(calls) == 1
    assert calls[0]["page"] == 0
    assert data == [{"symbol": "AAPL", "transactionDate": "2026-06-01"}]


def test_full_history_paginates_until_an_empty_page(monkeypatch):
    pages = {
        0: [{"symbol": "AAPL", "transactionDate": "2026-06-01"}],
        1: [{"symbol": "MSFT", "transactionDate": "2023-01-01"}],
        2: [],  # end of feed
    }
    requested_pages = []

    def fake_get(url, params, **kwargs):
        page = params["page"]
        requested_pages.append(page)
        return _FakeResponse(pages.get(page, []))

    monkeypatch.setattr("ba2_providers.fmp_common.fmp_http_get", fake_get)
    monkeypatch.setattr("time.sleep", lambda _s: None)  # skip the gentle inter-page pause

    e = _MixinHost()
    data = e._fetch_congress_trades("senate", symbol=None, full_history=True)

    assert requested_pages == [0, 1, 2]  # stopped right after the empty page
    assert data == pages[0] + pages[1]


def test_full_history_respects_max_pages_safety_cap(monkeypatch):
    requested_pages = []

    def fake_get(url, params, **kwargs):
        requested_pages.append(params["page"])
        return _FakeResponse([{"symbol": "X", "transactionDate": "2020-01-01"}])  # never empty

    monkeypatch.setattr("ba2_providers.fmp_common.fmp_http_get", fake_get)
    monkeypatch.setattr("time.sleep", lambda _s: None)

    e = _MixinHost()
    data = e._fetch_congress_trades("senate", symbol=None, full_history=True, max_pages=5)

    assert requested_pages == [0, 1, 2, 3, 4]
    assert len(data) == 5


def test_full_history_stops_gracefully_on_a_mid_pagination_fetch_error(monkeypatch):
    """Confirmed live 2026-07-18: FMP's house-latest endpoint hits a genuine HTTP 400 once
    `page` exceeds an internal depth limit well before max_pages -- a per-page failure must
    STOP the walk and keep whatever was accumulated so far, not discard everything (which
    would silently re-truncate history, just via a different failure mode than the original
    bug)."""
    import requests

    requested_pages = []

    def fake_get(url, params, **kwargs):
        page = params["page"]
        requested_pages.append(page)
        if page == 2:
            raise requests.exceptions.HTTPError("400 Client Error: Bad Request")
        return _FakeResponse([{"symbol": "AAPL", "transactionDate": f"2026-0{page + 1}-01"}])

    monkeypatch.setattr("ba2_providers.fmp_common.fmp_http_get", fake_get)
    monkeypatch.setattr("time.sleep", lambda _s: None)

    e = _MixinHost()
    data = e._fetch_congress_trades("senate", symbol=None, full_history=True, max_pages=200)

    assert requested_pages == [0, 1, 2]  # stopped right after the failing page
    assert len(data) == 2  # pages 0 and 1's rows were kept, not discarded


def test_full_history_is_a_noop_for_a_scoped_symbol_fetch(monkeypatch):
    """The per-symbol '{chamber}-trades' endpoint already returns one symbol's FULL
    history in a single call -- full_history=True must not change that call shape."""
    calls = []

    def fake_get(url, params, **kwargs):
        calls.append(dict(params))
        return _FakeResponse([{"symbol": "AAPL", "transactionDate": "2026-06-01"}])

    monkeypatch.setattr("ba2_providers.fmp_common.fmp_http_get", fake_get)

    e = _MixinHost()
    data = e._fetch_congress_trades("senate", symbol="AAPL", full_history=True)

    assert len(calls) == 1
    assert "page" not in calls[0]  # per-symbol endpoint never paginated
    assert data == [{"symbol": "AAPL", "transactionDate": "2026-06-01"}]


def test_shallow_and_deep_unscoped_fetches_use_different_cache_keys(monkeypatch):
    """Cache-key design: full_history=False/True must write to DIFFERENT
    (namespace, key) pairs under fmp_history_disk_cached, so a stale shallow cache
    entry can never silently satisfy a deep caller (or vice versa) -- see
    _fetch_congress_trades' "Pagination-depth design" docstring."""
    seen = []

    def fake_disk_cached(namespace, key, fetch_fn):
        seen.append((namespace, key))
        return fetch_fn()

    monkeypatch.setattr("ba2_providers.fmp_common.fmp_history_disk_cached", fake_disk_cached)
    monkeypatch.setattr("ba2_providers.fmp_common.fmp_http_get",
                        lambda url, params, **kwargs: _FakeResponse([]))
    monkeypatch.setattr("time.sleep", lambda _s: None)

    e = _MixinHost()
    e._fetch_congress_trades("senate", symbol=None, full_history=False)
    e._fetch_congress_trades("senate", symbol=None, full_history=True)

    assert seen == [
        ("congress_senate_latest", "ALL"),
        ("congress_senate_latest", "ALL_FULL_HISTORY"),
    ]
