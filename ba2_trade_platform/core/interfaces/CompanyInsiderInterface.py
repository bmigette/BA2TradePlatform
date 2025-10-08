"""
Interface for company insider trading data providers.

This interface defines methods for retrieving insider transactions and
aggregated insider sentiment metrics.
"""

from abc import abstractmethod
from typing import Dict, Any, Literal, Optional, Annotated
from datetime import datetime

from .DataProviderInterface import DataProviderInterface


class CompanyInsiderInterface(DataProviderInterface):
    """
    Interface for insider trading data providers.
    
    Providers implementing this interface supply insider trading transaction data
    and aggregated sentiment metrics based on insider activity.
    """
    
    @abstractmethod
    def get_insider_transactions(
        self,
        symbol: Annotated[str, "Stock ticker symbol"],
        end_date: Annotated[datetime, "End date for transactions (inclusive)"],
        start_date: Annotated[Optional[datetime], "Start date for transactions (mutually exclusive with lookback_days)"] = None,
        lookback_days: Annotated[Optional[int], "Days to look back from end_date (mutually exclusive with start_date)"] = None,
        format_type: Literal["dict", "markdown"] = "markdown"
    ) -> Dict[str, Any] | str:
        """
        Get insider trading transactions for a company.
        
        Args:
            symbol: Stock ticker symbol (e.g., 'AAPL', 'MSFT')
            end_date: End date (inclusive)
            start_date: Start date (use either this OR lookback_days, not both)
            lookback_days: Days to look back from end_date (use either this OR start_date, not both)
            format_type: Output format ('dict' or 'markdown')
        
        Note: Must provide either start_date or lookback_days, but not both.
        
        Returns:
            If format_type='dict': {
                "symbol": str,
                "start_date": str (ISO format),
                "end_date": str (ISO format),
                "transaction_count": int,
                "total_purchase_value": float,
                "total_sale_value": float,
                "net_transaction_value": float,
                "transactions": [{
                    "filing_date": str (ISO format),
                    "transaction_date": str (ISO format),
                    "insider_name": str,
                    "title": str,  # e.g., 'CEO', 'CFO', 'Director'
                    "transaction_type": str,  # 'purchase', 'sale', 'option_exercise', 'gift'
                    "shares": float,
                    "price": float,
                    "value": float,  # shares * price
                    "shares_owned_following": float (optional),
                    "form_type": str,  # e.g., 'Form 4', 'Form 144'
                    "sec_link": str (optional - link to SEC filing)
                }]
            }
            If format_type='markdown': Formatted markdown table with transactions
        
        Raises:
            ValueError: If both start_date and lookback_days are provided, or if neither is provided
        """
        pass
    
    @abstractmethod
    def get_insider_sentiment(
        self,
        symbol: Annotated[str, "Stock ticker symbol"],
        end_date: Annotated[datetime, "End date for sentiment calculation (inclusive)"],
        start_date: Annotated[Optional[datetime], "Start date (mutually exclusive with lookback_days)"] = None,
        lookback_days: Annotated[Optional[int], "Days to look back from end_date (mutually exclusive with start_date)"] = None,
        format_type: Literal["dict", "markdown"] = "markdown"
    ) -> Dict[str, Any] | str:
        """
        Get aggregated insider sentiment metrics for a company.
        
        Calculates sentiment scores based on insider buying and selling activity.
        
        Args:
            symbol: Stock ticker symbol (e.g., 'AAPL', 'MSFT')
            end_date: End date (inclusive)
            start_date: Start date (use either this OR lookback_days, not both)
            lookback_days: Days to look back from end_date (use either this OR start_date, not both)
            format_type: Output format ('dict' or 'markdown')
        
        Note: Must provide either start_date or lookback_days, but not both.
        
        Returns:
            If format_type='dict': {
                "symbol": str,
                "start_date": str (ISO format),
                "end_date": str (ISO format),
                "metrics": {
                    "mspr": float,  # Monthly Share Purchase Ratio
                    "total_purchases": int,
                    "total_sales": int,
                    "net_transactions": int,  # purchases - sales
                    "purchase_volume": float,  # Total shares purchased
                    "sale_volume": float,  # Total shares sold
                    "net_volume": float,  # purchase_volume - sale_volume
                    "purchase_value": float,  # Total dollar value of purchases
                    "sale_value": float,  # Total dollar value of sales
                    "net_value": float,  # purchase_value - sale_value
                    "unique_buyers": int,
                    "unique_sellers": int,
                    "sentiment": str,  # 'bullish', 'bearish', 'neutral'
                    "sentiment_score": float,  # -1.0 (very bearish) to 1.0 (very bullish)
                    "confidence": float  # 0.0 to 1.0 based on transaction volume
                },
                "by_insider_type": {
                    "ceo": {"purchases": int, "sales": int, "net_value": float},
                    "cfo": {"purchases": int, "sales": int, "net_value": float},
                    "director": {"purchases": int, "sales": int, "net_value": float},
                    "officer": {"purchases": int, "sales": int, "net_value": float},
                    "other": {"purchases": int, "sales": int, "net_value": float}
                }
            }
            If format_type='markdown': Formatted markdown with sentiment analysis
        
        Raises:
            ValueError: If both start_date and lookback_days are provided, or if neither is provided
        
        Notes:
            - MSPR (Monthly Share Purchase Ratio): Ratio of insider buying to selling
            - Sentiment score calculation typically weighs executive trades more heavily
            - Confidence increases with transaction volume and value
        """
        pass
