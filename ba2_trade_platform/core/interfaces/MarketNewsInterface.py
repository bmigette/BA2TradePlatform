"""
Interface for market news providers.

This interface defines methods for retrieving company-specific and global market news.
"""

from abc import abstractmethod
from typing import Dict, Any, Literal, Optional, Annotated
from datetime import datetime

from .DataProviderInterface import DataProviderInterface


class MarketNewsInterface(DataProviderInterface):
    """
    Interface for market news providers.
    
    Providers implementing this interface supply news articles for companies
    and general market news.
    """
    
    @abstractmethod
    def get_company_news(
        self,
        symbol: Annotated[str, "Stock ticker symbol"],
        end_date: Annotated[datetime, "End date for news (inclusive)"],
        start_date: Annotated[Optional[datetime], "Start date for news (mutually exclusive with lookback_days)"] = None,
        lookback_days: Annotated[Optional[int], "Days to look back from end_date (mutually exclusive with start_date)"] = None,
        limit: Annotated[int, "Maximum number of articles"] = 50,
        format_type: Literal["dict", "markdown"] = "markdown"
    ) -> Dict[str, Any] | str:
        """
        Get news articles for a specific company.
        
        Args:
            symbol: Stock ticker symbol (e.g., 'AAPL', 'MSFT')
            end_date: End date (inclusive)
            start_date: Start date (use either this OR lookback_days, not both)
            lookback_days: Days to look back from end_date (use either this OR start_date, not both)
            limit: Maximum number of articles to return
            format_type: Output format ('dict' or 'markdown')
        
        Note: Must provide either start_date or lookback_days, but not both.
        
        Returns:
            If format_type='dict': {
                "symbol": str,
                "start_date": str (ISO format),
                "end_date": str (ISO format),
                "article_count": int,
                "articles": [{
                    "title": str,
                    "summary": str,
                    "content": str (optional - full article text if available),
                    "source": str,
                    "author": str (optional),
                    "published_at": str (ISO format),
                    "url": str,
                    "image_url": str (optional),
                    "sentiment": str (optional - 'positive', 'negative', 'neutral'),
                    "sentiment_score": float (optional - -1.0 to 1.0),
                    "symbols": list[str] (optional - all symbols mentioned),
                    "tags": list[str] (optional - article tags/categories)
                }]
            }
            If format_type='markdown': Formatted markdown with article summaries
        
        Raises:
            ValueError: If both start_date and lookback_days are provided, or if neither is provided
        """
        pass
    
    @abstractmethod
    def get_global_news(
        self,
        end_date: Annotated[datetime, "End date for news (inclusive)"],
        start_date: Annotated[Optional[datetime], "Start date for news (mutually exclusive with lookback_days)"] = None,
        lookback_days: Annotated[Optional[int], "Days to look back from end_date (mutually exclusive with start_date)"] = None,
        limit: Annotated[int, "Maximum number of articles"] = 50,
        format_type: Literal["dict", "markdown"] = "markdown"
    ) -> Dict[str, Any] | str:
        """
        Get global/market news (not specific to any company).
        
        Args:
            end_date: End date (inclusive)
            start_date: Start date (use either this OR lookback_days, not both)
            lookback_days: Days to look back from end_date (use either this OR start_date, not both)
            limit: Maximum number of articles to return
            format_type: Output format ('dict' or 'markdown')
        
        Note: Must provide either start_date or lookback_days, but not both.
        
        Returns:
            If format_type='dict': {
                "start_date": str (ISO format),
                "end_date": str (ISO format),
                "article_count": int,
                "articles": [{
                    "title": str,
                    "summary": str,
                    "content": str (optional - full article text if available),
                    "source": str,
                    "author": str (optional),
                    "published_at": str (ISO format),
                    "url": str,
                    "image_url": str (optional),
                    "sentiment": str (optional - 'positive', 'negative', 'neutral'),
                    "sentiment_score": float (optional - -1.0 to 1.0),
                    "symbols": list[str] (optional - symbols mentioned in article),
                    "tags": list[str] (optional - article tags/categories),
                    "category": str (optional - 'market', 'economy', 'earnings', etc.)
                }]
            }
            If format_type='markdown': Formatted markdown with article summaries
        
        Raises:
            ValueError: If both start_date and lookback_days are provided, or if neither is provided
        """
        pass
