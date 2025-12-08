"""
Social Media Sentiment Data Providers

Providers for social media sentiment analysis across various platforms.

Available Providers:
    - AI: AI-powered sentiment analysis using web search across multiple platforms
          Supports both OpenAI and NagaAI models with automatic API selection.
"""

from .AISocialMediaSentiment import AISocialMediaSentiment

__all__ = [
    "AISocialMediaSentiment",
]
