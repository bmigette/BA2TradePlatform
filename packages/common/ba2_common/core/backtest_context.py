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

    def ohlcv(self): return self._get("ohlcv", "fmp")
    def fundamentals_details(self): return self._get("fundamentals_details", "fmp")
    def fundamentals_overview(self): return self._get("fundamentals_overview", "fmp")
    def insider(self): return self._get("insider", "fmp")
    def news(self): return self._get("news", "fmp")

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
