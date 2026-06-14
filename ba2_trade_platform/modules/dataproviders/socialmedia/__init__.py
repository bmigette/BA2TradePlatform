"""Social Media Sentiment Data Providers (Phase 6 merge-shim).

The StockTwits providers come from the package (single source of truth):
``ba2_providers.socialmedia.{StockTwitsSentiment, StockTwitsTrending}``.
The AI sentiment provider (AISocialMediaSentiment) is live-only (it needs the live
ModelFactory LLM stack), so it stays in this in-tree file and is re-exported here.
"""
# Non-AI providers from the package:
from ba2_providers.socialmedia import (  # noqa: F401
    StockTwitsSentiment,
    StockTwitsTrending,
)

# Live-only AI provider (stays in BA2TradePlatform; needs ModelFactory):
from .AISocialMediaSentiment import AISocialMediaSentiment  # noqa: F401

__all__ = [
    "AISocialMediaSentiment",
    "StockTwitsSentiment",
    "StockTwitsTrending",
]
