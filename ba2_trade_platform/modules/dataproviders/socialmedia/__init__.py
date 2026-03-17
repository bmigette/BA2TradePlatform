"""
Social Media Sentiment Data Providers

Providers for social media sentiment analysis across various platforms.

Available Providers:
    - AI: AI-powered sentiment analysis using web search across multiple platforms
          Supports both OpenAI and NagaAI models with automatic API selection.
    - StockTwits: Real-time StockTwits sentiment from public message stream
                  Uses curl_cffi for Cloudflare bypass. No API key required.
"""

from .AISocialMediaSentiment import AISocialMediaSentiment
from .StockTwitsSentiment import StockTwitsSentiment
from .StockTwitsTrending import StockTwitsTrending

__all__ = [
    "AISocialMediaSentiment",
    "StockTwitsSentiment",
    "StockTwitsTrending",
]
