"""
Interface for detailed company financials providers.

This interface defines methods for retrieving detailed financial statements:
balance sheets, income statements, and cash flow statements.
"""

from abc import abstractmethod
from typing import Dict, Any, Literal, Optional, Annotated
from datetime import datetime

from .DataProviderInterface import DataProviderInterface


class CompanyFundamentalsDetailsInterface(DataProviderInterface):
    """
    Interface for detailed company financials (financial statements).
    
    Providers implementing this interface supply detailed financial statements
    including balance sheets, income statements, and cash flow statements.
    """
    
    @abstractmethod
    def get_balance_sheet(
        self,
        symbol: Annotated[str, "Stock ticker symbol"],
        frequency: Annotated[Literal["annual", "quarterly"], "Reporting frequency"],
        end_date: Annotated[datetime, "End date for statement range (inclusive)"],
        start_date: Annotated[Optional[datetime], "Start date for statement range (mutually exclusive with lookback_periods)"] = None,
        lookback_periods: Annotated[Optional[int], "Number of periods to look back from end_date (mutually exclusive with start_date)"] = None,
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
        
        Note: Must provide either start_date or lookback_periods, but not both.
        
        Returns:
            Multiple balance sheets within the date range or period count.
            
            If format_type='dict': {
                "symbol": str,
                "frequency": str,
                "start_date": str (ISO format),
                "end_date": str (ISO format),
                "statements": [{
                    "fiscal_date_ending": str (ISO format),
                    "reported_currency": str,
                    "total_assets": float,
                    "total_current_assets": float,
                    "cash_and_cash_equivalents": float,
                    "cash_and_short_term_investments": float,
                    "inventory": float,
                    "current_net_receivables": float,
                    "total_non_current_assets": float,
                    "property_plant_equipment": float,
                    "accumulated_depreciation_amortization_ppe": float,
                    "intangible_assets": float,
                    "goodwill": float,
                    "investments": float,
                    "long_term_investments": float,
                    "short_term_investments": float,
                    "other_current_assets": float,
                    "other_non_current_assets": float,
                    "total_liabilities": float,
                    "total_current_liabilities": float,
                    "current_accounts_payable": float,
                    "deferred_revenue": float,
                    "current_debt": float,
                    "short_term_debt": float,
                    "total_non_current_liabilities": float,
                    "capital_lease_obligations": float,
                    "long_term_debt": float,
                    "current_long_term_debt": float,
                    "long_term_debt_noncurrent": float,
                    "short_long_term_debt_total": float,
                    "other_current_liabilities": float,
                    "other_non_current_liabilities": float,
                    "total_shareholder_equity": float,
                    "treasury_stock": float,
                    "retained_earnings": float,
                    "common_stock": float,
                    "common_stock_shares_outstanding": float
                    # Additional fields as available from provider
                }]
            }
            If format_type='markdown': Formatted markdown table with statements
        
        Raises:
            ValueError: If both start_date and lookback_periods are provided, or if neither is provided
        """
        pass
    
    @abstractmethod
    def get_income_statement(
        self,
        symbol: Annotated[str, "Stock ticker symbol"],
        frequency: Annotated[Literal["annual", "quarterly"], "Reporting frequency"],
        end_date: Annotated[datetime, "End date for statement range (inclusive)"],
        start_date: Annotated[Optional[datetime], "Start date for statement range (mutually exclusive with lookback_periods)"] = None,
        lookback_periods: Annotated[Optional[int], "Number of periods to look back from end_date (mutually exclusive with start_date)"] = None,
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
        
        Note: Must provide either start_date or lookback_periods, but not both.
        
        Returns:
            Multiple income statements within the date range or period count.
            
            If format_type='dict': {
                "symbol": str,
                "frequency": str,
                "start_date": str (ISO format),
                "end_date": str (ISO format),
                "statements": [{
                    "fiscal_date_ending": str (ISO format),
                    "reported_currency": str,
                    "gross_profit": float,
                    "total_revenue": float,
                    "cost_of_revenue": float,
                    "cost_of_goods_and_services_sold": float,
                    "operating_income": float,
                    "selling_general_and_administrative": float,
                    "research_and_development": float,
                    "operating_expenses": float,
                    "investment_income_net": float,
                    "net_interest_income": float,
                    "interest_income": float,
                    "interest_expense": float,
                    "non_interest_income": float,
                    "other_non_operating_income": float,
                    "depreciation": float,
                    "depreciation_and_amortization": float,
                    "income_before_tax": float,
                    "income_tax_expense": float,
                    "interest_and_debt_expense": float,
                    "net_income_from_continuing_operations": float,
                    "comprehensive_income_net_of_tax": float,
                    "ebit": float,
                    "ebitda": float,
                    "net_income": float
                    # Additional fields as available from provider
                }]
            }
            If format_type='markdown': Formatted markdown table with statements
        
        Raises:
            ValueError: If both start_date and lookback_periods are provided, or if neither is provided
        """
        pass
    
    @abstractmethod
    def get_cashflow_statement(
        self,
        symbol: Annotated[str, "Stock ticker symbol"],
        frequency: Annotated[Literal["annual", "quarterly"], "Reporting frequency"],
        end_date: Annotated[datetime, "End date for statement range (inclusive)"],
        start_date: Annotated[Optional[datetime], "Start date for statement range (mutually exclusive with lookback_periods)"] = None,
        lookback_periods: Annotated[Optional[int], "Number of periods to look back from end_date (mutually exclusive with start_date)"] = None,
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
        
        Note: Must provide either start_date or lookback_periods, but not both.
        
        Returns:
            Multiple cash flow statements within the date range or period count.
            
            If format_type='dict': {
                "symbol": str,
                "frequency": str,
                "start_date": str (ISO format),
                "end_date": str (ISO format),
                "statements": [{
                    "fiscal_date_ending": str (ISO format),
                    "reported_currency": str,
                    "operating_cashflow": float,
                    "payments_for_operating_activities": float,
                    "proceeds_from_operating_activities": float,
                    "change_in_operating_liabilities": float,
                    "change_in_operating_assets": float,
                    "depreciation_depletion_and_amortization": float,
                    "capital_expenditures": float,
                    "change_in_receivables": float,
                    "change_in_inventory": float,
                    "profit_loss": float,
                    "cashflow_from_investment": float,
                    "cashflow_from_financing": float,
                    "proceeds_from_repayments_of_short_term_debt": float,
                    "payments_for_repurchase_of_common_stock": float,
                    "payments_for_repurchase_of_equity": float,
                    "payments_for_repurchase_of_preferred_stock": float,
                    "dividend_payout": float,
                    "dividend_payout_common_stock": float,
                    "dividend_payout_preferred_stock": float,
                    "proceeds_from_issuance_of_common_stock": float,
                    "proceeds_from_issuance_of_long_term_debt_and_capital_securities_net": float,
                    "proceeds_from_issuance_of_preferred_stock": float,
                    "proceeds_from_repurchase_of_equity": float,
                    "proceeds_from_sale_of_treasury_stock": float,
                    "change_in_cash_and_cash_equivalents": float,
                    "change_in_exchange_rate": float,
                    "net_income": float
                    # Additional fields as available from provider
                }]
            }
            If format_type='markdown': Formatted markdown table with statements
        
        Raises:
            ValueError: If both start_date and lookback_periods are provided, or if neither is provided
        """
        pass
