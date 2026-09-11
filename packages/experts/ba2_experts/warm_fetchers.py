"""THE namespace -> fetch table, and the warm fetcher built on it (spec step 4).

Every warmable artifact is warmed by calling the SAME data-layer function the expert
calls -- never a hand-rolled re-implementation, or the warmed surface drifts away
from the read surface and a hermetic run dies on a cache miss the prewarm said it had
covered. ``prewarm_fetchers`` learned that the expensive way for its per-EXPERT
table; this module is the per-NAMESPACE table, and the two are now one:
``prewarm_fetchers.PrewarmFetchers`` composes an instance of this and its per-expert
methods are lists of namespace names. There is no second copy of the calls.

**Why here and not in the test platform.** The declaration
(``ba2_experts.replay_dependencies``) and the fetch that satisfies it have to move
together -- a namespace renamed in one and not the other is a silent gap -- so they
sit in the same package, which both the live host and the test platform import.

**Per-namespace depth.** The disk cache is keyed ``(namespace, symbol)`` with no
depth, and the provider asks FMP for a fixed, caller-independent limit -- so the file
is the same whatever ``lookback_periods`` a caller passes. The constants below still
mirror the DEEPEST reader, so the warm makes the same call the deepest reader makes.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Dict, Optional

from ba2_common.core.replay.dependencies import (
    KIND_HISTORY,
    KIND_INDICATOR,
    KIND_SERIES,
    KIND_TIMESERIES,
    Requirement,
)

# DEPTH IS A READ-SIDE PARAMETER HERE, NOT A CACHE-SIDE ONE -- verified, because the
# opposite belief is what the first version of this file wrote down.
#
# ``FMPCompanyDetailsProvider`` requests a FIXED, caller-independent limit for every one
# of these namespaces (``STATEMENT_HISTORY_DEPTH`` for the three statements,
# ``_PAST_EARNINGS_FETCH_LIMIT`` for the earnings calendar, ``limit=20`` for the
# estimates) and trims to ``lookback_periods`` in Python AFTER the fetch. So the file on
# disk is the same whatever depth is passed, and a shallow warm cannot truncate it.
#
# These constants therefore exist to keep the warm call IDENTICAL to the deepest reader's
# call -- so the warm exercises the same code path, and the payload it returns (for logs
# and for a caller that inspects it) is the one the deepest reader will see -- not to
# control what is cached.
#
#: ``DeterministicScorer.data.fetch_past_earnings`` reads 16 quarters (four years, for
#: the PEAD surprise standardization); ``analyst_target_model.fetch_estimator_inputs``
#: reads 4 and ``FMPEarningsDrift`` 8.
PAST_EARNINGS_PERIODS = 16
#: ``DeterministicScorer.data.fetch_statements`` reads 6 annual periods; FactorRanker
#: reads 1.
STATEMENT_PERIODS = 6
#: ``fetch_estimator_inputs`` reads 2 forward periods; nothing reads more. "quarterly"
#: is the cache NAMESPACE spelling, not a request for quarterly rows -- the endpoint
#: always returns annual ones.
ESTIMATE_PERIODS = 2


class WarmFetchError(RuntimeError):
    """This requirement cannot be fetched.

    A configuration gap, not a data gap: an unknown namespace, a missing API key, or
    a kind nothing can download (platform state). Raised rather than skipped so a plan
    that declares something unwarmable is visible instead of quietly never completing.
    """


@dataclass(frozen=True)
class NamespaceRequest:
    """What a namespace fetcher needs: who, when, and (where bounded) how far back."""

    namespace: str
    symbol: str
    end_date: datetime
    fmp_key: Optional[str]
    lookback_days: Optional[int] = None


class NamespaceFetchers:
    """The one table, with its provider instances cached across symbols.

    Construction is cheap: the providers are built on first use, so warming two
    namespaces costs one provider between them. Thread-safe enough for the warm queue
    and the prewarm pool: the providers hold an API key and nothing else, and every
    read they do goes through the shared disk cache.
    """

    def __init__(self) -> None:
        self._details = None
        self._insider = None
        self._lock = threading.Lock()

    # -- cached providers -------------------------------------------------- #
    def details(self):
        """The shared ``FMPCompanyDetailsProvider`` (statements / earnings / estimates)."""
        if self._details is None:
            with self._lock:
                if self._details is None:
                    from ba2_providers.fundamentals.details.FMPCompanyDetailsProvider import (
                        FMPCompanyDetailsProvider,
                    )
                    self._details = FMPCompanyDetailsProvider()
        return self._details

    def insider(self):
        if self._insider is None:
            with self._lock:
                if self._insider is None:
                    from ba2_providers.insider.FMPInsiderProvider import FMPInsiderProvider
                    self._insider = FMPInsiderProvider()
        return self._insider

    # -- the table --------------------------------------------------------- #
    @property
    def table(self) -> Dict[str, Callable[[NamespaceRequest], Any]]:
        """``fmp_history`` namespace -> the data-layer call that warms it."""
        return {
            "price_target": self._price_target,
            "grades_historical": self._grades_historical,
            "analyst_grades": self._analyst_grades,
            "income_statement_annual": self._income_statement,
            "balance_sheet_annual": self._balance_sheet,
            "cashflow_statement_annual": self._cashflow_statement,
            "past_earnings_quarterly": self._past_earnings,
            "earnings_estimates_quarterly": self._earnings_estimates,
            "insider_v2": self._insider_transactions,
            "finnhub_reco_trends": self._finnhub_reco_trends,
        }

    def namespaces(self) -> tuple:
        return tuple(sorted(self.table))

    def fetch(self, request: NamespaceRequest) -> Any:
        """Warm ONE namespace for ONE symbol. Raises :class:`WarmFetchError` if it cannot."""
        fetcher = self.table.get(request.namespace)
        if fetcher is None:
            raise WarmFetchError(
                f"no warm fetcher for the fmp_history namespace {request.namespace!r}; "
                f"known: {', '.join(self.namespaces())}")
        return fetcher(request)

    # -- fetchers ---------------------------------------------------------- #
    @staticmethod
    def _require_key(request: NamespaceRequest) -> str:
        if not request.fmp_key:
            raise WarmFetchError(
                f"FMP_API_KEY is not configured; {request.namespace} for {request.symbol} "
                f"cannot be warmed")
        return request.fmp_key

    def _price_target(self, request: NamespaceRequest):
        from ba2_experts.FMPRating import fetch_price_target_history_cached
        return fetch_price_target_history_cached(self._require_key(request), request.symbol)

    def _grades_historical(self, request: NamespaceRequest):
        from ba2_experts.FMPRating import fetch_grades_historical_cached
        return fetch_grades_historical_cached(self._require_key(request), request.symbol)

    def _analyst_grades(self, request: NamespaceRequest):
        from ba2_experts.FMPRating import fetch_analyst_grades_cached
        return fetch_analyst_grades_cached(self._require_key(request), request.symbol)

    def _statement(self, request: NamespaceRequest, getter_name: str):
        return getattr(self.details(), getter_name)(
            symbol=request.symbol, frequency="annual", end_date=request.end_date,
            lookback_periods=STATEMENT_PERIODS, as_of=request.end_date, format_type="dict")

    def _income_statement(self, request: NamespaceRequest):
        return self._statement(request, "get_income_statement")

    def _balance_sheet(self, request: NamespaceRequest):
        return self._statement(request, "get_balance_sheet")

    def _cashflow_statement(self, request: NamespaceRequest):
        return self._statement(request, "get_cashflow_statement")

    def _past_earnings(self, request: NamespaceRequest):
        return self.details().get_past_earnings(
            symbol=request.symbol, frequency="quarterly", end_date=request.end_date,
            lookback_periods=PAST_EARNINGS_PERIODS, format_type="dict")

    def _earnings_estimates(self, request: NamespaceRequest):
        return self.details().get_earnings_estimates(
            symbol=request.symbol, frequency="quarterly", as_of_date=request.end_date,
            lookback_periods=ESTIMATE_PERIODS, format_type="dict")

    def _insider_transactions(self, request: NamespaceRequest):
        if request.lookback_days is None:
            raise WarmFetchError(
                f"the insider history for {request.symbol} needs a bounded lookback; the "
                f"requirement carries none")
        return self.insider().get_insider_transactions(
            request.symbol, end_date=request.end_date, lookback_days=request.lookback_days,
            as_of=request.end_date, format_type="dict")

    def _finnhub_reco_trends(self, request: NamespaceRequest):
        raise WarmFetchError(
            "finnhub_reco_trends is warmed by ba2-test prewarm (it needs the Finnhub key, "
            "which this fetcher is not given); FinnHubRating is not a replay-adapted expert")


#: The estimator-model namespaces, in the order ``analyst_target_model.
#: fetch_estimator_inputs`` reads them. Named here so ``prewarm_fetchers``'
#: ``_warm_estimator_inputs`` and the replay dependency adapter cannot disagree about
#: which two namespaces "model mode" means.
ESTIMATOR_NAMESPACES = ("past_earnings_quarterly", "earnings_estimates_quarterly")


class DefaultWarmFetcher:
    """Fetch one :class:`Requirement` through the namespace table or the provider stack.

    Every input is stated by the caller -- keys, the reference date, and the OHLCV
    provider of the INDICATOR stack (which OHLCV source backs the host's indicator
    provider is host wiring and cannot be inferred here).

    A ``timeseries`` requirement is fetched through the provider IT NAMES, not through
    the indicator stack's. Getting that wrong is not a cosmetic mismatch: the planner
    resolves a ``fmp`` price requirement against ``FMPOHLCVProvider``'s directory, so a
    fetch that wrote ``YFinanceDataProvider``'s would leave the requirement ``missing``
    on every re-plan and re-download it forever.
    """

    def __init__(self, *, indicator_ohlcv_provider: str, end_date: datetime,
                 fmp_key: Optional[str], fred_key: Optional[str],
                 namespace_fetchers: Optional[NamespaceFetchers] = None) -> None:
        self.indicator_ohlcv_provider = indicator_ohlcv_provider
        self.end_date = end_date
        self.fmp_key = fmp_key
        self.fred_key = fred_key
        self._namespaces = namespace_fetchers or NamespaceFetchers()

    def __call__(self, requirement: Requirement) -> None:
        if requirement.kind == KIND_HISTORY:
            return self._fetch_history(requirement)
        if requirement.kind == KIND_SERIES:
            return self._fetch_series(requirement)
        if requirement.kind in (KIND_TIMESERIES, KIND_INDICATOR):
            return self._fetch_timeseries(requirement)
        raise WarmFetchError(
            f"nothing can download a {requirement.kind!r} requirement "
            f"({requirement.namespace}); the plan reports it instead")

    def _fetch_history(self, requirement: Requirement) -> None:
        window = requirement.window
        lookback_days = None
        if window is not None and window.start is not None:
            lookback_days = max(1, (window.end - window.start).days)
        self._namespaces.fetch(NamespaceRequest(
            namespace=requirement.namespace, symbol=requirement.symbol,
            end_date=self.end_date, fmp_key=self.fmp_key, lookback_days=lookback_days))

    def _fetch_series(self, requirement: Requirement) -> None:
        from ba2_providers.macro import fred_series

        if not self.fred_key:
            raise WarmFetchError(
                "fred_api_key is not configured; the macro series cannot be warmed")
        fred_series.refresh_series(requirement.namespace, self.fred_key)

    def _fetch_timeseries(self, requirement: Requirement) -> None:
        """Read the series through its provider, which fills that provider's parquet.

        An INDICATOR requirement has no artifact of its own -- an ATR is computed from
        these bars -- so warming the underlying series is the whole of the work. It
        names the registry CATEGORY rather than a provider, so that one (and only that
        one) uses the host's configured indicator OHLCV provider.
        """
        from ba2_providers import get_provider

        window = requirement.window
        if window is None or window.start is None:
            raise WarmFetchError(
                f"the price series for {requirement.symbol} needs a bounded window; the "
                f"requirement carries none")
        name = (self.indicator_ohlcv_provider if requirement.kind == KIND_INDICATOR
                else requirement.provider)
        provider = get_provider("ohlcv", name)
        provider.get_ohlcv_data(symbol=requirement.symbol, start_date=window.start,
                                end_date=window.end, interval=requirement.interval)


__all__ = [
    "DefaultWarmFetcher",
    "ESTIMATE_PERIODS",
    "ESTIMATOR_NAMESPACES",
    "NamespaceFetchers",
    "NamespaceRequest",
    "PAST_EARNINGS_PERIODS",
    "STATEMENT_PERIODS",
    "WarmFetchError",
]
