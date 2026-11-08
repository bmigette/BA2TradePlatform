"""FMPSenateTraderWeight: definitive 'no data' vs transient failure, and the pre-page-cap backfill."""
import logging

import pytest
import requests

from ba2_experts.FMPSenateTraderWeight import (
    FMPSenateTraderWeight, CONGRESS_ROSTER_NAMESPACE, CONGRESS_ROSTER_KEY,
)
from ba2_providers import fmp_common
from ba2_providers.fmp_common import (
    fmp_history_disk_write, frozen_ttl_cache, hermetic_fmp_history, reset_hermetic_misses, hermetic_miss_symbols,
)

KEY = "SECRETKEY999"


def _expert():
    e = FMPSenateTraderWeight.__new__(FMPSenateTraderWeight)
    e._api_key = KEY
    e.logger = logging.getLogger("t")
    return e


def _http_error(status):
    r = requests.Response()
    r.status_code = status
    return requests.exceptions.HTTPError(f"{status} for url: https://x?apikey={KEY}", response=r)


@pytest.fixture(autouse=True)
def _cache(monkeypatch, tmp_path):
    import ba2_common.config as cfg
    monkeypatch.setattr(cfg, "CACHE_FOLDER", str(tmp_path), raising=False)
    fmp_common._HISTORY_MEM_CACHE._store.clear()
    reset_hermetic_misses()
    yield
    fmp_common._HISTORY_MEM_CACHE._store.clear()
    reset_hermetic_misses()


def _raising(err):
    def f(*a, **k):
        raise err
    return f


def test_price_history_400_is_known_empty(monkeypatch):
    monkeypatch.setattr("ba2_providers.fmp_common.fmp_http_get", _raising(_http_error(400)))
    assert _expert()._fetch_price_history_uncached("9201") == []


def test_price_history_transient_failure_raises_when_frozen_and_never_a_false_empty(monkeypatch, caplog):
    monkeypatch.setattr("ba2_providers.fmp_common.fmp_http_get", _raising(fmp_common.FMPError("boom")))
    with frozen_ttl_cache(), caplog.at_level(logging.DEBUG), pytest.raises(ValueError):
        _expert()._fetch_price_history_uncached("AAPL")
    assert KEY not in caplog.text


def test_price_history_http_500_log_is_redacted_and_live_degrades(monkeypatch, caplog):
    monkeypatch.setattr("ba2_providers.fmp_common.fmp_http_get", _raising(_http_error(500)))
    with caplog.at_level(logging.DEBUG):
        assert _expert()._fetch_price_history_uncached("AAPL") == []   # live: log + degrade, as before
    assert KEY not in caplog.text


def test_trader_history_partial_is_not_returned_when_frozen(monkeypatch):
    def fake(url, params=None, **kw):
        if "senate" in url:
            raise fmp_common.FMPError("down")
        raise AssertionError("must not continue past the failed chamber")
    monkeypatch.setattr("ba2_providers.fmp_common.fmp_http_get", fake)
    with frozen_ttl_cache(), pytest.raises(ValueError):
        _expert()._fetch_trader_history_uncached("Jane Doe")


def test_pre_cap_backfill_adds_only_rows_older_than_the_house_feed_floor():
    e = _expert()
    house = [{"disclosureDate": "2023-06-01"}, {"disclosureDate": "2024-01-01"}]
    hist = {"Old Member": [{"disclosureDate": "2021-05-05", "symbol": "A"}, {"disclosureDate": "2023-07-01", "symbol": "B"}]}
    e._fetch_trader_history = lambda name: hist.get(name)
    with frozen_ttl_cache(), hermetic_fmp_history():
        fmp_history_disk_write(CONGRESS_ROSTER_NAMESPACE, CONGRESS_ROSTER_KEY, ["Old Member"])
        out = e._pre_cap_backfill(house)
        assert out == [{"disclosureDate": "2021-05-05", "symbol": "A"}]
        assert hermetic_miss_symbols() == set()


def test_pre_cap_backfill_without_roster_is_one_named_miss_in_a_hermetic_run():
    with frozen_ttl_cache(), hermetic_fmp_history():
        assert _expert()._pre_cap_backfill([{"disclosureDate": "2023-06-01"}]) == []
        assert hermetic_miss_symbols() == {f"{CONGRESS_ROSTER_NAMESPACE}/{CONGRESS_ROSTER_KEY}"}
