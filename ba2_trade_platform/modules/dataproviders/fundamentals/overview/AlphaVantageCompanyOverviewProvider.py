"""
Alpha Vantage Company Overview Provider

Provides company overview and high-level fundamentals from Alpha Vantage API.
"""

from typing import Dict, Any, Literal, Annotated
from datetime import datetime
import json

from ba2_trade_platform.core.interfaces import CompanyFundamentalsOverviewInterface
from ba2_trade_platform.core.provider_utils import log_provider_call
from ba2_trade_platform.logger import logger
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
        logger.debug("Initialized AlphaVantageCompanyOverviewProvider")
    
    def _make_api_request(self, function_name: str, params: dict) -> str:
        """
        Make API request to Alpha Vantage.
        
        Args:
            function_name: Alpha Vantage function name (e.g., 'OVERVIEW')
            params: Additional parameters for the API request
            
        Returns:
            API response as string
            
        Raises:
            AlphaVantageRateLimitError: When API rate limit is exceeded
        """
        api_params = params.copy()
        api_params.update({
            "function": function_name,
            "apikey": ALPHA_VANTAGE_API_KEY,
            "source": "ba2_trade_platform",
        })
        
        response = requests.get(self.API_BASE_URL, params=api_params)
        response.raise_for_status()
        
        response_text = response.text
        
        # Check for rate limit error
        try:
            response_json = json.loads(response_text)
            if "Information" in response_json:
                info_message = response_json["Information"]
                if "rate limit" in info_message.lower() or "api key" in info_message.lower():
                    raise AlphaVantageRateLimitError(f"Alpha Vantage rate limit exceeded: {info_message}")
        except json.JSONDecodeError:
            # Response is not JSON (likely CSV data), which is normal
            pass
        
        return response_text
    
    @log_provider_call
    def get_fundamentals_overview(
        self,
        symbol: Annotated[str, "Stock ticker symbol"],
        as_of_date: Annotated[datetime, "Date for fundamentals (uses most recent data as of this date)"],
        format_type: Literal["dict", "markdown"] = "markdown"
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
            
            if format_type == "dict":
                data = json.loads(result)
                
                # Transform to match interface specification
                response = {
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
                return response
            else:
                # Return as markdown (raw JSON for now, can be formatted better)
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

