"""A REAL captured session, built once and replayed by the tests in this package.

The replay commands are only worth anything if the bundle they read was produced
by the same recording path production uses, so nothing here hand-writes a
record: each case drives the actual ``_analysis_capture`` + ``_gather_and_process``
pair from ``MarketExpertInterface`` -- the code every live ``run_analysis`` calls
-- against deterministic fakes, with a real ``ReplayStore`` installed. What lands
in the bundle is therefore what a live session would land, minus the trading DB
rows (which ``run_analysis`` writes and replay never reads).

Eight analyses, chosen to cover what the report has to distinguish:

* the four recorded experts, one of them (DeterministicScorer) with a DataFrame
  in its bundle and a REAL tapped OHLCV read behind it,
* two analyses that read the evaluation clock in BOTH halves of the pair
  (FMPRating with the rating-recency filter on; FMPEarningsDrift down its
  FMP-typed calendar branch), because a flat list of clock reads replays those
  wrongly and every other fixture would pass anyway,
* one SKIP (FMPRating with no analyst coverage),
* one ERROR (a malformed provider row the calculator chokes on).

Two deliberate choices:

* **The clock is pinned** for the whole capture -- deterministic, and stepping by
  a second per read. A fixture built on ``datetime.now()`` re-dates itself every
  run, so a date-boundary failure is unreproducible; a fixture on a CONSTANT clock
  would hide the phase-tagging bug entirely, because the gather read and the
  process read would be the same instant.
* **Quotes and OHLCV go through the REAL taps** rather than a lambda, because the
  gather-tape replay has to serve them from the tape and can only do that if the
  production tap, with the production request identity, actually recorded them.
"""
from __future__ import annotations

import importlib
import json
import logging
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from unittest import mock

import pandas as pd

from ba2_common.core.backtest_context import LiveProviderBundle
from ba2_common.core.interfaces.MarketDataProviderInterface import ohlcv_identity
from ba2_common.core.replay import (
    ReplayStore,
    SessionRecord,
    encode,
    observe_provider,
    set_replay_store,
)
from ba2_common.core.replay.store import ObjectStore
from ba2_common.core.types import AnalysisUseCase

SESSION_ID = "S-REPLAY-TEST"

#: The instant every recorded analysis starts at. Pinned so a fixture built today
#: still means the same thing tomorrow (and on a date boundary).
NOW = datetime(2026, 9, 11, 14, 30, tzinfo=timezone.utc)

#: Successive clock reads step by this much instead of returning one constant.
#: A truly constant clock would make the phase-tagging bug INVISIBLE -- feeding
#: _process the gather read would produce the same instant and every assertion
#: would pass -- while still leaving the fixture deterministic, which is the
#: property that actually matters. Stepping keeps determinism AND makes the two
#: phases distinguishable.
CLOCK_STEP = timedelta(seconds=1)

#: Analysis ids, so a test can name the row it means.
RATING_ID = "1001"
DRIFT_ID = "1002"
INSIDER_ID = "1003"
SCORER_ID = "1004"
SKIP_ID = "1005"
ERROR_ID = "1006"
RECENCY_ID = "1007"
CALENDAR_ID = "1008"

ALL_IDS = (RATING_ID, DRIFT_ID, INSIDER_ID, SCORER_ID, SKIP_ID, ERROR_ID,
           RECENCY_ID, CALENDAR_ID)

#: The two analyses that read the evaluation clock in BOTH halves of the pair.
BOTH_PHASE_IDS = (RECENCY_ID, CALENDAR_ID)


# --------------------------------------------------------------------------- #
# The pinned clock
# --------------------------------------------------------------------------- #
class _SteppedDateTime(datetime):
    """A deterministic clock: read *n* returns ``NOW + n * CLOCK_STEP``."""

    reads = 0

    @classmethod
    def now(cls, tz=None):
        value = NOW + cls.reads * CLOCK_STEP
        cls.reads += 1
        return value if tz is not None else value.replace(tzinfo=None)

    @classmethod
    def utcnow(cls):
        return cls.now(timezone.utc).replace(tzinfo=None)


@contextmanager
def pinned_clock():
    """Pin every ``replay_now()`` read to the deterministic :class:`_SteppedDateTime`.

    One patch is enough because every recorded expert reads its evaluation time
    through ``ba2_common.core.replay.clock`` -- which is the point of that seam.
    """
    clock = importlib.import_module("ba2_common.core.replay.clock")
    _SteppedDateTime.reads = 0
    with mock.patch.object(clock, "datetime", _SteppedDateTime):
        yield


def _days_ago_iso(days: int) -> str:
    return (NOW - timedelta(days=days)).date().isoformat()


# --------------------------------------------------------------------------- #
# Host stand-ins
# --------------------------------------------------------------------------- #
@dataclass
class FakeMarketAnalysis:
    """Only the four attributes the capture scope reads off the live row."""

    id: str
    symbol: str
    subtype: Any = AnalysisUseCase.ENTER_MARKET
    created_at: Optional[datetime] = None


class TapedAccount:
    """The REAL tapped quote method, over a counting stub implementation.

    Bound off ``ReadOnlyAccountInterface`` (not re-implemented) so the tap under
    test is production's, including its TTL memo and its provenance probe.
    """

    def __init__(self, account_id: int, price: float):
        from ba2_common.core.interfaces.ReadOnlyAccountInterface import (
            ReadOnlyAccountInterface,
        )
        self.id = account_id
        self.price = price
        self.calls = 0
        ReadOnlyAccountInterface._GLOBAL_PRICE_CACHE.pop(account_id, None)
        for name in ("_CACHE_LOCK", "_GLOBAL_PRICE_CACHE", "_SYMBOL_LOCKS",
                     "_SYMBOL_LOCKS_LOCK"):
            setattr(type(self), name, getattr(ReadOnlyAccountInterface, name))

    def _get_instrument_current_price_impl(self, symbol_or_symbols, price_type="bid"):
        self.calls += 1
        if isinstance(symbol_or_symbols, list):
            return {symbol: self.price for symbol in symbol_or_symbols}
        return self.price

    def get_instrument_current_price(self, *args, **kwargs):
        from ba2_common.core.interfaces.ReadOnlyAccountInterface import (
            ReadOnlyAccountInterface,
        )
        return ReadOnlyAccountInterface.get_instrument_current_price(self, *args, **kwargs)

    def _cached_price_symbols(self, *args, **kwargs):
        from ba2_common.core.interfaces.ReadOnlyAccountInterface import (
            ReadOnlyAccountInterface,
        )
        return ReadOnlyAccountInterface._cached_price_symbols(self, *args, **kwargs)

    def _get_symbol_lock(self, key):
        from ba2_common.core.interfaces.ReadOnlyAccountInterface import (
            ReadOnlyAccountInterface,
        )
        return ReadOnlyAccountInterface._get_symbol_lock(self, key)


class TapedOHLCV:
    """A fake OHLCV body behind the PRODUCTION tap and the PRODUCTION identity.

    ``MarketDataProviderInterface.get_ohlcv_data`` cannot be used directly here:
    its body reads and writes the real parquet cache, which a hermetic test may
    not touch. What matters for replay is the tap and the request identity, and
    both are imported rather than restated -- an identity written out a second
    time would drift and the tape would miss for reasons the test invented.
    """

    def __init__(self, frame: pd.DataFrame):
        self.frame = frame
        self.windows: List[Dict[str, Any]] = []

    @observe_provider("market_data", "get_ohlcv_data", identity=ohlcv_identity)
    def get_ohlcv_data(self, symbol, start_date=None, end_date=None, interval="1d",
                       use_cache=True, max_cache_age_hours=24, lookback_days=30):
        self.windows.append(ohlcv_identity({
            "self": self, "symbol": symbol, "interval": interval,
            "start_date": start_date, "end_date": end_date,
            "lookback_days": lookback_days, "use_cache": use_cache}))
        return self.frame.copy()


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


def _resolver(mapping: Dict[str, Any]) -> Callable[..., Any]:
    def get_provider(category, name=None, **kwargs):
        return mapping[category]
    return get_provider


def _attach_quote(expert, account: TapedAccount) -> None:
    expert._get_current_price = lambda symbol: account.get_instrument_current_price(symbol)


# --------------------------------------------------------------------------- #
# Cases
# --------------------------------------------------------------------------- #
@dataclass
class Case:
    expert: Any
    settings: Dict[str, Any]
    patches: List[Any]
    market_analysis: FakeMarketAnalysis
    validate: Optional[Callable[[Dict[str, Any]], None]] = None
    expects_error: bool = False


def rating_case(analysis_id: str = RATING_ID, *, consensus_rows=None,
                max_analyst_age_months: int = 0, symbol: str = "AAPL") -> Case:
    """FMPRating. With ``max_analyst_age_months > 0`` it reads a clock in BOTH halves."""
    module = importlib.import_module("ba2_experts.FMPRating")
    consensus = {"symbol": symbol, "targetConsensus": 130.0, "targetHigh": 160.0,
                 "targetLow": 110.0, "targetMedian": 128.0}
    rows = [consensus] if consensus_rows is None else consensus_rows
    upgrades = [{"symbol": symbol, "strongBuy": 10, "buy": 5, "hold": 3,
                 "sell": 1, "strongSell": 1}]
    targets = [{"publishedDate": _days_ago_iso(2), "priceTarget": 126.0},
               {"publishedDate": _days_ago_iso(5), "priceTarget": 132.0}]
    grades = [{"date": _days_ago_iso(9), "gradingCompany": f"House {n}"}
              for n in range(12)]

    def fake_http_get(url, params=None, **kwargs):
        endpoint = kwargs["endpoint"]
        if endpoint == "price-target-consensus":
            return _Resp(list(rows))
        if endpoint == "upgrades-downgrades-consensus":
            return _Resp(list(upgrades))
        # The dated price-target history is faked at the HTTP layer, not by
        # replacing ``fetch_price_target_history_cached``: that module-level
        # function IS the tap (DeterministicScorer calls it directly), so patching
        # it out would delete the observation the gather tape has to serve.
        if endpoint == "price-target":
            return _Resp([dict(row) for row in targets])
        raise AssertionError(f"unexpected FMP endpoint {endpoint!r}")

    expert = module.FMPRating.__new__(module.FMPRating)
    expert.id = 11
    expert.logger = logging.getLogger("replayfixture.FMPRating")
    expert._api_key = "TEST-API-KEY"
    _attach_quote(expert, TapedAccount(9000 + int(analysis_id), 100.0))
    settings = {"profit_ratio": 1.0, "min_analysts": 10, "target_price_type": "consensus",
                "price_target_window_days": 90, "min_price_targets_per_quarter": 0,
                "max_analyst_age_months": max_analyst_age_months}
    expert._gather_window_days = settings["price_target_window_days"]
    expert._gather_max_analyst_age = max_analyst_age_months
    patches = [
        mock.patch.object(module, "fmp_http_get", fake_http_get),
        mock.patch.object(module, "fetch_analyst_grades_cached",
                          lambda api_key, sym: list(grades)),
        mock.patch.object(module, "_CONSENSUS_CACHE", module.TTLCache(300)),
        mock.patch.object(module, "_UPGRADE_CACHE", module.TTLCache(300)),
        mock.patch.object(module, "_PRICE_TARGET_HISTORY_CACHE", module.TTLCache(300)),
    ]
    return Case(expert, settings, patches, FakeMarketAnalysis(analysis_id, symbol))


def recency_case(analysis_id: str = RECENCY_ID) -> Case:
    """The rating-recency filter: a clock read in ``_gather`` AND one in ``_process``."""
    return rating_case(analysis_id, max_analyst_age_months=6, symbol="AMD")


def _drift_settings() -> Dict[str, Any]:
    return {"surprise_min_pct": 5.0, "max_days_since_report": 30,
            "expected_profit_percent": 8.0, "expected_profit_mode": "static",
            "dynamic_scale": 0.0, "max_expected_profit_percent": 100.0,
            "model_target_method": "forward"}


def drift_case(analysis_id: str = DRIFT_ID) -> Case:
    """FMPEarningsDrift down the PER-SYMBOL branch (a plain details provider)."""
    module = importlib.import_module("ba2_experts.FMPEarningsDrift")

    class _FakeDetails:
        def get_past_earnings(self, symbol, frequency, end_date, lookback_periods,
                              format_type, **kwargs):
            return {"earnings": [{"report_date": _days_ago_iso(1), "reported_eps": 1.2,
                                  "estimated_eps": 1.0, "surprise_percent": 20.0}]}

    expert = module.FMPEarningsDrift.__new__(module.FMPEarningsDrift)
    expert.id = 12
    expert.logger = logging.getLogger("replayfixture.FMPEarningsDrift")
    _attach_quote(expert, TapedAccount(9000 + int(analysis_id), 55.0))
    expert._live_providers = lambda: LiveProviderBundle(
        _resolver({"fundamentals_details": _FakeDetails()}))
    settings = _drift_settings()
    expert._gather_max_days_since_report = settings["max_days_since_report"]
    expert._gather_expected_profit_mode = settings["expected_profit_mode"]
    return Case(expert, settings, [], FakeMarketAnalysis(analysis_id, "MSFT"))


def calendar_drift_case(analysis_id: str = CALENDAR_ID) -> Case:
    """FMPEarningsDrift down the LIVE CALENDAR branch.

    The branch turns on ``isinstance(details_provider, FMPCompanyDetailsProvider)``,
    so the fake subclasses the real provider. ``_gather`` reads the clock to build
    the calendar window and ``_process`` reads it again to age the report: the
    both-phases case for EarningsDrift.
    """
    module = importlib.import_module("ba2_experts.FMPEarningsDrift")
    from ba2_providers.fmp_common import TTLCache
    from ba2_providers.fundamentals.details.FMPCompanyDetailsProvider import (
        FMPCompanyDetailsProvider,
    )

    calendar_rows = [
        {"symbol": "NFLX", "date": _days_ago_iso(2), "eps": 3.1, "epsEstimated": 2.5},
        {"symbol": "MSFT", "date": _days_ago_iso(3), "eps": 2.0, "epsEstimated": 1.9},
    ]

    class _Details(FMPCompanyDetailsProvider):
        def __init__(self):
            self.api_key = "TEST-API-KEY"

        def get_past_earnings(self, symbol, frequency, end_date, lookback_periods,
                              format_type, **kwargs):
            raise AssertionError("the calendar row carried both EPS figures")

    expert = module.FMPEarningsDrift.__new__(module.FMPEarningsDrift)
    expert.id = 15
    expert.logger = logging.getLogger("replayfixture.FMPEarningsDriftCalendar")
    _attach_quote(expert, TapedAccount(9000 + int(analysis_id), 610.0))
    expert._live_providers = lambda: LiveProviderBundle(
        _resolver({"fundamentals_details": _Details()}))
    settings = _drift_settings()
    expert._gather_max_days_since_report = settings["max_days_since_report"]
    expert._gather_expected_profit_mode = settings["expected_profit_mode"]
    patches = [
        mock.patch.object(module.fmpsdk, "earning_calendar",
                          lambda apikey=None, from_date=None, to_date=None, **kw:
                          list(calendar_rows)),
        mock.patch.object(module, "_CALENDAR_CACHE",
                          TTLCache(module._CALENDAR_CACHE_TTL_SECONDS)),
    ]
    return Case(expert, settings, patches, FakeMarketAnalysis(analysis_id, "NFLX"))


def insider_case(analysis_id: str = INSIDER_ID, *, malformed_rows: bool = False) -> Case:
    module = importlib.import_module("ba2_experts.FMPInsiderClusterBuy")
    rows: List[Any] = [
        {"insider_name": "A", "transaction_type": "P-Purchase", "value": 150_000},
        {"insider_name": "B", "transaction_type": "P-Purchase", "value": 120_000},
        {"insider_name": "C", "transaction_type": "P-Purchase", "value": 200_000},
    ]
    if malformed_rows:
        # A provider row that is not a row at all. The live analysis FAILED on it;
        # the point of recording the inputs that produced a failure is that the
        # failure reproduces from them, so the fault is in the DATA (which the
        # bundle carries) and not monkeypatched onto the instance (which no
        # replay could ever reconstruct).
        rows = ["not-a-row"]

    class _FakeInsider:
        def get_insider_transactions(self, symbol, end_date, lookback_days=None,
                                     as_of=None, format_type="dict", **kwargs):
            return {"start_date": "2026-08-01T00:00:00", "end_date": "2026-08-31T00:00:00",
                    "transactions": list(rows)}

    expert = module.FMPInsiderClusterBuy.__new__(module.FMPInsiderClusterBuy)
    expert.id = 13
    expert.logger = logging.getLogger("replayfixture.FMPInsiderClusterBuy")
    _attach_quote(expert, TapedAccount(9000 + int(analysis_id), 42.0))
    expert._live_providers = lambda: LiveProviderBundle(_resolver({"insider": _FakeInsider()}))
    settings = {"lookback_days": 30, "min_insiders": 3, "min_total_value": 200_000.0,
                "expected_profit_percent": 10.0, "expected_profit_mode": "static",
                "model_target_method": "forward", "max_expected_profit_percent": 100.0}
    expert._gather_lookback_days = settings["lookback_days"]
    expert._gather_expected_profit_mode = settings["expected_profit_mode"]
    return Case(expert, settings, [], FakeMarketAnalysis(analysis_id, "NVDA"),
                validate=expert._require_current_price, expects_error=malformed_rows)


#: FMP's raw per-symbol statement payloads, keyed by the disk-cache namespace the
#: provider asks for. Faked UNDER the provider's tap, so the production method and
#: its production identity are what the capture records.
_SCORER_HISTORY = {
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

#: The FRED files the macro section reads, seeded into the module's in-process memo
#: so the REAL (tapped) point-in-time read runs with no disk and no network.
_SCORER_FRED = {
    "VIXCLS": [{"date": "2026-09-09", "value": "14.5"},
               {"date": "2026-09-10", "value": "15.5"}],
    "UNRATE": [{"date": "2026-07-01", "value": "4.1", "realtime_start": "2026-08-02"},
               {"date": "2026-08-01", "value": "4.2", "realtime_start": "2026-09-05"}],
    "BAA10Y": [{"date": "2026-09-09", "value": "1.8"},
               {"date": "2026-09-10", "value": "1.9"}],
    "T10Y3M": [{"date": "2026-09-09", "value": "0.4"},
               {"date": "2026-09-10", "value": "0.5"}],
}

_SCORER_GRADES = [{"date": _days_ago_iso(6), "analystRatingsStrongBuy": 6,
                   "analystRatingsbuy": 4, "analystRatingsHold": 2,
                   "analystRatingsSell": 1, "analystRatingsStrongSell": 0}]
_SCORER_TARGETS = [{"publishedDate": _days_ago_iso(2), "priceTarget": 120.0},
                   {"publishedDate": _days_ago_iso(8), "priceTarget": 116.0}]


def scorer_case(analysis_id: str = SCORER_ID, bars: int = 400) -> Case:
    """DeterministicScorer over its REAL data module -- no ``data.*`` stubs.

    Every fake sits BELOW a tapped boundary: the OHLCV tap, the fundamentals-details
    provider methods, FMP's dated grades/price-target fetchers and the FRED
    point-in-time read. Stubbing ``data.fetch_statements``/``fetch_macro_series``
    (as this fixture used to) sat ABOVE all of them, recorded nothing, and made the
    scorer's gather-tape row a permanent ``missing_capture``.
    """
    from ba2_experts.DeterministicScorer import DeterministicScorer, data
    from ba2_providers.fmp_common import TTLCache
    from ba2_providers.macro import fred_series

    # importlib for both: each package re-exports the CLASS under its module's name.
    details_module = importlib.import_module(
        "ba2_providers.fundamentals.details.FMPCompanyDetailsProvider")
    rating = importlib.import_module("ba2_experts.FMPRating")

    frame = _scorer_frame(bars)
    provider = TapedOHLCV(frame)
    fundamentals = details_module.FMPCompanyDetailsProvider.__new__(
        details_module.FMPCompanyDetailsProvider)
    fundamentals.api_key = "TEST-API-KEY"

    def fake_history(namespace, symbol, fetch_fn, *args, **kwargs):
        if namespace not in _SCORER_HISTORY:
            raise AssertionError(f"unexpected statement namespace {namespace!r}")
        return [dict(row) for row in _SCORER_HISTORY[namespace]]

    def fake_http_get(url, params=None, **kwargs):
        endpoint = kwargs["endpoint"]
        if endpoint == "grades-historical":
            return _Resp([dict(row) for row in _SCORER_GRADES])
        if endpoint == "price-target":
            return _Resp([dict(row) for row in _SCORER_TARGETS])
        raise AssertionError(f"unexpected FMP endpoint {endpoint!r}")

    expert = DeterministicScorer.__new__(DeterministicScorer)
    expert.id = 14
    expert.logger = logging.getLogger("replayfixture.DeterministicScorer")
    expert._get_fmp_api_key = lambda: "TEST-API-KEY"
    settings = {"w_technical": 1.0, "w_fundamental": 0.0, "w_analyst": 0.5, "w_macro": 0.0,
                "w_earnings": 0.0, "macro_mode": "off", "min_history_days": 260,
                "index_symbol": "SPY", "use_model_target": False,
                "theta_buy": 0.2, "theta_sell": -0.2}
    expert._live_providers = lambda: LiveProviderBundle(_resolver({
        "ohlcv": provider, "fundamentals_details": fundamentals}))
    expert._gather_w_analyst = settings["w_analyst"]
    expert._gather_w_earnings = settings["w_earnings"]
    expert._gather_index_symbol = settings["index_symbol"]
    expert._gather_use_model_target = settings["use_model_target"]
    data.reset_caches()
    patches = [
        mock.patch.object(details_module, "fmp_history_disk_cached", fake_history),
        mock.patch.object(rating, "fmp_http_get", fake_http_get),
        mock.patch.object(rating, "_GRADES_HISTORICAL_CACHE", TTLCache(300)),
        mock.patch.object(rating, "_PRICE_TARGET_HISTORY_CACHE", TTLCache(300)),
        # The memo IS the disk for this fixture: no file is read and none is written.
        mock.patch.dict(fred_series._MEM,
                        {sid: [dict(row) for row in rows]
                         for sid, rows in _SCORER_FRED.items()}, clear=True),
    ]
    return Case(expert, settings, patches, FakeMarketAnalysis(analysis_id, "GOOG"))


def skip_case(analysis_id: str = SKIP_ID) -> Case:
    """FMPRating with no analyst coverage -> the live SKIPPED outcome."""
    return rating_case(analysis_id, consensus_rows=[], symbol="TSLA")


def error_case(analysis_id: str = ERROR_ID) -> Case:
    """An analysis that FAILED live, on data the bundle carries -- so it replays."""
    return insider_case(analysis_id, malformed_rows=True)


CASE_BUILDERS: Tuple[Tuple[str, Callable[[], Case]], ...] = (
    (RATING_ID, rating_case),
    (DRIFT_ID, drift_case),
    (INSIDER_ID, insider_case),
    (SCORER_ID, scorer_case),
    (SKIP_ID, skip_case),
    (ERROR_ID, error_case),
    (RECENCY_ID, recency_case),
    (CALENDAR_ID, calendar_drift_case),
)


# --------------------------------------------------------------------------- #
# Building and exporting the session
# --------------------------------------------------------------------------- #
def capture_session(store_root, export_dir) -> Path:
    """Record every case into ``store_root`` and export them to ``export_dir``."""
    store = ReplayStore(store_root, writer="sync")
    store.begin_session(SessionRecord(
        session_id=SESSION_ID, instance_id="replay-test-instance",
        started_at=NOW, exchange_tz="America/New_York",
        app_version="test", package_versions={"ba2_common": "test"},
        source_revision="0" * 40, dirty=False))
    set_replay_store(store)
    try:
        for _analysis_id, builder in CASE_BUILDERS:
            case = builder()
            # What live run_analysis pins before _gather (the gather-time symbol).
            case.expert._gather_symbol = case.market_analysis.symbol
            with ExitStack() as stack:
                for patch in case.patches:
                    stack.enter_context(patch)
                stack.enter_context(pinned_clock())
                capture = case.expert._analysis_capture(
                    case.market_analysis, case.settings,
                    case.expert._use_case_of(case.market_analysis))
                try:
                    with capture:
                        case.expert._gather_and_process(
                            case.expert._live_providers(), case.settings,
                            market_analysis=case.market_analysis,
                            use_case=case.expert._use_case_of(case.market_analysis),
                            validate=case.validate)
                except AttributeError:
                    # The error case: live raised here and the record says so.
                    if not case.expects_error:
                        raise
    finally:
        set_replay_store(None)
        exported = store.export_session(SESSION_ID, export_dir)
        store.close(timeout=5.0)
    return exported


def rebuild_object(bundle_dir, analysis_id: str, role: str, mutate) -> str:
    """Rewrite one recorded object (``bundle_object`` / ``recommendation_object``).

    Editing the object file in place would only produce a hash mismatch (the name
    IS the content), so the mutated value is re-encoded, published as a NEW object
    and the manifest re-pointed at it -- which is exactly what a tampered-with
    bundle that still loads would look like.
    """
    root = Path(bundle_dir)
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    store = ObjectStore(root)
    entry = next(a for a in manifest["analyses"] if a["analysis_id"] == analysis_id)
    kind, data, meta = store.get(entry[role])
    from ba2_common.core.replay.codec import decode

    value = decode(kind, data, meta, frames=store.get)
    mutate(value)
    entry[role] = _publish(store, manifest, encode(value))
    (root / "manifest.json").write_text(
        json.dumps(manifest, indent=2, allow_nan=False, ensure_ascii=False),
        encoding="utf-8")
    return entry[role]


def rebuild_bundle_object(bundle_dir, analysis_id: str, mutate) -> str:
    """Rewrite one analysis's captured input bundle through ``mutate``."""
    return rebuild_object(bundle_dir, analysis_id, "bundle_object", mutate)


def _publish(store: ObjectStore, manifest: Dict[str, Any], encoded) -> str:
    """Publish an encoded value (and its frames) and list it in the manifest."""
    known = {item["hash"] for item in manifest["objects"]}
    for side in tuple(encoded.sides) + (encoded,):
        object_hash = store.put(side.kind, side.data)
        if object_hash not in known:
            manifest["objects"].append({
                "hash": object_hash, "kind": side.kind, "size": len(side.data),
                "path": store.relative_path_for(object_hash, side.kind).as_posix()})
            known.add(object_hash)
    from ba2_common.core.replay import content_hash

    return content_hash(encoded.kind, encoded.data)


def drop_observations(bundle_dir, analysis_id: str, method: str) -> int:
    """Remove one analysis's observations for ``method`` from the manifest."""
    root = Path(bundle_dir)
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    before = len(manifest["observations"])
    manifest["observations"] = [
        o for o in manifest["observations"]
        if not (o["method"] == method and analysis_id in o["analysis_ids"])]
    (root / "manifest.json").write_text(
        json.dumps(manifest, indent=2, allow_nan=False, ensure_ascii=False),
        encoding="utf-8")
    return before - len(manifest["observations"])


def edit_analysis(bundle_dir, analysis_id: str, **fields) -> Dict[str, Any]:
    """Overwrite fields of one recorded analysis in the manifest."""
    root = Path(bundle_dir)
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    entry = next(a for a in manifest["analyses"] if a["analysis_id"] == analysis_id)
    entry.update(fields)
    (root / "manifest.json").write_text(
        json.dumps(manifest, indent=2, allow_nan=False, ensure_ascii=False),
        encoding="utf-8")
    return entry


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _scorer_frame(bars: int) -> pd.DataFrame:
    index = pd.date_range("2024-01-01", periods=bars, freq="B", tz="UTC")
    closes = [100.0 + (i % 7) * 0.5 + i * 0.01 for i in range(bars)]
    return pd.DataFrame({
        "Date": index,
        "Open": closes,
        "High": [close + 1 for close in closes],
        "Low": [close - 1 for close in closes],
        "Close": closes,
        "Volume": [1_000_000 + i for i in range(bars)],
    })
