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
from .AINewsProvider import AINewsProvider
from .OpenAINewsProvider import OpenAINewsProvider  # Legacy - deprecated
from .GoogleNewsProvider import GoogleNewsProvider
from .FMPNewsProvider import FMPNewsProvider
from .FinnhubNewsProvider import FinnhubNewsProvider
# from .RedditNewsProvider import RedditNewsProvider

__all__ = [
    "AlpacaNewsProvider",
    "AlphaVantageNewsProvider",
    "AINewsProvider",
    "OpenAINewsProvider",  # Legacy - deprecated
    "GoogleNewsProvider",
    "FMPNewsProvider",
    "FinnhubNewsProvider",
]
