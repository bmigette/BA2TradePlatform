"""
ba2_providers - Market Data Providers

This package contains all data provider implementations for the BA2 Trade platform.
Providers are organized by data type (indicators, fundamentals, news, macro, insider,
social media, screener).

Provider Registry:
    Each subdirectory contains a registry mapping provider names to provider classes.
    Use the get_provider() function to instantiate providers dynamically.

Usage:
    from ba2_providers import get_provider

    # Get a news provider
    alpaca_news = get_provider("news", "alpaca")
    news = alpaca_news.get_company_news("AAPL", end_date=datetime.now(), lookback_days=7)

    # Get an indicators provider (requires OHLCV provider)
    from ba2_providers import YFinanceDataProvider
    ohlcv_provider = YFinanceDataProvider()
    pandas_indicators = get_provider("indicators", "pandas")(ohlcv_provider)
    rsi = pandas_indicators.get_indicator("AAPL", "rsi", end_date=datetime.now(), lookback_days=30)
"""

from ba2_common.logger import logger

from typing import Type, Dict
from ba2_common.core.interfaces import (
    DataProviderInterface,
    MarketIndicatorsInterface,
    CompanyFundamentalsOverviewInterface,
    CompanyFundamentalsDetailsInterface,
    MarketNewsInterface,
    MacroEconomicsInterface,
    CompanyInsiderInterface,
    SocialMediaDataProviderInterface,
    ScreenerProviderInterface,
    OptionsDataProviderInterface,
    RiskStatsInterface,
    ValuationSnapshotInterface,
)

# Legacy data provider (to be migrated)
from .ohlcv.YFinanceDataProvider import YFinanceDataProvider
from .ohlcv.AlphaVantageOHLCVProvider import AlphaVantageOHLCVProvider
from .ohlcv.AlpacaOHLCVProvider import AlpacaOHLCVProvider
from .ohlcv.FMPOHLCVProvider import FMPOHLCVProvider
from .ohlcv.EODHDOHLCVProvider import EODHDOHLCVProvider
from .ohlcv.PolygonOHLCVProvider import PolygonOHLCVProvider

# Import provider implementations
from .news import AlpacaNewsProvider, AlphaVantageNewsProvider, GoogleNewsProvider, FMPNewsProvider, FinnhubNewsProvider, LocalFilesNewsProvider
from .indicators import PandasIndicatorCalc, AlphaVantageIndicatorsProvider
from .fundamentals import (
    AlphaVantageCompanyOverviewProvider,
    FMPCompanyOverviewProvider,
    AlphaVantageCompanyDetailsProvider,
    YFinanceCompanyDetailsProvider,
    FMPCompanyDetailsProvider
)
from .macro import FREDMacroProvider
from .insider import FMPInsiderProvider
from .socialmedia import StockTwitsSentiment, StockTwitsTrending
from .screener import FMPScreenerProvider, FMPHistoricalScreenerProvider
from .options import (AlpacaOptionsProvider, TastyTradeOptionsProvider,
                      ThetaDataOptionsProvider)
from .riskstats import FinanceCalcRiskStatsProvider
from .valuation import FinanceCalcValuationProvider

# Provider registries - will be populated as providers are implemented
OHLCV_PROVIDERS: Dict[str, Type[DataProviderInterface]] = {
    "yfinance": YFinanceDataProvider,
    "alphavantage": AlphaVantageOHLCVProvider,
    "alpaca": AlpacaOHLCVProvider,
    "fmp": FMPOHLCVProvider,
    "eodhd": EODHDOHLCVProvider,
    "polygon": PolygonOHLCVProvider,
}
INDICATORS_PROVIDERS: Dict[str, Type[MarketIndicatorsInterface]] = {
    "pandas": PandasIndicatorCalc,
    "alphavantage": AlphaVantageIndicatorsProvider,
}

# Compute providers (deterministic injection blocks) — pure-math, no API key/network
RISK_STATS_PROVIDERS: Dict[str, Type[RiskStatsInterface]] = {
    "finance_calc": FinanceCalcRiskStatsProvider,
}
VALUATION_PROVIDERS: Dict[str, Type[ValuationSnapshotInterface]] = {
    "finance_calc": FinanceCalcValuationProvider,
}

FUNDAMENTALS_OVERVIEW_PROVIDERS: Dict[str, Type[CompanyFundamentalsOverviewInterface]] = {
    "alphavantage": AlphaVantageCompanyOverviewProvider,
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
    "fmp": FMPNewsProvider,
    "finnhub": FinnhubNewsProvider,
    "localfiles": LocalFilesNewsProvider,
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

SOCIALMEDIA_PROVIDERS: Dict[str, Type[SocialMediaDataProviderInterface]] = {
    "stocktwits": StockTwitsSentiment,
    "stocktwits_trending": StockTwitsTrending,
}

SCREENER_PROVIDERS: Dict[str, Type[ScreenerProviderInterface]] = {
    "fmp": FMPScreenerProvider,
    "fmp_historical": FMPHistoricalScreenerProvider,
}

# Bulk HISTORICAL option data used to build the offline options cache (NOT the broker
# side -- that is OptionsAccountInterface). "alpaca" is the incumbent but is capped at a
# measured 2024-01-18 history floor at every tier; "thetadata" exists to reach further
# back (4-12y by tier) and needs a locally-running Theta Terminal rather than an API key.
# "tastytrade" reads dxfeed, which serves daily candles for ALREADY-EXPIRED contracts and
# is the only one of the three whose bars carry imp_volatility and open_interest -- the two
# fields that are NULL across all 6,757,055 rows of the incumbent cache and that therefore
# kill delta selection and min_open_interest in backtest.
OPTIONS_PROVIDERS: Dict[str, Type[OptionsDataProviderInterface]] = {
    "alpaca": AlpacaOptionsProvider,
    "thetadata": ThetaDataOptionsProvider,
    "tastytrade": TastyTradeOptionsProvider,
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
                 - 'socialmedia': Social media sentiment analysis
                 - 'options': Bulk historical option data (cache builders)
                 - 'risk_stats': Deterministic risk statistics (computed locally)
                 - 'valuation': Default-assumption valuation snapshot (computed locally)
        provider_name: Provider name (e.g., 'alpaca', 'yfinance', 'alphavantage', 'openai')
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
        "socialmedia": SOCIALMEDIA_PROVIDERS,
        "screener": SCREENER_PROVIDERS,
        "options": OPTIONS_PROVIDERS,
        "risk_stats": RISK_STATS_PROVIDERS,
        "valuation": VALUATION_PROVIDERS,
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


__all__ = [
    # Provider classes
    "YFinanceDataProvider",
    "AlphaVantageOHLCVProvider",
    "AlpacaOHLCVProvider",
    "FMPOHLCVProvider",
    "EODHDOHLCVProvider",
    "PolygonOHLCVProvider",
    "AlpacaNewsProvider",
    "AlphaVantageNewsProvider",
    "GoogleNewsProvider",
    "FMPNewsProvider",
    "FinnhubNewsProvider",
    "LocalFilesNewsProvider",
    "PandasIndicatorCalc",
    "AlphaVantageIndicatorsProvider",
    "AlphaVantageCompanyOverviewProvider",
    "FMPCompanyOverviewProvider",
    "AlphaVantageCompanyDetailsProvider",
    "YFinanceCompanyDetailsProvider",
    "FMPCompanyDetailsProvider",
    "FREDMacroProvider",
    "FMPInsiderProvider",
    "StockTwitsSentiment",
    "StockTwitsTrending",
    "FMPScreenerProvider",
    "FMPHistoricalScreenerProvider",
    "FinanceCalcRiskStatsProvider",
    "FinanceCalcValuationProvider",

    # Helper functions
    "get_provider",
]
