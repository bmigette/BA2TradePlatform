"""
Market News Data Providers

Providers for company-specific and global market news

Available Providers:
    - Alpaca: News from Alpaca Markets API
    - AlphaVantage: News from Alpha Vantage API
    - Google: Google News scraping
    - FMP: News from Financial Modeling Prep API
    - Finnhub: News from Finnhub API
    - LocalFiles: News from locally exported JSON files
"""

from .AlpacaNewsProvider import AlpacaNewsProvider
from .AlphaVantageNewsProvider import AlphaVantageNewsProvider
from .GoogleNewsProvider import GoogleNewsProvider
from .FMPNewsProvider import FMPNewsProvider
from .FinnhubNewsProvider import FinnhubNewsProvider
from .LocalFilesNewsProvider import LocalFilesNewsProvider

__all__ = [
    "AlpacaNewsProvider",
    "AlphaVantageNewsProvider",
    "GoogleNewsProvider",
    "FMPNewsProvider",
    "FinnhubNewsProvider",
    "LocalFilesNewsProvider",
]
