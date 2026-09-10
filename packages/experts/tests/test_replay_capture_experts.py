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
from ba2_common.core.models import ExpertRecommendation, MarketAnalysis
from ba2_common.core.replay import (
    ReplayStatus,
    ReplayStore,
    SessionRecord,
    encode,
    get_replay_store,
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


def _assert_bundle_equal(captured, fresh):
    assert set(captured) == set(fresh), (
        f"captured bundle keys differ: {sorted(captured)} vs {sorted(fresh)}")
    for key, expected in fresh.items():
        got = captured[key]
        if isinstance(expected, pd.DataFrame):
            pd.testing.assert_frame_equal(got, expected)
        elif isinstance(expected, pd.Series):
            pd.testing.assert_series_equal(got, expected)
        else:
            assert got == expected, f"bundle[{key!r}] differs: {got!r} != {expected!r}"


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
        raise AssertionError(f"unexpected FMP endpoint {endpoint!r}")

    def fake_price_targets(api_key, symbol):
        counters["price_targets"] += 1
        return list(price_targets)

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
        mock.patch.object(mod, "fetch_price_target_history_cached", fake_price_targets),
        mock.patch.object(mod, "fetch_analyst_grades_cached", fake_grades),
    ]

    def reset():
        # Fresh TTL memos: without this, run 2 is served from run 1's cache and
        # "identical call counts" would be true for the wrong reason.
        mod._CONSENSUS_CACHE = TTLCache(300)
        mod._UPGRADE_CACHE = TTLCache(300)
        for key in counters:
            counters[key] = 0

    return expert, settings, patches, counters, reset


def _earnings_drift_case(earnings_rows=None):
    mod = importlib.import_module("ba2_experts.FMPEarningsDrift")

    rows = earnings_rows if earnings_rows is not None else [
        {"report_date": "2026-06-10", "reported_eps": 1.2,
         "estimated_eps": 1.0, "surprise_percent": 20.0}]
    counters = {"past_earnings": 0}

    class _FakeDetails:
        def get_past_earnings(self, symbol, frequency, end_date, lookback_periods,
                              format_type, **kw):
            counters["past_earnings"] += 1
            return {"earnings": list(rows)}

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
    providers = LiveProviderBundle(_resolver({"fundamentals_details": _FakeDetails(),
                                              "ohlcv": FakeOHLCV()}))
    expert._live_providers = lambda: providers

    def reset():
        counters["past_earnings"] = 0

    return expert, settings, [], counters, reset


def _insider_case(transactions=None):
    mod = importlib.import_module("ba2_experts.FMPInsiderClusterBuy")

    rows = transactions if transactions is not None else [
        {"insider_name": "A", "transaction_type": "P-Purchase", "value": 100_000},
        {"insider_name": "B", "transaction_type": "P-Purchase", "value": 100_000},
        {"insider_name": "C", "transaction_type": "P-Purchase", "value": 100_000},
    ]
    counters = {"insider": 0}

    class _FakeInsider:
        def get_insider_transactions(self, symbol, end_date, lookback_days=None,
                                     as_of=None, format_type="dict", **kw):
            counters["insider"] += 1
            return {"start_date": "2026-05-14T00:00:00",
                    "end_date": "2026-06-13T00:00:00",
                    "transactions": list(rows)}

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
    providers = LiveProviderBundle(_resolver({"insider": _FakeInsider(),
                                              "ohlcv": FakeOHLCV()}))
    expert._live_providers = lambda: providers

    def reset():
        counters["insider"] = 0

    return expert, settings, [], counters, reset


def _ds_frame(bars=400):
    index = pd.date_range("2024-01-01", periods=bars, freq="B", tz="UTC")
    closes = [100.0 + (i % 7) * 0.5 + i * 0.01 for i in range(bars)]
    return pd.DataFrame({
        "Date": index,
        "Open": closes, "High": [c + 1 for c in closes],
        "Low": [c - 1 for c in closes], "Close": closes,
        "Volume": [1_000_000 + i for i in range(bars)],
    })


def _deterministic_scorer_case(bars=400):
    from unittest import mock

    from ba2_experts.DeterministicScorer import DeterministicScorer, data

    frame = _ds_frame(bars)
    counters = {"ohlcv": 0, "statements": 0, "earnings": 0, "macro": 0, "index": 0}

    def fake_ohlcv(providers, symbol, as_of, lookback_days=None):
        counters["ohlcv"] += 1
        return frame.copy()

    def fake_statements(providers, symbol, as_of, lookback_periods=6):
        counters["statements"] += 1
        return {"income": [], "balance": [], "cashflow": []}

    def fake_earnings(providers, symbol, as_of, lookback_periods=16):
        counters["earnings"] += 1
        return []

    def fake_macro(providers, as_of):
        counters["macro"] += 1
        return {"vix": None, "unrate_series": None,
                "spread_10y3m_series": None, "oas_series": None}

    def fake_index(providers, as_of, index_symbol="SPY"):
        counters["index"] += 1
        return frame["Close"].copy()

    expert = DeterministicScorer.__new__(DeterministicScorer)
    expert.id = 14
    expert.logger = logging.getLogger("capture.DeterministicScorer")
    expert._gather_symbol = "AAPL"
    expert._gather_w_analyst = 0.5
    expert._gather_w_earnings = 0.0
    expert._gather_index_symbol = "SPY"
    expert._gather_use_model_target = False
    expert._get_fmp_api_key = lambda: None
    settings = {
        "w_technical": 1.0, "w_fundamental": 0.0, "w_analyst": 0.5, "w_macro": 0.0,
        "w_earnings": 0.0, "macro_mode": "off", "min_history_days": 260,
        "index_symbol": "SPY", "use_model_target": False,
        "theta_buy": 0.2, "theta_sell": -0.2,
    }
    expert._resolve_settings = lambda keys: dict(settings)
    expert._live_providers = lambda: LiveProviderBundle(_resolver({"ohlcv": FakeOHLCV()}))

    patches = [
        mock.patch.object(data, "fetch_ohlcv", fake_ohlcv),
        mock.patch.object(data, "fetch_statements", fake_statements),
        mock.patch.object(data, "fetch_past_earnings", fake_earnings),
        mock.patch.object(data, "fetch_macro_series", fake_macro),
        mock.patch.object(data, "fetch_index_closes", fake_index),
    ]

    def reset():
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
#: target count. EarningsDrift/Insider: the cached_get alias layer. The
#: DeterministicScorer fixture stubs its data module ABOVE the tapped provider
#: boundaries, so it records none -- stated as a number rather than left as
#: "some", so a tap that stopped firing is a failure and not a shrug.
EXPECTED_OBSERVATIONS = {
    "FMPRating": 3,
    "FMPEarningsDrift": 1,
    "FMPInsiderClusterBuy": 1,
    "DeterministicScorer": 0,
}

#: Which experts read an evaluation clock on their live path. Insider does not --
#: its decision is a pure aggregation of the provider's own dated window.
EXPECTS_CLOCK_READS = {
    "FMPRating": True,
    "FMPEarningsDrift": True,
    "FMPInsiderClusterBuy": False,
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
        stack.enter_context(freeze_now_for_live_path())
        store = stack.enter_context(capture_to(tmp_path / "guard"))
        with pytest.raises(ValueError, match="Unable to get current price"):
            expert.run_analysis("AAPL", market_analysis)
        records = list(store.index.analyses("S-TEST"))

    assert records[0].outcome == ReplayStatus.OUTCOME_ERROR
    assert "Unable to get current price" in records[0].error


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
