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
from ba2_trade_platform.modules.dataproviders.alpha_vantage_common import (
    AlphaVantageBaseProvider,
    AlphaVantageRateLimitError
)


class AlphaVantageCompanyDetailsProvider(AlphaVantageBaseProvider, CompanyFundamentalsDetailsInterface):
    """
    Alpha Vantage company details provider.
    
    Provides detailed financial statements including balance sheets,
    income statements, and cash flow statements from Alpha Vantage API.
    """
    
    
    API_BASE_URL = "https://www.alphavantage.co/query"
    
    def __init__(self, source: str = "ba2_trade_platform"):
        """
        Initialize Alpha Vantage company details provider.
        
        Args:
            source: Source identifier for API tracking (e.g., 'ba2_trade_platform', 'trading_agents')
        """
        AlphaVantageBaseProvider.__init__(self, source)
        CompanyFundamentalsDetailsInterface.__init__(self)
        self.api_key = get_app_setting("alpha_vantage_api_key")
        if not self.api_key:
            raise ValueError("Alpha Vantage API key not configured. Please set 'alpha_vantage_api_key' in app settings.")
        logger.debug(f"Initialized AlphaVantageCompanyDetailsProvider with source: {source}")
    
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
            result = self.make_api_request("BALANCE_SHEET", params)
            
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
            result = self.make_api_request("INCOME_STATEMENT", params)
            
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
            result = self.make_api_request("CASH_FLOW", params)
            
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

    def get_provider_name(self) -> str:
        """Get the provider name."""
        return "alphavantage"
    
    def get_supported_features(self) -> list[str]:
        """Get supported features of this provider."""
        return ["balance_sheet", "income_statement", "cashflow_statement", "past_earnings", "earnings_estimates"]
    
    def validate_config(self) -> bool:
        """
        Validate provider configuration.
        
        Returns:
            bool: True if configuration is valid
        """
        # AlphaVantage API key validated by alpha_vantage_common module
        return True
    
    def _format_as_dict(self, data: Any) -> Dict[str, Any]:
        """
        Format data as a structured dictionary.
        
        Args:
            data: Provider data
            
        Returns:
            Dict[str, Any]: Structured dictionary
        """
        if isinstance(data, dict):
            return data
        return {"data": data}
    
    def _format_as_markdown(self, data: Any) -> str:
        """
        Format data as markdown for LLM consumption.
        
        Args:
            data: Provider data
            
        Returns:
            str: Markdown-formatted string
        """
        if isinstance(data, dict):
            md = "# Data\n\n"
            for key, value in data.items():
                if isinstance(value, (list, dict)):
                    md += f"**{key}**: (complex data)\n"
                else:
                    md += f"**{key}**: {value}\n"
            return md
        return str(data)
    
    @log_provider_call
    def get_past_earnings(
        self,
        symbol: Annotated[str, "Stock ticker symbol"],
        frequency: Annotated[Literal["annual", "quarterly"], "Reporting frequency"],
        end_date: Annotated[datetime, "End date for earnings range (inclusive)"],
        lookback_periods: Annotated[int, "Number of periods to look back from end_date"] = 8,
        format_type: Literal["dict", "markdown"] = "markdown"
    ) -> Dict[str, Any] | str:
        """
        Get historical earnings data for a company from Alpha Vantage.
        
        Uses Alpha Vantage's EARNINGS function which provides quarterly and annual earnings.
        
        Args:
            symbol: Stock ticker symbol
            frequency: Data frequency ('quarterly' or 'annual')
            end_date: End date (inclusive) - gets most recent earnings as of this date
            lookback_periods: Number of periods to look back (default 8 quarters = 2 years)
            format_type: Output format ('dict' or 'markdown')
        
        Returns:
            Historical earnings data in requested format
        """
        logger.debug(f"Fetching AlphaVantage past earnings for {symbol} ({frequency})")
        
        try:
            # Call Alpha Vantage EARNINGS endpoint
            params = {"symbol": symbol}
            result = self.make_api_request("EARNINGS", params)
            
            data = json.loads(result)
            
            # Determine which earnings data to use based on frequency
            key = "annualEarnings" if frequency == "annual" else "quarterlyEarnings"
            earnings_list = data.get(key, [])
            
            if not earnings_list:
                logger.warning(f"No earnings data found for {symbol}")
                if format_type == "dict":
                    return {
                        "symbol": symbol,
                        "frequency": frequency,
                        "end_date": end_date.isoformat(),
                        "lookback_periods": lookback_periods,
                        "earnings": [],
                        "retrieved_at": datetime.now().isoformat()
                    }
                else:
                    return f"# Past Earnings for {symbol}\n\nNo earnings data available.\n"
            
            # Filter and process earnings data
            result_dict = {
                "symbol": symbol.upper(),
                "frequency": frequency,
                "end_date": end_date.isoformat(),
                "lookback_periods": lookback_periods,
                "earnings": [],
                "retrieved_at": datetime.now().isoformat()
            }
            
            filtered_earnings = []
            for earning in earnings_list:
                # Parse the fiscal date ending
                fiscal_date_str = earning.get("fiscalDateEnding", "")
                if not fiscal_date_str:
                    continue
                
                try:
                    fiscal_date = datetime.strptime(fiscal_date_str, "%Y-%m-%d")
                except:
                    continue
                
                # Filter by end_date
                if fiscal_date > end_date:
                    continue
                
                # AlphaVantage provides reportedEPS and estimatedEPS in EARNINGS endpoint
                reported_eps = earning.get("reportedEPS", "None")
                estimated_eps = earning.get("estimatedEPS", "None")
                
                # Handle "None" string values from API
                reported_eps_val = float(reported_eps) if reported_eps and reported_eps != "None" else 0
                estimated_eps_val = float(estimated_eps) if estimated_eps and estimated_eps != "None" else 0
                
                earnings_entry = {
                    "fiscal_date_ending": fiscal_date.strftime("%Y-%m-%d"),
                    "report_date": earning.get("reportedDate", fiscal_date_str),
                    "reported_eps": reported_eps_val,
                    "estimated_eps": estimated_eps_val
                }
                
                # Calculate surprise if both values available
                if reported_eps_val and estimated_eps_val:
                    earnings_entry["surprise"] = reported_eps_val - estimated_eps_val
                    if estimated_eps_val != 0:
                        earnings_entry["surprise_percent"] = (earnings_entry["surprise"] / abs(estimated_eps_val)) * 100
                    else:
                        earnings_entry["surprise_percent"] = 0
                else:
                    earnings_entry["surprise"] = None
                    earnings_entry["surprise_percent"] = None
                
                filtered_earnings.append((fiscal_date, earnings_entry))
            
            # Sort by date descending (most recent first)
            filtered_earnings.sort(key=lambda x: x[0], reverse=True)
            
            # Apply lookback_periods limit
            filtered_earnings = filtered_earnings[:lookback_periods]
            
            # Extract just the earnings data
            result_dict["earnings"] = [e[1] for e in filtered_earnings]
            
            logger.info(f"Retrieved {len(result_dict['earnings'])} past earnings periods for {symbol}")
            
            # Format output
            if format_type == "dict":
                return result_dict
            else:
                return self._format_past_earnings_markdown(result_dict)
        
        except Exception as e:
            logger.error(f"Error retrieving past earnings for {symbol}: {e}")
            if format_type == "dict":
                return {"error": str(e), "symbol": symbol}
            return f"Error retrieving past earnings for {symbol}: {str(e)}"
    
    @log_provider_call
    def get_earnings_estimates(
        self,
        symbol: Annotated[str, "Stock ticker symbol"],
        frequency: Annotated[Literal["annual", "quarterly"], "Reporting frequency"],
        as_of_date: Annotated[datetime, "Date for estimates (uses most recent estimates as of this date)"],
        lookback_periods: Annotated[int, "Number of future periods to retrieve estimates for"] = 4,
        format_type: Literal["dict", "markdown"] = "markdown"
    ) -> Dict[str, Any] | str:
        """
        Get future earnings estimates for a company from Alpha Vantage.
        
        Note: Alpha Vantage's EARNINGS endpoint provides limited forward-looking estimates.
        This method attempts to extract future estimates from the earnings data.
        
        Args:
            symbol: Stock ticker symbol
            frequency: Data frequency ('quarterly' or 'annual')
            as_of_date: Date for estimates (returns most recent estimates as of this date)
            lookback_periods: Number of future periods to retrieve estimates for (default 4)
            format_type: Output format ('dict' or 'markdown')
        
        Returns:
            Future earnings estimates in requested format (limited data from Alpha Vantage)
        """
        logger.debug(f"Fetching AlphaVantage earnings estimates for {symbol} ({frequency})")
        
        try:
            # Call Alpha Vantage EARNINGS endpoint
            params = {"symbol": symbol}
            result = self.make_api_request("EARNINGS", params)
            
            data = json.loads(result)
            
            # Determine which earnings data to use based on frequency
            key = "annualEarnings" if frequency == "annual" else "quarterlyEarnings"
            earnings_list = data.get(key, [])
            
            if not earnings_list:
                logger.warning(f"No earnings estimates found for {symbol}")
                if format_type == "dict":
                    return {
                        "symbol": symbol,
                        "frequency": frequency,
                        "as_of_date": as_of_date.isoformat(),
                        "estimates": [],
                        "retrieved_at": datetime.now().isoformat()
                    }
                else:
                    return f"# Earnings Estimates for {symbol}\n\nNo estimates available.\n"
            
            # Filter and process estimates data
            result_dict = {
                "symbol": symbol.upper(),
                "frequency": frequency,
                "as_of_date": as_of_date.isoformat(),
                "estimates": [],
                "retrieved_at": datetime.now().isoformat()
            }
            
            filtered_estimates = []
            for earning in earnings_list:
                # Parse the fiscal date ending
                fiscal_date_str = earning.get("fiscalDateEnding", "")
                if not fiscal_date_str:
                    continue
                
                try:
                    fiscal_date = datetime.strptime(fiscal_date_str, "%Y-%m-%d")
                except:
                    continue
                
                # Only include future dates (after as_of_date)
                if fiscal_date < as_of_date:
                    continue
                
                # Extract estimated EPS (Alpha Vantage provides this in EARNINGS endpoint)
                estimated_eps = earning.get("estimatedEPS", "None")
                
                # Handle "None" string values from API
                if estimated_eps and estimated_eps != "None":
                    estimated_eps_val = float(estimated_eps)
                else:
                    # Skip entries without estimates
                    continue
                
                estimate_entry = {
                    "fiscal_date_ending": fiscal_date.strftime("%Y-%m-%d"),
                    "estimated_eps_avg": estimated_eps_val,
                    "estimated_eps_high": estimated_eps_val,  # Alpha Vantage doesn't provide high/low, use avg
                    "estimated_eps_low": estimated_eps_val,
                    "number_of_analysts": 0  # Alpha Vantage doesn't provide analyst count
                }
                
                filtered_estimates.append((fiscal_date, estimate_entry))
            
            # Sort by date ascending (earliest future period first)
            filtered_estimates.sort(key=lambda x: x[0])
            
            # Apply lookback_periods limit (here it means "forward periods")
            filtered_estimates = filtered_estimates[:lookback_periods]
            
            # Extract just the estimates data
            result_dict["estimates"] = [e[1] for e in filtered_estimates]
            
            logger.info(f"Retrieved {len(result_dict['estimates'])} earnings estimates for {symbol}")
            
            # Format output
            if format_type == "dict":
                return result_dict
            else:
                return self._format_earnings_estimates_markdown(result_dict)
        
        except Exception as e:
            logger.error(f"Error retrieving earnings estimates for {symbol}: {e}")
            if format_type == "dict":
                return {"error": str(e), "symbol": symbol}
            return f"Error retrieving earnings estimates for {symbol}: {str(e)}"
    
    def _format_past_earnings_markdown(self, data: Dict[str, Any]) -> str:
        """Format past earnings data as markdown."""
        md = f"# Past Earnings: {data['symbol']} ({data['frequency']})\n\n"
        md += f"**Retrieved**: {data['retrieved_at']}\n"
        md += f"**Lookback Periods**: {data['lookback_periods']}\n\n"
        
        if "error" in data:
            md += f"**Error**: {data['error']}\n"
            return md
        
        if not data["earnings"]:
            md += "*No earnings data available*\n"
            return md
        
        md += "| Date | Reported EPS | Estimated EPS | Surprise | Surprise % |\n"
        md += "|------|--------------|---------------|----------|------------|\n"
        
        for earning in data["earnings"]:
            date = earning["fiscal_date_ending"]
            reported = f"${earning['reported_eps']:.2f}" if earning["reported_eps"] else "N/A"
            estimated = f"${earning['estimated_eps']:.2f}" if earning["estimated_eps"] else "N/A"
            surprise = f"${earning['surprise']:.2f}" if earning["surprise"] is not None else "N/A"
            surprise_pct = f"{earning['surprise_percent']:.1f}%" if earning["surprise_percent"] is not None else "N/A"
            
            md += f"| {date} | {reported} | {estimated} | {surprise} | {surprise_pct} |\n"
        
        return md
    
    def _format_earnings_estimates_markdown(self, data: Dict[str, Any]) -> str:
        """Format earnings estimates data as markdown."""
        md = f"# Earnings Estimates: {data['symbol']} ({data['frequency']})\n\n"
        md += f"**Retrieved**: {data['retrieved_at']}\n"
        md += f"**As of Date**: {data['as_of_date']}\n\n"
        
        if "error" in data:
            md += f"**Error**: {data['error']}\n"
            return md
        
        if not data["estimates"]:
            md += "*No earnings estimates available*\n"
            return md
        
        md += "| Date | Avg Estimate | High Estimate | Low Estimate | # Analysts |\n"
        md += "|------|--------------|---------------|--------------|------------|\n"
        
        for estimate in data["estimates"]:
            date = estimate["fiscal_date_ending"]
            avg = f"${estimate['estimated_eps_avg']:.2f}" if estimate["estimated_eps_avg"] else "N/A"
            high = f"${estimate['estimated_eps_high']:.2f}" if estimate["estimated_eps_high"] else "N/A"
            low = f"${estimate['estimated_eps_low']:.2f}" if estimate["estimated_eps_low"] else "N/A"
            analysts = estimate["number_of_analysts"]
            
            md += f"| {date} | {avg} | {high} | {low} | {analysts} |\n"
        
        return md

