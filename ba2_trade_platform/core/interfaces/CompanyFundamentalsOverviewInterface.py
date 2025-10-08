"""
Interface for company fundamentals overview providers.

This interface defines methods for retrieving high-level company fundamentals
like P/E ratio, market cap, EPS, etc.
"""

from abc import abstractmethod
from typing import Dict, Any, Literal, Annotated
from datetime import datetime

from .DataProviderInterface import DataProviderInterface


class CompanyFundamentalsOverviewInterface(DataProviderInterface):
    """
    Interface for company fundamentals overview (high-level metrics).
    
    Providers implementing this interface supply key company metrics like
    market cap, P/E ratio, EPS, dividend yield, etc.
    """
    
    @abstractmethod
    def get_fundamentals_overview(
        self,
        symbol: Annotated[str, "Stock ticker symbol"],
        as_of_date: Annotated[datetime, "Date for fundamentals (uses most recent data as of this date)"],
        format_type: Literal["dict", "markdown"] = "markdown"
    ) -> Dict[str, Any] | str:
        """
        Get company fundamentals overview.
        
        This is a point-in-time query that returns the most recent fundamentals
        data available as of the specified date.
        
        Args:
            symbol: Stock ticker symbol (e.g., 'AAPL', 'MSFT')
            as_of_date: Date for fundamentals (returns most recent data as of this date)
            format_type: Output format ('dict' or 'markdown')
        
        Note: This is a point-in-time query (as_of_date), not a range query.
        Returns the most recent fundamentals data available as of the specified date.
        
        Returns:
            If format_type='dict': {
                "symbol": str,
                "company_name": str,
                "as_of_date": str (ISO format),
                "data_date": str (ISO format - actual date of the fundamentals data),
                "metrics": {
                    "market_cap": float,
                    "pe_ratio": float,
                    "peg_ratio": float,
                    "eps": float,
                    "dividend_yield": float,
                    "beta": float,
                    "52_week_high": float,
                    "52_week_low": float,
                    "price_to_book": float,
                    "price_to_sales": float,
                    "profit_margin": float,
                    "operating_margin": float,
                    "return_on_assets": float,
                    "return_on_equity": float,
                    "revenue": float,
                    "revenue_per_share": float,
                    "quarterly_earnings_growth": float,
                    "quarterly_revenue_growth": float,
                    "analyst_target_price": float,
                    "trailing_pe": float,
                    "forward_pe": float,
                    "shares_outstanding": float,
                    "shares_float": float
                    # Additional metrics as available from provider
                }
            }
            If format_type='markdown': Formatted markdown table with key metrics
        """
        pass
