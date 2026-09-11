"""Live expert capture: what is recorded, and what must not change (spec step 2).

Each of the four recorded experts runs its REAL live ``run_analysis`` twice
against the same deterministic fakes -- once with capture off, once with capture
on -- and the two runs must be indistinguishable to everything except the store:

* the same ``Recommendation``, compared both with ``almost_equals`` and as EXACT
  codec bytes (a float that moved in the 12th place is a difference);
* the same provider fake call counts ("capture adds zero external requests");
* the same persisted ``ExpertRecommendation`` row.

And with capture on, the store must hold exactly one analysis record whose
captured bundle equals a fresh ``_gather``, whose clock reads are present for the
experts that read a clock and absent for the one that does not, and whose outcome
names what actually happened -- recommendation, skip or error.
"""
import importlib
import logging
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone

import pandas as pd
import pytest

from ba2_common.core.backtest_context import LiveProviderBundle
from ba2_common.core.db import add_instance, get_instance
from ba2_common.core.interfaces.MarketDataProviderInterface import ohlcv_identity
from ba2_common.core.models import ExpertRecommendation, MarketAnalysis
from ba2_common.core.replay import (
    ReplayStatus,
    ReplayStore,
    SessionRecord,
    current_capture,
    encode,
    get_replay_store,
    observe_provider,
    set_replay_store,
)
from ba2_common.core.types import AnalysisUseCase, MarketAnalysisStatus

from tests.golden_fixtures import FakeOHLCV, freeze_now_for_live_path

NOW = datetime(2026, 6, 13, tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# Store harness
# --------------------------------------------------------------------------- #
@contextmanager
def capture_to(tmp_path):
    """Install a synchronous store for the duration (capture ON)."""
    store = ReplayStore(tmp_path, writer="sync")
    store.begin_session(SessionRecord(
        session_id="S-TEST",
        instance_id="test-instance",
        started_at=NOW,
        exchange_tz="America/New_York",
        app_version="test",
        dirty=False,
    ))
    set_replay_store(store)
    try:
        yield store
    finally:
        set_replay_store(None)
        store.close(timeout=5.0)


def _market_analysis(symbol, expert_instance_id):
    row = MarketAnalysis(
        symbol=symbol, expert_instance_id=expert_instance_id,
        status=MarketAnalysisStatus.PENDING, subtype=AnalysisUseCase.ENTER_MARKET,
    )
    return get_instance(MarketAnalysis, add_instance(row))


def _spy_process(expert, sink):
    """Record what ``_process`` returned WITHOUT changing it (test-side only)."""
    original = type(expert)._process

    def spy(self, bundle, settings, as_of=None):
        recommendation = original(self, bundle, settings, as_of=as_of)
        sink.append(recommendation)
        return recommendation

    expert._process = spy.__get__(expert, type(expert))


def _recommendation_rows(market_analysis_id):
    from sqlmodel import select

    from ba2_common.core.db import get_db

    with get_db() as session:
        return session.exec(
            select(ExpertRecommendation).where(
                ExpertRecommendation.market_analysis_id == market_analysis_id)
        ).all()


def _run(case, capture_root=None):
    """Drive one expert's live run_analysis; return (recommendation, calls, state)."""
    expert, settings, patches, counters, reset = case()
    produced = []
    _spy_process(expert, produced)
    reset()
    market_analysis = _market_analysis(expert._gather_symbol, expert.id)

    with ExitStack() as stack:
        for patch in patches:
            stack.enter_context(patch)
        stack.enter_context(freeze_now_for_live_path())
        if capture_root is not None:
            store = stack.enter_context(capture_to(capture_root))
        else:
            store = None
            assert get_replay_store() is None
        expert.run_analysis(expert._gather_symbol, market_analysis)
        records = list(store.index.analyses("S-TEST")) if store is not None else []
        observations = (list(store.index.session_observations("S-TEST"))
                        if store is not None else [])
        bundles = ([store.decode_object(r.bundle_object) if r.bundle_object else None
                    for r in records] if store is not None else [])

    return {
        "recommendation": produced[-1] if produced else None,
        "calls": dict(counters),
        "market_analysis": get_instance(MarketAnalysis, market_analysis.id),
        "rows": _recommendation_rows(market_analysis.id),
        "records": records,
        "observations": observations,
        "bundles": bundles,
        "expert": expert,
        "settings": settings,
    }


def _assert_identical(off, on):
    """Capture on changed NOTHING the live path can observe."""
    left, right = off["recommendation"], on["recommendation"]
    assert left is not None and right is not None
    assert left.almost_equals(right), f"decision drift:\n  off={left}\n  on ={right}"
    assert encode(left).data == encode(right).data, (
        "the recommendations differ in their exact serialized form")
    assert off["calls"] == on["calls"], (
        f"capture changed the provider call counts: {off['calls']} -> {on['calls']}")
    assert off["market_analysis"].status == on["market_analysis"].status
    assert len(off["rows"]) == len(on["rows"])
    for a, b in zip(off["rows"], on["rows"]):
        assert (a.recommended_action, a.confidence, a.expected_profit_percent,
                a.price_at_date, a.details) == (
            b.recommended_action, b.confidence, b.expected_profit_percent,
            b.price_at_date, b.details)


def _decode_object(root, object_hash):
    """Decode one stored object out of a closed store (tests read what was written)."""
    store = ReplayStore(root, writer="sync")
    try:
        return store.decode_object(object_hash)
    finally:
        store.close()


def _assert_bundle_equal(captured, fresh):
    assert set(captured) == set(fresh), (
        f"captured bundle keys differ: {sorted(captured)} vs {sorted(fresh)}")
    for key, expected in fresh.items():
        _assert_value_equal(captured[key], expected, f"bundle[{key!r}]")


def _assert_value_equal(got, expected, path):
    """Compare one bundle value, descending into containers.

    A plain ``==`` cannot compare a dict that HOLDS a frame (DeterministicScorer's
    ``macro_inputs`` holds three FRED Series): dict equality compares the Series
    element-wise and then asks for its truth value, which raises rather than
    failing the comparison it looks like it is making.
    """
    if isinstance(expected, pd.DataFrame):
        pd.testing.assert_frame_equal(got, expected)
    elif isinstance(expected, pd.Series):
        pd.testing.assert_series_equal(got, expected)
    elif isinstance(expected, dict):
        assert isinstance(got, dict) and set(got) == set(expected), (
            f"{path} keys differ: {got!r} != {expected!r}")
        for key, value in expected.items():
            _assert_value_equal(got[key], value, f"{path}[{key!r}]")
    elif isinstance(expected, (list, tuple)):
        assert len(got) == len(expected), f"{path} length differs"
        for index, value in enumerate(expected):
            _assert_value_equal(got[index], value, f"{path}[{index}]")
    else:
        assert got == expected, f"{path} differs: {got!r} != {expected!r}"


# --------------------------------------------------------------------------- #
# Fixtures: each builder returns (expert, settings, patches, counters, reset)
#
# ``counters`` counts the UNDERLYING fetches (not the tapped methods), so a tap
# that fetched a second time to fill in metadata would show up here. ``reset``
# clears the module-level TTL/coverage memos so run 2 fetches exactly as run 1
# did -- otherwise "identical call counts" would pass for the wrong reason.
# --------------------------------------------------------------------------- #
class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


def _fmp_rating_case():
    from unittest import mock

    from ba2_providers.fmp_common import TTLCache

    # importlib, not ``from ba2_experts import FMPRating``: the package __init__
    # re-exports the CLASS under the module's name, so the plain import hands back
    # a class and every monkeypatch below would land on the wrong object.
    mod = importlib.import_module("ba2_experts.FMPRating")

    consensus = {"targetConsensus": 130.0, "targetHigh": 160.0,
                 "targetLow": 110.0, "targetMedian": 128.0}
    upgrade = [{"strongBuy": 10, "buy": 5, "hold": 3, "sell": 1, "strongSell": 1}]
    price_targets = [
        {"publishedDate": "2026-06-10", "priceTarget": 110.0},
        {"publishedDate": "2026-06-09", "priceTarget": 124.0},
        {"publishedDate": "2026-06-08", "priceTarget": 128.0},
    ]
    counters = {"consensus": 0, "upgrades": 0, "price_targets": 0, "grades": 0}

    def fake_http_get(url, params=None, **kw):
        endpoint = kw.get("endpoint")
        if endpoint == "price-target-consensus":
            counters["consensus"] += 1
            return _Resp([consensus])
        if endpoint == "upgrades-downgrades-consensus":
            counters["upgrades"] += 1
            return _Resp(upgrade)
        # The dated price-target history is faked at the HTTP layer, not by
        # replacing ``fetch_price_target_history_cached``: that function IS the tap
        # (DeterministicScorer calls it directly), so patching it out would silently
        # delete the observation this fixture exists to assert.
        if endpoint == "price-target":
            counters["price_targets"] += 1
            return _Resp([dict(row) for row in price_targets])
        raise AssertionError(f"unexpected FMP endpoint {endpoint!r}")

    def fake_grades(api_key, symbol):
        counters["grades"] += 1
        return []

    expert = mod.FMPRating.__new__(mod.FMPRating)
    expert.id = 11
    expert.logger = logging.getLogger("capture.FMPRating")
    expert._api_key = "TEST-API-KEY"
    expert._gather_symbol = "AAPL"
    expert._get_current_price = lambda symbol: 100.0
    settings = {"profit_ratio": 1.0, "min_analysts": 10,
                "target_price_type": "consensus", "price_target_window_days": 90,
                "max_analyst_age_months": 0, "min_price_targets_per_quarter": 0}
    expert._resolve_settings = lambda keys: dict(settings)

    patches = [
        mock.patch.object(mod, "fmp_http_get", fake_http_get),
        mock.patch.object(mod, "fetch_analyst_grades_cached", fake_grades),
    ]

    def reset():
        # Fresh TTL memos: without this, run 2 is served from run 1's cache and
        # "identical call counts" would be true for the wrong reason.
        mod._CONSENSUS_CACHE = TTLCache(300)
        mod._UPGRADE_CACHE = TTLCache(300)
        mod._PRICE_TARGET_HISTORY_CACHE = TTLCache(300)
        for key in counters:
            counters[key] = 0

    return expert, settings, patches, counters, reset


#: Raw FMP ``historical_earning_calendar`` rows, faked UNDER the provider's tap.
_DRIFT_EARNINGS = [{"date": "2026-06-10", "eps": 1.2, "epsEstimated": 1.0, "time": "amc"}]

#: Raw FMP insider rows (v4 shape), faked under the insider provider's own fetch.
_INSIDER_ROWS = [
    {"transactionDate": "2026-06-02", "filingDate": "2026-06-03",
     "reportingName": "A", "transactionType": "P-Purchase",
     "securitiesTransacted": 1_000.0, "price": 100.0, "typeOfOwner": "officer"},
    {"transactionDate": "2026-06-03", "filingDate": "2026-06-04",
     "reportingName": "B", "transactionType": "P-Purchase",
     "securitiesTransacted": 1_000.0, "price": 100.0, "typeOfOwner": "director"},
    {"transactionDate": "2026-06-04", "filingDate": "2026-06-05",
     "reportingName": "C", "transactionType": "P-Purchase",
     "securitiesTransacted": 1_000.0, "price": 100.0, "typeOfOwner": "officer"},
]


class _PerSymbolDetails:
    """A real FMPCompanyDetailsProvider behind a type the calendar branch skips.

    ``_gather``'s live calendar shortcut is chosen by ``isinstance(provider,
    FMPCompanyDetailsProvider)``, so this fixture cannot simply hold the real
    provider: it would switch the expert onto the OTHER branch (the one
    ``_calendar_expert`` already covers) and this case would stop testing the
    per-symbol path. Delegating instead keeps the branch and still routes the
    fetch through the real, TAPPED provider method -- which is the whole point:
    the alias layer and the provider method are two distinct boundaries, and a
    hand-written stub here recorded only the first of them.
    """

    def __init__(self, inner):
        self._inner = inner

    def get_past_earnings(self, *args, **kwargs):
        return self._inner.get_past_earnings(*args, **kwargs)


def _earnings_drift_case(earnings_rows=None):
    from unittest import mock

    mod = importlib.import_module("ba2_experts.FMPEarningsDrift")
    details_module = importlib.import_module(
        "ba2_providers.fundamentals.details.FMPCompanyDetailsProvider")

    rows = _DRIFT_EARNINGS if earnings_rows is None else earnings_rows
    counters = {"past_earnings": 0}

    def fake_history(namespace, symbol, fetch_fn, *a, **kw):
        counters["past_earnings"] += 1
        if not namespace.startswith("past_earnings_"):
            raise AssertionError(f"unexpected cache namespace {namespace!r}")
        return [dict(row) for row in rows]

    inner = details_module.FMPCompanyDetailsProvider.__new__(
        details_module.FMPCompanyDetailsProvider)
    inner.api_key = "TEST-API-KEY"

    expert = mod.FMPEarningsDrift.__new__(mod.FMPEarningsDrift)
    expert.id = 12
    expert.logger = logging.getLogger("capture.FMPEarningsDrift")
    expert._gather_symbol = "AAPL"
    expert._gather_max_days_since_report = 30
    expert._gather_expected_profit_mode = "static"
    expert._get_current_price = lambda symbol: 100.0
    settings = {"surprise_min_pct": 5.0, "max_days_since_report": 30,
                "expected_profit_percent": 8.0}
    expert._resolve_settings = lambda keys: dict(settings)
    providers = LiveProviderBundle(_resolver({
        "fundamentals_details": _PerSymbolDetails(inner), "ohlcv": FakeOHLCV()}))
    expert._live_providers = lambda: providers
    patches = [mock.patch.object(details_module, "fmp_history_disk_cached", fake_history)]

    def reset():
        counters["past_earnings"] = 0

    return expert, settings, patches, counters, reset


def _insider_case(transactions=None):
    from unittest import mock

    mod = importlib.import_module("ba2_experts.FMPInsiderClusterBuy")
    insider_module = importlib.import_module(
        "ba2_providers.insider.FMPInsiderProvider")

    rows = _INSIDER_ROWS if transactions is None else transactions
    counters = {"insider": 0}

    def fake_history(namespace, symbol, fetch_fn, *a, **kw):
        counters["insider"] += 1
        if namespace != "insider_v2":
            raise AssertionError(f"unexpected cache namespace {namespace!r}")
        return [dict(row) for row in rows]

    # The REAL provider, with its per-symbol history faked underneath: the window
    # filter, the purchase/sale accounting and the row mapping the expert reads are
    # production's, not a stub's approximation of them.
    provider = insider_module.FMPInsiderProvider.__new__(
        insider_module.FMPInsiderProvider)
    provider.api_key = "TEST-API-KEY"

    expert = mod.FMPInsiderClusterBuy.__new__(mod.FMPInsiderClusterBuy)
    expert.id = 13
    expert.logger = logging.getLogger("capture.FMPInsiderClusterBuy")
    expert._gather_symbol = "AAPL"
    # Gather-time attrs live run_analysis resolves from settings BEFORE _gather;
    # the fresh-gather comparison below calls _gather directly, so it needs them too.
    expert._gather_lookback_days = 30
    expert._gather_expected_profit_mode = "static"
    expert._get_current_price = lambda symbol: 100.0
    settings = {"lookback_days": 30, "min_insiders": 3, "min_total_value": 200_000.0,
                "expected_profit_percent": 10.0}
    expert._resolve_settings = lambda keys: dict(settings)
    providers = LiveProviderBundle(_resolver({"insider": provider,
                                              "ohlcv": FakeOHLCV()}))
    expert._live_providers = lambda: providers
    patches = [mock.patch.object(insider_module, "fmp_history_disk_cached", fake_history)]

    def reset():
        counters["insider"] = 0

    return expert, settings, patches, counters, reset


def _ds_frame(bars=400):
    index = pd.date_range("2024-01-01", periods=bars, freq="B", tz="UTC")
    closes = [100.0 + (i % 7) * 0.5 + i * 0.01 for i in range(bars)]
    return pd.DataFrame({
        "Date": index,
        "Open": closes, "High": [c + 1 for c in closes],
        "Low": [c - 1 for c in closes], "Close": closes,
        "Volume": [1_000_000 + i for i in range(bars)],
    })


class _TapedOHLCV:
    """A fake OHLCV body behind the PRODUCTION tap and the PRODUCTION identity.

    ``MarketDataProviderInterface.get_ohlcv_data`` cannot be driven directly here
    (its body reads and writes the real parquet cache), and what this fixture
    needs is the tap, not the cache. The identity function is IMPORTED rather than
    restated so the recorded identity is production's.
    """

    def __init__(self, frame, counters):
        self.frame = frame
        self._counters = counters

    @observe_provider("market_data", "get_ohlcv_data", identity=ohlcv_identity)
    def get_ohlcv_data(self, symbol, start_date=None, end_date=None, interval="1d",
                       use_cache=True, max_cache_age_hours=24, lookback_days=30):
        self._counters["ohlcv"] += 1
        return self.frame.copy()


#: FMP's raw per-symbol history payloads, keyed by the disk-cache namespace the
#: provider asks for. Replaced UNDER the provider's tap, so the production method,
#: its production identity and its production formatting all run.
_DS_HISTORY = {
    "balance_sheet_annual": [{"date": "2025-12-31", "fillingDate": "2026-02-05",
                              "reportedCurrency": "USD", "totalAssets": 1_000.0,
                              "totalLiabilities": 400.0,
                              "totalStockholdersEquity": 600.0}],
    "income_statement_annual": [{"date": "2025-12-31", "fillingDate": "2026-02-05",
                                 "revenue": 500.0, "netIncome": 50.0,
                                 "grossProfit": 200.0, "operatingIncome": 80.0}],
    "cash_flow_statement_annual": [{"date": "2025-12-31", "fillingDate": "2026-02-05",
                                    "operatingCashFlow": 90.0, "freeCashFlow": 60.0}],
    "cashflow_statement_annual": [{"date": "2025-12-31", "fillingDate": "2026-02-05",
                                   "operatingCashFlow": 90.0, "freeCashFlow": 60.0}],
}

#: The FRED files DeterministicScorer's macro section reads, seeded into the
#: module's in-process memo so the REAL (tapped) point-in-time read runs offline.
_DS_FRED = {
    "VIXCLS": [{"date": "2026-06-11", "value": "14.5"},
               {"date": "2026-06-12", "value": "15.5"}],
    "UNRATE": [{"date": "2026-04-01", "value": "4.1", "realtime_start": "2026-05-02"},
               {"date": "2026-05-01", "value": "4.2", "realtime_start": "2026-06-05"}],
    "BAA10Y": [{"date": "2026-06-11", "value": "1.8"},
               {"date": "2026-06-12", "value": "1.9"}],
    "T10Y3M": [{"date": "2026-06-11", "value": "0.4"},
               {"date": "2026-06-12", "value": "0.5"}],
}

_DS_GRADES = [{"date": "2026-06-01", "analystRatingsStrongBuy": 6,
               "analystRatingsbuy": 4, "analystRatingsHold": 2,
               "analystRatingsSell": 1, "analystRatingsStrongSell": 0}]
_DS_TARGETS = [{"publishedDate": "2026-06-10", "priceTarget": 120.0},
               {"publishedDate": "2026-06-04", "priceTarget": 116.0}]


def _deterministic_scorer_case(bars=400):
    """DeterministicScorer over the REAL data module: no ``data.*`` stubs.

    Every fake here sits BELOW a tapped boundary -- the OHLCV tap, the
    fundamentals-details provider methods, FMP's grades/price-target fetchers and
    the FRED point-in-time read -- so what the store ends up holding is what a live
    session would hold. Stubbing ``data.fetch_statements``/``fetch_macro_series``
    (as this fixture used to) sat ABOVE all of them and recorded nothing, which is
    how those boundaries stayed unrecorded for a whole delivery.
    """
    from unittest import mock

    from ba2_experts.DeterministicScorer import DeterministicScorer, data
    from ba2_providers.fmp_common import TTLCache
    from ba2_providers.macro import fred_series

    # importlib for both: each package re-exports the CLASS under its module's
    # name, so a plain import would hand back a class and every patch below would
    # land on the wrong object.
    details_module = importlib.import_module(
        "ba2_providers.fundamentals.details.FMPCompanyDetailsProvider")
    rating = importlib.import_module("ba2_experts.FMPRating")

    frame = _ds_frame(bars)
    counters = {"ohlcv": 0, "statements": 0, "fmp_http": 0, "fred": 0}

    def fake_history(namespace, symbol, fetch_fn, *a, **kw):
        counters["statements"] += 1
        if namespace not in _DS_HISTORY:
            raise AssertionError(f"unexpected statement namespace {namespace!r}")
        return [dict(row) for row in _DS_HISTORY[namespace]]

    def fake_http_get(url, params=None, **kw):
        endpoint = kw["endpoint"]
        counters["fmp_http"] += 1
        if endpoint == "grades-historical":
            return _Resp([dict(row) for row in _DS_GRADES])
        if endpoint == "price-target":
            return _Resp([dict(row) for row in _DS_TARGETS])
        raise AssertionError(f"unexpected FMP endpoint {endpoint!r}")

    original_load = fred_series._load

    def counting_load(series_id):
        counters["fred"] += 1
        return original_load(series_id)

    provider = _TapedOHLCV(frame, counters)
    fundamentals = details_module.FMPCompanyDetailsProvider.__new__(
        details_module.FMPCompanyDetailsProvider)
    fundamentals.api_key = "TEST-API-KEY"

    expert = DeterministicScorer.__new__(DeterministicScorer)
    expert.id = 14
    expert.logger = logging.getLogger("capture.DeterministicScorer")
    expert._gather_symbol = "AAPL"
    expert._gather_w_analyst = 0.5
    expert._gather_w_earnings = 0.0
    expert._gather_index_symbol = "SPY"
    expert._gather_use_model_target = False
    expert._get_fmp_api_key = lambda: "TEST-API-KEY"
    settings = {
        "w_technical": 1.0, "w_fundamental": 0.0, "w_analyst": 0.5, "w_macro": 0.0,
        "w_earnings": 0.0, "macro_mode": "off", "min_history_days": 260,
        "index_symbol": "SPY", "use_model_target": False,
        "theta_buy": 0.2, "theta_sell": -0.2,
    }
    expert._resolve_settings = lambda keys: dict(settings)
    expert._live_providers = lambda: LiveProviderBundle(_resolver({
        "ohlcv": provider, "fundamentals_details": fundamentals}))

    patches = [
        mock.patch.object(details_module, "fmp_history_disk_cached", fake_history),
        mock.patch.object(rating, "fmp_http_get", fake_http_get),
        mock.patch.object(fred_series, "_load", counting_load),
    ]

    def reset():
        # Fresh memos: without this, run 2 is served from run 1's caches and
        # "identical call counts" would be true for the wrong reason.
        data.reset_caches()
        rating._GRADES_HISTORICAL_CACHE = TTLCache(300)
        rating._PRICE_TARGET_HISTORY_CACHE = TTLCache(300)
        # The FRED memo IS the disk in this fixture: seeded, not cleared.
        fred_series._MEM.update({sid: [dict(r) for r in rows]
                                 for sid, rows in _DS_FRED.items()})
        for key in counters:
            counters[key] = 0

    return expert, settings, patches, counters, reset


def _resolver(mapping):
    def get_provider(category, name, **kw):
        return mapping[category]
    return get_provider


CASES = {
    "FMPRating": _fmp_rating_case,
    "FMPEarningsDrift": _earnings_drift_case,
    "FMPInsiderClusterBuy": _insider_case,
    "DeterministicScorer": _deterministic_scorer_case,
}

#: Tapped provider calls the LIVE path of each fixture actually routes through.
#: FMPRating: the two consensus snapshots + the price-target history behind the
#: target count. EarningsDrift/Insider: the cached_get alias layer.
#: DeterministicScorer: its own OHLCV window (1) + the index window (1) + three
#: statements + the dated grades and price-target histories (2) + four FRED
#: series = 11. Stated as a number rather than left as "some", so a tap that
#: stopped firing is a failure and not a shrug.
EXPECTED_OBSERVATIONS = {
    "FMPRating": 3,
    # The alias layer AND the provider method are two distinct boundaries over one
    # fetch: ``past_earnings_get`` records the uniform as_of/lookback request the
    # expert made, ``get_past_earnings`` the provider-level window that answered it.
    "FMPEarningsDrift": 2,
    # Insider reads only the alias layer -- its provider method carries no tap.
    "FMPInsiderClusterBuy": 1,
    "DeterministicScorer": 11,
}

#: Which experts read an evaluation clock on their live path. All four do now:
#: Insider's own decision is a pure aggregation of the provider's dated window, but
#: the window itself ends at "now", and that end date reaches the insider request's
#: identity -- so the alias layer derives it through ``replay_now`` rather than an
#: un-replayable ``datetime.now()``.
EXPECTS_CLOCK_READS = {
    "FMPRating": True,
    "FMPEarningsDrift": True,
    "FMPInsiderClusterBuy": True,
    "DeterministicScorer": True,
}


# --------------------------------------------------------------------------- #
# 1. Capture changes nothing
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("name", sorted(CASES))
def test_capture_off_and_on_produce_the_same_analysis(name, tmp_path):
    off = _run(CASES[name])
    on = _run(CASES[name], capture_root=tmp_path / name)
    _assert_identical(off, on)
    assert off["records"] == [] and on["records"], "capture on recorded nothing"


# --------------------------------------------------------------------------- #
# 2. What the record contains
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("name", sorted(CASES))
def test_one_record_per_analysis_with_the_captured_bundle(name, tmp_path):
    on = _run(CASES[name], capture_root=tmp_path / name)

    assert len(on["records"]) == 1
    record = on["records"][0]
    assert record.expert_class == name
    assert record.symbol == "AAPL"
    assert record.use_case == AnalysisUseCase.ENTER_MARKET.value
    assert record.outcome == ReplayStatus.OUTCOME_RECOMMENDATION
    assert record.bundle_capture_status == ReplayStatus.CAPTURE_CAPTURED
    assert record.branch_flags["as_of_is_none"] is True, (
        "the record must say the live branch ran -- capture must never switch an "
        "analysis onto analyze_as_of (spec section 4)")
    assert record.capture_failures == {}, "recording degraded silently"
    assert record.settings_object is not None


@pytest.mark.parametrize("name", sorted(CASES))
def test_the_captured_bundle_equals_a_fresh_gather(name, tmp_path):
    """The stored bundle IS the normalized input, not a summary of it."""
    on = _run(CASES[name], capture_root=tmp_path / name)
    captured = on["bundles"][0]

    expert, _settings, patches, _counters, reset = CASES[name]()
    reset()
    with ExitStack() as stack:
        for patch in patches:
            stack.enter_context(patch)
        stack.enter_context(freeze_now_for_live_path())
        fresh = expert._gather(expert._live_providers(), as_of=None)

    _assert_bundle_equal(captured, fresh)


@pytest.mark.parametrize("name", sorted(CASES))
def test_clock_reads_are_recorded_only_where_a_clock_is_read(name, tmp_path):
    on = _run(CASES[name], capture_root=tmp_path / name)
    reads = list(on["records"][0].clock_reads)

    if EXPECTS_CLOCK_READS[name]:
        assert reads, f"{name} reads an evaluation clock but recorded none"
        for value in reads:
            parsed = datetime.fromisoformat(value)
            assert parsed.tzinfo is not None, "a recorded read must be tz-aware"
    else:
        assert reads == [], (
            f"{name} does not read a clock on its live path; recording one means a "
            f"read appeared where none was")


@pytest.mark.parametrize("name", sorted(CASES))
def test_observations_are_one_per_tapped_provider_call(name, tmp_path):
    on = _run(CASES[name], capture_root=tmp_path / name)
    observed = on["observations"]

    assert len(observed) == EXPECTED_OBSERVATIONS[name]
    for observation in observed:
        assert observation.analysis_ids == (on["records"][0].analysis_id,)
        assert observation.payload_object is not None
        blob = repr(observation.request_identity).lower()
        for forbidden in ("api_key", "apikey", "token", "test-api-key"):
            assert forbidden not in blob, (
                f"{forbidden!r} leaked into a recorded identity: "
                f"{observation.request_identity}")


def test_fmp_rating_records_the_consensus_endpoints_it_consumed(tmp_path):
    """The three FMP returns the live decision consumed, with their payloads."""
    root = tmp_path / "fmprating"
    on = _run(CASES["FMPRating"], capture_root=root)

    methods = sorted(o.method for o in on["observations"])
    assert methods == ["price_target_consensus", "price_target_history",
                       "upgrade_downgrade_consensus"]

    store = ReplayStore(root, writer="sync")
    try:
        payloads = {o.method: store.decode_object(o.payload_object)
                    for o in on["observations"]}
    finally:
        store.close()

    assert payloads["price_target_consensus"]["targetConsensus"] == 130.0
    assert payloads["upgrade_downgrade_consensus"][0]["strongBuy"] == 10
    assert len(payloads["price_target_history"]) == 3
    assert all(o.provenance == ReplayStatus.PROVENANCE_UNKNOWN
               for o in on["observations"]), (
        "an FMP helper cannot tell a fresh fetch from a TTL-memo hit, so its "
        "provenance stays honestly unknown rather than claiming 'network'")


def test_deterministic_scorer_records_every_boundary_its_gather_read(tmp_path):
    """Statements, macro, the index window and the dated analyst history.

    These reads used to be invisible: the fixture stubbed ``data.fetch_*`` ABOVE
    the provider boundaries and the boundaries themselves carried no tap, so a
    DeterministicScorer analysis recorded nothing but its own bundle. The counts
    below are per METHOD, so one tap falling silent is a named failure.
    """
    root = tmp_path / "scorer"
    on = _run(CASES["DeterministicScorer"], capture_root=root)

    methods = sorted(o.method for o in on["observations"])
    assert methods == [
        "get_balance_sheet", "get_cashflow_statement", "get_income_statement",
        "get_ohlcv_data", "get_ohlcv_data",
        "get_series_as_of", "get_series_as_of", "get_series_as_of", "get_series_as_of",
        "grades_historical", "price_target_history",
    ]

    by_method = {}
    for observation in on["observations"]:
        by_method.setdefault(observation.method, []).append(observation)

    # The index window is a SECOND OHLCV request, recorded in its own right.
    assert sorted(o.request_identity["symbol"]
                  for o in by_method["get_ohlcv_data"]) == ["AAPL", "SPY"]
    assert sorted(o.request_identity["series_id"]
                  for o in by_method["get_series_as_of"]) == [
        "BAA10Y", "T10Y3M", "UNRATE", "VIXCLS"]
    assert all(o.request_identity["as_of"] is None
               for o in by_method["get_series_as_of"]), (
        "the live macro read asks for every vintage published so far")
    assert by_method["get_balance_sheet"][0].request_identity["end_date"] == (
        on["records"][0].clock_reads[1]), (
        "the statement window must end at a RECORDED clock read -- an un-replayed "
        "datetime.now() in a request identity can never be matched again")

    store = ReplayStore(root, writer="sync")
    try:
        payloads = {method: store.decode_object(observations[0].payload_object)
                    for method, observations in by_method.items()}
    finally:
        store.close()
    assert payloads["get_balance_sheet"]["statements"][0]["total_assets"] == 1_000.0
    assert payloads["price_target_history"][0]["priceTarget"] == 120.0
    assert list(payloads["get_series_as_of"]) in ([14.5, 15.5], [4.1], [1.8, 1.9],
                                                  [0.4, 0.5])


def test_the_dated_analyst_history_is_recorded_once_per_call(tmp_path):
    """One boundary, one record -- whichever caller reaches it.

    DeterministicScorer imports ``fetch_grades_historical_cached`` directly while
    FMPRating reaches the same function through its instance method. With the tap
    on BOTH levels the expert's call would be recorded twice (one fetch, two
    observations); with it only on the instance method every DS read went
    unrecorded. It sits on the fetcher, and this pins both halves of that.
    """
    from ba2_common.core.replay import CaptureContext, CaptureHealth, use_capture_context

    on = _run(CASES["DeterministicScorer"], capture_root=tmp_path / "once")
    methods = [o.method for o in on["observations"]]
    assert methods.count("grades_historical") == 1
    assert methods.count("price_target_history") == 1
    scorer_identity = next(o.request_identity for o in on["observations"]
                           if o.method == "grades_historical")

    # The same session, now through FMPRating's instance method.
    expert, _settings, patches, _counters, reset = CASES["DeterministicScorer"]()
    reset()
    mod = importlib.import_module("ba2_experts.FMPRating")
    rating = mod.FMPRating.__new__(mod.FMPRating)
    rating._api_key = "TEST-API-KEY"
    rating.logger = logging.getLogger("capture.FMPRating.delegation")

    context = CaptureContext(
        analysis_meta={"analysis_id": "A2", "attempt_id": "T2", "session_id": "S-TEST",
                       "expert_class": "FMPRating", "expert_instance_id": 11,
                       "symbol": "AAPL", "use_case": "enter_market",
                       "scheduled_at": None, "started_at": NOW},
        health=CaptureHealth())
    with ExitStack() as stack:
        for patch in patches:
            stack.enter_context(patch)
        stack.enter_context(use_capture_context(context))
        rows = rating._fetch_grades_historical("AAPL")

    observed = [pending.observation for pending in context.observations]
    assert len(observed) == 1, (
        "the instance method wraps an already-tapped fetcher: one call, one record")
    assert observed[0].method == "grades_historical"
    assert observed[0].request_identity == scorer_identity, (
        "both callers must record the SAME identity, or a tape can serve only one")
    assert rows and rows[0]["analystRatingsStrongBuy"] == 6


# --------------------------------------------------------------------------- #
# 2b. The macro memo: one recorded read per ANALYSIS, and nothing left behind
# --------------------------------------------------------------------------- #
@contextmanager
def _seeded_fred():
    """The FRED files the macro section reads, seeded into the module memo.

    ``patch.dict(clear=True)``: the memo is process-wide, so a seed left behind
    would serve these rows to any later test that reads the same series.
    """
    from unittest import mock

    from ba2_providers.macro import fred_series

    with mock.patch.dict(fred_series._MEM,
                         {sid: [dict(row) for row in rows]
                          for sid, rows in _DS_FRED.items()}, clear=True):
        yield fred_series


def _macro_meta(analysis_id):
    return {"analysis_id": analysis_id, "attempt_id": analysis_id, "session_id": "S-TEST",
            "expert_class": "DeterministicScorer", "expert_instance_id": 14,
            "symbol": "AAPL", "use_case": "enter_market", "scheduled_at": None,
            "started_at": NOW}


def test_the_live_macro_memo_still_serves_repeated_calls_from_memory():
    """Capture OFF: the memo behaves exactly as it always has -- one load per series.

    The point of this memo is that a live instance analysing 30 symbols reads each
    FRED series once, not 30 times. Keying it on anything that changes per call (a
    clock read, say) disables it silently: every number stays correct and the work
    quietly multiplies.
    """
    from ba2_experts.DeterministicScorer import data

    loads = []
    with _seeded_fred() as fred_series:
        original = fred_series._load
        try:
            fred_series._load = lambda series_id: (loads.append(series_id),
                                                   original(series_id))[1]
            data.reset_caches()
            assert current_capture() is None
            for _ in range(5):
                out = data.fetch_macro_series(None, None)
        finally:
            fred_series._load = original
            data.reset_caches()

    assert out["vix"] == 15.5
    assert len(loads) == 4, (
        f"5 live calls should load each of the 4 series once; loaded {loads}")


def test_each_analysis_records_its_own_macro_reads_and_leaves_no_memo_behind(tmp_path):
    """Capture ON: every analysis records 4 macro observations, and the memo empties.

    Two failure modes, one key. A memo shared across analyses (the constant "live"
    key) serves the second analysis from the first one's entries, so its bundle
    holds macro values whose reads were never recorded -- unreplayable, and silently
    so. A memo keyed per analysis that nobody clears grows by four pandas Series per
    analysis for as long as the instance runs.
    """
    from ba2_common.core.replay import capture_scope
    from ba2_experts.DeterministicScorer import data

    recorded = {}
    with capture_to(tmp_path / "macro") as store:
        with _seeded_fred():
            data.reset_caches()
            for analysis_id in ("A1", "A2"):
                with capture_scope(store, _macro_meta(analysis_id)) as context:
                    context.set_phase(ReplayStatus.PHASE_GATHER)
                    context.set_skip("test scope")
                    data.fetch_macro_series(None, None)
                    recorded[analysis_id] = [
                        pending.observation.request_identity["series_id"]
                        for pending in context.observations]

    assert sorted(recorded["A1"]) == ["BAA10Y", "T10Y3M", "UNRATE", "VIXCLS"]
    assert sorted(recorded["A2"]) == sorted(recorded["A1"]), (
        "the second analysis was served from the first one's memo, so its macro "
        "inputs entered the bundle with no observation behind them")
    assert data._MACRO_CACHE._store == {}, (
        f"the closed analyses left {len(data._MACRO_CACHE._store)} macro entries in a "
        f"process-wide memo")


# --------------------------------------------------------------------------- #
# 3. Skip and error paths
# --------------------------------------------------------------------------- #
def test_fmp_rating_skip_is_recorded_with_its_reason(tmp_path):
    """No analyst coverage -> the live SKIPPED outcome, recorded as a skip."""
    from unittest import mock

    mod = importlib.import_module("ba2_experts.FMPRating")

    def case():
        expert, settings, patches, counters, reset = _fmp_rating_case()

        def no_coverage(url, params=None, **kw):
            counters["consensus"] += 1
            return _Resp([])            # empty list == no analyst coverage

        patches = [mock.patch.object(mod, "fmp_http_get", no_coverage)] + patches[1:]
        return expert, settings, patches, counters, reset

    off = _run(case)
    on = _run(case, capture_root=tmp_path / "skip")
    _assert_identical(off, on)

    record = on["records"][0]
    assert record.outcome == ReplayStatus.OUTCOME_SKIP
    assert record.skip_reason == "no consensus data"
    assert record.recommendation_object is not None, (
        "a skip carries its Recommendation too: `outcome` already says what the "
        "live platform acted on, and the object is what lets a replay compare the "
        "current_price, details and confidence a skip still carries. Recording "
        "only the reason left those fields unreplayable")
    skipped = _decode_object(tmp_path / "skip", record.recommendation_object)
    assert skipped.skip is True and skipped.skip_reason == "no consensus data"
    assert skipped.current_price == 100.0
    assert record.bundle_capture_status == ReplayStatus.CAPTURE_CAPTURED
    assert on["market_analysis"].status == MarketAnalysisStatus.SKIPPED
    assert on["market_analysis"].state["skip_reason"] == "no_analyst_coverage"


def test_deterministic_scorer_skip_is_recorded_with_its_reason(tmp_path):
    """Too little OHLCV history -> _process skips; the record says so."""
    case = lambda: _deterministic_scorer_case(bars=10)  # noqa: E731
    off = _run(case)
    on = _run(case, capture_root=tmp_path / "dsskip")
    _assert_identical(off, on)

    record = on["records"][0]
    assert record.outcome == ReplayStatus.OUTCOME_SKIP
    assert record.skip_reason == "insufficient_history"
    skipped = _decode_object(tmp_path / "dsskip", record.recommendation_object)
    assert skipped.skip is True and skipped.details.startswith("Insufficient OHLCV history")
    assert on["market_analysis"].status == MarketAnalysisStatus.SKIPPED


def test_an_error_inside_process_is_recorded_and_still_propagates(tmp_path):
    """Recording an error must not swallow it: the live failure path is unchanged."""
    expert, settings, patches, counters, reset = _insider_case()
    reset()
    boom = RuntimeError("calculator exploded")

    def exploding(self, bundle, settings, as_of=None):
        raise boom

    expert._process = exploding.__get__(expert, type(expert))
    market_analysis = _market_analysis("AAPL", expert.id)

    with ExitStack() as stack:
        for patch in patches:
            stack.enter_context(patch)
        stack.enter_context(freeze_now_for_live_path())
        store = stack.enter_context(capture_to(tmp_path / "error"))
        with pytest.raises(RuntimeError, match="calculator exploded"):
            expert.run_analysis("AAPL", market_analysis)
        records = list(store.index.analyses("S-TEST"))

    assert len(records) == 1
    assert records[0].outcome == ReplayStatus.OUTCOME_ERROR
    assert "calculator exploded" in records[0].error
    assert records[0].bundle_capture_status == ReplayStatus.CAPTURE_CAPTURED, (
        "the inputs that produced the failure are exactly what a replay needs")
    assert get_instance(
        MarketAnalysis, market_analysis.id).status == MarketAnalysisStatus.FAILED


def test_a_price_guard_failure_is_recorded_as_an_error(tmp_path):
    """The guard between _gather and _process still raises, and is recorded."""
    expert, settings, patches, counters, reset = _earnings_drift_case()
    reset()
    expert._get_current_price = lambda symbol: None
    market_analysis = _market_analysis("AAPL", expert.id)

    with ExitStack() as stack:
        for patch in patches:
            stack.enter_context(patch)
        stack.enter_context(freeze_now_for_live_path())
        store = stack.enter_context(capture_to(tmp_path / "guard"))
        with pytest.raises(ValueError, match="Unable to get current price"):
            expert.run_analysis("AAPL", market_analysis)
        records = list(store.index.analyses("S-TEST"))

    assert records[0].outcome == ReplayStatus.OUTCOME_ERROR
    assert "Unable to get current price" in records[0].error


def test_a_skip_with_no_reason_is_recorded_as_the_contract_breach_it_is(tmp_path):
    """``skip=True`` with no reason is a defect in the EXPERT, not in the recorder.

    Recording it as a reasonless skip would put a row in the store that no
    coverage report can act on; blaming the recorder ("capture degraded") would
    point at the wrong code. The record names the broken contract, and the live
    analysis still completes exactly as it would have.
    """
    from ba2_common.core.types import OrderRecommendation, Recommendation

    expert, settings, patches, counters, reset = _deterministic_scorer_case()
    reset()

    def reasonless_skip(self, bundle, settings, as_of=None):
        return Recommendation(
            signal=OrderRecommendation.HOLD, confidence=0.0, current_price=1.0,
            details="something was missing", expected_profit_percent=0.0,
            skip=True, skip_reason=None)

    expert._process = reasonless_skip.__get__(expert, type(expert))
    market_analysis = _market_analysis("AAPL", expert.id)

    with ExitStack() as stack:
        for patch in patches:
            stack.enter_context(patch)
        stack.enter_context(freeze_now_for_live_path())
        store = stack.enter_context(capture_to(tmp_path / "noreason"))
        expert.run_analysis("AAPL", market_analysis)
        records = list(store.index.analyses("S-TEST"))

    assert get_instance(
        MarketAnalysis, market_analysis.id).status == MarketAnalysisStatus.SKIPPED, (
        "the live outcome is unchanged -- only the RECORD says the contract broke")
    assert records[0].outcome == ReplayStatus.OUTCOME_ERROR
    assert "skip" in records[0].error and "reason" in records[0].error
    assert records[0].skip_reason is None
    assert records[0].capture_failures == {}, (
        "an expert contract breach must not be counted as capture degradation")


def test_an_unsupported_bundle_value_is_a_gap_not_a_failed_analysis(tmp_path):
    """A bundle the codec cannot represent degrades COVERAGE, never the analysis.

    The status says ``unsupported`` (not ``captured``), the gap names the role,
    and the recommendation the live path produced is unchanged -- which is the
    whole point of "recording is observational".
    """
    class _NotEncodable:
        pass

    expert, settings, patches, counters, reset = _insider_case()
    reset()
    original_gather = type(expert)._gather

    def gather_with_junk(self, providers, as_of):
        bundle = original_gather(self, providers, as_of)
        bundle["a_thing_the_codec_refuses"] = _NotEncodable()
        return bundle

    expert._gather = gather_with_junk.__get__(expert, type(expert))
    market_analysis = _market_analysis("AAPL", expert.id)

    with ExitStack() as stack:
        for patch in patches:
            stack.enter_context(patch)
        stack.enter_context(freeze_now_for_live_path())
        store = stack.enter_context(capture_to(tmp_path / "unsupported"))
        expert.run_analysis("AAPL", market_analysis)
        records = list(store.index.analyses("S-TEST"))
        coverage = list(store.index.coverage("S-TEST"))

    assert get_instance(
        MarketAnalysis, market_analysis.id).status == MarketAnalysisStatus.COMPLETED, (
        "an unrepresentable input must not fail the analysis")
    assert records[0].bundle_capture_status == ReplayStatus.CAPTURE_UNSUPPORTED
    assert records[0].bundle_object is None
    assert "bundle" in records[0].capture_gaps
    assert records[0].outcome == ReplayStatus.OUTCOME_RECOMMENDATION
    assert [c.status for c in coverage] == [ReplayStatus.COVERAGE_MISSING_CAPTURE], (
        "the gap must be visible in coverage, not silently absent")


# --------------------------------------------------------------------------- #
# 3b. The EarningsDrift calendar shortcut: the bulk response AND the chosen row
# --------------------------------------------------------------------------- #
def _calendar_expert(calendar_rows, per_symbol_row=None):
    """The LIVE calendar-shortcut branch, which needs a real FMPCompanyDetailsProvider."""
    from ba2_providers.fmp_common import TTLCache
    from ba2_providers.fundamentals.details.FMPCompanyDetailsProvider import (
        FMPCompanyDetailsProvider,
    )

    mod = importlib.import_module("ba2_experts.FMPEarningsDrift")
    mod._CALENDAR_CACHE = TTLCache(mod._CALENDAR_CACHE_TTL_SECONDS)
    counters = {"calendar": 0, "past_earnings": 0}

    class _Details(FMPCompanyDetailsProvider):
        def __init__(self):
            self.api_key = "TEST-API-KEY"

        def get_past_earnings(self, symbol, frequency, end_date, lookback_periods,
                              format_type, **kw):
            counters["past_earnings"] += 1
            return {"earnings": [per_symbol_row] if per_symbol_row else []}

    def fake_calendar(apikey=None, from_date=None, to_date=None, **kw):
        counters["calendar"] += 1
        return list(calendar_rows)

    expert = mod.FMPEarningsDrift.__new__(mod.FMPEarningsDrift)
    expert.id = 12
    expert.logger = logging.getLogger("capture.calendar")
    expert._gather_symbol = "AAPL"
    expert._gather_max_days_since_report = 30
    expert._gather_expected_profit_mode = "static"
    expert._get_current_price = lambda symbol: 100.0
    settings = {"surprise_min_pct": 5.0, "max_days_since_report": 30,
                "expected_profit_percent": 8.0}
    expert._resolve_settings = lambda keys: dict(settings)
    expert._live_providers = lambda: LiveProviderBundle(
        _resolver({"fundamentals_details": _Details(), "ohlcv": FakeOHLCV()}))

    from unittest import mock
    patches = [mock.patch.object(mod.fmpsdk, "earning_calendar", fake_calendar)]

    def reset():
        mod._CALENDAR_CACHE = TTLCache(mod._CALENDAR_CACHE_TTL_SECONDS)
        for key in counters:
            counters[key] = 0

    return expert, settings, patches, counters, reset


def test_the_bulk_calendar_and_the_chosen_row_are_both_recorded(tmp_path):
    """The market-wide response AND the one row this symbol's decision consumed."""
    rows = [
        {"symbol": "AAPL", "date": "2026-06-10", "eps": 1.2, "epsEstimated": 1.0},
        {"symbol": "MSFT", "date": "2026-06-11", "eps": 2.0, "epsEstimated": 1.9},
    ]
    case = lambda: _calendar_expert(rows)  # noqa: E731

    off = _run(case)
    on = _run(case, capture_root=tmp_path / "calendar")
    _assert_identical(off, on)
    assert off["calls"]["past_earnings"] == 0, (
        "the calendar row carried both EPS figures, so no per-symbol fetch runs")

    by_method = {o.method: o for o in on["observations"]}
    assert set(by_method) == {"earning_calendar", "earnings_calendar_row"}

    store = ReplayStore(tmp_path / "calendar", writer="sync")
    try:
        payloads = {m: store.decode_object(o.payload_object)
                    for m, o in by_method.items()}
    finally:
        store.close()

    # The bulk response is recorded as the helper returned it (deduped by symbol)...
    assert set(payloads["earning_calendar"]) == {"AAPL", "MSFT"}
    # ...and the chosen row is recorded in its own right, so a replay does not have
    # to re-derive which row this symbol used.
    assert payloads["earnings_calendar_row"]["symbol"] == "AAPL"
    assert by_method["earnings_calendar_row"].request_identity["symbol"] == "AAPL"

    identity = by_method["earning_calendar"].request_identity
    assert set(identity) == {"from", "to"}, (
        "the calendar identity is the DATE RANGE; the api key is not part of what "
        "makes the response what it is")
    assert "TEST-API-KEY" not in repr(identity)


def test_a_symbol_absent_from_the_calendar_records_the_absence(tmp_path):
    """"This symbol did not report in the window" is a fact, not a missing record."""
    rows = [{"symbol": "MSFT", "date": "2026-06-11", "eps": 2.0, "epsEstimated": 1.9}]
    case = lambda: _calendar_expert(rows)  # noqa: E731

    on = _run(case, capture_root=tmp_path / "absent")
    row_observation = next(o for o in on["observations"]
                           if o.method == "earnings_calendar_row")

    assert row_observation.response_class == ReplayStatus.RESPONSE_EMPTY
    assert row_observation.payload_kind == ReplayStatus.PAYLOAD_NONE
    assert on["records"][0].outcome == ReplayStatus.OUTCOME_RECOMMENDATION


# --------------------------------------------------------------------------- #
# 4. Every live call site is actually wired
# --------------------------------------------------------------------------- #
def test_every_recorded_expert_routes_its_live_path_through_the_capture_scope():
    """Guard: a call site that reverted to a bare _gather/_process records nothing.

    Reading the source is the only check that survives someone 'simplifying' the
    call site back -- the behaviour tests above would still pass with capture off
    only if the store were never installed, which is exactly the regression.
    """
    import inspect

    from ba2_experts.DeterministicScorer import DeterministicScorer
    from ba2_experts.FMPEarningsDrift import FMPEarningsDrift
    from ba2_experts.FMPInsiderClusterBuy import FMPInsiderClusterBuy
    from ba2_experts.FMPRating import FMPRating

    for expert_class in (FMPRating, FMPEarningsDrift, FMPInsiderClusterBuy,
                         DeterministicScorer):
        source = inspect.getsource(expert_class.run_analysis)
        assert "_analysis_capture" in source, (
            f"{expert_class.__name__}.run_analysis no longer opens a capture scope")
        assert "_gather_and_process" in source, (
            f"{expert_class.__name__}.run_analysis no longer uses the recorded pair")
        assert "self._gather(" not in source, (
            f"{expert_class.__name__}.run_analysis gathers outside the recorded pair")
        assert "_record_skip" not in source, (
            f"{expert_class.__name__}.run_analysis records the skip itself; "
            f"_gather_and_process owns the outcome so the experts cannot disagree "
            f"about whether a skip also leaves a recommendation on the row")
