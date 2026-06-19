"""
Social Media Sentiment Data Providers

Providers for social media sentiment analysis across various platforms.

Available Providers:
    - StockTwits: Real-time StockTwits sentiment from public message stream
                  Uses curl_cffi for Cloudflare bypass. No API key required.
"""

from .StockTwitsSentiment import StockTwitsSentiment
from .StockTwitsTrending import StockTwitsTrending

__all__ = [
    "StockTwitsSentiment",
    "StockTwitsTrending",
]
