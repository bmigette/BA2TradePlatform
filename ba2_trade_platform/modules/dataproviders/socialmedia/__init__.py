"""
Social Media Sentiment Data Providers

Providers for social media sentiment analysis across various platforms.

Available Providers:
    - AI: AI-powered sentiment analysis using web search across multiple platforms
    - OpenAI: Legacy - deprecated (use AI provider instead)
"""

from .AISocialMediaSentiment import AISocialMediaSentiment
from .OpenAISocialMediaSentiment import OpenAISocialMediaSentiment  # Legacy - deprecated

__all__ = [
    "AISocialMediaSentiment",
    "OpenAISocialMediaSentiment",  # Legacy - deprecated
]
