"""
YFinance Company Details Provider

Provides detailed financial statements (balance sheet, income statement, cash flow)
using Yahoo Finance as the data source.
"""

from typing import Dict, Any, Literal, Optional
from datetime import datetime
import yfinance as yf
import pandas as pd

from ba2_trade_platform.core.interfaces import CompanyFundamentalsDetailsInterface
from ba2_trade_platform.core.provider_utils import log_provider_call
from ba2_trade_platform.logger import logger


class YFinanceCompanyDetailsProvider(CompanyFundamentalsDetailsInterface):
    """
    Yahoo Finance company financial details provider.
    
    Provides access to:
    - Balance sheets (quarterly and annual)
    - Income statements (quarterly and annual)
    - Cash flow statements (quarterly and annual)
    
    Data is retrieved directly from Yahoo Finance API via yfinance library.
    """
    
    def __init__(self):
        """Initialize YFinance company details provider."""
        super().__init__()
        logger.info("YFinanceCompanyDetailsProvider initialized successfully")
    
    def get_provider_name(self) -> str:
        """Get the provider name."""
        return "yfinance"
    
    def get_supported_features(self) -> list[str]:
        """Get supported features of this provider."""
        return ["balance_sheet", "income_statement", "cashflow_statement"]
    
    def validate_config(self) -> bool:
        """
        Validate provider configuration.
        
        YFinance doesn't require API keys, so always returns True.
        
        Returns:
            bool: Always True (no configuration needed)
        """
        return True
    
    def _format_as_dict(self, data: Any) -> Dict[str, Any]:
        """
        Format data as a structured dictionary.
        
        Args:
            data: Financial statement data
            
        Returns:
            Dict[str, Any]: Structured dictionary
        """
        # Data is already in dict format from our methods
        if isinstance(data, dict):
            return data
        return {"data": data}
    
    def _format_as_markdown(self, data: Any) -> str:
        """
        Format data as markdown for LLM consumption.
        
        Args:
            data: Financial statement data (dict format)
            
        Returns:
            str: Markdown-formatted string
        """
        if not isinstance(data, dict):
            return str(data)
        
        # Determine statement type and use appropriate formatter
        statement_type = data.get("statement_type", "unknown")
        
        if statement_type == "balance_sheet":
            return self._format_balance_sheet_markdown(data)
        elif statement_type == "income_statement":
            return self._format_income_statement_markdown(data)
        elif statement_type == "cashflow":
            return self._format_cashflow_markdown(data)
        else:
            # Generic markdown formatting
            md = f"# Financial Data\n\n"
            for key, value in data.items():
                md += f"**{key}**: {value}\n"
            return md
    
    @log_provider_call
    def get_balance_sheet(
        self,
        symbol: str,
        frequency: Literal["quarterly", "annual"] = "quarterly",
        end_date: datetime = None,
        start_date: Optional[datetime] = None,
        lookback_periods: Optional[int] = None,
        format_type: Literal["dict", "markdown"] = "markdown"
    ) -> Dict[str, Any] | str:
        """
        Get balance sheet data for a company.
        
        Args:
            symbol: Stock ticker symbol
            frequency: Data frequency ('quarterly' or 'annual')
            end_date: End date (inclusive) - gets most recent statement as of this date
            start_date: Start date (use either this OR lookback_periods, not both)
            lookback_periods: Number of periods to look back (use either this OR start_date, not both)
            format_type: Output format ('dict' or 'markdown')
        
        Returns:
            Balance sheet data in requested format
        
        Raises:
            ValueError: If both start_date and lookback_periods are provided, or if neither is provided
        """
        # Validate date range parameters
        if start_date is not None and lookback_periods is not None:
            raise ValueError("Cannot specify both start_date and lookback_periods")
        if start_date is None and lookback_periods is None:
            raise ValueError("Must specify either start_date or lookback_periods")
        
        # Default end_date to now if not provided
        if end_date is None:
            end_date = datetime.now()
        
        try:
            ticker_obj = yf.Ticker(symbol.upper())
            
            if frequency.lower() == "quarterly":
                data = ticker_obj.quarterly_balance_sheet
            else:
                data = ticker_obj.balance_sheet
                
            if data.empty:
                error_msg = f"No balance sheet data found for symbol '{symbol}'"
                logger.warning(error_msg)
                if format_type == "dict":
                    return {"error": error_msg, "symbol": symbol}
                return error_msg
            
            # Convert DataFrame to dict format
            result = {
                "symbol": symbol.upper(),
                "frequency": frequency,
                "statement_type": "balance_sheet",
                "start_date": start_date.isoformat() if start_date else None,
                "end_date": end_date.isoformat(),
                "periods": [],
                "retrieved_at": datetime.now().isoformat()
            }
            
            # Add each period (column) as a separate entry
            filtered_periods = []
            for period_date in data.columns:
                # Convert to datetime for comparison
                if hasattr(period_date, 'to_pydatetime'):
                    period_dt = period_date.to_pydatetime()
                elif hasattr(period_date, 'date'):
                    period_dt = datetime.combine(period_date.date(), datetime.min.time())
                else:
                    continue
                
                # Filter by date range
                if period_dt > end_date:
                    continue
                if start_date and period_dt < start_date:
                    continue
                
                period_data = {
                    "date": period_dt.strftime("%Y-%m-%d"),
                    "items": {}
                }
                
                for item_name in data.index:
                    value = data.loc[item_name, period_date]
                    if pd.notna(value):
                        period_data["items"][str(item_name)] = float(value)
                
                filtered_periods.append((period_dt, period_data))
            
            # Sort by date descending (most recent first)
            filtered_periods.sort(key=lambda x: x[0], reverse=True)
            
            # Apply lookback_periods if specified
            if lookback_periods is not None:
                filtered_periods = filtered_periods[:lookback_periods]
            
            # Extract just the period data (without datetime used for sorting)
            result["periods"] = [p[1] for p in filtered_periods]
            
            # Update start_date if using lookback_periods
            if lookback_periods is not None and result["periods"]:
                result["start_date"] = result["periods"][-1]["date"]
            
            logger.info(f"Retrieved balance sheet for {symbol}: {len(result['periods'])} periods")
            
            # Format output
            if format_type == "dict":
                return result
            else:  # markdown
                return self._format_balance_sheet_markdown(result)
                
        except Exception as e:
            logger.error(f"Error retrieving balance sheet for {symbol}: {e}")
            if format_type == "dict":
                return {"error": str(e), "symbol": symbol}
            return f"Error retrieving balance sheet for {symbol}: {str(e)}"
    
    @log_provider_call
    def get_income_statement(
        self,
        symbol: str,
        frequency: Literal["quarterly", "annual"] = "quarterly",
        end_date: datetime = None,
        start_date: Optional[datetime] = None,
        lookback_periods: Optional[int] = None,
        format_type: Literal["dict", "markdown"] = "markdown"
    ) -> Dict[str, Any] | str:
        """
        Get income statement data for a company.
        
        Args:
            symbol: Stock ticker symbol
            frequency: Data frequency ('quarterly' or 'annual')
            end_date: End date (inclusive) - gets most recent statement as of this date
            start_date: Start date (use either this OR lookback_periods, not both)
            lookback_periods: Number of periods to look back (use either this OR start_date, not both)
            format_type: Output format ('dict' or 'markdown')
        
        Returns:
            Income statement data in requested format
        
        Raises:
            ValueError: If both start_date and lookback_periods are provided, or if neither is provided
        """
        # Validate date range parameters
        if start_date is not None and lookback_periods is not None:
            raise ValueError("Cannot specify both start_date and lookback_periods")
        if start_date is None and lookback_periods is None:
            raise ValueError("Must specify either start_date or lookback_periods")
        
        # Default end_date to now if not provided
        if end_date is None:
            end_date = datetime.now()
        
        try:
            ticker_obj = yf.Ticker(symbol.upper())
            
            if frequency.lower() == "quarterly":
                data = ticker_obj.quarterly_income_stmt
            else:
                data = ticker_obj.income_stmt
                
            if data.empty:
                error_msg = f"No income statement data found for symbol '{symbol}'"
                logger.warning(error_msg)
                if format_type == "dict":
                    return {"error": error_msg, "symbol": symbol}
                return error_msg
            
            # Convert DataFrame to dict format
            result = {
                "symbol": symbol.upper(),
                "frequency": frequency,
                "statement_type": "income_statement",
                "start_date": start_date.isoformat() if start_date else None,
                "end_date": end_date.isoformat(),
                "periods": [],
                "retrieved_at": datetime.now().isoformat()
            }
            
            # Add each period (column) as a separate entry
            filtered_periods = []
            for period_date in data.columns:
                # Convert to datetime for comparison
                if hasattr(period_date, 'to_pydatetime'):
                    period_dt = period_date.to_pydatetime()
                elif hasattr(period_date, 'date'):
                    period_dt = datetime.combine(period_date.date(), datetime.min.time())
                else:
                    continue
                
                # Filter by date range
                if period_dt > end_date:
                    continue
                if start_date and period_dt < start_date:
                    continue
                
                period_data = {
                    "date": period_dt.strftime("%Y-%m-%d"),
                    "items": {}
                }
                
                for item_name in data.index:
                    value = data.loc[item_name, period_date]
                    if pd.notna(value):
                        period_data["items"][str(item_name)] = float(value)
                
                filtered_periods.append((period_dt, period_data))
            
            # Sort by date descending (most recent first)
            filtered_periods.sort(key=lambda x: x[0], reverse=True)
            
            # Apply lookback_periods if specified
            if lookback_periods is not None:
                filtered_periods = filtered_periods[:lookback_periods]
            
            # Extract just the period data (without datetime used for sorting)
            result["periods"] = [p[1] for p in filtered_periods]
            
            # Update start_date if using lookback_periods
            if lookback_periods is not None and result["periods"]:
                result["start_date"] = result["periods"][-1]["date"]
            
            logger.info(f"Retrieved income statement for {symbol}: {len(result['periods'])} periods")
            
            # Format output
            if format_type == "dict":
                return result
            else:  # markdown
                return self._format_income_statement_markdown(result)
                
        except Exception as e:
            logger.error(f"Error retrieving income statement for {symbol}: {e}")
            if format_type == "dict":
                return {"error": str(e), "symbol": symbol}
            return f"Error retrieving income statement for {symbol}: {str(e)}"
    
    @log_provider_call
    def get_cashflow_statement(
        self,
        symbol: str,
        frequency: Literal["quarterly", "annual"] = "quarterly",
        end_date: datetime = None,
        start_date: Optional[datetime] = None,
        lookback_periods: Optional[int] = None,
        format_type: Literal["dict", "markdown"] = "markdown"
    ) -> Dict[str, Any] | str:
        """
        Get cash flow statement data for a company.
        
        Args:
            symbol: Stock ticker symbol
            frequency: Data frequency ('quarterly' or 'annual')
            end_date: End date (inclusive) - gets most recent statement as of this date
            start_date: Start date (use either this OR lookback_periods, not both)
            lookback_periods: Number of periods to look back (use either this OR start_date, not both)
            format_type: Output format ('dict' or 'markdown')
        
        Returns:
            Cash flow statement data in requested format
        
        Raises:
            ValueError: If both start_date and lookback_periods are provided, or if neither is provided
        """
        # Validate date range parameters
        if start_date is not None and lookback_periods is not None:
            raise ValueError("Cannot specify both start_date and lookback_periods")
        if start_date is None and lookback_periods is None:
            raise ValueError("Must specify either start_date or lookback_periods")
        
        # Default end_date to now if not provided
        if end_date is None:
            end_date = datetime.now()
        
        try:
            ticker_obj = yf.Ticker(symbol.upper())
            
            if frequency.lower() == "quarterly":
                data = ticker_obj.quarterly_cashflow
            else:
                data = ticker_obj.cashflow
                
            if data.empty:
                error_msg = f"No cash flow data found for symbol '{symbol}'"
                logger.warning(error_msg)
                if format_type == "dict":
                    return {"error": error_msg, "symbol": symbol}
                return error_msg
            
            # Convert DataFrame to dict format
            result = {
                "symbol": symbol.upper(),
                "frequency": frequency,
                "statement_type": "cashflow",
                "start_date": start_date.isoformat() if start_date else None,
                "end_date": end_date.isoformat(),
                "periods": [],
                "retrieved_at": datetime.now().isoformat()
            }
            
            # Add each period (column) as a separate entry
            filtered_periods = []
            for period_date in data.columns:
                # Convert to datetime for comparison
                if hasattr(period_date, 'to_pydatetime'):
                    period_dt = period_date.to_pydatetime()
                elif hasattr(period_date, 'date'):
                    period_dt = datetime.combine(period_date.date(), datetime.min.time())
                else:
                    continue
                
                # Filter by date range
                if period_dt > end_date:
                    continue
                if start_date and period_dt < start_date:
                    continue
                
                period_data = {
                    "date": period_dt.strftime("%Y-%m-%d"),
                    "items": {}
                }
                
                for item_name in data.index:
                    value = data.loc[item_name, period_date]
                    if pd.notna(value):
                        period_data["items"][str(item_name)] = float(value)
                
                filtered_periods.append((period_dt, period_data))
            
            # Sort by date descending (most recent first)
            filtered_periods.sort(key=lambda x: x[0], reverse=True)
            
            # Apply lookback_periods if specified
            if lookback_periods is not None:
                filtered_periods = filtered_periods[:lookback_periods]
            
            # Extract just the period data (without datetime used for sorting)
            result["periods"] = [p[1] for p in filtered_periods]
            
            # Update start_date if using lookback_periods
            if lookback_periods is not None and result["periods"]:
                result["start_date"] = result["periods"][-1]["date"]
            
            logger.info(f"Retrieved cash flow statement for {symbol}: {len(result['periods'])} periods")
            
            # Format output
            if format_type == "dict":
                return result
            else:  # markdown
                return self._format_cashflow_markdown(result)
                
        except Exception as e:
            logger.error(f"Error retrieving cash flow for {symbol}: {e}")
            if format_type == "dict":
                return {"error": str(e), "symbol": symbol}
            return f"Error retrieving cash flow for {symbol}: {str(e)}"
    
    def _format_balance_sheet_markdown(self, data: Dict[str, Any]) -> str:
        """Format balance sheet data as markdown."""
        md = f"# Balance Sheet: {data['symbol']} ({data['frequency']})\n\n"
        md += f"**Retrieved**: {data['retrieved_at']}\n\n"
        
        if "error" in data:
            md += f"**Error**: {data['error']}\n"
            return md
        
        for period in data["periods"]:
            md += f"## Period: {period['date']}\n\n"
            
            # Group items by category
            assets = {k: v for k, v in period["items"].items() if "Asset" in k or "Cash" in k or "Receivable" in k}
            liabilities = {k: v for k, v in period["items"].items() if "Liability" in k or "Debt" in k or "Payable" in k}
            equity = {k: v for k, v in period["items"].items() if "Equity" in k or "Stock" in k}
            
            if assets:
                md += "### Assets\n"
                for key, value in assets.items():
                    md += f"- **{key}**: ${value:,.0f}\n"
                md += "\n"
            
            if liabilities:
                md += "### Liabilities\n"
                for key, value in liabilities.items():
                    md += f"- **{key}**: ${value:,.0f}\n"
                md += "\n"
            
            if equity:
                md += "### Equity\n"
                for key, value in equity.items():
                    md += f"- **{key}**: ${value:,.0f}\n"
                md += "\n"
        
        return md
    
    def _format_income_statement_markdown(self, data: Dict[str, Any]) -> str:
        """Format income statement data as markdown."""
        md = f"# Income Statement: {data['symbol']} ({data['frequency']})\n\n"
        md += f"**Retrieved**: {data['retrieved_at']}\n\n"
        
        if "error" in data:
            md += f"**Error**: {data['error']}\n"
            return md
        
        for period in data["periods"]:
            md += f"## Period: {period['date']}\n\n"
            
            # Key metrics
            for key, value in period["items"].items():
                md += f"- **{key}**: ${value:,.0f}\n"
            md += "\n"
        
        return md
    
    def _format_cashflow_markdown(self, data: Dict[str, Any]) -> str:
        """Format cash flow data as markdown."""
        md = f"# Cash Flow Statement: {data['symbol']} ({data['frequency']})\n\n"
        md += f"**Retrieved**: {data['retrieved_at']}\n\n"
        
        if "error" in data:
            md += f"**Error**: {data['error']}\n"
            return md
        
        for period in data["periods"]:
            md += f"## Period: {period['date']}\n\n"
            
            # Group by cash flow categories
            operating = {k: v for k, v in period["items"].items() if "Operating" in k}
            investing = {k: v for k, v in period["items"].items() if "Investing" in k}
            financing = {k: v for k, v in period["items"].items() if "Financing" in k}
            
            if operating:
                md += "### Operating Activities\n"
                for key, value in operating.items():
                    md += f"- **{key}**: ${value:,.0f}\n"
                md += "\n"
            
            if investing:
                md += "### Investing Activities\n"
                for key, value in investing.items():
                    md += f"- **{key}**: ${value:,.0f}\n"
                md += "\n"
            
            if financing:
                md += "### Financing Activities\n"
                for key, value in financing.items():
                    md += f"- **{key}**: ${value:,.0f}\n"
                md += "\n"
        
        return md
