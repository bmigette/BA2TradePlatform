"""Estimates router + providers: the AV provider through the hermetic disk cache, the FMP typed refusal and live
path, the bundle routing and the source/name helpers. Fakes only; no network."""
import json
import os
import pathlib
from datetime import date, datetime, timedelta

import pytest

import ba2_common.config as cfg
from ba2_common.core import estimates_pit as pit
from ba2_common.core.backtest_context import LiveProviderBundle
from ba2_common.core.estimates_router import EstimatesRoutedDetails
from ba2_common.core import estimates_source as es
from ba2_providers import fmp_common
import sys

from ba2_providers.fundamentals.details.FMPCompanyDetailsProvider import FMPCompanyDetailsProvider
import ba2_providers.fundamentals.estimates.AlphaVantageEstimatesProvider  # noqa: F401

# the packages re-export the CLASS under the module's name: take the real modules from sys.modules
fmp_mod = sys.modules["ba2_providers.fundamentals.details.FMPCompanyDetailsProvider"]
av_mod = sys.modules["ba2_providers.fundamentals.estimates.AlphaVantageEstimatesProvider"]

FIX = pathlib.Path(__file__).parent / "fixtures" / "alphavantage"
PAST = datetime(2022, 12, 1)


def _cache_dir():
    d = os.path.join(cfg.CACHE_FOLDER, "fmp_history")
    os.makedirs(d, exist_ok=True)
    return d


def _write(namespace, symbol, payload):
    with open(os.path.join(_cache_dir(), f"{namespace}__{symbol}.json"), "w") as fh:
        json.dump(payload, fh)


def _av_payload(sym="MSFT"):
    return {"fetched_on": "2026-11-07",
            "estimates": json.loads((FIX / f"earnings_estimates_{sym}.json").read_text()),
            "earnings": json.loads((FIX / f"earnings_{sym}.json").read_text())}


@pytest.fixture
def av(monkeypatch):
    monkeypatch.setattr(av_mod, "get_app_setting", lambda key, default=None: "x")

    def _no_network(*a, **k):
        raise AssertionError("network used")
    monkeypatch.setattr(av_mod, "make_api_request", _no_network)
    fmp_common._HISTORY_MEM_CACHE._store.clear()
    fmp_common.reset_hermetic_misses()
    return av_mod.AlphaVantageEstimatesProvider()


def test_backtest_reads_only_the_cache_and_is_point_in_time(av):
    _write(av_mod.AV_CONSENSUS_NAMESPACE, "MSFT", _av_payload())
    _write(av_mod.SPLIT_CALENDAR_NAMESPACE, "MSFT", {"symbol": "MSFT", "historical": []})
    with fmp_common.frozen_ttl_cache(), fmp_common.hermetic_fmp_history():
        out = av.get_quarterly_eps_consensus("MSFT", PAST)
    q = {r["fiscal_period_end"]: r for r in out["quarters"]}
    assert out["source"] == "alphavantage" and q["2022-12-31"]["eps_avg"] == pytest.approx(2.29)
    assert all(r["snapshot_date"] <= "2022-12-01" for r in out["quarters"])


def test_get_earnings_estimates_has_the_fmp_shape_and_marks_the_quarterly_granularity(av):
    _write(av_mod.AV_CONSENSUS_NAMESPACE, "MSFT", _av_payload())
    _write(av_mod.SPLIT_CALENDAR_NAMESPACE, "MSFT", {"symbol": "MSFT", "historical": []})
    with fmp_common.frozen_ttl_cache(), fmp_common.hermetic_fmp_history():
        out = av.get_earnings_estimates("MSFT", "quarterly", PAST, lookback_periods=2, format_type="dict")
    assert out["frequency"] == "quarterly" and out["source"] == "alphavantage" and len(out["estimates"]) <= 2
    assert set(out["estimates"][0]) >= {"fiscal_date_ending", "estimated_eps_avg", "estimated_eps_high",
                                        "estimated_eps_low", "number_of_analysts"}


def test_a_payload_miss_in_a_backtest_is_recorded_not_fetched(av):
    with fmp_common.frozen_ttl_cache(), fmp_common.hermetic_fmp_history():
        out = av.get_quarterly_eps_consensus("NOPAYLOAD", PAST)      # make_api_request would assert
    assert out["quarters"] == [] and out["unavailable"] == av_mod.UNAVAILABLE_NOT_PREWARMED
    assert f"{av_mod.AV_CONSENSUS_NAMESPACE}/NOPAYLOAD" in fmp_common.hermetic_miss_symbols()


def test_the_no_estimates_sentinel_reads_as_no_data_not_a_prewarm_gap(av):
    _write(av_mod.AV_CONSENSUS_NAMESPACE, "NODATA", [])
    with fmp_common.frozen_ttl_cache(), fmp_common.hermetic_fmp_history():
        out = av.get_quarterly_eps_consensus("NODATA", PAST)
    assert out["quarters"] == [] and out["unavailable"] == av_mod.UNAVAILABLE_NO_DATA
    assert f"{av_mod.AV_CONSENSUS_NAMESPACE}/NODATA" not in fmp_common.hermetic_miss_symbols()


def test_a_decision_before_any_snapshot_reads_as_none_as_of(av):
    _write(av_mod.AV_CONSENSUS_NAMESPACE, "MSFT", _av_payload())
    _write(av_mod.SPLIT_CALENDAR_NAMESPACE, "MSFT", {"symbol": "MSFT", "historical": []})
    with fmp_common.frozen_ttl_cache(), fmp_common.hermetic_fmp_history():
        out = av.get_quarterly_eps_consensus("MSFT", datetime(1990, 1, 2))
    assert out["quarters"] == [] and out["unavailable"] == av_mod.UNAVAILABLE_NONE_AS_OF


def test_an_empty_av_answer_is_a_cacheable_no_data_payload(monkeypatch):
    calls = []
    monkeypatch.setattr(av_mod, "make_api_request", lambda f, p, source="x": calls.append(f) or {})
    assert av_mod.fetch_av_consensus_payload("ZZZZ") == {} and calls == ["EARNINGS_ESTIMATES"]


def test_a_split_calendar_miss_raises_instead_of_reading_as_never_split(av):
    _write(av_mod.AV_CONSENSUS_NAMESPACE, "NOSPLITS", _av_payload())
    with fmp_common.frozen_ttl_cache(), fmp_common.hermetic_fmp_history():
        with pytest.raises(av_mod.SplitCalendarMissing):
            av.get_quarterly_eps_consensus("NOSPLITS", PAST)


def test_the_basis_comes_from_the_cached_split_calendar(av):
    """NVDA: 10:1 in 2024 -> the 2024-04-30 quarter (reported 2024-05-22) read in April 2024 is divided by 10."""
    _write(av_mod.AV_CONSENSUS_NAMESPACE, "NVDA", _av_payload("NVDA"))
    _write(av_mod.SPLIT_CALENDAR_NAMESPACE, "NVDA", {"symbol": "NVDA", "historical": [
        {"date": "2024-06-10", "numerator": 10, "denominator": 1},
        {"date": "2021-07-20", "numerator": 4, "denominator": 1}]})
    with fmp_common.frozen_ttl_cache(), fmp_common.hermetic_fmp_history():
        out = av.get_quarterly_eps_consensus("NVDA", datetime(2024, 4, 1))
    q = {r["fiscal_period_end"]: r for r in out["quarters"]}
    assert q["2024-04-30"]["split_factor"] == 10.0 and q["2024-04-30"]["eps_avg"] < 1.0


def test_fetch_payload_makes_two_calls_and_rejects_an_error_body(monkeypatch):
    calls = []

    def fake(function, params, source="x"):
        calls.append(function)
        return json.loads((FIX / f"{'earnings_estimates' if function == 'EARNINGS_ESTIMATES' else 'earnings'}_MSFT.json").read_text())
    monkeypatch.setattr(av_mod, "make_api_request", fake)
    monkeypatch.setattr(av_mod.time, "sleep", lambda s: None)
    payload = av_mod.fetch_av_consensus_payload("MSFT")
    assert calls == ["EARNINGS_ESTIMATES", "EARNINGS"] and set(payload) == {"fetched_on", "estimates", "earnings"}
    monkeypatch.setattr(av_mod, "make_api_request", lambda *a, **k: {"Error Message": "bad symbol"})
    with pytest.raises(av_mod.AlphaVantageEstimatesError):
        av_mod.fetch_av_consensus_payload("ZZZZ")


# ----------------------------------------------------------------------------------------- FMP
def _fmp(monkeypatch, rows):
    p = FMPCompanyDetailsProvider.__new__(FMPCompanyDetailsProvider)
    p.api_key = "x"
    monkeypatch.setattr(fmp_mod, "fmp_history_disk_cached", lambda ns, sym, fn, *a, **k: rows)
    return p


def test_fmp_estimates_with_a_historical_as_of_in_a_backtest_raise_the_typed_error(monkeypatch):
    p = _fmp(monkeypatch, [])
    with fmp_common.hermetic_fmp_history():
        with pytest.raises(pit.EstimatesNotPointInTime):
            p.get_earnings_estimates("AAPL", "quarterly", datetime(2021, 3, 1), 2, "dict")
        with pytest.raises(pit.EstimatesNotPointInTime):
            p.get_quarterly_eps_consensus("AAPL", datetime(2021, 3, 1))


def test_fmp_estimates_are_unchanged_live_and_for_today(monkeypatch):
    rows = [{"date": "2999-06-30", "estimatedEpsAvg": 5.0, "estimatedEpsHigh": 6.0, "estimatedEpsLow": 4.0,
             "numberAnalystsEstimatedEps": 7}]
    p = _fmp(monkeypatch, rows)
    out = p.get_earnings_estimates("AAPL", "quarterly", datetime.now(), 2, "dict")        # live: flag unset
    assert out["estimates"][0]["estimated_eps_avg"] == 5.0
    with fmp_common.hermetic_fmp_history():                                                  # today in a backtest
        out = p.get_earnings_estimates("AAPL", "quarterly", datetime.now(), 2, "dict")
    assert out["estimates"][0]["number_of_analysts"] == 7
    # a historical as_of OUTSIDE a hermetic run (e.g. prewarm, tools) is not refused
    assert p.get_earnings_estimates("AAPL", "quarterly", datetime(2021, 3, 1), 2, "dict")["estimates"]


def test_the_replay_permission_lets_a_historical_read_through_and_is_scoped(monkeypatch):
    p = _fmp(monkeypatch, [{"date": "2999-06-30", "estimatedEpsAvg": 5.0}])
    with fmp_common.hermetic_fmp_history():
        with pit.estimate_snapshot_allowed():
            assert p.get_earnings_estimates("AAPL", "quarterly", datetime(2021, 3, 1), 2, "dict")["estimates"]
        with pytest.raises(pit.EstimatesNotPointInTime):
            p.get_earnings_estimates("AAPL", "quarterly", datetime(2021, 3, 1), 2, "dict")


def test_fmp_live_quarterly_consensus_lists_the_unreported_quarters(monkeypatch):
    today = date.today()
    rows = [
        {"date": (today + timedelta(days=60)).isoformat(), "epsEstimated": 3.2, "eps": None,
         "fiscalDateEnding": (today + timedelta(days=30)).isoformat()},
        {"date": (today + timedelta(days=20)).isoformat(), "epsEstimated": 2.9, "eps": None, "fiscalDateEnding": "2026-09-30"},
        {"date": (today - timedelta(days=20)).isoformat(), "epsEstimated": 2.5, "eps": 2.6},       # reported
        {"date": (today + timedelta(days=90)).isoformat(), "epsEstimated": None, "eps": None},     # no consensus
    ]
    out = _fmp(monkeypatch, rows).get_quarterly_eps_consensus("AAPL")
    assert [q["eps_avg"] for q in out["quarters"]] == [2.9, 3.2] and out["source"] == "fmp"
    assert out["quarters"][0]["snapshot_date"] == today.isoformat() and out["quarters"][0]["eps_high"] is None


# ------------------------------------------------------------------------------- router + bundle
class _Rec:
    def __init__(self, name):
        self.name = name
        self.calls = []

    def get_earnings_estimates(self, *a, **k):
        self.calls.append(("get_earnings_estimates", a, k))
        return self.name

    def get_quarterly_eps_consensus(self, *a, **k):
        self.calls.append(("get_quarterly_eps_consensus", a, k))
        return self.name

    def get_past_earnings(self, *a, **k):
        self.calls.append(("get_past_earnings", a, k))
        return self.name


def test_router_sends_only_the_estimate_methods_to_the_estimates_provider():
    fmp, avp = _Rec("fmp"), _Rec("av")
    r = EstimatesRoutedDetails(fmp, avp, "alphavantage")
    assert r.get_earnings_estimates("A", "quarterly", 1) == "av"
    assert r.get_quarterly_eps_consensus("A", None) == "av"
    assert r.get_past_earnings("A") == "fmp" and r.base_provider is fmp
    assert [c[0] for c in fmp.calls] == ["get_past_earnings"]


def _bundle(source, made):
    def get_provider(category, name, **kw):
        made.append((category, name))
        return _Rec(f"{category}/{name}")
    return LiveProviderBundle(get_provider, estimates_source=source)


def test_bundle_with_an_alphavantage_source_routes_estimates_there():
    made = []
    d = _bundle("alphavantage", made).fundamentals_details()
    assert d.get_earnings_estimates("A") == "estimates/alphavantage"
    assert d.get_past_earnings("A") == "fundamentals_details/fmp"
    assert d.estimates_source == "alphavantage"


def test_bundle_with_fmp_builds_one_provider_and_memoises_the_router():
    made = []
    b = _bundle("fmp", made)
    assert b.fundamentals_details() is b.fundamentals_details()
    assert made == [("fundamentals_details", "fmp")]


def test_live_bundle_reads_the_setting_through_the_host_resolver():
    es.set_live_estimates_source_resolver(lambda: "alphavantage")
    try:
        assert _bundle(None, []).fundamentals_details().estimates_source == "alphavantage"
    finally:
        es.set_live_estimates_source_resolver(None)
    assert _bundle(None, []).fundamentals_details().estimates_source == "fmp"     # unwired = the declared default


def test_unknown_source_refuses():
    with pytest.raises(es.UnknownEstimatesSource):
        _bundle("yahoo", []).fundamentals_details()
    es.set_live_estimates_source_resolver(lambda: "bogus")
    try:
        with pytest.raises(es.UnknownEstimatesSource):
            es.live_estimates_source()
    finally:
        es.set_live_estimates_source_resolver(None)


def test_a_run_config_must_name_its_estimates_source():
    assert es.resolve_job_estimates_source({"estimates_source": "alphavantage"}) == "alphavantage"
    for bad in ({}, {"estimates_source": None}, {"estimates_source": ""}):
        with pytest.raises(es.EstimatesSourceMissing):
            es.resolve_job_estimates_source(bad)
    with pytest.raises(es.UnknownEstimatesSource):
        es.resolve_job_estimates_source({"estimates_source": "yahoo"})


def test_job_name_carries_the_source_once():
    assert es.with_estimates_source_name("job", "alphavantage") == "job-estalphavantage"
    assert es.with_estimates_source_name("job-estalphavantage", "alphavantage") == "job-estalphavantage"
    assert es.with_estimates_source_name("job", "fmp") == "job-estfmp"
    assert es.with_estimates_source_name("job", "fmp") != es.with_estimates_source_name("job", "alphavantage")


def test_registry_has_the_estimates_category():
    from ba2_providers import ESTIMATES_PROVIDERS
    assert set(ESTIMATES_PROVIDERS) == set(es.ESTIMATES_SOURCES)



def test_a_backtest_needs_no_api_key(monkeypatch):
    """A GA worker holds neither the AV nor (necessarily) the FMP key: cache reads must not ask for them."""
    monkeypatch.setattr(av_mod, "get_app_setting", lambda key, default=None: None)
    monkeypatch.setattr(av_mod, "make_api_request", lambda *a, **k: pytest.fail("network used"))
    fmp_common._HISTORY_MEM_CACHE._store.clear()
    fmp_common.reset_hermetic_misses()
    _write(av_mod.AV_CONSENSUS_NAMESPACE, "MSFT", _av_payload())
    _write(av_mod.SPLIT_CALENDAR_NAMESPACE, "MSFT", {"symbol": "MSFT", "historical": []})
    with fmp_common.frozen_ttl_cache(), fmp_common.hermetic_fmp_history():
        out = av_mod.AlphaVantageEstimatesProvider().get_quarterly_eps_consensus("MSFT", PAST)
    assert out["quarters"]
