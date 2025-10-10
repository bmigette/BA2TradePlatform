"""
BA2 Trade Platform - Data Providers Module

This module contains all data provider implementations for the BA2 Trade Platform.
Providers are organized by data type (indicators, fundamentals, news, macro, insider).

Provider Registry:
    Each subdirectory contains a registry mapping provider names to provider classes.
    Use the get_provider() function to instantiate providers dynamically.

Usage:
    from ba2_trade_platform.modules.dataproviders import get_provider
    
    # Get a news provider
    alpaca_news = get_provider("news", "alpaca")
    news = alpaca_news.get_company_news("AAPL", end_date=datetime.now(), lookback_days=7)
    
    # Get an indicators provider (requires OHLCV provider)
    from ba2_trade_platform.modules.dataproviders import YFinanceDataProvider
    ohlcv_provider = YFinanceDataProvider()
    pandas_indicators = get_provider("indicators", "pandas")(ohlcv_provider)
    rsi = pandas_indicators.get_indicator("AAPL", "rsi", end_date=datetime.now(), lookback_days=30)
"""

from ba2_trade_platform.logger import logger

from typing import Type, Dict
from ba2_trade_platform.core.interfaces import (
    DataProviderInterface,
    MarketIndicatorsInterface,
    CompanyFundamentalsOverviewInterface,
    CompanyFundamentalsDetailsInterface,
    MarketNewsInterface,
    MacroEconomicsInterface,
    CompanyInsiderInterface
)

# Legacy data provider (to be migrated)
from .ohlcv.YFinanceDataProvider import YFinanceDataProvider
from .ohlcv.AlphaVantageOHLCVProvider import AlphaVantageOHLCVProvider
from .ohlcv.AlpacaOHLCVProvider import AlpacaOHLCVProvider
from .ohlcv.FMPOHLCVProvider import FMPOHLCVProvider

# Import provider implementations
from .news import AlpacaNewsProvider, AlphaVantageNewsProvider, GoogleNewsProvider, OpenAINewsProvider, FMPNewsProvider
from .indicators import PandasIndicatorCalc, AlphaVantageIndicatorsProvider
from .fundamentals import (
    AlphaVantageCompanyOverviewProvider,
    OpenAICompanyOverviewProvider,
    FMPCompanyOverviewProvider,
    AlphaVantageCompanyDetailsProvider,
    YFinanceCompanyDetailsProvider,
    FMPCompanyDetailsProvider
)
from .macro import FREDMacroProvider
from .insider import FMPInsiderProvider

# Provider registries - will be populated as providers are implemented
OHLCV_PROVIDERS: Dict[str, Type[DataProviderInterface]] = {
    "yfinance": YFinanceDataProvider,
    "alphavantage": AlphaVantageOHLCVProvider,
    "alpaca": AlpacaOHLCVProvider,
    "fmp": FMPOHLCVProvider,
}
INDICATORS_PROVIDERS: Dict[str, Type[MarketIndicatorsInterface]] = {
    "pandas": PandasIndicatorCalc,
    "alphavantage": AlphaVantageIndicatorsProvider,
}

FUNDAMENTALS_OVERVIEW_PROVIDERS: Dict[str, Type[CompanyFundamentalsOverviewInterface]] = {
    "alphavantage": AlphaVantageCompanyOverviewProvider,
    "openai": OpenAICompanyOverviewProvider,
    "fmp": FMPCompanyOverviewProvider,
}

FUNDAMENTALS_DETAILS_PROVIDERS: Dict[str, Type[CompanyFundamentalsDetailsInterface]] = {
    "alphavantage": AlphaVantageCompanyDetailsProvider,
    "yfinance": YFinanceCompanyDetailsProvider,
    "fmp": FMPCompanyDetailsProvider,
    # "simfin": SimFinFundamentalsDetailsProvider,
}

NEWS_PROVIDERS: Dict[str, Type[MarketNewsInterface]] = {
    "alpaca": AlpacaNewsProvider,
    "alphavantage": AlphaVantageNewsProvider,
    "google": GoogleNewsProvider,
    "openai": OpenAINewsProvider,
    "fmp": FMPNewsProvider,
    # "finnhub": FinnhubNewsProvider,
    # "reddit": RedditNewsProvider,
}

MACRO_PROVIDERS: Dict[str, Type[MacroEconomicsInterface]] = {
    "fred": FREDMacroProvider,
}

INSIDER_PROVIDERS: Dict[str, Type[CompanyInsiderInterface]] = {
    "fmp": FMPInsiderProvider,
    # "alphavantage": AlphaVantageInsiderProvider,
    # "yfinance": YFinanceInsiderProvider,
    # "finnhub": FinnhubInsiderProvider,
}


def get_provider(category: str, provider_name: str, **kwargs) -> DataProviderInterface:
    """
    Get a provider instance by category and name.
    
    Args:
        category: Provider category - one of:
                 - 'ohlcv': OHLCV stock price data
                 - 'indicators': Technical indicators
                 - 'fundamentals': Complete fundamentals (overview + statements)
                 - 'fundamentals_overview': Company fundamentals overview
                 - 'fundamentals_details': Detailed financial statements
                 - 'news': Market and company news
                 - 'macro': Macroeconomic data
                 - 'insider': Insider trading data
        provider_name: Provider name (e.g., 'alpaca', 'yfinance', 'alphavantage')
        **kwargs: Additional arguments to pass to the provider constructor
                 (e.g., source='trading_agents' for Alpha Vantage providers)
    
    Returns:
        DataProviderInterface: Instantiated provider
    
    Raises:
        ValueError: If category or provider_name is not found
    
    Example:
        >>> news_provider = get_provider("news", "alpaca")
        >>> news = news_provider.get_company_news("AAPL", end_date=datetime.now(), lookback_days=7)
        
        >>> # With custom source for Alpha Vantage
        >>> av_news = get_provider("news", "alphavantage", source="trading_agents")
    """
    registries = {
        "ohlcv": OHLCV_PROVIDERS,
        "indicators": INDICATORS_PROVIDERS,
        "fundamentals_overview": FUNDAMENTALS_OVERVIEW_PROVIDERS,
        "fundamentals_details": FUNDAMENTALS_DETAILS_PROVIDERS,
        "news": NEWS_PROVIDERS,
        "macro": MACRO_PROVIDERS,
        "insider": INSIDER_PROVIDERS,
    }
    
    if category not in registries:
        raise ValueError(
            f"Unknown provider category: {category}. "
            f"Available categories: {', '.join(registries.keys())}"
        )
    
    provider_class = registries[category].get(provider_name)
    if not provider_class:
        available = ', '.join(registries[category].keys()) or 'none'
        raise ValueError(
            f"Provider '{provider_name}' not found in category '{category}'. "
            f"Available providers: {available}"
        )
    
    # Try to instantiate with kwargs, fall back to no-arg constructor for compatibility
    try:
        return provider_class(**kwargs)
    except TypeError:
        # Provider doesn't accept these kwargs, use default constructor
        if kwargs:
            logger.warning(
                f"Provider {provider_name} in category {category} doesn't accept "
                f"constructor arguments: {list(kwargs.keys())}. Using default constructor."
            )
        return provider_class()


def list_providers(category: str = None) -> Dict[str, list[str]]:
    """
    List available providers by category.
    
    Args:
        category: Optional category to filter by. If None, returns all categories.
    
    Returns:
        Dict mapping category names to lists of provider names
    
    Example:
        >>> list_providers("news")
        {'news': ['alpaca', 'alphavantage', 'google', 'finnhub', 'reddit']}
        
        >>> list_providers()
        {
            'indicators': ['alphavantage', 'yfinance'],
            'news': ['alpaca', 'alphavantage', 'google'],
            ...
        }
    """
    all_registries = {
        "ohlcv": OHLCV_PROVIDERS,
        "indicators": INDICATORS_PROVIDERS,
        "fundamentals_overview": FUNDAMENTALS_OVERVIEW_PROVIDERS,
        "fundamentals_details": FUNDAMENTALS_DETAILS_PROVIDERS,
        "news": NEWS_PROVIDERS,
        "macro": MACRO_PROVIDERS,
        "insider": INSIDER_PROVIDERS,
    }
    
    if category:
        if category not in all_registries:
            raise ValueError(
                f"Unknown provider category: {category}. "
                f"Available categories: {', '.join(all_registries.keys())}"
            )
        return {category: list(all_registries[category].keys())}
    
    return {cat: list(registry.keys()) for cat, registry in all_registries.items()}


__all__ = [
    # Provider classes
    "YFinanceDataProvider",
    "AlphaVantageOHLCVProvider",
    "AlpacaOHLCVProvider",
    "FMPOHLCVProvider",
    "AlpacaNewsProvider",
    "AlphaVantageNewsProvider",
    "GoogleNewsProvider",
    "OpenAINewsProvider",
    "FMPNewsProvider",
    "PandasIndicatorCalc",
    "AlphaVantageIndicatorsProvider",
    "AlphaVantageCompanyOverviewProvider",
    "OpenAICompanyOverviewProvider",
    "FMPCompanyOverviewProvider",
    "AlphaVantageCompanyDetailsProvider",
    "YFinanceCompanyDetailsProvider",
    "FMPCompanyDetailsProvider",
    "FREDMacroProvider",
    "FMPInsiderProvider",
    
    # Interfaces
    "DataProviderInterface",
    "MarketIndicatorsInterface",
    "CompanyFundamentalsOverviewInterface",
    "CompanyFundamentalsDetailsInterface",
    "MarketNewsInterface",
    "MacroEconomicsInterface",
    "CompanyInsiderInterface",
    
    # Helper functions
    "get_provider",
    "list_providers",
    
    # Provider registries
    "OHLCV_PROVIDERS",
    "INDICATORS_PROVIDERS",
    "FUNDAMENTALS_OVERVIEW_PROVIDERS",
    "FUNDAMENTALS_DETAILS_PROVIDERS",
    "NEWS_PROVIDERS",
    "MACRO_PROVIDERS",
    "INSIDER_PROVIDERS",
]
