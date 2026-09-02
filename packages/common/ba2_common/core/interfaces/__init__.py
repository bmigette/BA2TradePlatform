"""
ba2_common — Core Interfaces Module

This module contains all interface base classes plus the langchain-free
LLM-service seam for the BA2 Trade packages:
- Account interfaces for broker integrations
- Market expert interfaces for AI trading strategies
- Data provider interfaces for market data, fundamentals, news, etc.
- LLMServiceInterface seam (the host injects a ModelFactory-backed impl)

Import all interfaces from this module for easy access:
    from ba2_common.core.interfaces import (
        AccountInterface,
        MarketExpertInterface,
        DataProviderInterface,
        MarketIndicatorsInterface,
        LLMServiceInterface,
        # ... etc
    )
"""

# Core platform interfaces
from .ReadOnlyAccountInterface import ReadOnlyAccountInterface
from .AccountInterface import AccountInterface
from .OptionsAccountInterface import OptionsAccountInterface
from .OptionsDataProviderInterface import (
    OptionsDataProviderInterface, OptionContractMeta, OptionEodBar, CandleBatch,
)
from .MarketExpertInterface import MarketExpertInterface, BacktestInterface
from .ExpertDataExportInterface import (
    ExpertDataExportInterface, ExpertMetric, ExpertDataExport,
    MetricDetailLayout, plan_metric_detail,
    DETAIL_INLINE_MAX_CHARS, DETAIL_TOOLTIP_STYLE,
)
from .ExtendableSettingsInterface import ExtendableSettingsInterface
from .SmartRiskExpertInterface import SmartRiskExpertInterface
from .LiveExpertInterface import LiveExpertInterface

# Data provider interfaces
from .DataProviderInterface import DataProviderInterface
from .MarketIndicatorsInterface import MarketIndicatorsInterface
from .CompanyFundamentalsOverviewInterface import CompanyFundamentalsOverviewInterface
from .CompanyFundamentalsDetailsInterface import CompanyFundamentalsDetailsInterface
from .MarketNewsInterface import MarketNewsInterface
from .MacroEconomicsInterface import MacroEconomicsInterface
from .CompanyInsiderInterface import CompanyInsiderInterface
from .MarketDataProviderInterface import MarketDataProviderInterface
from .SocialMediaDataProviderInterface import SocialMediaDataProviderInterface
from .ScreenerProviderInterface import ScreenerProviderInterface
from .RiskStatsInterface import RiskStatsInterface
from .ValuationSnapshotInterface import ValuationSnapshotInterface

# LLM-service seam (Task 4) — langchain-free; host injects the concrete service
from .LLMServiceInterface import (
    LLMServiceInterface,
    LLMServiceNotConfigured,
    set_llm_service,
    get_llm_service,
)


__all__ = [
    # Core platform interfaces
    "ReadOnlyAccountInterface",
    "AccountInterface",
    "OptionsAccountInterface",
    "OptionsDataProviderInterface",
    "OptionContractMeta",
    "OptionEodBar",
    "CandleBatch",
    "MarketExpertInterface",
    "BacktestInterface",
    "ExpertDataExportInterface",
    "ExpertMetric",
    "ExpertDataExport",
    "MetricDetailLayout",
    "plan_metric_detail",
    "DETAIL_INLINE_MAX_CHARS",
    "DETAIL_TOOLTIP_STYLE",
    "ExtendableSettingsInterface",
    "SmartRiskExpertInterface",
    "LiveExpertInterface",

    # Data provider interfaces
    "DataProviderInterface",
    "MarketIndicatorsInterface",
    "CompanyFundamentalsOverviewInterface",
    "CompanyFundamentalsDetailsInterface",
    "MarketNewsInterface",
    "MacroEconomicsInterface",
    "CompanyInsiderInterface",
    "MarketDataProviderInterface",
    "SocialMediaDataProviderInterface",
    "ScreenerProviderInterface",
    "RiskStatsInterface",
    "ValuationSnapshotInterface",

    # LLM-service seam
    "LLMServiceInterface",
    "LLMServiceNotConfigured",
    "set_llm_service",
    "get_llm_service",
]
