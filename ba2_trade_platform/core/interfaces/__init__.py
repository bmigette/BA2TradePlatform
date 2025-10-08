"""
BA2 Trade Platform - Core Interfaces Module

This module contains all interface definitions for the BA2 Trade Platform:
- Account interfaces for broker integrations
- Market expert interfaces for AI trading strategies
- Data provider interfaces for market data, fundamentals, news, etc.

Import all interfaces from this module for easy access:
    from ba2_trade_platform.core.interfaces import (
        AccountInterface,
        MarketExpertInterface,
        DataProviderInterface,
        MarketIndicatorsInterface,
        # ... etc
    )
"""

# Core platform interfaces
from .AccountInterface import AccountInterface
from .MarketExpertInterface import MarketExpertInterface
from .ExtendableSettingsInterface import ExtendableSettingsInterface

# Data provider interfaces
from .DataProviderInterface import DataProviderInterface
from .MarketIndicatorsInterface import MarketIndicatorsInterface
from .CompanyFundamentalsOverviewInterface import CompanyFundamentalsOverviewInterface
from .CompanyFundamentalsDetailsInterface import CompanyFundamentalsDetailsInterface
from .MarketNewsInterface import MarketNewsInterface
from .MacroEconomicsInterface import MacroEconomicsInterface
from .CompanyInsiderInterface import CompanyInsiderInterface
from .MarketDataProviderInterface import MarketDataProvider


__all__ = [
    # Core platform interfaces
    "AccountInterface",
    "MarketExpertInterface",
    "ExtendableSettingsInterface",
    
    # Data provider interfaces
    "DataProviderInterface",
    "MarketIndicatorsInterface",
    "CompanyFundamentalsOverviewInterface",
    "CompanyFundamentalsDetailsInterface",
    "MarketNewsInterface",
    "MacroEconomicsInterface",
    "CompanyInsiderInterface",
    "MarketDataProvider",
]
