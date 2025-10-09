"""
Alpha Vantage Financial Details Provider

Provides detailed financial statements (balance sheet, income statement, cash flow)
from Alpha Vantage API.
"""

from typing import Dict, Any, Literal, Optional, Annotated
from datetime import datetime
import json
import requests

from ba2_trade_platform.core.interfaces import CompanyFundamentalsDetailsInterface
from ba2_trade_platform.core.provider_utils import log_provider_call
from ba2_trade_platform.logger import logger
from ba2_trade_platform.config import get_app_setting
from ba2_trade_platform.modules.dataproviders.alpha_vantage_common import make_api_request


class AlphaVantageRateLimitError(Exception):
    """Exception raised when Alpha Vantage API rate limit is exceeded."""
    pass


class AlphaVantageCompanyDetailsProvider(CompanyFundamentalsDetailsInterface):
    """
    Alpha Vantage company details provider.
    
    Provides detailed financial statements including balance sheets,
    income statements, and cash flow statements from Alpha Vantage API.
    """
    
    
    API_BASE_URL = "https://www.alphavantage.co/query"
    
    def __init__(self):
        """Initialize Alpha Vantage company details provider."""
        super().__init__()
        self.api_key = get_app_setting("alpha_vantage_api_key")
        if not self.api_key:
            raise ValueError("Alpha Vantage API key not configured. Please set 'alpha_vantage_api_key' in app settings.")
        logger.debug("Initialized AlphaVantageCompanyDetailsProvider")
    
    # Removed _make_api_request - using shared alpha_vantage_common.make_api_request instead
    
    @log_provider_call
    def get_balance_sheet(
        self,
        symbol: Annotated[str, "Stock ticker symbol"],
        frequency: Annotated[Literal["annual", "quarterly"], "Reporting frequency"],
        end_date: Annotated[datetime, "End date for statement range (inclusive)"],
        start_date: Annotated[Optional[datetime], "Start date for statement range (mutually exclusive with lookback_periods)"] = None,
        lookback_periods: Annotated[Optional[int], "Number of periods to look back from end_date (mutually exclusive with start_date)"] = None,
        format_type: Literal["dict", "markdown", "both"] = "markdown"
    ) -> Dict[str, Any] | str:
        """
        Get balance sheet(s) for a company.
        
        Args:
            symbol: Stock ticker symbol
            frequency: Annual or quarterly reporting
            end_date: End date (inclusive)
            start_date: Start date (use either this OR lookback_periods, not both)
            lookback_periods: Number of periods to look back (use either this OR start_date, not both)
            format_type: Output format - 'dict' for structured data, 'markdown' for text
            
        Returns:
            Balance sheet data with assets, liabilities, and equity
        """
        # Validate inputs
        if start_date and lookback_periods:
            raise ValueError("Provide either start_date OR lookback_periods, not both")
        if not start_date and not lookback_periods:
            raise ValueError("Must provide either start_date or lookback_periods")
        
        logger.debug(f"Fetching balance sheet for {symbol} (frequency: {frequency})")
        
        try:
            params = {"symbol": symbol}
            result = make_api_request("BALANCE_SHEET", params)
            
            # Build dict response (always build it for "both" format support)
            data = json.loads(result)
            
            # Filter statements based on frequency and date range
            key = "annualReports" if frequency == "annual" else "quarterlyReports"
            statements = data.get(key, [])
            
            # Filter by date range
            filtered_statements = self._filter_statements_by_date(
                statements, end_date, start_date, lookback_periods
            )
            
            dict_response = {
                "symbol": symbol.upper(),
                "frequency": frequency,
                "start_date": start_date.isoformat() if start_date else None,
                "end_date": end_date.isoformat(),
                "statements": filtered_statements
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
            logger.error(f"Failed to get balance sheet for {symbol}: {e}")
            raise
    
    @log_provider_call
    def get_income_statement(
        self,
        symbol: Annotated[str, "Stock ticker symbol"],
        frequency: Annotated[Literal["annual", "quarterly"], "Reporting frequency"],
        end_date: Annotated[datetime, "End date for statement range (inclusive)"],
        start_date: Annotated[Optional[datetime], "Start date for statement range (mutually exclusive with lookback_periods)"] = None,
        lookback_periods: Annotated[Optional[int], "Number of periods to look back from end_date (mutually exclusive with start_date)"] = None,
        format_type: Literal["dict", "markdown", "both"] = "markdown"
    ) -> Dict[str, Any] | str:
        """
        Get income statement(s) for a company.
        
        Args:
            symbol: Stock ticker symbol
            frequency: Annual or quarterly reporting
            end_date: End date (inclusive)
            start_date: Start date (use either this OR lookback_periods, not both)
            lookback_periods: Number of periods to look back (use either this OR start_date, not both)
            format_type: Output format - 'dict' for structured data, 'markdown' for text
            
        Returns:
            Income statement with revenue, expenses, and net income
        """
        # Validate inputs
        if start_date and lookback_periods:
            raise ValueError("Provide either start_date OR lookback_periods, not both")
        if not start_date and not lookback_periods:
            raise ValueError("Must provide either start_date or lookback_periods")
        
        logger.debug(f"Fetching income statement for {symbol} (frequency: {frequency})")
        
        try:
            params = {"symbol": symbol}
            result = make_api_request("INCOME_STATEMENT", params)
            
            # Build dict response (always build it for "both" format support)
            data = json.loads(result)
            
            # Filter statements based on frequency and date range
            key = "annualReports" if frequency == "annual" else "quarterlyReports"
            statements = data.get(key, [])
            
            # Filter by date range
            filtered_statements = self._filter_statements_by_date(
                statements, end_date, start_date, lookback_periods
            )
            
            dict_response = {
                "symbol": symbol.upper(),
                "frequency": frequency,
                "start_date": start_date.isoformat() if start_date else None,
                "end_date": end_date.isoformat(),
                "statements": filtered_statements
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
            logger.error(f"Failed to get income statement for {symbol}: {e}")
            raise
    
    @log_provider_call
    def get_cashflow_statement(
        self,
        symbol: Annotated[str, "Stock ticker symbol"],
        frequency: Annotated[Literal["annual", "quarterly"], "Reporting frequency"],
        end_date: Annotated[datetime, "End date for statement range (inclusive)"],
        start_date: Annotated[Optional[datetime], "Start date for statement range (mutually exclusive with lookback_periods)"] = None,
        lookback_periods: Annotated[Optional[int], "Number of periods to look back from end_date (mutually exclusive with start_date)"] = None,
        format_type: Literal["dict", "markdown", "both"] = "markdown"
    ) -> Dict[str, Any] | str:
        """
        Get cash flow statement(s) for a company.
        
        Args:
            symbol: Stock ticker symbol
            frequency: Annual or quarterly reporting
            end_date: End date (inclusive)
            start_date: Start date (use either this OR lookback_periods, not both)
            lookback_periods: Number of periods to look back (use either this OR start_date, not both)
            format_type: Output format - 'dict' for structured data, 'markdown' for text
            
        Returns:
            Cash flow statement with operating, investing, and financing activities
        """
        # Validate inputs
        if start_date and lookback_periods:
            raise ValueError("Provide either start_date OR lookback_periods, not both")
        if not start_date and not lookback_periods:
            raise ValueError("Must provide either start_date or lookback_periods")
        
        logger.debug(f"Fetching cash flow for {symbol} (frequency: {frequency})")
        
        try:
            params = {"symbol": symbol}
            result = make_api_request("CASH_FLOW", params)
            
            # Build dict response (always build it for "both" format support)
            data = json.loads(result)
            
            # Filter statements based on frequency and date range
            key = "annualReports" if frequency == "annual" else "quarterlyReports"
            statements = data.get(key, [])
            
            # Filter by date range
            filtered_statements = self._filter_statements_by_date(
                statements, end_date, start_date, lookback_periods
            )
            
            dict_response = {
                "symbol": symbol.upper(),
                "frequency": frequency,
                "start_date": start_date.isoformat() if start_date else None,
                "end_date": end_date.isoformat(),
                "statements": filtered_statements
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
                # Return as markdown
                return result
                
        except Exception as e:
            logger.error(f"Failed to get cash flow for {symbol}: {e}")
            raise
    
    def _filter_statements_by_date(
        self,
        statements: list,
        end_date: datetime,
        start_date: Optional[datetime] = None,
        lookback_periods: Optional[int] = None
    ) -> list:
        """
        Filter statements by date range or lookback periods.
        
        Args:
            statements: List of statement dicts from Alpha Vantage
            end_date: End date for filtering
            start_date: Start date for filtering (optional)
            lookback_periods: Number of periods to include (optional)
            
        Returns:
            Filtered list of statements
        """
        # Parse and sort statements by date (most recent first)
        parsed_statements = []
        for stmt in statements:
            fiscal_date = stmt.get("fiscalDateEnding")
            if fiscal_date:
                try:
                    date_obj = datetime.strptime(fiscal_date, "%Y-%m-%d")
                    if date_obj <= end_date:
                        parsed_statements.append((date_obj, stmt))
                except ValueError:
                    logger.warning(f"Could not parse fiscal date: {fiscal_date}")
        
        # Sort by date (most recent first)
        parsed_statements.sort(key=lambda x: x[0], reverse=True)
        
        # Filter by lookback_periods or start_date
        if lookback_periods:
            filtered = parsed_statements[:lookback_periods]
        else:
            filtered = [(date_obj, stmt) for date_obj, stmt in parsed_statements if date_obj >= start_date]
        
        # Return just the statements (without dates)
        return [stmt for _, stmt in filtered]

