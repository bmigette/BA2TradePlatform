"""Interface for deterministic risk-statistics compute providers.

A risk-stats provider computes (never fetches) risk analytics for a symbol from
OHLCV history: descriptive return stats, annualized realized volatility, max
drawdown, VaR, and benchmark-relative beta/correlation/regression. Output follows
the standard format_type contract (markdown / dict / both).
"""

from abc import abstractmethod
from typing import Annotated, Any, Dict, Literal, Optional
from datetime import datetime

from ba2_common.core.interfaces.DataProviderInterface import DataProviderInterface


class RiskStatsInterface(DataProviderInterface):
    """Interface for risk-statistics compute providers."""

    @abstractmethod
    def __init__(self, ohlcv_provider: DataProviderInterface, benchmark_symbol: str = "SPY"):
        """Args:
        ohlcv_provider: provider implementing get_ohlcv_data (composition, like
                        MarketIndicatorsInterface — the provider never fetches itself).
        benchmark_symbol: ticker used for beta/correlation (default SPY).
        """

    @abstractmethod
    def get_risk_stats(
        self,
        symbol: Annotated[str, "Stock ticker symbol"],
        end_date: Annotated[datetime, "Analysis date — nothing after this date may be read"],
        lookback_days: Annotated[int, "Calendar days of history to compute over"] = 365,
        format_type: Literal["markdown", "dict", "both"] = "markdown",
    ) -> Dict[str, Any] | str:
        """Compute the risk-statistics report for symbol as of end_date."""
