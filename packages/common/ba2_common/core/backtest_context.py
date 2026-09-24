"""ProviderBundle + BacktestContext — the injected accessors experts use in _gather.

Phase 1 ships only LiveProviderBundle (wraps the live get_provider registry) so
analyze_as_of(now) works through the real providers. The backtest-cache-backed
bundle (pointing at the parquet/SQLite as_of cache + a separate backtest DB) is
built in Phase 4; this module defines the protocol it must satisfy.

NOTE (replan reconciliation): the ba2_providers.get_provider registry has NO
"congress" category — Senate/House trades are fetched by the Senate experts via
their own FMP-http helpers (_fetch_senate_trades/_fetch_house_trades), so this
bundle does NOT expose a congress() accessor. The "indicators" "pandas" provider
(PandasIndicatorCalc) REQUIRES an OHLCV provider in its constructor, so
indicators() constructs it with the bundle's OHLCV provider rather than calling
get_provider("indicators", "pandas") with no args (which would raise TypeError).

Pure value-object module: imports NO provider/DB module at load time (get_provider
is injected as a callable), so importing ba2_common.core.backtest_context pulls
neither ba2_providers nor a DB engine.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, Optional, Protocol, runtime_checkable


@runtime_checkable
class ProviderBundle(Protocol):
    """Typed accessor over the provider set an expert needs. Methods return the
    SAME provider objects the live registry returns, so _gather is provider-agnostic."""
    def ohlcv(self) -> Any: ...
    def fundamentals_details(self) -> Any: ...
    def fundamentals_overview(self) -> Any: ...
    def insider(self) -> Any: ...
    def news(self) -> Any: ...
    def indicators(self) -> Any: ...
    def price_at_date(self, symbol: str, as_of: Optional[datetime]) -> Optional[float]: ...


class LiveProviderBundle:
    """Live bundle: resolves providers via an injected get_provider callable.

    The host (or the test harness) passes get_provider so ba2_common keeps no
    edge to ba2_providers. price_at_date resolves the as_of close via the ohlcv
    provider's get_ohlcv_data (Decision 1: one price source for all experts)."""

    def __init__(self, get_provider: Callable[..., Any]):
        self._get = get_provider
        self._memo: Dict[str, Any] = {}

    def _once(self, category: str, name: str):
        """Resolve ``(category, name)`` once PER BUNDLE and reuse that instance.

        WHY. The registry accessors below are called inside every expert's
        ``_gather``, i.e. once per (symbol, decision). ``get_provider`` builds a
        NEW object every time, and these providers' constructors are not free --
        ``FMPCompanyDetailsProvider.__init__`` reads ``FMP_API_KEY`` out of the
        AppSetting table. Measured on a real 10-symbol / 501-bar
        DeterministicScorer backtest: 10,020 constructions, 4.6 s, for what is a
        stateless api-key holder.

        WHY HERE, and not in ``get_provider``. ``get_provider`` is the registry's
        factory; other callers pass constructor kwargs and some genuinely want a
        distinct instance, so caching there would change resolution for everyone.
        A BUNDLE, by contrast, already has exactly the right lifetime: live builds
        a fresh one per ``run_analysis`` (``MarketExpertInterface._live_providers``),
        a backtest builds one per run. So a memo here can never outlive the scope
        the bundle was made for, and live cannot be served a provider built for an
        earlier analysis.

        ``ohlcv`` deliberately does NOT go through this. The backtest host's
        resolver returns a per-run OHLCV OVERRIDE that it may install or clear at
        any point in the run (``seam_wiring._current_ohlcv_override``), so the
        ohlcv provider must stay a live lookup -- and it costs nothing, because
        the override is returned rather than constructed. ``indicators`` is built
        FROM it and is left alone for the same reason.
        """
        key = f"{category}/{name}"
        provider = self._memo.get(key)
        if provider is None:
            provider = self._get(category, name)
            self._memo[key] = provider
        return provider

    def ohlcv(self): return self._get("ohlcv", "fmp")
    def fundamentals_details(self): return self._once("fundamentals_details", "fmp")
    def fundamentals_overview(self): return self._once("fundamentals_overview", "fmp")
    def insider(self): return self._once("insider", "fmp")
    def news(self): return self._once("news", "fmp")

    def indicators(self):
        # PandasIndicatorCalc requires an OHLCV provider in its constructor; pass
        # this bundle's OHLCV provider (replan reconciliation).
        return self._get("indicators", "pandas", ohlcv_provider=self.ohlcv())

    def price_at_date(self, symbol: str, as_of: Optional[datetime]) -> Optional[float]:
        prov = self.ohlcv()
        df = prov.get_ohlcv_data(symbol, end_date=as_of, lookback_days=7, interval="1d")
        if df is None or getattr(df, "empty", True):
            return None
        return float(df["Close"].iloc[-1])


@dataclass
class BacktestContext:
    """Carries everything analyze_as_of needs, set from OUTSIDE the expert."""
    providers: ProviderBundle
    settings: Dict[str, Any]                    # resolved + optimizer-overridden per trial
    as_of: Optional[datetime] = None
    account: Any = None                         # BacktestAccount (Phase 4); None in golden test
    subtype: Any = None                         # AnalysisUseCase for subtype-aware experts
    extra: Dict[str, Any] = field(default_factory=dict)
