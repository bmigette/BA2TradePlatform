"""
Market News Data Providers

Providers for company-specific and global market news

Available Providers:
    - Alpaca: News from Alpaca Markets API
    - AlphaVantage: News from Alpha Vantage API
    - OpenAI: AI-generated news summaries and analysis
    - Google: Google News scraping
    - Finnhub: News from Finnhub API
    - Reddit: Reddit financial discussions and sentiment
"""

from .AlpacaNewsProvider import AlpacaNewsProvider
from .AlphaVantageNewsProvider import AlphaVantageNewsProvider
from .OpenAINewsProvider import OpenAINewsProvider
from .GoogleNewsProvider import GoogleNewsProvider
# from .FinnhubNewsProvider import FinnhubNewsProvider
# from .RedditNewsProvider import RedditNewsProvider

__all__ = [
    "AlpacaNewsProvider",
    "AlphaVantageNewsProvider",
    "OpenAINewsProvider",
    "GoogleNewsProvider",
]
