"""
Social Media Sentiment Data Providers

Providers for social media sentiment analysis across various platforms.

Available Providers:
    - OpenAI: AI-powered sentiment analysis using web search across multiple platforms
"""

from .OpenAISocialMediaSentiment import OpenAISocialMediaSentiment

__all__ = [
    "OpenAISocialMediaSentiment",
]
