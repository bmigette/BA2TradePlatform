"""
Company Fundamentals Data Providers

Contains providers for company fundamentals data, organized by detail level:
- overview/: High-level metrics (P/E, market cap, EPS, etc.)
- details/: Detailed financial statements (balance sheet, income, cashflow)

Available Providers:
- AlphaVantageCompanyOverviewProvider: Company overview from Alpha Vantage
- AICompanyOverviewProvider: Company overview using AI web search
- FMPCompanyOverviewProvider: Company overview from Financial Modeling Prep
- AlphaVantageCompanyDetailsProvider: Financial statements from Alpha Vantage
- YFinanceCompanyDetailsProvider: Financial statements from Yahoo Finance
- FMPCompanyDetailsProvider: Financial statements from Financial Modeling Prep

Phase 6 note: ``overview``/``details`` below are now shims to ``ba2_providers``
(``overview`` is a merge-shim that adds the live AICompanyOverviewProvider). This
aggregator keeps re-exporting from the local sub-packages, so all names resolve
unchanged.
"""

from .overview import (
    AlphaVantageCompanyOverviewProvider, 
    AICompanyOverviewProvider,
    FMPCompanyOverviewProvider
)
from .details import AlphaVantageCompanyDetailsProvider, YFinanceCompanyDetailsProvider, FMPCompanyDetailsProvider

__all__ = [
    "AlphaVantageCompanyOverviewProvider",
    "AICompanyOverviewProvider",
    "FMPCompanyOverviewProvider",
    "AlphaVantageCompanyDetailsProvider",
    "YFinanceCompanyDetailsProvider",
    "FMPCompanyDetailsProvider",
]
