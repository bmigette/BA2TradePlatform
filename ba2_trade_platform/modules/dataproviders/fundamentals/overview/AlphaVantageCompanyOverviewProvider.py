"""
Alpha Vantage Company Overview Provider

Provides company overview and high-level fundamentals from Alpha Vantage API.
"""

from typing import Dict, Any, Literal, Annotated
from datetime import datetime
import json
import requests

from ba2_trade_platform.core.interfaces import CompanyFundamentalsOverviewInterface
from ba2_trade_platform.core.provider_utils import log_provider_call
from ba2_trade_platform.logger import logger
from ba2_trade_platform.config import get_app_setting
from ba2_trade_platform.modules.dataproviders.alpha_vantage_common import make_api_request


class AlphaVantageRateLimitError(Exception):
    """Exception raised when Alpha Vantage API rate limit is exceeded."""
    pass


class AlphaVantageCompanyOverviewProvider(CompanyFundamentalsOverviewInterface):
    """
    Alpha Vantage company overview provider.
    
    Provides high-level company fundamentals including market cap, P/E ratio,
    EPS, dividend yield, and other key metrics from Alpha Vantage API.
    """
    
    API_BASE_URL = "https://www.alphavantage.co/query"
    
    def __init__(self):
        """Initialize Alpha Vantage company overview provider."""
        super().__init__()
        self.api_key = get_app_setting("alpha_vantage_api_key")
        if not self.api_key:
            raise ValueError("Alpha Vantage API key not configured. Please set 'alpha_vantage_api_key' in app settings.")
        logger.debug("Initialized AlphaVantageCompanyOverviewProvider")
    
    # Removed _make_api_request - using shared alpha_vantage_common.make_api_request instead
    
    @log_provider_call
    def get_fundamentals_overview(
        self,
        symbol: Annotated[str, "Stock ticker symbol"],
        as_of_date: Annotated[datetime, "Date for fundamentals (uses most recent data as of this date)"],
        format_type: Literal["dict", "markdown", "both"] = "markdown"
    ) -> Dict[str, Any] | str:
        """
        Get company fundamentals overview.
        
        Args:
            symbol: Stock ticker symbol
            as_of_date: Date for fundamentals (Alpha Vantage returns most recent data)
            format_type: Output format - 'dict' for structured data, 'markdown' for text
            
        Returns:
            Company overview data with key metrics
            
        Note: Alpha Vantage OVERVIEW endpoint returns the most recent data available.
        The as_of_date parameter is accepted for interface compatibility but Alpha Vantage
        doesn't support historical point-in-time queries for overview data.
        """
        logger.debug(f"Fetching company overview for {symbol} (as of {as_of_date.date()})")
        
        try:
            params = {"symbol": symbol}
            result = make_api_request("OVERVIEW", params)
            
            # Build dict response (always build it for "both" format support)
            data = json.loads(result)
            
            # Transform to match interface specification
            dict_response = {
                "symbol": symbol.upper(),
                "company_name": data.get("Name", ""),
                "as_of_date": as_of_date.isoformat(),
                "data_date": as_of_date.isoformat(),  # Alpha Vantage doesn't provide this
                "metrics": {
                    "market_cap": self._safe_float(data.get("MarketCapitalization")),
                    "pe_ratio": self._safe_float(data.get("PERatio")),
                    "peg_ratio": self._safe_float(data.get("PEGRatio")),
                    "eps": self._safe_float(data.get("EPS")),
                    "dividend_yield": self._safe_float(data.get("DividendYield")),
                    "beta": self._safe_float(data.get("Beta")),
                    "52_week_high": self._safe_float(data.get("52WeekHigh")),
                    "52_week_low": self._safe_float(data.get("52WeekLow")),
                    "price_to_book": self._safe_float(data.get("PriceToBookRatio")),
                    "price_to_sales": self._safe_float(data.get("PriceToSalesRatioTTM")),
                    "profit_margin": self._safe_float(data.get("ProfitMargin")),
                    "operating_margin": self._safe_float(data.get("OperatingMarginTTM")),
                    "return_on_assets": self._safe_float(data.get("ReturnOnAssetsTTM")),
                    "return_on_equity": self._safe_float(data.get("ReturnOnEquityTTM")),
                    "revenue": self._safe_float(data.get("RevenueTTM")),
                    "revenue_per_share": self._safe_float(data.get("RevenuePerShareTTM")),
                    "quarterly_earnings_growth": self._safe_float(data.get("QuarterlyEarningsGrowthYOY")),
                    "quarterly_revenue_growth": self._safe_float(data.get("QuarterlyRevenueGrowthYOY")),
                    "analyst_target_price": self._safe_float(data.get("AnalystTargetPrice")),
                    "trailing_pe": self._safe_float(data.get("TrailingPE")),
                    "forward_pe": self._safe_float(data.get("ForwardPE")),
                    "shares_outstanding": self._safe_float(data.get("SharesOutstanding")),
                    "shares_float": self._safe_float(data.get("SharesFloat")),
                    # Additional fields
                    "sector": data.get("Sector"),
                    "industry": data.get("Industry"),
                    "description": data.get("Description"),
                    "exchange": data.get("Exchange"),
                    "currency": data.get("Currency"),
                    "country": data.get("Country"),
                }
            }
            
            # Return based on format_type
            if format_type == "dict":
                return dict_response
            elif format_type == "both":
                return {
                    "text": result,
                    "data": dict_response
                }
            else:  # markdown
                return result
                
        except Exception as e:
            logger.error(f"Failed to get company overview for {symbol}: {e}")
            raise
    
    @staticmethod
    def _safe_float(value: str | None) -> float | None:
        """Safely convert string to float, returning None if conversion fails."""
        if value is None or value == "None" or value == "":
            return None
        try:
            return float(value)
        except (ValueError, TypeError):
            return None

