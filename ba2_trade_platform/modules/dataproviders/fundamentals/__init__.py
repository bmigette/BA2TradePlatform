"""
Company Fundamentals Data Providers

Contains providers for company fundamentals data, organized by detail level:
- overview/: High-level metrics (P/E, market cap, EPS, etc.)
- details/: Detailed financial statements (balance sheet, income, cashflow)

Available Providers:
- AlphaVantageCompanyOverviewProvider: Company overview from Alpha Vantage
- OpenAICompanyOverviewProvider: Company overview from OpenAI
- FMPCompanyOverviewProvider: Company overview from Financial Modeling Prep
- AlphaVantageCompanyDetailsProvider: Financial statements from Alpha Vantage
- YFinanceCompanyDetailsProvider: Financial statements from Yahoo Finance
- FMPCompanyDetailsProvider: Financial statements from Financial Modeling Prep
"""

from .overview import (
    AlphaVantageCompanyOverviewProvider, 
    AICompanyOverviewProvider,
    OpenAICompanyOverviewProvider,  # Legacy - deprecated
    FMPCompanyOverviewProvider
)
from .details import AlphaVantageCompanyDetailsProvider, YFinanceCompanyDetailsProvider, FMPCompanyDetailsProvider

__all__ = [
    "AlphaVantageCompanyOverviewProvider",
    "AICompanyOverviewProvider",
    "OpenAICompanyOverviewProvider",  # Legacy - deprecated
    "FMPCompanyOverviewProvider",
    "AlphaVantageCompanyDetailsProvider",
    "YFinanceCompanyDetailsProvider",
    "FMPCompanyDetailsProvider",
]
