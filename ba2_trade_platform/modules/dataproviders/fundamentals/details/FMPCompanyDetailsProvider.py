"""
Financial Modeling Prep (FMP) Company Fundamentals Details Provider

This provider uses the FMP API to retrieve detailed financial statements.

API Documentation: https://site.financialmodelingprep.com/developer/docs#financial-statements
"""

from typing import Dict, Any, Literal, Optional
from datetime import datetime

import fmpsdk
import requests

from ba2_trade_platform.core.interfaces import CompanyFundamentalsDetailsInterface
from ba2_trade_platform.core.provider_utils import validate_date_range
from ba2_trade_platform.config import get_app_setting
from ba2_trade_platform.logger import logger


class FMPCompanyDetailsProvider(CompanyFundamentalsDetailsInterface):
    """
    Financial Modeling Prep Company Fundamentals Details Provider.
    
    Provides access to detailed financial statements from FMP API:
    - Balance sheets (annual and quarterly)
    - Income statements (annual and quarterly)
    - Cash flow statements (annual and quarterly)
    
    Requires FMP API key (free tier available).
    """
    
    def __init__(self):
        """Initialize the FMP Company Details Provider with API key."""
        super().__init__()
        
        # Get API key from settings
        self.api_key = get_app_setting("FMP_API_KEY")
        
        if not self.api_key:
            raise ValueError(
                "FMP API key not configured. "
                "Please set 'FMP_API_KEY' in AppSetting table."
            )
        
        logger.info("FMPCompanyDetailsProvider initialized successfully")
    
    def get_provider_name(self) -> str:
        """Get the provider name."""
        return "fmp"
    
    def get_supported_features(self) -> list[str]:
        """Get supported features of this provider."""
        return ["balance_sheet", "income_statement", "cashflow_statement", "past_earnings", "earnings_estimates"]
    
    def validate_config(self) -> bool:
        """Validate provider configuration."""
        return bool(self.api_key)
    
    def _format_as_dict(self, data: Any) -> Dict[str, Any]:
        """Format fundamentals data as dictionary."""
        if isinstance(data, dict):
            return data
        return {"data": data}
    
    def _format_as_markdown(self, data: Any) -> str:
        """Format fundamentals data as markdown."""
        if isinstance(data, str):
            return data
        if isinstance(data, dict) and "markdown" in data:
            return data["markdown"]
        return str(data)
    
    def get_balance_sheet(
        self,
        symbol: str,
        frequency: Literal["annual", "quarterly"],
        end_date: datetime,
        start_date: Optional[datetime] = None,
        lookback_periods: Optional[int] = None,
        format_type: Literal["dict", "markdown"] = "markdown"
    ) -> Dict[str, Any] | str:
        """
        Get balance sheet(s) for a company.
        
        Args:
            symbol: Stock ticker symbol (e.g., 'AAPL', 'MSFT')
            frequency: Annual or quarterly reporting
            end_date: End date (inclusive) - gets most recent statement as of this date
            start_date: Start date (use either this OR lookback_periods, not both)
            lookback_periods: Number of periods to look back (use either this OR start_date, not both)
            format_type: Output format ('dict' or 'markdown')
        
        Returns:
            Balance sheet data in requested format
        
        Raises:
            ValueError: If both start_date and lookback_periods are provided, or if neither is provided
        """
        logger.debug(f"Fetching FMP balance sheet for {symbol} ({frequency})")
        
        try:
            # FMP balance_sheet_statement returns list of balance sheets
            period = "annual" if frequency == "annual" else "quarter"
            balance_data = fmpsdk.balance_sheet_statement(
                apikey=self.api_key,
                symbol=symbol,
                period=period,
                limit=lookback_periods or 10  # Default to 10 periods
            )
            
            if not balance_data:
                logger.warning(f"No balance sheet data returned from FMP for {symbol}")
                return self._format_empty_statement(symbol, frequency, "balance_sheet", end_date, format_type)
            
            # Filter by date if needed
            filtered_statements = self._filter_statements_by_date(
                balance_data, end_date, start_date, lookback_periods
            )
            
            if format_type == "dict":
                return {
                    "symbol": symbol,
                    "frequency": frequency,
                    "end_date": end_date.isoformat(),
                    "statement_count": len(filtered_statements),
                    "statements": [
                        {
                            "fiscal_date_ending": stmt.get("date", ""),
                            "reported_currency": stmt.get("reportedCurrency", "USD"),
                            "total_assets": stmt.get("totalAssets"),
                            "total_current_assets": stmt.get("totalCurrentAssets"),
                            "cash_and_cash_equivalents": stmt.get("cashAndCashEquivalents"),
                            "cash_and_short_term_investments": stmt.get("cashAndShortTermInvestments"),
                            "inventory": stmt.get("inventory"),
                            "current_net_receivables": stmt.get("netReceivables"),
                            "total_non_current_assets": stmt.get("totalNonCurrentAssets"),
                            "property_plant_equipment": stmt.get("propertyPlantEquipmentNet"),
                            "intangible_assets": stmt.get("intangibleAssets"),
                            "goodwill": stmt.get("goodwill"),
                            "long_term_investments": stmt.get("longTermInvestments"),
                            "short_term_investments": stmt.get("shortTermInvestments"),
                            "other_current_assets": stmt.get("otherCurrentAssets"),
                            "other_non_current_assets": stmt.get("otherNonCurrentAssets"),
                            "total_liabilities": stmt.get("totalLiabilities"),
                            "total_current_liabilities": stmt.get("totalCurrentLiabilities"),
                            "current_accounts_payable": stmt.get("accountPayables"),
                            "deferred_revenue": stmt.get("deferredRevenue"),
                            "short_term_debt": stmt.get("shortTermDebt"),
                            "total_non_current_liabilities": stmt.get("totalNonCurrentLiabilities"),
                            "long_term_debt": stmt.get("longTermDebt"),
                            "other_current_liabilities": stmt.get("otherCurrentLiabilities"),
                            "other_non_current_liabilities": stmt.get("otherNonCurrentLiabilities"),
                            "total_shareholder_equity": stmt.get("totalStockholdersEquity"),
                            "retained_earnings": stmt.get("retainedEarnings"),
                            "common_stock": stmt.get("commonStock"),
                            "common_stock_shares_outstanding": stmt.get("commonStock")  # FMP doesn't have separate field
                        }
                        for stmt in filtered_statements
                    ]
                }
            else:
                # Markdown format
                return self._format_balance_sheet_markdown(symbol, frequency, filtered_statements)
                
        except Exception as e:
            logger.error(f"Error fetching FMP balance sheet for {symbol}: {e}")
            return f"Error fetching balance sheet: {str(e)}"
    
    def get_income_statement(
        self,
        symbol: str,
        frequency: Literal["annual", "quarterly"],
        end_date: datetime,
        start_date: Optional[datetime] = None,
        lookback_periods: Optional[int] = None,
        format_type: Literal["dict", "markdown"] = "markdown"
    ) -> Dict[str, Any] | str:
        """
        Get income statement(s) for a company.
        
        Args:
            symbol: Stock ticker symbol (e.g., 'AAPL', 'MSFT')
            frequency: Annual or quarterly reporting
            end_date: End date (inclusive) - gets most recent statement as of this date
            start_date: Start date (use either this OR lookback_periods, not both)
            lookback_periods: Number of periods to look back (use either this OR start_date, not both)
            format_type: Output format ('dict' or 'markdown')
        
        Returns:
            Income statement data in requested format
        
        Raises:
            ValueError: If both start_date and lookback_periods are provided, or if neither is provided
        """
        logger.debug(f"Fetching FMP income statement for {symbol} ({frequency})")
        
        try:
            # FMP income_statement returns list of income statements
            period = "annual" if frequency == "annual" else "quarter"
            income_data = fmpsdk.income_statement(
                apikey=self.api_key,
                symbol=symbol,
                period=period,
                limit=lookback_periods or 10
            )
            
            if not income_data:
                logger.warning(f"No income statement data returned from FMP for {symbol}")
                return self._format_empty_statement(symbol, frequency, "income_statement", end_date, format_type)
            
            # Filter by date if needed
            filtered_statements = self._filter_statements_by_date(
                income_data, end_date, start_date, lookback_periods
            )
            
            if format_type == "dict":
                return {
                    "symbol": symbol,
                    "frequency": frequency,
                    "end_date": end_date.isoformat(),
                    "statement_count": len(filtered_statements),
                    "statements": [
                        {
                            "fiscal_date_ending": stmt.get("date", ""),
                            "reported_currency": stmt.get("reportedCurrency", "USD"),
                            "total_revenue": stmt.get("revenue"),
                            "cost_of_revenue": stmt.get("costOfRevenue"),
                            "gross_profit": stmt.get("grossProfit"),
                            "operating_expenses": stmt.get("operatingExpenses"),
                            "research_and_development": stmt.get("researchAndDevelopmentExpenses"),
                            "selling_general_administrative": stmt.get("sellingGeneralAndAdministrativeExpenses"),
                            "operating_income": stmt.get("operatingIncome"),
                            "interest_expense": stmt.get("interestExpense"),
                            "interest_income": stmt.get("interestIncome"),
                            "other_income_expense": stmt.get("otherIncomeExpenses"),
                            "income_before_tax": stmt.get("incomeBeforeTax"),
                            "income_tax_expense": stmt.get("incomeTaxExpense"),
                            "net_income": stmt.get("netIncome"),
                            "eps": stmt.get("eps"),
                            "eps_diluted": stmt.get("epsdiluted"),
                            "weighted_average_shares_outstanding": stmt.get("weightedAverageShsOut"),
                            "weighted_average_shares_diluted": stmt.get("weightedAverageShsOutDil"),
                            "ebitda": stmt.get("ebitda"),
                            "depreciation_and_amortization": stmt.get("depreciationAndAmortization")
                        }
                        for stmt in filtered_statements
                    ]
                }
            else:
                # Markdown format
                return self._format_income_statement_markdown(symbol, frequency, filtered_statements)
                
        except Exception as e:
            logger.error(f"Error fetching FMP income statement for {symbol}: {e}")
            return f"Error fetching income statement: {str(e)}"
    
    def get_cashflow_statement(
        self,
        symbol: str,
        frequency: Literal["annual", "quarterly"],
        end_date: datetime,
        start_date: Optional[datetime] = None,
        lookback_periods: Optional[int] = None,
        format_type: Literal["dict", "markdown"] = "markdown"
    ) -> Dict[str, Any] | str:
        """
        Get cash flow statement(s) for a company.
        
        Args:
            symbol: Stock ticker symbol (e.g., 'AAPL', 'MSFT')
            frequency: Annual or quarterly reporting
            end_date: End date (inclusive) - gets most recent statement as of this date
            start_date: Start date (use either this OR lookback_periods, not both)
            lookback_periods: Number of periods to look back (use either this OR start_date, not both)
            format_type: Output format ('dict' or 'markdown')
        
        Returns:
            Cash flow statement data in requested format
        
        Raises:
            ValueError: If both start_date and lookback_periods are provided, or if neither is provided
        """
        logger.debug(f"Fetching FMP cash flow statement for {symbol} ({frequency})")
        
        try:
            # FMP cash_flow_statement returns list of cash flow statements
            period = "annual" if frequency == "annual" else "quarter"
            cashflow_data = fmpsdk.cash_flow_statement(
                apikey=self.api_key,
                symbol=symbol,
                period=period,
                limit=lookback_periods or 10
            )
            
            if not cashflow_data:
                logger.warning(f"No cash flow data returned from FMP for {symbol}")
                return self._format_empty_statement(symbol, frequency, "cashflow_statement", end_date, format_type)
            
            # Filter by date if needed
            filtered_statements = self._filter_statements_by_date(
                cashflow_data, end_date, start_date, lookback_periods
            )
            
            if format_type == "dict":
                return {
                    "symbol": symbol,
                    "frequency": frequency,
                    "end_date": end_date.isoformat(),
                    "statement_count": len(filtered_statements),
                    "statements": [
                        {
                            "fiscal_date_ending": stmt.get("date", ""),
                            "reported_currency": stmt.get("reportedCurrency", "USD"),
                            "operating_cash_flow": stmt.get("operatingCashFlow"),
                            "net_income": stmt.get("netIncome"),
                            "depreciation_and_amortization": stmt.get("depreciationAndAmortization"),
                            "deferred_income_tax": stmt.get("deferredIncomeTax"),
                            "stock_based_compensation": stmt.get("stockBasedCompensation"),
                            "change_in_working_capital": stmt.get("changeInWorkingCapital"),
                            "change_in_receivables": stmt.get("accountsReceivables"),
                            "change_in_inventory": stmt.get("inventory"),
                            "change_in_payables": stmt.get("accountsPayables"),
                            "other_operating_activities": stmt.get("otherOperatingActivities"),
                            "investing_cash_flow": stmt.get("netCashUsedForInvestingActivites"),
                            "capital_expenditures": stmt.get("capitalExpenditure"),
                            "investments": stmt.get("investments"),
                            "acquisitions": stmt.get("acquisitionsNet"),
                            "other_investing_activities": stmt.get("otherInvestingActivites"),
                            "financing_cash_flow": stmt.get("netCashUsedProvidedByFinancingActivities"),
                            "debt_repayment": stmt.get("debtRepayment"),
                            "common_stock_issued": stmt.get("commonStockIssued"),
                            "common_stock_repurchased": stmt.get("commonStockRepurchased"),
                            "dividends_paid": stmt.get("dividendsPaid"),
                            "other_financing_activities": stmt.get("otherFinancingActivites"),
                            "net_change_in_cash": stmt.get("netChangeInCash"),
                            "free_cash_flow": stmt.get("freeCashFlow")
                        }
                        for stmt in filtered_statements
                    ]
                }
            else:
                # Markdown format
                return self._format_cashflow_statement_markdown(symbol, frequency, filtered_statements)
                
        except Exception as e:
            logger.error(f"Error fetching FMP cash flow statement for {symbol}: {e}")
            return f"Error fetching cash flow statement: {str(e)}"
    
    def _filter_statements_by_date(
        self,
        statements: list,
        end_date: datetime,
        start_date: Optional[datetime],
        lookback_periods: Optional[int]
    ) -> list:
        """Filter statements by date range or period count."""
        if not statements:
            return []
        
        # If lookback_periods is specified, just take that many
        if lookback_periods:
            return statements[:lookback_periods]
        
        # Otherwise filter by date
        if start_date:
            # validate_date_range returns tuple (start_date, end_date)
            actual_start_date, actual_end_date = validate_date_range(start_date, end_date, lookback_periods)
            # Use validated end_date if provided
            if actual_end_date:
                end_date = actual_end_date
        else:
            actual_start_date = None
        
        if not actual_start_date:
            return statements
        
        filtered = []
        for stmt in statements:
            stmt_date_str = stmt.get("date", "")
            if stmt_date_str:
                try:
                    stmt_date = datetime.fromisoformat(stmt_date_str.split("T")[0])
                    if actual_start_date <= stmt_date <= end_date:
                        filtered.append(stmt)
                except (ValueError, AttributeError):
                    continue
        
        return filtered
    
    def _format_balance_sheet_markdown(self, symbol: str, frequency: str, statements: list) -> str:
        """Format balance sheet as markdown table."""
        if not statements:
            return f"# Balance Sheet for {symbol}\n\nNo data available.\n"
        
        markdown = f"# Balance Sheet for {symbol} ({frequency.title()})\n\n"
        markdown += "| Metric | " + " | ".join([s.get("date", "")[:10] for s in statements[:5]]) + " |\n"
        markdown += "|--------|" + "|".join(["--------:"] * min(5, len(statements))) + "|\n"
        
        metrics = [
            ("Total Assets", "totalAssets"),
            ("Total Current Assets", "totalCurrentAssets"),
            ("Cash & Equivalents", "cashAndCashEquivalents"),
            ("Total Liabilities", "totalLiabilities"),
            ("Total Equity", "totalStockholdersEquity"),
        ]
        
        for label, key in metrics:
            values = [f"${s.get(key, 0)/1e9:.2f}B" if s.get(key) else "N/A" for s in statements[:5]]
            markdown += f"| {label} | " + " | ".join(values) + " |\n"
        
        return markdown
    
    def _format_income_statement_markdown(self, symbol: str, frequency: str, statements: list) -> str:
        """Format income statement as markdown table."""
        if not statements:
            return f"# Income Statement for {symbol}\n\nNo data available.\n"
        
        markdown = f"# Income Statement for {symbol} ({frequency.title()})\n\n"
        markdown += "| Metric | " + " | ".join([s.get("date", "")[:10] for s in statements[:5]]) + " |\n"
        markdown += "|--------|" + "|".join(["--------:"] * min(5, len(statements))) + "|\n"
        
        metrics = [
            ("Revenue", "revenue"),
            ("Gross Profit", "grossProfit"),
            ("Operating Income", "operatingIncome"),
            ("Net Income", "netIncome"),
            ("EPS (Diluted)", "epsdiluted"),
        ]
        
        for label, key in metrics:
            if key == "epsdiluted":
                values = [f"${s.get(key, 0):.2f}" if s.get(key) else "N/A" for s in statements[:5]]
            else:
                values = [f"${s.get(key, 0)/1e9:.2f}B" if s.get(key) else "N/A" for s in statements[:5]]
            markdown += f"| {label} | " + " | ".join(values) + " |\n"
        
        return markdown
    
    def _format_cashflow_statement_markdown(self, symbol: str, frequency: str, statements: list) -> str:
        """Format cash flow statement as markdown table."""
        if not statements:
            return f"# Cash Flow Statement for {symbol}\n\nNo data available.\n"
        
        markdown = f"# Cash Flow Statement for {symbol} ({frequency.title()})\n\n"
        markdown += "| Metric | " + " | ".join([s.get("date", "")[:10] for s in statements[:5]]) + " |\n"
        markdown += "|--------|" + "|".join(["--------:"] * min(5, len(statements))) + "|\n"
        
        metrics = [
            ("Operating Cash Flow", "operatingCashFlow"),
            ("Investing Cash Flow", "netCashUsedForInvestingActivites"),
            ("Financing Cash Flow", "netCashUsedProvidedByFinancingActivities"),
            ("Free Cash Flow", "freeCashFlow"),
            ("Net Change in Cash", "netChangeInCash"),
        ]
        
        for label, key in metrics:
            values = [f"${s.get(key, 0)/1e9:.2f}B" if s.get(key) else "N/A" for s in statements[:5]]
            markdown += f"| {label} | " + " | ".join(values) + " |\n"
        
        return markdown
    
    def _format_empty_statement(
        self,
        symbol: str,
        frequency: str,
        statement_type: str,
        end_date: datetime,
        format_type: Literal["dict", "markdown"]
    ) -> Dict[str, Any] | str:
        """Format an empty response when no statements are found."""
        if format_type == "dict":
            return {
                "symbol": symbol,
                "frequency": frequency,
                "end_date": end_date.isoformat(),
                "statement_count": 0,
                "statements": []
            }
        else:
            statement_label = statement_type.replace("_", " ").title()
            return f"# {statement_label} for {symbol}\n\nNo statements found.\n"
    
    def get_past_earnings(
        self,
        symbol: str,
        frequency: Literal["annual", "quarterly"],
        end_date: datetime,
        lookback_periods: int = 8,
        format_type: Literal["dict", "markdown"] = "markdown"
    ) -> Dict[str, Any] | str:
        """
        Get historical earnings data for a company from FMP.
        
        Uses FMP's historical earnings endpoint which provides actual vs estimated EPS.
        
        Args:
            symbol: Stock ticker symbol
            frequency: Data frequency ('quarterly' or 'annual')
            end_date: End date (inclusive) - gets most recent earnings as of this date
            lookback_periods: Number of periods to look back (default 8 quarters = 2 years)
            format_type: Output format ('dict' or 'markdown')
        
        Returns:
            Historical earnings data in requested format
        """
        logger.debug(f"Fetching FMP past earnings for {symbol} ({frequency})")
        
        try:
            # FMP uses 'quarter' and 'annual' for the period parameter
            period = "quarter" if frequency.lower() == "quarterly" else "annual"
            
            # Fetch earnings data using FMP SDK
            # Note: FMP earnings endpoint returns actual vs estimated EPS
            earnings_data = fmpsdk.historical_earning_calendar(
                apikey=self.api_key,
                symbol=symbol
            )
            
            if not earnings_data:
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
            result = {
                "symbol": symbol.upper(),
                "frequency": frequency,
                "end_date": end_date.isoformat(),
                "lookback_periods": lookback_periods,
                "earnings": [],
                "retrieved_at": datetime.now().isoformat()
            }
            
            filtered_earnings = []
            for earning in earnings_data:
                # Parse the date from the earning record
                earning_date_str = earning.get("date", "")
                if not earning_date_str:
                    continue
                
                try:
                    earning_date = datetime.strptime(earning_date_str, "%Y-%m-%d")
                except:
                    continue
                
                # Filter by end_date
                if earning_date > end_date:
                    continue
                
                # Build earnings entry
                reported_eps = earning.get("eps", 0)
                estimated_eps = earning.get("epsEstimated", 0)
                
                earnings_entry = {
                    "fiscal_date_ending": earning_date.strftime("%Y-%m-%d"),
                    "report_date": earning_date_str,
                    "reported_eps": float(reported_eps) if reported_eps else 0,
                    "estimated_eps": float(estimated_eps) if estimated_eps else 0
                }
                
                # Calculate surprise
                if reported_eps and estimated_eps:
                    earnings_entry["surprise"] = earnings_entry["reported_eps"] - earnings_entry["estimated_eps"]
                    if earnings_entry["estimated_eps"] != 0:
                        earnings_entry["surprise_percent"] = (earnings_entry["surprise"] / abs(earnings_entry["estimated_eps"])) * 100
                    else:
                        earnings_entry["surprise_percent"] = 0
                else:
                    earnings_entry["surprise"] = None
                    earnings_entry["surprise_percent"] = None
                
                filtered_earnings.append((earning_date, earnings_entry))
            
            # Sort by date descending (most recent first)
            filtered_earnings.sort(key=lambda x: x[0], reverse=True)
            
            # Apply lookback_periods limit
            filtered_earnings = filtered_earnings[:lookback_periods]
            
            # Extract just the earnings data
            result["earnings"] = [e[1] for e in filtered_earnings]
            
            logger.info(f"Retrieved {len(result['earnings'])} past earnings periods for {symbol}")
            
            # Format output
            if format_type == "dict":
                return result
            else:
                return self._format_past_earnings_markdown(result)
        
        except Exception as e:
            logger.error(f"Error retrieving past earnings for {symbol}: {e}")
            if format_type == "dict":
                return {"error": str(e), "symbol": symbol}
            return f"Error retrieving past earnings for {symbol}: {str(e)}"
    
    def get_earnings_estimates(
        self,
        symbol: str,
        frequency: Literal["annual", "quarterly"],
        as_of_date: datetime,
        lookback_periods: int = 4,
        format_type: Literal["dict", "markdown"] = "markdown"
    ) -> Dict[str, Any] | str:
        """
        Get future earnings estimates for a company from FMP.
        
        Uses FMP's analyst estimates endpoint for forward-looking EPS estimates.
        Note: FMP SDK doesn't have analyst_estimates method, so we use direct API call.
        
        Args:
            symbol: Stock ticker symbol
            frequency: Data frequency ('quarterly' or 'annual')
            as_of_date: Date for estimates (returns most recent estimates as of this date)
            lookback_periods: Number of future periods to retrieve estimates for (default 4)
            format_type: Output format ('dict' or 'markdown')
        
        Returns:
            Future earnings estimates in requested format
        """
        logger.debug(f"Fetching FMP earnings estimates for {symbol} ({frequency})")
        
        try:
            # Use direct API call since fmpsdk doesn't have analyst_estimates method
            # FMP API: https://financialmodelingprep.com/api/v3/analyst-estimates/{symbol}
            url = f"https://financialmodelingprep.com/api/v3/analyst-estimates/{symbol}"
            params = {
                "apikey": self.api_key,
                "limit": 20  # Get extra to ensure we have enough future estimates
            }
            
            response = requests.get(url, params=params, timeout=30)
            response.raise_for_status()
            estimates_data = response.json()
            
            if not estimates_data:
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
            result = {
                "symbol": symbol.upper(),
                "frequency": frequency,
                "as_of_date": as_of_date.isoformat(),
                "estimates": [],
                "retrieved_at": datetime.now().isoformat()
            }
            
            filtered_estimates = []
            for estimate in estimates_data:
                # Parse the date from the estimate record
                estimate_date_str = estimate.get("date", "")
                if not estimate_date_str:
                    continue
                
                try:
                    estimate_date = datetime.strptime(estimate_date_str, "%Y-%m-%d")
                except:
                    continue
                
                # Only include future estimates (after as_of_date)
                if estimate_date < as_of_date:
                    continue
                
                # Build estimate entry
                estimate_entry = {
                    "fiscal_date_ending": estimate_date.strftime("%Y-%m-%d"),
                    "estimated_eps_avg": float(estimate.get("estimatedEpsAvg", 0)),
                    "estimated_eps_high": float(estimate.get("estimatedEpsHigh", 0)),
                    "estimated_eps_low": float(estimate.get("estimatedEpsLow", 0)),
                    "number_of_analysts": int(estimate.get("numberAnalystEstimatedEps", 0))
                }
                
                filtered_estimates.append((estimate_date, estimate_entry))
            
            # Sort by date ascending (earliest future period first)
            filtered_estimates.sort(key=lambda x: x[0])
            
            # Apply lookback_periods limit (here it means "forward periods")
            filtered_estimates = filtered_estimates[:lookback_periods]
            
            # Extract just the estimates data
            result["estimates"] = [e[1] for e in filtered_estimates]
            
            logger.info(f"Retrieved {len(result['estimates'])} earnings estimates for {symbol}")
            
            # Format output
            if format_type == "dict":
                return result
            else:
                return self._format_earnings_estimates_markdown(result)
        
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
