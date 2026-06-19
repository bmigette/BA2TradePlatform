"""Phase 1 Task 12 — golden-test fixtures (deterministic fakes per expert).

This module supplies, for every backtestable expert, the *exact same* deterministic
provider/fetcher fakes + resolved settings the per-expert Task 5-10 tests use, so the
golden test in ``test_golden_live_vs_asof.py`` can prove, for ALL experts at once:

    rec_live = _process(_gather(live, as_of=None), settings)         # the live path
    rec_asof = expert.analyze_as_of(now, BacktestContext(...))       # the backtest path
    rec_live.almost_equals(rec_asof)   # on (signal, confidence, expected_profit,
                                       #     details, skip, skip_reason), current_price pinned

Design notes
------------
* The fakes are TIME-INVARIANT (a fixed earnings row, fixed trends, fixed consensus),
  so the only difference exercised between as_of=NOW and as_of=None is the ``as_of``
  *plumbing* — exactly what the gate must prove.
* ``FakeOHLCV`` returns a constant close so the backtest (as_of set) ``current_price``
  is pinned. The LIVE (as_of=None) path now reads the account/broker quote via
  ``_get_current_price`` (the original live source, restored after Phase 1 wrongly
  routed it through the OHLCV close); each builder therefore STUBS
  ``expert._get_current_price`` to return the SAME pinned price the as_of FakeOHLCV
  close yields, so the golden equality stays meaningful (and the live path never hits
  the loud ``_StubInstanceResolver``). The harness ALSO re-pins
  ``rec_asof.current_price = rec_live.current_price`` so a price-source diff can never
  mask logic drift.
* The FinnHubRating fixture deliberately keeps its newest trends period ON/BEFORE NOW
  so the as_of pick (latest period <= as_of) equals the live trends[0] pick (the only
  way live==as_of for the period-selection lookahead fix — see Task 8).
* FactorRanker resolves its factor data through the ``ba2_experts.FactorRanker.data``
  module fetchers (NOT the provider bundle), so its fixture patches those fetchers via
  the ``patch`` hook returned by the factory rather than the provider resolver.
* Senate experts keep their own FMP-http fetchers (NOT in the get_provider registry);
  the fixtures stub those fetchers and route only the OHLCV price through the bundle.

Every expert is built via ``__new__`` (bypassing ``__init__``'s DB read /
_load_expert_instance / API-key fetch), mirroring the per-expert Task 5-10 ``_expert()``
helpers, and the gather-time attrs each multi-stage expert needs are set directly.
"""
from __future__ import annotations

import importlib
import logging
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple
from unittest import mock

import pandas as pd

from ba2_common.core.types import AnalysisUseCase

_LOG = logging.getLogger("golden_fixtures")

NOW = datetime(2026, 6, 13, tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# Clock freeze for the live (as_of=None) path — the ONLY thing that makes the
# golden test date-INDEPENDENT.
#
# Several experts compute their live decision via ``now = as_of or
# datetime.now(timezone.utc)`` (e.g. FMPEarningsDrift "days_since_report",
# the Senate experts' trade-age math). On the as_of path that ``now`` is the
# pinned NOW; on the live path it is the wall clock. When the wall clock drifts
# past NOW the two paths diverge and the gate fails on the run date alone.
#
# The fix (TEST-ONLY — production decision logic is untouched): during the live
# ``_gather``/``_process`` call we replace the module-level ``datetime`` symbol
# in every expert module the golden cases touch with a subclass whose ``now()``
# returns the pinned NOW. So ``datetime.now(timezone.utc)`` on the live path
# yields EXACTLY the same instant analyze_as_of() is handed, and both branches
# share one clock regardless of the calendar date. Everything else about
# ``datetime`` (strptime, arithmetic, tz) is inherited unchanged.
# --------------------------------------------------------------------------- #
class _FrozenDateTime(datetime):
    """A ``datetime`` whose ``now()``/``utcnow()`` return the pinned NOW.

    Subclassing ``datetime`` keeps strptime/replace/arithmetic identical to the
    real class; only the wall-clock readers are pinned."""

    @classmethod
    def now(cls, tz=None):  # noqa: D401 - mirror datetime.now signature
        return NOW.astimezone(tz) if tz is not None else NOW.replace(tzinfo=None)

    @classmethod
    def utcnow(cls):
        return NOW.replace(tzinfo=None)


# Expert modules whose live decision path reads the wall clock (or might in
# future). Patching the module-level ``datetime`` name they each bound via
# ``from datetime import datetime`` is enough; missing/edge modules are skipped.
_CLOCK_BOUND_MODULES = (
    "ba2_experts.FMPEarningsDrift",
    "ba2_experts.FMPInsiderClusterBuy",
    "ba2_experts.FinnHubRating",
    "ba2_experts.FMPSenateTraderCopy",
    "ba2_experts.FMPSenateTraderWeight",
    "ba2_experts.FMPRating",
    "ba2_experts.FactorRanker.data",
    "ba2_experts.FactorRanker",
)


@contextmanager
def freeze_now_for_live_path():
    """Pin ``datetime.now()`` to NOW in every clock-reading expert module.

    Wrap ONLY the live (as_of=None) ``_gather``/``_process`` call with this so
    the live branch reads the same instant the as_of branch is given. The as_of
    branch never falls back to ``datetime.now`` (as_of is always set), so it is
    unaffected whether or not it runs inside the freeze."""
    with ExitStack() as stack:
        for mod_name in _CLOCK_BOUND_MODULES:
            try:
                module = importlib.import_module(mod_name)
            except Exception:  # pragma: no cover - module not importable here
                continue
            if not hasattr(module, "datetime"):
                continue
            stack.enter_context(mock.patch.object(module, "datetime", _FrozenDateTime))
        yield


# --------------------------------------------------------------------------- #
# Shared deterministic OHLCV fake (constant close -> pinned current_price)
# --------------------------------------------------------------------------- #
class FakeOHLCV:
    """Constant-close OHLCV provider. ``price_map`` lets the Senate basket expert
    mark some symbols unsupported (None close); a scalar pins one close for all."""

    def __init__(self, close: float = 100.0, price_map: Optional[Dict[str, Optional[float]]] = None):
        self._close = close
        self._price_map = price_map

    def get_ohlcv_data(self, symbol, end_date=None, lookback_days=7, interval="1d"):
        if self._price_map is not None:
            price = self._price_map.get(str(symbol).upper())
            if price is None:
                return pd.DataFrame({"Close": []})
            return pd.DataFrame({"Close": [price]})
        return pd.DataFrame({"Close": [self._close]})


def _resolver(mapping: Dict[str, Any]) -> Callable[..., Any]:
    """A get_provider(category, name, **kw) callable backed by a category->provider map."""
    def get_provider(category, name, **kw):
        return mapping[category]
    return get_provider


@contextmanager
def _noop():
    yield


# --------------------------------------------------------------------------- #
# Per-expert fixture builders. Each returns:
#   (expert, settings, get_provider, opts)
# where opts = {"subtype": <AnalysisUseCase|None>, "patch": <ctxmgr factory|None>,
#               "live_settings": <dict|None for the live _process call>}.
# ``patch`` is a no-arg callable returning a context manager that installs any
# module-level fetcher monkeypatches FactorRanker needs (active for BOTH paths).
# --------------------------------------------------------------------------- #
def _build_earnings_drift():
    from ba2_experts.FMPEarningsDrift import FMPEarningsDrift

    class FakeDetails:
        def get_past_earnings(self, symbol, frequency, end_date, lookback_periods,
                              format_type, **kw):
            return {"earnings": [{"report_date": "2026-06-10", "reported_eps": 1.2,
                                  "estimated_eps": 1.0, "surprise_percent": 20.0}]}

    e = FMPEarningsDrift.__new__(FMPEarningsDrift)
    e.id = 1
    e._gather_symbol = "AAPL"
    e._get_current_price = lambda sym: 100.0  # live account quote == as_of FakeOHLCV close
    settings = {"surprise_min_pct": 5.0, "max_days_since_report": 30,
                "expected_profit_percent": 8.0}
    gp = _resolver({"fundamentals_details": FakeDetails(), "ohlcv": FakeOHLCV()})
    return e, settings, gp, {"subtype": None, "patch": None, "live_settings": None}


def _build_insider_cluster():
    from ba2_experts.FMPInsiderClusterBuy import FMPInsiderClusterBuy

    three_buyers = {
        "start_date": "2026-05-14T00:00:00", "end_date": "2026-06-13T00:00:00",
        "transactions": [
            {"insider_name": "A", "transaction_type": "P-Purchase", "value": 100_000},
            {"insider_name": "B", "transaction_type": "P-Purchase", "value": 100_000},
            {"insider_name": "C", "transaction_type": "P-Purchase", "value": 100_000},
        ],
    }

    class FakeInsider:
        def get_insider_transactions(self, symbol, end_date, lookback_days=None,
                                     as_of=None, format_type="dict", **kw):
            return three_buyers

    e = FMPInsiderClusterBuy.__new__(FMPInsiderClusterBuy)
    e.id = 1
    e._gather_symbol = "AAPL"
    e._get_current_price = lambda sym: 100.0  # live account quote == as_of FakeOHLCV close
    e._gather_lookback_days = 30
    settings = {"lookback_days": 30, "min_insiders": 3, "min_total_value": 200_000.0,
                "expected_profit_percent": 10.0}
    gp = _resolver({"insider": FakeInsider(), "ohlcv": FakeOHLCV()})
    return e, settings, gp, {"subtype": None, "patch": None, "live_settings": None}


def _build_finnhub_rating():
    from ba2_experts.FinnHubRating import FinnHubRating

    # Newest period (2026-06-01) is ON/BEFORE NOW so the as_of pick == trends[0] pick.
    trends = [
        {"period": "2026-06-01", "strongBuy": 10, "buy": 0, "hold": 0, "sell": 0, "strongSell": 0},
        {"period": "2026-05-01", "strongBuy": 0, "buy": 0, "hold": 10, "sell": 0, "strongSell": 0},
    ]
    e = FinnHubRating.__new__(FinnHubRating)
    e.id = 1
    e._gather_symbol = "AAPL"
    e._get_current_price = lambda sym: 123.0  # live account quote == as_of FakeOHLCV(close=123.0)
    e._fetch_recommendation_trends = lambda symbol: trends
    settings = {"buy_threshold": 4.5, "overweight_threshold": 3.5,
                "hold_threshold": 2.5, "underweight_threshold": 1.5}
    gp = _resolver({"ohlcv": FakeOHLCV(close=123.0)})
    return e, settings, gp, {"subtype": None, "patch": None, "live_settings": None}


def _copy_trade(first, last, sym, ttype, disclose, exec_date, amount="$15,001 - $50,000"):
    return {"firstName": first, "lastName": last, "symbol": sym, "type": ttype,
            "disclosureDate": disclose, "transactionDate": exec_date, "amount": amount}


def _build_senate_copy(subtype: AnalysisUseCase):
    from ba2_experts.FMPSenateTraderCopy import FMPSenateTraderCopy

    senate = [_copy_trade("Nancy", "Pelosi", "AAPL", "purchase", "2026-06-01", "2026-05-20")]
    e = FMPSenateTraderCopy.__new__(FMPSenateTraderCopy)
    e.id = 1
    e._gather_symbol = "MULTI"
    e.logger = _LOG
    # live account quote per symbol mirrors the as_of price_map (None for unsupported)
    e._get_current_price = lambda sym: {"AAPL": 100.0}.get(str(sym).upper())
    e._fetch_senate_trades = lambda symbol=None: senate
    e._fetch_house_trades = lambda symbol=None: []
    # settings passed to analyze_as_of (which merges context.subtype into _subtype).
    settings = {"copy_trade_names": "Nancy Pelosi", "max_disclose_date_days": 365,
                "max_trade_exec_days": 365}
    # The live _process call needs the subtype baked into settings (analyze_as_of
    # gets it from context.subtype instead).
    live_settings = {**settings, "_subtype": subtype}
    gp = _resolver({"ohlcv": FakeOHLCV(price_map={"AAPL": 100.0})})
    return e, settings, gp, {"subtype": subtype, "patch": None, "live_settings": live_settings}


def _weight_trade(first, last, sym, ttype, disclose, exec_date, amount="$100,001 - $250,000"):
    return {"firstName": first, "lastName": last, "symbol": sym, "type": ttype,
            "disclosureDate": disclose, "transactionDate": exec_date, "amount": amount}


def _build_senate_weight():
    from ba2_experts.FMPSenateTraderWeight import FMPSenateTraderWeight

    trades = [
        _weight_trade("Alice", "Aa", "AAPL", "purchase", "2026-06-01", "2026-05-20"),
        _weight_trade("Bob", "Bb", "AAPL", "purchase", "2026-06-02", "2026-05-21"),
    ]
    history = {
        "Alice Aa": [_weight_trade("Alice", "Aa", "AAPL", "purchase", "2026-05-01", "2026-05-10")],
        "Bob Bb": [_weight_trade("Bob", "Bb", "AAPL", "purchase", "2026-05-02", "2026-05-11")],
    }
    e = FMPSenateTraderWeight.__new__(FMPSenateTraderWeight)
    e.id = 1
    e._gather_symbol = "AAPL"
    e.logger = _LOG
    e._get_current_price = lambda sym: 100.0  # live account quote == as_of price_map AAPL close
    e._fetch_senate_trades = lambda s: trades
    e._fetch_house_trades = lambda s: []
    e._fetch_trader_history = lambda name: history.get(name)
    e._get_price_at_date = lambda sym, date: 95.0  # small move -> confidence not floored
    settings = {"max_disclose_date_days": 365, "max_trade_exec_days": 365,
                "max_trade_price_delta_pct": 1000.0, "growth_confidence_multiplier": 5.0,
                "confidence_to_profit_factor": 0.15, "min_traders": 2, "min_trades": 2}
    gp = _resolver({"ohlcv": FakeOHLCV(price_map={"AAPL": 100.0})})
    return e, settings, gp, {"subtype": None, "patch": None, "live_settings": None}


# FactorRanker value-only fixtures (value ranks AAA>BBB>CCC>DDD, top-2 = AAA,BBB).
_FR_UNIVERSE = ["AAA", "BBB", "CCC", "DDD"]
_FR_VALUE_INPUTS = {
    "AAA": {"eps_ttm": 10.0, "price": 100.0, "fcf_ttm": 0.0, "enterprise_value": 1.0},
    "BBB": {"eps_ttm": 7.0, "price": 100.0, "fcf_ttm": 0.0, "enterprise_value": 1.0},
    "CCC": {"eps_ttm": 4.0, "price": 100.0, "fcf_ttm": 0.0, "enterprise_value": 1.0},
    "DDD": {"eps_ttm": 1.0, "price": 100.0, "fcf_ttm": 0.0, "enterprise_value": 1.0},
}
_FR_SETTINGS = {
    "winsorize_pct": 0.0, "top_n": 2, "weighting": "equal",
    "max_weight_per_name": 1.0, "gross_exposure": 1.0, "pead_drift_window_days": 60,
    "_factor_weights": {"momentum": 0.0, "value": 1.0, "quality": 0.0, "pead": 0.0},
}


def _build_factor_ranker():
    from ba2_experts.FactorRanker import FactorRanker, data

    e = FactorRanker.__new__(FactorRanker)
    e.id = 1
    e.logger = logging.getLogger("golden.FactorRanker")
    e._gather_settings = _FR_SETTINGS
    e._resolve_universe = lambda *a, **k: list(_FR_UNIVERSE)  # accepts as_of
    e._gather_holdings = lambda: []

    def patch():
        """Install the data-module fetcher fakes for BOTH golden paths."""
        import unittest.mock as mock

        def fake_value(symbols, as_of=None):
            return {s: _FR_VALUE_INPUTS[s] for s in symbols if s in _FR_VALUE_INPUTS}

        def fake_quality(symbols, as_of=None):
            return {}

        def fake_pead(symbols, as_of=None):
            return {}

        def fake_prices(symbols, as_of=None, **kw):
            return {s: pd.Series([10.0, 11.0, 12.0]) for s in symbols}

        return mock.patch.multiple(
            data,
            fetch_value_inputs=fake_value,
            fetch_quality_inputs=fake_quality,
            fetch_pead_inputs=fake_pead,
            fetch_close_prices=fake_prices,
        )

    gp = _resolver({"ohlcv": FakeOHLCV()})
    return e, _FR_SETTINGS, gp, {"subtype": None, "patch": patch, "live_settings": None}


def _build_fmp_rating():
    from ba2_experts.FMPRating import FMPRating

    # LIVE snapshots (as_of=None path): FMP's current consensus endpoints.
    consensus = {"targetConsensus": 130.0, "targetHigh": 160.0,
                 "targetLow": 110.0, "targetMedian": 128.0}
    upgrade = [{"strongBuy": 10, "buy": 5, "hold": 3, "sell": 1, "strongSell": 1}]

    # BACKTEST (as_of) reconstruction inputs — dated FMP history that, filtered
    # no-lookahead (date/publishedDate <= NOW) and within the trailing window,
    # reconstructs EXACTLY the live snapshot above so live == analyze_as_of(NOW):
    #   * price-target rows: sorted [110,124,128,128,160] -> mean 130, max 160,
    #     min 110, median 128  == the live consensus dict.
    #   * grades-historical latest row <= NOW -> the live upgrade counts (uses the
    #     verbose analystRatings* field names to exercise the alias mapping).
    # A second row in EACH list is dated AFTER NOW with wildly different values to
    # PROVE no-lookahead: if the as_of filter leaked, the reconstruction (and the
    # decision) would change.
    price_target_history = [
        {"publishedDate": "2026-06-10", "priceTarget": 110.0},
        {"publishedDate": "2026-06-09", "priceTarget": 124.0},
        {"publishedDate": "2026-06-08", "priceTarget": 128.0},
        {"publishedDate": "2026-06-07", "priceTarget": 128.0},
        {"publishedDate": "2026-06-06", "priceTarget": 160.0},
        # FUTURE row (after NOW) — must be ignored (no-lookahead).
        {"publishedDate": "2026-07-01", "priceTarget": 9999.0},
    ]
    grades_history = [
        {"date": "2026-06-10", "analystRatingsStrongBuy": 10, "analystRatingsbuy": 5,
         "analystRatingsHold": 3, "analystRatingsSell": 1, "analystRatingsStrongSell": 1},
        # FUTURE row (after NOW) — must be ignored (no-lookahead).
        {"date": "2026-07-01", "analystRatingsStrongBuy": 0, "analystRatingsbuy": 0,
         "analystRatingsHold": 0, "analystRatingsSell": 50, "analystRatingsStrongSell": 50},
    ]

    e = FMPRating.__new__(FMPRating)
    e.id = 1
    e._gather_symbol = "AAPL"
    e._get_current_price = lambda sym: 100.0  # live account quote == as_of FakeOHLCV close
    # Live snapshot fetchers (as_of=None path).
    e._fetch_price_target_consensus = lambda symbol: consensus
    e._fetch_upgrade_downgrade = lambda symbol: upgrade
    # Dated history fetchers (as_of reconstruction path).
    e._fetch_price_target_history = lambda symbol: price_target_history
    e._fetch_grades_historical = lambda symbol: grades_history
    settings = {"profit_ratio": 1.0, "min_analysts": 10, "target_price_type": "consensus",
                "price_target_window_days": 90}
    gp = _resolver({"ohlcv": FakeOHLCV()})
    return e, settings, gp, {"subtype": None, "patch": None, "live_settings": None}


# Registry: case-name -> builder. The two FMPSenateTraderCopy subtype rows share a
# builder parametrised by subtype.
FIXTURE_BUILDERS: Dict[str, Callable[[], Tuple[Any, Dict[str, Any], Callable, Dict[str, Any]]]] = {
    "FMPEarningsDrift": _build_earnings_drift,
    "FMPInsiderClusterBuy": _build_insider_cluster,
    "FinnHubRating": _build_finnhub_rating,
    "FMPSenateTraderCopy[ENTER_MARKET]": lambda: _build_senate_copy(AnalysisUseCase.ENTER_MARKET),
    "FMPSenateTraderCopy[OPEN_POSITIONS]": lambda: _build_senate_copy(AnalysisUseCase.OPEN_POSITIONS),
    "FMPSenateTraderWeight": _build_senate_weight,
    "FactorRanker": _build_factor_ranker,
    "FMPRating": _build_fmp_rating,
}
