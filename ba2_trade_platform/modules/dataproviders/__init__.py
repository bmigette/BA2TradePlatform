"""Live provider registry (Phase 6 merge-shim).

The provider implementations now live in ``ba2_providers`` (single source of
truth). ``get_provider`` delegates to ``ba2_providers.get_provider`` for every
category/name, with an OVERLAY for the three live-only AI providers that stayed in
BA2TradePlatform (they need the live ModelFactory LLM stack and so were never
extracted):

    ("news", "ai")                 -> AINewsProvider
    ("fundamentals_overview", "ai") -> AICompanyOverviewProvider
    ("socialmedia", "ai")          -> AISocialMediaSentiment

All provider classes the live ``__all__`` named are re-exported here so existing
``from ba2_trade_platform.modules.dataproviders import X`` imports keep resolving.
The package registry adds screener['fmp_historical'] (Phase 3) and the StockTwits
socialmedia providers — preserved automatically by delegating to the package
get_provider. The package get_provider's instantiate-then-TypeError-fallback logic
is byte-identical to the original live registry.
"""
from ba2_trade_platform.logger import logger

# Package provider classes + helper (the non-AI providers, single source of truth).
# ba2_providers has __all__, so `*` is bounded to its public provider classes.
from ba2_providers import *  # noqa: F401,F403
from ba2_providers import get_provider as _pkg_get_provider  # noqa: F401

# The package's registry dicts are NOT in its __all__ but some live callers import
# them by name (e.g. ``from ...dataproviders import OHLCV_PROVIDERS``). Re-export
# them explicitly so those imports keep resolving.
from ba2_providers import (  # noqa: F401
    OHLCV_PROVIDERS,
    INDICATORS_PROVIDERS,
    FUNDAMENTALS_OVERVIEW_PROVIDERS,
    FUNDAMENTALS_DETAILS_PROVIDERS,
    NEWS_PROVIDERS,
    MACRO_PROVIDERS,
    INSIDER_PROVIDERS,
    SOCIALMEDIA_PROVIDERS,
    SCREENER_PROVIDERS,
)

# Live-only AI providers (stayed in BA2TradePlatform; need ModelFactory):
from .news.AINewsProvider import AINewsProvider
from .fundamentals.overview.AICompanyOverviewProvider import AICompanyOverviewProvider
from .socialmedia.AISocialMediaSentiment import AISocialMediaSentiment

# Overlay of the live-only AI providers keyed by (category, provider_name). The
# keys mirror the original live registry exactly (note: 'fundamentals_overview',
# NOT 'fundamentals').
_LIVE_AI = {
    ("news", "ai"): AINewsProvider,
    ("fundamentals_overview", "ai"): AICompanyOverviewProvider,
    ("socialmedia", "ai"): AISocialMediaSentiment,
}


def get_provider(category: str, provider_name: str, **kwargs):
    """Get a provider instance by category and name.

    Live-only AI providers are served from the overlay; everything else delegates
    to the package registry. Mirrors the original instantiate-then-TypeError
    fallback for both paths so kwargs-incompatible providers degrade identically.
    """
    cls = _LIVE_AI.get((category, provider_name))
    if cls is not None:
        try:
            return cls(**kwargs)
        except TypeError:
            if kwargs:
                logger.warning(
                    f"Provider {provider_name} in category {category} doesn't accept "
                    f"constructor arguments: {list(kwargs.keys())}. Using default constructor."
                )
            return cls()
    return _pkg_get_provider(category, provider_name, **kwargs)


__all__ = [
    # Provider classes
    "YFinanceDataProvider",
    "AlphaVantageOHLCVProvider",
    "AlpacaOHLCVProvider",
    "FMPOHLCVProvider",
    "AlpacaNewsProvider",
    "AlphaVantageNewsProvider",
    "GoogleNewsProvider",
    "AINewsProvider",
    "FMPNewsProvider",
    "FinnhubNewsProvider",
    "PandasIndicatorCalc",
    "AlphaVantageIndicatorsProvider",
    "AlphaVantageCompanyOverviewProvider",
    "AICompanyOverviewProvider",
    "FMPCompanyOverviewProvider",
    "AlphaVantageCompanyDetailsProvider",
    "YFinanceCompanyDetailsProvider",
    "FMPCompanyDetailsProvider",
    "FREDMacroProvider",
    "FMPInsiderProvider",
    "AISocialMediaSentiment",
    "StockTwitsSentiment",
    "StockTwitsTrending",
    "FMPScreenerProvider",
    "FMPHistoricalScreenerProvider",

    # Helper functions
    "get_provider",
]
