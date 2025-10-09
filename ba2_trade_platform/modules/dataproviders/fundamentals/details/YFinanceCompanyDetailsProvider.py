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
    
    def get_supported_features(self) -> Dict[str, Any]:
        """Get supported features of this provider."""
        return {
            "balance_sheet": True,
            "income_statement": True,
            "cashflow_statement": True,
            "frequencies": ["quarterly", "annual"],
            "historical_periods": "Typically 4 quarters or 4 annual periods",
            "rate_limits": {
                "requests_per_minute": 2000,  # Very generous for free tier
                "notes": "Yahoo Finance is generally lenient with rate limits"
            }
        }
    
    @log_provider_call
    def get_balance_sheet(
        self,
        symbol: str,
        frequency: Literal["quarterly", "annual"] = "quarterly",
        format_type: Literal["dict", "markdown", "both"] = "markdown"
    ) -> Dict[str, Any] | str:
        """
        Get balance sheet data for a company.
        
        Args:
            symbol: Stock ticker symbol
            frequency: Data frequency ('quarterly' or 'annual')
            format_type: Output format ('dict', 'markdown', or 'both')
        
        Returns:
            Balance sheet data in requested format
        """
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
                "periods": [],
                "retrieved_at": datetime.now().isoformat()
            }
            
            # Add each period (column) as a separate entry
            for period_date in data.columns:
                period_data = {
                    "date": period_date.strftime("%Y-%m-%d") if hasattr(period_date, 'strftime') else str(period_date),
                    "items": {}
                }
                
                for item_name in data.index:
                    value = data.loc[item_name, period_date]
                    if pd.notna(value):
                        period_data["items"][str(item_name)] = float(value)
                
                result["periods"].append(period_data)
            
            logger.info(f"Retrieved balance sheet for {symbol}: {len(result['periods'])} periods")
            
            # Format output
            if format_type == "dict":
                return result
            elif format_type == "both":
                return {
                    "text": self._format_balance_sheet_markdown(result),
                    "data": result
                }
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
        format_type: Literal["dict", "markdown", "both"] = "markdown"
    ) -> Dict[str, Any] | str:
        """
        Get income statement data for a company.
        
        Args:
            symbol: Stock ticker symbol
            frequency: Data frequency ('quarterly' or 'annual')
            format_type: Output format ('dict', 'markdown', or 'both')
        
        Returns:
            Income statement data in requested format
        """
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
                "periods": [],
                "retrieved_at": datetime.now().isoformat()
            }
            
            # Add each period (column) as a separate entry
            for period_date in data.columns:
                period_data = {
                    "date": period_date.strftime("%Y-%m-%d") if hasattr(period_date, 'strftime') else str(period_date),
                    "items": {}
                }
                
                for item_name in data.index:
                    value = data.loc[item_name, period_date]
                    if pd.notna(value):
                        period_data["items"][str(item_name)] = float(value)
                
                result["periods"].append(period_data)
            
            logger.info(f"Retrieved income statement for {symbol}: {len(result['periods'])} periods")
            
            # Format output
            if format_type == "dict":
                return result
            elif format_type == "both":
                return {
                    "text": self._format_income_statement_markdown(result),
                    "data": result
                }
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
        format_type: Literal["dict", "markdown", "both"] = "markdown"
    ) -> Dict[str, Any] | str:
        """
        Get cash flow statement data for a company.
        
        Args:
            symbol: Stock ticker symbol
            frequency: Data frequency ('quarterly' or 'annual')
            format_type: Output format ('dict', 'markdown', or 'both')
        
        Returns:
            Cash flow statement data in requested format
        """
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
                "periods": [],
                "retrieved_at": datetime.now().isoformat()
            }
            
            # Add each period (column) as a separate entry
            for period_date in data.columns:
                period_data = {
                    "date": period_date.strftime("%Y-%m-%d") if hasattr(period_date, 'strftime') else str(period_date),
                    "items": {}
                }
                
                for item_name in data.index:
                    value = data.loc[item_name, period_date]
                    if pd.notna(value):
                        period_data["items"][str(item_name)] = float(value)
                
                result["periods"].append(period_data)
            
            logger.info(f"Retrieved cash flow statement for {symbol}: {len(result['periods'])} periods")
            
            # Format output
            if format_type == "dict":
                return result
            elif format_type == "both":
                return {
                    "text": self._format_cashflow_markdown(result),
                    "data": result
                }
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
