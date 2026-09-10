"""A REAL captured session, built once and replayed by the tests in this package.

The replay commands are only worth anything if the bundle they read was produced
by the same recording path production uses, so nothing here hand-writes a
record: each case drives the actual ``_analysis_capture`` + ``_gather_and_process``
pair from ``MarketExpertInterface`` -- the code every live ``run_analysis`` calls
-- against deterministic fakes, with a real ``ReplayStore`` installed. What lands
in the bundle is therefore what a live session would land, minus the trading DB
rows (which ``run_analysis`` writes and replay never reads).

Six analyses, chosen to cover what the report has to distinguish:

* the four recorded experts, one of them (DeterministicScorer) with a DataFrame
  in its bundle,
* one SKIP (FMPRating with no analyst coverage),
* one ERROR (a calculator that raises inside ``_process``).

Quotes go through the REAL tapped ``ReadOnlyAccountInterface.get_instrument_current_price``
rather than a lambda, because the gather-tape replay has to serve the quote from
the tape and can only do that if the tap actually recorded it.
"""
from __future__ import annotations

import importlib
import json
import logging
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from unittest import mock

import pandas as pd

from ba2_common.core.backtest_context import LiveProviderBundle
from ba2_common.core.replay import (
    ReplayStore,
    SessionRecord,
    encode,
    set_replay_store,
)
from ba2_common.core.replay.store import ObjectStore
from ba2_common.core.types import AnalysisUseCase

SESSION_ID = "S-REPLAY-TEST"

#: Analysis ids, so a test can name the row it means.
RATING_ID = "1001"
DRIFT_ID = "1002"
INSIDER_ID = "1003"
SCORER_ID = "1004"
SKIP_ID = "1005"
ERROR_ID = "1006"

ALL_IDS = (RATING_ID, DRIFT_ID, INSIDER_ID, SCORER_ID, SKIP_ID, ERROR_ID)


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
# Cases: (expert, settings, patches, market_analysis, validate)
# --------------------------------------------------------------------------- #
@dataclass
class Case:
    expert: Any
    settings: Dict[str, Any]
    patches: List[Any]
    market_analysis: FakeMarketAnalysis
    validate: Optional[Callable[[Dict[str, Any]], None]] = None
    expects_error: bool = False


def rating_case(analysis_id: str = RATING_ID, *, consensus_rows=None) -> Case:
    module = importlib.import_module("ba2_experts.FMPRating")
    consensus = {"symbol": "AAPL", "targetConsensus": 130.0, "targetHigh": 160.0,
                 "targetLow": 110.0, "targetMedian": 128.0}
    rows = [consensus] if consensus_rows is None else consensus_rows
    upgrades = [{"symbol": "AAPL", "strongBuy": 10, "buy": 5, "hold": 3,
                 "sell": 1, "strongSell": 1}]
    targets = [{"publishedDate": _days_ago_iso(2), "priceTarget": 126.0},
               {"publishedDate": _days_ago_iso(5), "priceTarget": 132.0}]

    def fake_http_get(url, params=None, **kwargs):
        endpoint = kwargs["endpoint"]
        if endpoint == "price-target-consensus":
            return _Resp(list(rows))
        if endpoint == "upgrades-downgrades-consensus":
            return _Resp(list(upgrades))
        raise AssertionError(f"unexpected FMP endpoint {endpoint!r}")

    expert = module.FMPRating.__new__(module.FMPRating)
    expert.id = 11
    expert.logger = logging.getLogger("replayfixture.FMPRating")
    expert._api_key = "TEST-API-KEY"
    _attach_quote(expert, TapedAccount(9011, 100.0))
    settings = {"profit_ratio": 1.0, "min_analysts": 10, "target_price_type": "consensus",
                "price_target_window_days": 90, "min_price_targets_per_quarter": 0,
                "max_analyst_age_months": 0}
    expert._gather_window_days = settings["price_target_window_days"]
    expert._gather_max_analyst_age = settings["max_analyst_age_months"]
    patches = [
        mock.patch.object(module, "fmp_http_get", fake_http_get),
        mock.patch.object(module, "fetch_price_target_history_cached",
                          lambda api_key, symbol: list(targets)),
        mock.patch.object(module, "fetch_analyst_grades_cached", lambda api_key, symbol: []),
        mock.patch.object(module, "_CONSENSUS_CACHE", module.TTLCache(300)),
        mock.patch.object(module, "_UPGRADE_CACHE", module.TTLCache(300)),
    ]
    return Case(expert, settings, patches, FakeMarketAnalysis(analysis_id, "AAPL"))


def drift_case(analysis_id: str = DRIFT_ID) -> Case:
    module = importlib.import_module("ba2_experts.FMPEarningsDrift")

    class _FakeDetails:
        def get_past_earnings(self, symbol, frequency, end_date, lookback_periods,
                              format_type, **kwargs):
            return {"earnings": [{"report_date": _days_ago_iso(1), "reported_eps": 1.2,
                                  "estimated_eps": 1.0, "surprise_percent": 20.0}]}

    expert = module.FMPEarningsDrift.__new__(module.FMPEarningsDrift)
    expert.id = 12
    expert.logger = logging.getLogger("replayfixture.FMPEarningsDrift")
    _attach_quote(expert, TapedAccount(9012, 55.0))
    expert._live_providers = lambda: LiveProviderBundle(
        _resolver({"fundamentals_details": _FakeDetails()}))
    settings = {"surprise_min_pct": 5.0, "max_days_since_report": 30,
                "expected_profit_percent": 8.0, "expected_profit_mode": "static",
                "dynamic_scale": 0.0, "max_expected_profit_percent": 100.0,
                "model_target_method": "forward"}
    expert._gather_max_days_since_report = settings["max_days_since_report"]
    expert._gather_expected_profit_mode = settings["expected_profit_mode"]
    return Case(expert, settings, [], FakeMarketAnalysis(analysis_id, "MSFT"))


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
    _attach_quote(expert, TapedAccount(9016 if malformed_rows else 9013, 42.0))
    expert._live_providers = lambda: LiveProviderBundle(_resolver({"insider": _FakeInsider()}))
    settings = {"lookback_days": 30, "min_insiders": 3, "min_total_value": 200_000.0,
                "expected_profit_percent": 10.0, "expected_profit_mode": "static",
                "model_target_method": "forward", "max_expected_profit_percent": 100.0}
    expert._gather_lookback_days = settings["lookback_days"]
    expert._gather_expected_profit_mode = settings["expected_profit_mode"]
    return Case(expert, settings, [], FakeMarketAnalysis(analysis_id, "NVDA"),
                validate=expert._require_current_price, expects_error=malformed_rows)


def scorer_case(analysis_id: str = SCORER_ID, bars: int = 400) -> Case:
    from ba2_experts.DeterministicScorer import DeterministicScorer, data

    frame = _scorer_frame(bars)
    expert = DeterministicScorer.__new__(DeterministicScorer)
    expert.id = 14
    expert.logger = logging.getLogger("replayfixture.DeterministicScorer")
    expert._get_fmp_api_key = lambda: None
    settings = {"w_technical": 1.0, "w_fundamental": 0.0, "w_analyst": 0.5, "w_macro": 0.0,
                "w_earnings": 0.0, "macro_mode": "off", "min_history_days": 260,
                "index_symbol": "SPY", "use_model_target": False,
                "theta_buy": 0.2, "theta_sell": -0.2}
    expert._live_providers = lambda: LiveProviderBundle(_resolver({}))
    expert._gather_w_analyst = settings["w_analyst"]
    expert._gather_w_earnings = settings["w_earnings"]
    expert._gather_index_symbol = settings["index_symbol"]
    expert._gather_use_model_target = settings["use_model_target"]
    patches = [
        mock.patch.object(data, "fetch_ohlcv",
                          lambda providers, symbol, as_of, lookback_days=None: frame.copy()),
        mock.patch.object(data, "fetch_statements",
                          lambda providers, symbol, as_of, lookback_periods=6: {
                              "income": [], "balance": [], "cashflow": []}),
        mock.patch.object(data, "fetch_past_earnings",
                          lambda providers, symbol, as_of, lookback_periods=16: []),
        mock.patch.object(data, "fetch_macro_series",
                          lambda providers, as_of: {"vix": None, "unrate_series": None,
                                                    "spread_10y3m_series": None,
                                                    "oas_series": None}),
        mock.patch.object(data, "fetch_index_closes",
                          lambda providers, as_of, index_symbol="SPY": frame["Close"].copy()),
    ]
    return Case(expert, settings, patches, FakeMarketAnalysis(analysis_id, "GOOG"))


def skip_case(analysis_id: str = SKIP_ID) -> Case:
    """FMPRating with no analyst coverage -> the live SKIPPED outcome."""
    case = rating_case(analysis_id, consensus_rows=[])
    case.market_analysis.symbol = "TSLA"
    return case


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
)


# --------------------------------------------------------------------------- #
# Building and exporting the session
# --------------------------------------------------------------------------- #
def capture_session(store_root, export_dir) -> Path:
    """Record all six analyses into ``store_root`` and export them to ``export_dir``."""
    store = ReplayStore(store_root, writer="sync")
    store.begin_session(SessionRecord(
        session_id=SESSION_ID, instance_id="replay-test-instance",
        started_at=datetime.now(timezone.utc), exchange_tz="America/New_York",
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


def rebuild_bundle_object(bundle_dir, analysis_id: str, mutate) -> str:
    """Rewrite one analysis's captured bundle through ``mutate``; return the new hash.

    Editing the object file in place would only produce a hash mismatch (the name
    IS the content), so the mutated value is re-encoded, published as a NEW object
    and the manifest re-pointed at it -- which is exactly what a tampered-with
    bundle that still loads would look like.
    """
    root = Path(bundle_dir)
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    store = ObjectStore(root)
    entry = next(a for a in manifest["analyses"] if a["analysis_id"] == analysis_id)
    kind, data, meta = store.get(entry["bundle_object"])
    from ba2_common.core.replay.codec import decode

    value = decode(kind, data, meta, frames=store.get)
    mutate(value)
    encoded = encode(value)
    known = {item["hash"] for item in manifest["objects"]}
    for side in encoded.sides:
        side_hash = store.put(side.kind, side.data)
        if side_hash not in known:
            manifest["objects"].append({"hash": side_hash, "kind": side.kind,
                                        "size": len(side.data),
                                        "path": store.relative_path_for(
                                            side_hash, side.kind).as_posix()})
            known.add(side_hash)
    new_hash = store.put(encoded.kind, encoded.data)
    if new_hash not in known:
        manifest["objects"].append({"hash": new_hash, "kind": encoded.kind,
                                    "size": len(encoded.data),
                                    "path": store.relative_path_for(
                                        new_hash, encoded.kind).as_posix()})
    entry["bundle_object"] = new_hash
    (root / "manifest.json").write_text(
        json.dumps(manifest, indent=2, allow_nan=False, ensure_ascii=False),
        encoding="utf-8")
    return new_hash


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


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _days_ago_iso(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()


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
