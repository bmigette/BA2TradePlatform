"""Market News Data Providers (Phase 6 merge-shim).

The non-AI news providers come from the package (single source of truth):
``ba2_providers.news.{AlpacaNewsProvider, AlphaVantageNewsProvider,
GoogleNewsProvider, FMPNewsProvider, FinnhubNewsProvider}``.
The AI news provider (AINewsProvider) is live-only (it needs the live ModelFactory
LLM stack), so it stays in this in-tree file and is re-exported here.
"""
# Non-AI providers from the package:
from ba2_providers.news import (  # noqa: F401
    AlpacaNewsProvider,
    AlphaVantageNewsProvider,
    GoogleNewsProvider,
    FMPNewsProvider,
    FinnhubNewsProvider,
)

# Live-only AI provider (stays in BA2TradePlatform; needs ModelFactory):
from .AINewsProvider import AINewsProvider  # noqa: F401

__all__ = [
    "AlpacaNewsProvider",
    "AlphaVantageNewsProvider",
    "AINewsProvider",
    "GoogleNewsProvider",
    "FMPNewsProvider",
    "FinnhubNewsProvider",
]
