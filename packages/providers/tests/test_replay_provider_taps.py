"""Provider taps: what they record, and what they must never change (spec step 2).

Two properties, asserted per tap:

1. **Capture off is not a code path.** With no capture context every tapped
   function returns the SAME object (identity, not equality) and calls its
   underlying fetch exactly as often as it did before the tap existed.
2. **Capture on adds nothing external.** The tap records the return it was
   already handed -- same call count, same object back -- plus a sanitized
   identity that never carries an api key.

Plus the two things a tap can only get wrong once: quote provenance (memo vs
network) and context propagation into a thread pool (a ContextVar does not cross
into a worker, so a fan-out would silently record nothing).
"""
from datetime import datetime, timezone

import pandas as pd
import pytest

from ba2_common.core.replay import (
    CaptureContext,
    CaptureHealth,
    ReplayStatus,
    capture_aware_submit,
    current_capture,
    observe_provider,
    record_observation,
    run_in_capture_context,
    sanitize_identity,
    use_capture_context,
)
from ba2_providers.cache.cached_get import insider_get, past_earnings_get


# --------------------------------------------------------------------------- #
# Harness
# --------------------------------------------------------------------------- #
def _context():
    """A capture-mode context with no store behind it (records into memory)."""
    return CaptureContext(
        analysis_meta={
            "analysis_id": "A1", "attempt_id": "T1", "session_id": "S1",
            "expert_class": "TestExpert", "expert_instance_id": 7,
            "symbol": "AAPL", "use_case": "enter_market",
            "scheduled_at": None, "started_at": datetime.now(timezone.utc),
        },
        health=CaptureHealth(),
    )


def _observations(context):
    return [pending.observation for pending in context.observations]


def _payloads(context):
    return [pending.payload for pending in context.observations]


class _FakeDetails:
    """Counts calls so a tap that fetched twice would be caught."""

    def __init__(self, payload):
        self.payload = payload
        self.calls = 0

    def get_past_earnings(self, symbol, frequency, end_date, lookback_periods,
                          format_type, **kw):
        self.calls += 1
        return self.payload


class _FakeInsider:
    def __init__(self, payload):
        self.payload = payload
        self.calls = 0

    def get_insider_transactions(self, symbol, end_date, lookback_days=None,
                                 as_of=None, format_type="dict", **kw):
        self.calls += 1
        return self.payload


# --------------------------------------------------------------------------- #
# 1. Passthrough with no context
# --------------------------------------------------------------------------- #
def test_cached_get_taps_are_passthrough_without_a_context():
    """No capture context: the SAME object comes back and the provider is called once."""
    assert current_capture() is None

    earnings = {"earnings": [{"report_date": "2026-06-10", "reported_eps": 1.2}]}
    details = _FakeDetails(earnings)
    out = past_earnings_get(details, "AAPL")
    assert out is earnings, "the tap must hand back the provider's own object"
    assert details.calls == 1

    insider = {"transactions": [], "start_date": "", "end_date": ""}
    provider = _FakeInsider(insider)
    out = insider_get(provider, "AAPL", lookback=30)
    assert out is insider
    assert provider.calls == 1


def test_call_counts_are_identical_with_and_without_capture():
    """Capture on issues ZERO additional provider calls (spec section 1)."""
    details = _FakeDetails({"earnings": []})
    past_earnings_get(details, "AAPL")
    off = details.calls

    context = _context()
    with use_capture_context(context):
        past_earnings_get(details, "AAPL")
    on = details.calls - off

    assert off == 1 and on == 1


# --------------------------------------------------------------------------- #
# 2. What is recorded
# --------------------------------------------------------------------------- #
def test_past_earnings_tap_records_identity_and_payload():
    earnings = {"earnings": [{"report_date": "2026-06-10", "reported_eps": 1.2}]}
    details = _FakeDetails(earnings)
    as_of = datetime(2026, 6, 13, tzinfo=timezone.utc)

    context = _context()
    with use_capture_context(context):
        out = past_earnings_get(details, "AAPL", as_of=as_of, lookback_periods=4)
    assert out is earnings

    observed = _observations(context)
    assert len(observed) == 1
    one = observed[0]
    assert (one.provider, one.method) == ("provider_cache", "past_earnings_get")
    assert one.request_identity == {
        "provider": "_FakeDetails", "symbol": "AAPL",
        "as_of": as_of.isoformat(), "frequency": "quarterly",
        "lookback_periods": 4, "format_type": "dict",
    }
    assert one.response_class == ReplayStatus.RESPONSE_DATA
    assert _payloads(context)[0] == earnings


def test_insider_tap_records_an_empty_answer_as_empty_not_missing():
    """An empty result is a FACT about the window, not an absent observation."""
    provider = _FakeInsider({"transactions": [], "start_date": "", "end_date": ""})
    context = _context()
    with use_capture_context(context):
        insider_get(provider, "AAPL", lookback=14)

    one = _observations(context)[0]
    assert one.method == "insider_get"
    assert one.request_identity["lookback"] == 14
    # The dict itself is non-empty (it carries the window), so 'data' is correct;
    # what matters is that the empty transaction list is recorded verbatim.
    assert _payloads(context)[0]["transactions"] == []


def test_the_payload_is_frozen_against_later_mutation():
    """The provider's object may be mutated afterwards; the record must not follow."""
    payload = {"transactions": [{"value": 1}]}
    provider = _FakeInsider(payload)
    context = _context()
    with use_capture_context(context):
        out = insider_get(provider, "AAPL")
    out["transactions"][0]["value"] = 999

    assert _payloads(context)[0]["transactions"][0]["value"] == 1


def test_a_failing_provider_propagates_unchanged_and_records_nothing():
    """A tap observes RETURNS. There is no return here, so there is nothing to record.

    Recording the attempt would put a row in the store that no replay can use and
    that reads like a response; swallowing the error would be worse still. The
    exception must arrive at the caller exactly as the provider raised it.
    """
    boom = ValueError("FMP quota exhausted")

    class _Failing:
        def get_insider_transactions(self, *a, **k):
            raise boom

    context = _context()
    with use_capture_context(context):
        with pytest.raises(ValueError) as excinfo:
            insider_get(_Failing(), "AAPL")

    assert excinfo.value is boom, "the tap re-wrapped or replaced the exception"
    assert _observations(context) == [], (
        "a failed fetch has no return value to observe")
    assert context.capture_failures == {}, (
        "a provider failure is not a RECORDING failure and must not be counted as one")


def test_a_broken_identity_function_never_breaks_the_call(monkeypatch):
    """A defect in the recording code degrades coverage, not the provider call."""
    from ba2_common.core.replay import observe as observe_module

    @observe_provider("test", "explodes",
                      identity=lambda a: 1 / 0)          # a bug in the tap itself
    def _fetch(value):
        return {"value": value}

    context = _context()
    with use_capture_context(context):
        result = _fetch(7)

    assert result == {"value": 7}, "the caller must still get its value"
    assert context.capture_failures, "the degradation must be counted"
    assert context.health.total >= 1


# --------------------------------------------------------------------------- #
# 3. No credential ever reaches the store
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("key", [
    "api_key", "apikey", "APIKEY", "ApiKey", "token", "access_token",
    "secret", "password", "authorization",
])
def test_sanitize_drops_every_credential_shaped_key(key):
    assert sanitize_identity({key: "s3cr3t", "symbol": "AAPL"}) == {"symbol": "AAPL"}


def test_sanitize_redacts_a_credential_embedded_in_a_value():
    out = sanitize_identity(
        {"url": "https://financialmodelingprep.com/api/v4/x?symbol=AAPL&apikey=S3CRET"})
    assert "S3CRET" not in out["url"]
    assert "symbol=AAPL" in out["url"]


def test_sanitize_redacts_a_credential_NESTED_inside_a_value():
    """A key one dict deep is exactly as exported as one at the root."""
    out = sanitize_identity({"request": {
        "rows": [{"url": "https://fmp/x?symbol=AAPL&apikey=S3CRET"}],
        "apikey": "S3CRET",
    }})
    blob = repr(out)
    assert "S3CRET" not in blob, blob
    assert "symbol=AAPL" in blob


def test_no_recorded_identity_contains_an_api_key():
    """The end-to-end guarantee across every tap exercised in this module."""
    context = _context()
    with use_capture_context(context):
        past_earnings_get(_FakeDetails({"earnings": []}), "AAPL")
        insider_get(_FakeInsider({"transactions": []}), "AAPL")
        record_observation(provider="fmp", method="earning_calendar",
                           identity={"from": "2026-06-01", "to": "2026-06-13",
                                     "apikey": "S3CRET"},
                           payload={}, provenance="network")

    blob = repr([o.request_identity for o in _observations(context)])
    for forbidden in ("S3CRET", "apikey", "api_key", "token"):
        assert forbidden not in blob, f"{forbidden!r} leaked into a recorded identity"


def test_identity_values_are_json_encodable():
    """The index stores request_identity as JSON: a datetime here would drop the row."""
    import json

    context = _context()
    as_of = datetime(2026, 6, 13, tzinfo=timezone.utc)
    with use_capture_context(context):
        insider_get(_FakeInsider({"transactions": []}), "AAPL", as_of=as_of)

    json.dumps(_observations(context)[0].request_identity, allow_nan=False)


# --------------------------------------------------------------------------- #
# 4. Quote provenance: memo vs network
# --------------------------------------------------------------------------- #
class _StubAccount:
    """The real ReadOnlyAccountInterface price path with a counting impl."""

    def __init__(self):
        from ba2_common.core.interfaces.ReadOnlyAccountInterface import (
            ReadOnlyAccountInterface,
        )
        self.id = 4242
        self.calls = 0
        self._interface = ReadOnlyAccountInterface
        ReadOnlyAccountInterface._GLOBAL_PRICE_CACHE.pop(self.id, None)

    # bound straight off the interface so the tap under test is the real one
    def _get_instrument_current_price_impl(self, symbol_or_symbols, price_type='bid'):
        self.calls += 1
        if isinstance(symbol_or_symbols, list):
            return {s: 100.0 for s in symbol_or_symbols}
        return 100.0

    def get_instrument_current_price(self, *a, **k):
        from ba2_common.core.interfaces.ReadOnlyAccountInterface import (
            ReadOnlyAccountInterface,
        )
        return ReadOnlyAccountInterface.get_instrument_current_price(self, *a, **k)

    def _cached_price_symbols(self, *a, **k):
        from ba2_common.core.interfaces.ReadOnlyAccountInterface import (
            ReadOnlyAccountInterface,
        )
        return ReadOnlyAccountInterface._cached_price_symbols(self, *a, **k)

    def _get_symbol_lock(self, key):
        from ba2_common.core.interfaces.ReadOnlyAccountInterface import (
            ReadOnlyAccountInterface,
        )
        return ReadOnlyAccountInterface._get_symbol_lock(self, key)

    _CACHE_LOCK = None
    _GLOBAL_PRICE_CACHE = None


def _account():
    from ba2_common.core.interfaces.ReadOnlyAccountInterface import ReadOnlyAccountInterface

    account = _StubAccount()
    _StubAccount._CACHE_LOCK = ReadOnlyAccountInterface._CACHE_LOCK
    _StubAccount._GLOBAL_PRICE_CACHE = ReadOnlyAccountInterface._GLOBAL_PRICE_CACHE
    _StubAccount._SYMBOL_LOCKS = ReadOnlyAccountInterface._SYMBOL_LOCKS
    _StubAccount._SYMBOL_LOCKS_LOCK = ReadOnlyAccountInterface._SYMBOL_LOCKS_LOCK
    return account


def test_price_provenance_is_network_then_memo_cache():
    account = _account()
    context = _context()
    with use_capture_context(context):
        first = account.get_instrument_current_price("AAPL")
        second = account.get_instrument_current_price("AAPL")

    assert first == second == 100.0
    assert account.calls == 1, "the second read is served from the TTL memo"

    observed = _observations(context)
    assert len(observed) == 2, "a cache HIT is an observation too (spec section 4)"
    assert observed[0].provenance == ReplayStatus.PROVENANCE_NETWORK
    assert observed[1].provenance == ReplayStatus.PROVENANCE_MEMO_CACHE
    assert observed[0].request_identity["symbols"] == ["AAPL"]
    assert observed[0].request_identity["price_type"] == "bid"


def test_price_tap_does_not_change_the_returned_value_or_call_count():
    account = _account()
    off = account.get_instrument_current_price("MSFT")
    calls_off = account.calls

    account2 = _account()
    context = _context()
    with use_capture_context(context):
        on = account2.get_instrument_current_price("MSFT")

    assert off == on
    assert account2.calls == calls_off


# --------------------------------------------------------------------------- #
# 5. Pool propagation (the ContextVar problem)
# --------------------------------------------------------------------------- #
@observe_provider("test", "pooled_fetch", identity=lambda a: {"chunk": a["chunk"]})
def _pooled_fetch(chunk):
    return {"chunk": chunk}


def test_capture_aware_submit_propagates_into_the_pool():
    """The screener fan-out pattern: submit -> the worker still records."""
    from concurrent.futures import ThreadPoolExecutor

    context = _context()
    with use_capture_context(context):
        with ThreadPoolExecutor(max_workers=3) as executor:
            futures = [capture_aware_submit(executor, _pooled_fetch, i) for i in range(3)]
            results = [f.result() for f in futures]

    assert results == [{"chunk": 0}, {"chunk": 1}, {"chunk": 2}]
    chunks = sorted(o.request_identity["chunk"] for o in _observations(context))
    assert chunks == [0, 1, 2], "a pooled tap recorded nothing -- context did not cross"


def test_run_in_capture_context_propagates_through_executor_map():
    """The FMP OHLCV chunked-history pattern: map -> the workers still record."""
    from concurrent.futures import ThreadPoolExecutor

    context = _context()
    with use_capture_context(context):
        with ThreadPoolExecutor(max_workers=3) as executor:
            results = list(executor.map(run_in_capture_context(_pooled_fetch), range(3)))

    assert results == [{"chunk": 0}, {"chunk": 1}, {"chunk": 2}]
    assert len(_observations(context)) == 3


def test_pool_helpers_are_plain_without_a_context():
    from concurrent.futures import ThreadPoolExecutor

    assert current_capture() is None
    with ThreadPoolExecutor(max_workers=2) as executor:
        assert capture_aware_submit(executor, _pooled_fetch, 9).result() == {"chunk": 9}
        assert list(executor.map(run_in_capture_context(_pooled_fetch), [1])) == [{"chunk": 1}]


# --------------------------------------------------------------------------- #
# 6. The screener tap
# --------------------------------------------------------------------------- #
def test_screener_screen_is_tapped_and_records_its_filters():
    """StockScreener.screen records the ranked result it returned, and its filters."""
    from ba2_providers.StockScreener import StockScreener

    screener = StockScreener.__new__(StockScreener)
    screener._as_of = None
    screener._settings = {"screener_max_stocks": 3, "screener_api_key": "S3CRET"}
    screener._progress_callback = None
    result = {"results": [{"symbol": "AAA"}], "stats": {"screener_candidates": 1}}

    # Drive the DECORATED wrapper with a stub body: the tap under test is the
    # decorator, not the 200-line pipeline it wraps.
    original = StockScreener.screen.__wrapped__
    try:
        StockScreener.screen = observe_provider(
            "screener", "screen",
            identity=lambda a: {"as_of": a["self"]._as_of,
                                "filters": a["self"]._settings},
        )(lambda self: result)
        context = _context()
        with use_capture_context(context):
            out = screener.screen()
    finally:
        StockScreener.screen = observe_provider(
            "screener", "screen",
            identity=lambda a: {"as_of": a["self"]._as_of,
                                "filters": a["self"]._settings},
        )(original)

    assert out is result
    one = _observations(context)[0]
    assert one.provider == "screener" and one.method == "screen"
    assert one.request_identity["filters"]["screener_max_stocks"] == 3
    assert "screener_api_key" not in one.request_identity["filters"]
    assert _payloads(context)[0]["results"] == [{"symbol": "AAA"}]


def test_screener_screen_carries_the_tap_in_production_code():
    """Guard: the decorator is actually applied on the shipped method."""
    from ba2_providers.StockScreener import StockScreener

    assert hasattr(StockScreener.screen, "__wrapped__"), (
        "StockScreener.screen is no longer tapped")


# --------------------------------------------------------------------------- #
# 7. OHLCV provenance from the native-cache counters
# --------------------------------------------------------------------------- #
def test_ohlcv_provenance_reads_the_cache_counters():
    """A hit is recorded as disk_cache, a miss as network, ambiguity as unknown."""
    from ba2_common.core import native_cache
    from ba2_common.core.interfaces.MarketDataProviderInterface import _ohlcv_provenance

    before = (native_cache.STATS.hits, native_cache.STATS.misses)
    native_cache.STATS.hits += 1
    assert _ohlcv_provenance({"use_cache": True}, before) == ReplayStatus.PROVENANCE_DISK_CACHE

    before = (native_cache.STATS.hits, native_cache.STATS.misses)
    native_cache.STATS.misses += 1
    assert _ohlcv_provenance({"use_cache": True}, before) == ReplayStatus.PROVENANCE_NETWORK

    # Two counters moved (a concurrent read): unknown, never a confident guess.
    before = (native_cache.STATS.hits, native_cache.STATS.misses)
    native_cache.STATS.hits += 1
    native_cache.STATS.misses += 1
    assert _ohlcv_provenance({"use_cache": True}, before) == ReplayStatus.PROVENANCE_UNKNOWN

    # Caching disabled: it can only have come from the source.
    assert _ohlcv_provenance({"use_cache": False}, None) == ReplayStatus.PROVENANCE_NETWORK


def test_get_ohlcv_data_records_the_frame_it_returned():
    """The interface method is tapped: identity names the request, payload IS the frame."""
    from ba2_common.core.interfaces.MarketDataProviderInterface import (
        MarketDataProviderInterface,
    )

    frame = pd.DataFrame({
        "Date": pd.to_datetime(["2026-06-10", "2026-06-11"]),
        "Open": [1.0, 2.0], "High": [1.0, 2.0], "Low": [1.0, 2.0],
        "Close": [1.5, 2.5], "Volume": [10, 20],
    })

    class _Provider(MarketDataProviderInterface):
        def _get_ohlcv_data_impl(self, symbol, start_date, end_date, interval):
            return frame.copy()

        def get_provider_name(self):
            return "test"

        def get_supported_features(self):
            return []

        def validate_config(self):
            return True

    provider = _Provider()
    end = datetime(2026, 6, 12, tzinfo=timezone.utc)
    context = _context()
    with use_capture_context(context):
        out = provider.get_ohlcv_data("AAPL", end_date=end, lookback_days=10,
                                      use_cache=False)

    assert len(out) == 2
    one = _observations(context)[0]
    assert (one.provider, one.method) == ("market_data", "get_ohlcv_data")
    assert one.request_identity["symbol"] == "AAPL"
    assert one.request_identity["interval"] == "1d"
    assert one.request_identity["provider"] == "_Provider"
    assert one.payload_kind == ReplayStatus.PAYLOAD_FRAME
    assert one.provenance == ReplayStatus.PROVENANCE_NETWORK
    pd.testing.assert_frame_equal(_payloads(context)[0], out)


# --------------------------------------------------------------------------- #
# 8. The fundamentals-details taps (statements, past earnings, estimates)
#
# DeterministicScorer and the analyst-target estimator call these provider
# methods DIRECTLY -- not through the ``cached_get`` alias layer -- so the tap
# has to sit on the methods themselves. The fakes below replace the disk/network
# fetch UNDER the tap (``fmp_history_disk_cached``), so the production method,
# its production identity and its production formatting are what run.
# --------------------------------------------------------------------------- #
_BALANCE_ROWS = [{"date": "2025-12-31", "fillingDate": "2026-02-05",
                  "reportedCurrency": "USD", "totalAssets": 1_000.0,
                  "totalLiabilities": 400.0}]
_INCOME_ROWS = [{"date": "2025-12-31", "fillingDate": "2026-02-05",
                 "revenue": 500.0, "netIncome": 50.0}]
_CASHFLOW_ROWS = [{"date": "2025-12-31", "fillingDate": "2026-02-05",
                   "operatingCashFlow": 90.0, "freeCashFlow": 60.0}]
_EARNINGS_ROWS = [{"date": "2026-05-01", "eps": 1.2, "epsEstimated": 1.0, "time": "amc"}]
_ESTIMATE_ROWS = [{"date": "2026-12-31", "estimatedEpsAvg": 2.0,
                   "numberAnalystEstimatedRevenue": 9}]

_HISTORY_BY_NAMESPACE = {
    "balance_sheet_annual": _BALANCE_ROWS,
    "income_statement_annual": _INCOME_ROWS,
    "cash_flow_statement_annual": _CASHFLOW_ROWS,
    "cashflow_statement_annual": _CASHFLOW_ROWS,
    "past_earnings_quarterly": _EARNINGS_ROWS,
    "earnings_estimates_quarterly": _ESTIMATE_ROWS,
}


class _FMPDetails:
    """A real FMPCompanyDetailsProvider with its fetch replaced UNDER the tap."""

    def __init__(self):
        import importlib

        # importlib, not ``from ... import FMPCompanyDetailsProvider``: the package
        # re-exports the CLASS under the module's name, so the plain import hands
        # back a class and the patch below would land on the wrong object.
        module = importlib.import_module(
            "ba2_providers.fundamentals.details.FMPCompanyDetailsProvider")
        self.module = module
        self.provider = module.FMPCompanyDetailsProvider.__new__(
            module.FMPCompanyDetailsProvider)
        self.provider.api_key = "S3CRET-FMP-KEY"
        self.namespaces = []

    def __enter__(self):
        from unittest import mock

        def fake_history(namespace, symbol, fetch_fn, *a, **kw):
            self.namespaces.append(namespace)
            if namespace not in _HISTORY_BY_NAMESPACE:
                raise AssertionError(f"unexpected cache namespace {namespace!r}")
            return list(_HISTORY_BY_NAMESPACE[namespace])

        self._patch = mock.patch.object(self.module, "fmp_history_disk_cached", fake_history)
        self._patch.start()
        return self

    def __exit__(self, *exc):
        self._patch.stop()
        return False


def test_statement_taps_record_identity_and_payload():
    end = datetime(2026, 6, 13, tzinfo=timezone.utc)
    context = _context()
    with _FMPDetails() as details:
        with use_capture_context(context):
            out = details.provider.get_balance_sheet(
                symbol="AAPL", frequency="annual", end_date=end,
                lookback_periods=6, format_type="dict")

    observed = _observations(context)
    assert len(observed) == 1
    one = observed[0]
    assert (one.provider, one.method) == ("fundamentals_details", "get_balance_sheet")
    assert one.request_identity == {
        "provider": "FMPCompanyDetailsProvider", "symbol": "AAPL",
        "frequency": "annual", "start_date": None, "end_date": end.isoformat(),
        "lookback_periods": 6, "as_of": None, "format_type": "dict",
    }
    assert _payloads(context)[0] == out
    assert out["statements"][0]["total_assets"] == 1_000.0


@pytest.mark.parametrize("method", ["get_income_statement", "get_cashflow_statement"])
def test_the_other_statement_taps_record_under_their_own_method(method):
    end = datetime(2026, 6, 13, tzinfo=timezone.utc)
    context = _context()
    with _FMPDetails() as details:
        with use_capture_context(context):
            getattr(details.provider, method)(
                symbol="MSFT", frequency="annual", end_date=end,
                lookback_periods=2, format_type="dict")

    one = _observations(context)[0]
    assert (one.provider, one.method) == ("fundamentals_details", method)
    assert one.request_identity["symbol"] == "MSFT"
    assert one.request_identity["lookback_periods"] == 2


def test_past_earnings_and_estimates_taps_record_their_windows():
    end = datetime(2026, 6, 13, tzinfo=timezone.utc)
    context = _context()
    with _FMPDetails() as details:
        with use_capture_context(context):
            earnings = details.provider.get_past_earnings(
                symbol="AAPL", frequency="quarterly", end_date=end,
                lookback_periods=16, format_type="dict")
            estimates = details.provider.get_earnings_estimates(
                symbol="AAPL", frequency="quarterly", as_of_date=end,
                lookback_periods=2, format_type="dict")

    by_method = {o.method: o for o in _observations(context)}
    assert set(by_method) == {"get_past_earnings", "get_earnings_estimates"}
    assert by_method["get_past_earnings"].request_identity == {
        "provider": "FMPCompanyDetailsProvider", "symbol": "AAPL",
        "frequency": "quarterly", "end_date": end.isoformat(),
        "lookback_periods": 16, "format_type": "dict",
    }
    assert by_method["get_earnings_estimates"].request_identity == {
        "provider": "FMPCompanyDetailsProvider", "symbol": "AAPL",
        "frequency": "quarterly", "as_of_date": end.isoformat(),
        "lookback_periods": 2, "format_type": "dict",
    }
    assert earnings["earnings"][0]["reported_eps"] == 1.2
    assert isinstance(estimates, dict)


def test_the_details_taps_are_passthrough_and_add_no_fetch():
    """No context: the same value back. Context on: the SAME number of fetches."""
    end = datetime(2026, 6, 13, tzinfo=timezone.utc)
    assert current_capture() is None
    with _FMPDetails() as details:
        off = details.provider.get_balance_sheet(
            symbol="AAPL", frequency="annual", end_date=end, lookback_periods=6,
            format_type="dict")
        fetches_off = len(details.namespaces)

        context = _context()
        with use_capture_context(context):
            on = details.provider.get_balance_sheet(
                symbol="AAPL", frequency="annual", end_date=end, lookback_periods=6,
                format_type="dict")
        fetches_on = len(details.namespaces) - fetches_off

    assert fetches_off == 1 and fetches_on == 1
    assert off == on


def test_no_fmp_key_reaches_a_recorded_details_identity():
    """The provider holds the key on ``self``; the record must never carry it."""
    end = datetime(2026, 6, 13, tzinfo=timezone.utc)
    context = _context()
    with _FMPDetails() as details:
        with use_capture_context(context):
            details.provider.get_past_earnings(
                symbol="AAPL", frequency="quarterly", end_date=end,
                lookback_periods=4, format_type="dict")

    blob = repr([o.request_identity for o in _observations(context)])
    for forbidden in ("S3CRET", "api_key", "apikey"):
        assert forbidden not in blob, f"{forbidden!r} leaked into a recorded identity"


# --------------------------------------------------------------------------- #
# 9. The FRED tap
# --------------------------------------------------------------------------- #
def _seed_fred(series_id="VIXCLS"):
    """Seed the module's in-process memo so the real read runs with no disk/network."""
    from ba2_providers.macro import fred_series

    fred_series._MEM[series_id] = [
        {"date": "2026-06-11", "value": "14.5"},
        {"date": "2026-06-12", "value": "15.5"},
    ]
    return fred_series


def test_fred_tap_records_the_series_it_returned():
    fred_series = _seed_fred()
    context = _context()
    try:
        with use_capture_context(context):
            out = fred_series.get_series_as_of("VIXCLS", None)
    finally:
        fred_series.reset_cache()

    one = _observations(context)[0]
    assert (one.provider, one.method) == ("macro", "get_series_as_of")
    assert one.request_identity == {"series_id": "VIXCLS", "as_of": None}
    assert one.provenance == ReplayStatus.PROVENANCE_DISK_CACHE, (
        "get_series_as_of never reaches the network -- it reads the synced file")
    assert one.payload_kind == ReplayStatus.PAYLOAD_FRAME
    pd.testing.assert_series_equal(_payloads(context)[0], out)
    assert list(out) == [14.5, 15.5]


def test_fred_tap_records_the_as_of_cut_it_was_asked_for():
    fred_series = _seed_fred()
    as_of = datetime(2026, 6, 11, tzinfo=timezone.utc)
    context = _context()
    try:
        with use_capture_context(context):
            out = fred_series.get_series_as_of("VIXCLS", as_of)
    finally:
        fred_series.reset_cache()

    assert list(out) == [14.5], "the as_of cut is the point of this provider"
    assert _observations(context)[0].request_identity["as_of"] == as_of.isoformat()


def test_fred_tap_is_passthrough_without_a_context():
    fred_series = _seed_fred()
    assert current_capture() is None
    try:
        out = fred_series.get_series_as_of("VIXCLS", None)
    finally:
        fred_series.reset_cache()
    assert isinstance(out, pd.Series) and list(out) == [14.5, 15.5]


# --------------------------------------------------------------------------- #
# 10. The indicator tap (the ATR that sizes a live position)
# --------------------------------------------------------------------------- #
def _indicator_provider(calls):
    from ba2_common.core.interfaces.MarketIndicatorsInterface import (
        MarketIndicatorsInterface,
    )

    class _Indicators(MarketIndicatorsInterface):
        def __init__(self):
            pass

        def get_indicator(self, symbol, indicator, start_date=None, end_date=None,
                          lookback_days=None, interval="1d", format_type="markdown",
                          period=None):
            calls.append((symbol, indicator, period))
            return {"symbol": symbol, "indicator": indicator,
                    "values": [1.5, 2.5], "dates": ["2026-06-12", "2026-06-13"]}

        def get_supported_indicators(self):
            return ["atr"]

        def _format_as_dict(self, data):
            return data

        def _format_as_markdown(self, data):
            return str(data)

        def get_provider_name(self):
            return "test"

        def get_supported_features(self):
            return ["atr"]

        def validate_config(self):
            return True

    return _Indicators()


def test_indicator_tap_records_identity_and_payload():
    end = datetime(2026, 6, 13, tzinfo=timezone.utc)
    calls = []
    provider = _indicator_provider(calls)
    context = _context()
    with use_capture_context(context):
        out = provider.get_indicator("AAPL", "atr", end_date=end, lookback_days=60,
                                     interval="1d", format_type="dict", period=14)

    one = _observations(context)[0]
    assert (one.provider, one.method) == ("indicators", "get_indicator")
    assert one.request_identity == {
        "symbol": "AAPL", "indicator": "atr", "period": 14,
        "interval": "1d", "end_date": end.isoformat(),
    }
    assert _payloads(context)[0] == out
    assert calls == [("AAPL", "atr", 14)], "the tap must not fetch a second time"


def test_indicator_tap_is_passthrough_without_a_context():
    calls = []
    provider = _indicator_provider(calls)
    assert current_capture() is None
    out = provider.get_indicator("AAPL", "atr", end_date=None, period=14)
    assert out["values"] == [1.5, 2.5]
    assert len(calls) == 1


def test_a_subclass_of_a_tapped_implementation_does_not_record_twice():
    """``_replay_tapped`` stops one call being recorded once per class in the MRO."""
    calls = []
    base = type(_indicator_provider(calls))

    class _Derived(base):
        pass

    context = _context()
    with use_capture_context(context):
        _Derived().get_indicator("AAPL", "atr", end_date=None, period=14)

    assert len(_observations(context)) == 1


# --------------------------------------------------------------------------- #
# 11. Routing guard: every boundary a recorded analysis reads is tapped
# --------------------------------------------------------------------------- #
def _production_boundaries():
    import importlib

    from ba2_common.core.interfaces.MarketDataProviderInterface import (
        MarketDataProviderInterface,
    )
    from ba2_providers.fundamentals.details.FMPCompanyDetailsProvider import (
        FMPCompanyDetailsProvider,
    )
    from ba2_providers.indicators.AlphaVantageIndicatorsProvider import (
        AlphaVantageIndicatorsProvider,
    )
    from ba2_providers.indicators.PandasIndicatorCalc import PandasIndicatorCalc
    from ba2_providers.macro import fred_series

    # importlib: ba2_experts re-exports the CLASS under the module's name.
    rating = importlib.import_module("ba2_experts.FMPRating")

    return [
        ("FMPCompanyDetailsProvider.get_balance_sheet",
         FMPCompanyDetailsProvider.get_balance_sheet),
        ("FMPCompanyDetailsProvider.get_income_statement",
         FMPCompanyDetailsProvider.get_income_statement),
        ("FMPCompanyDetailsProvider.get_cashflow_statement",
         FMPCompanyDetailsProvider.get_cashflow_statement),
        ("FMPCompanyDetailsProvider.get_past_earnings",
         FMPCompanyDetailsProvider.get_past_earnings),
        ("FMPCompanyDetailsProvider.get_earnings_estimates",
         FMPCompanyDetailsProvider.get_earnings_estimates),
        ("fred_series.get_series_as_of", fred_series.get_series_as_of),
        ("FMPRating.fetch_grades_historical_cached", rating.fetch_grades_historical_cached),
        ("FMPRating.fetch_price_target_history_cached",
         rating.fetch_price_target_history_cached),
        ("MarketDataProviderInterface.get_ohlcv_data",
         MarketDataProviderInterface.get_ohlcv_data),
        ("PandasIndicatorCalc.get_indicator", PandasIndicatorCalc.get_indicator),
        ("AlphaVantageIndicatorsProvider.get_indicator",
         AlphaVantageIndicatorsProvider.get_indicator),
    ]


@pytest.mark.parametrize("name,attribute", _production_boundaries(),
                         ids=[name for name, _ in _production_boundaries()])
def test_every_recorded_provider_boundary_carries_its_tap(name, attribute):
    """A boundary that lost its decorator records nothing, silently.

    Reading the shipped attribute is the only check that survives someone
    'simplifying' the decorator away: the behaviour tests above would still pass
    against a provider the test itself wrapped.
    """
    assert hasattr(attribute, "__wrapped__"), f"{name} is no longer tapped"


def test_the_analyst_history_is_tapped_at_exactly_one_level():
    """FMPRating's instance methods DELEGATE to the tapped module fetchers.

    A tap on both levels would record one fetch twice -- the same response under
    two observations -- and a replay comparing what the live path consumed would
    see a fetch that never happened. The module fetcher is the single boundary
    because DeterministicScorer imports it directly and never touches the
    instance method.
    """
    import importlib

    rating = importlib.import_module("ba2_experts.FMPRating")
    for method in ("_fetch_grades_historical", "_fetch_price_target_history"):
        assert not hasattr(getattr(rating.FMPRating, method), "__wrapped__"), (
            f"FMPRating.{method} carries a second tap around the module fetcher's")
