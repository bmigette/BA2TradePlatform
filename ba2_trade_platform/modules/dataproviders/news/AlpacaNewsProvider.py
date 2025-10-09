"""
Alpaca Markets News Provider

This provider uses the Alpaca Markets News API to retrieve company-specific
and general market news.

API Documentation: https://docs.alpaca.markets/reference/news-3
"""

from typing import Dict, Any, Literal, Optional
from datetime import datetime, timezone

from alpaca.data.historical import NewsClient
from alpaca.data.requests import NewsRequest

from ba2_trade_platform.core.interfaces import MarketNewsInterface
from ba2_trade_platform.core.provider_utils import (
    validate_date_range,
    validate_lookback_days,
    calculate_date_range,
    log_provider_call,
)
from ba2_trade_platform.config import get_app_setting
from ba2_trade_platform.logger import logger


class AlpacaNewsProvider(MarketNewsInterface):
    """
    Alpaca Markets News Provider.
    
    Provides access to news articles from Alpaca Markets API, including:
    - Company-specific news articles
    - General market news
    - Article summaries and metadata
    - Source attribution
    
    Requires Alpaca Markets API credentials (free tier available).
    """
    
    def __init__(self):
        """Initialize the Alpaca News Provider with API credentials."""
        super().__init__()
        
        # Get API credentials from settings
        self.api_key = get_app_setting("alpaca_market_api_key")
        self.api_secret = get_app_setting("alpaca_market_api_secret")
        
        if not self.api_key or not self.api_secret:
            raise ValueError(
                "Alpaca API credentials not configured. "
                "Please set 'alpaca_market_api_key' and 'alpaca_market_api_secret' in AppSetting table."
            )
        
        # Initialize Alpaca News Client
        try:
            self.client = NewsClient(api_key=self.api_key, secret_key=self.api_secret)
            logger.info("AlpacaNewsProvider initialized successfully")
        except Exception as e:
            logger.error(f"Failed to initialize Alpaca News Client: {e}", exc_info=True)
            raise
    
    def get_provider_name(self) -> str:
        """Get the provider name."""
        return "alpaca"
    
    def get_supported_features(self) -> list[str]:
        """Get supported features of this provider."""
        return ["company_news", "global_news"]
    
    def validate_config(self) -> bool:
        """
        Validate provider configuration.
        
        Returns:
            bool: True if API credentials are configured and client is initialized
        """
        return self.client is not None and self.api_key is not None and self.api_secret is not None
    
    def _format_as_dict(self, data: Any) -> Dict[str, Any]:
        """
        Format data as a structured dictionary.
        
        Args:
            data: News data (already in dict format)
            
        Returns:
            Dict[str, Any]: Structured dictionary
        """
        if isinstance(data, dict):
            return data
        return {"data": data}
    
    def _format_as_markdown(self, data: Dict[str, Any]) -> str:
        """
        Format news data as markdown.
        
        Args:
            data: News data dictionary
        
        Returns:
            Markdown-formatted string
        """
        # Detect if this is company-specific news by checking for 'symbol' key
        is_company_news = "symbol" in data
        
        lines = []
        
        # Header
        if is_company_news:
            lines.append(f"# News for {data['symbol']}")
        else:
            lines.append("# Global Market News")
        
        lines.append(f"**Period:** {data['start_date'][:10]} to {data['end_date'][:10]}")
        lines.append(f"**Articles:** {data['article_count']}")
        lines.append("")
        
        # Articles
        for i, article in enumerate(data["articles"], 1):
            lines.append(f"## {i}. {article['title']}")
            lines.append(f"**Source:** {article['source']}")
            
            if article.get('author'):
                lines.append(f"**Author:** {article['author']}")
            
            if article.get('published_at'):
                pub_date = article['published_at'][:19].replace('T', ' ')
                lines.append(f"**Published:** {pub_date}")
            
            if article.get('symbols'):
                symbols_str = ", ".join(article['symbols'])
                lines.append(f"**Symbols:** {symbols_str}")
            
            lines.append("")
            lines.append(article['summary'])
            
            if article.get('url'):
                lines.append(f"\n[Read more]({article['url']})")
            
            lines.append("")
            lines.append("---")
            lines.append("")
        
        return "\n".join(lines)
    
    @log_provider_call
    def get_company_news(
        self,
        symbol: str,
        end_date: datetime,
        start_date: Optional[datetime] = None,
        lookback_days: Optional[int] = None,
        limit: int = 50,
        format_type: Literal["dict", "markdown", "both"] = "markdown"
    ) -> Dict[str, Any] | str:
        """
        Get news articles for a specific company.
        
        Args:
            symbol: Stock ticker symbol (e.g., 'AAPL', 'MSFT')
            end_date: End date (inclusive)
            start_date: Start date (use either this OR lookback_days, not both)
            lookback_days: Days to look back from end_date (use either this OR start_date, not both)
            limit: Maximum number of articles to return (max: 50 per request)
            format_type: Output format ('dict', 'markdown', or 'both')
        
        Returns:
            News data in requested format
        
        Raises:
            ValueError: If both start_date and lookback_days are provided, or if neither is provided
        """
        # Validate date parameters
        if start_date and lookback_days:
            raise ValueError("Provide either start_date OR lookback_days, not both")
        if not start_date and not lookback_days:
            raise ValueError("Must provide either start_date or lookback_days")
        
        # Calculate date range
        if lookback_days:
            lookback_days = validate_lookback_days(lookback_days, max_lookback=365)
            start_date, end_date = calculate_date_range(end_date, lookback_days)
        else:
            start_date, end_date = validate_date_range(start_date, end_date, max_days=365)
        
        # Limit to max 50 per Alpaca API constraints
        limit = min(limit, 50)
        
        logger.info(
            f"Fetching news for {symbol} from {start_date.date()} to {end_date.date()} "
            f"(limit={limit})"
        )
        
        try:
            # Create news request
            request = NewsRequest(
                symbols=symbol,
                start=start_date,
                end=end_date,
                limit=limit,
                sort="desc"  # Most recent first
            )
            
            # Fetch news from Alpaca
            news_response = self.client.get_news(request)
            
            # Convert to dict format
            articles = []
            for article in news_response.data.get("news", []):
                articles.append({
                    "title": article.headline,
                    "summary": article.summary,
                    "source": article.source,
                    "author": article.author if hasattr(article, "author") else None,
                    "published_at": article.created_at.isoformat() if article.created_at else None,
                    "url": article.url,
                    "image_url": article.images[0].url if article.images else None,
                    "symbols": article.symbols if article.symbols else [symbol],
                })
            
            result = {
                "symbol": symbol,
                "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat(),
                "article_count": len(articles),
                "articles": articles
            }
            
            logger.info(f"Retrieved {len(articles)} news articles for {symbol}")
            
            # Format output
            if format_type == "dict":
                return result
            elif format_type == "both":
                return {
                    "text": self._format_as_markdown(result),
                    "data": result
                }
            else:  # markdown
                return self._format_as_markdown(result)
            
        except Exception as e:
            logger.error(f"Error fetching company news for {symbol}: {e}", exc_info=True)
            raise
    
    @log_provider_call
    def get_global_news(
        self,
        end_date: datetime,
        start_date: Optional[datetime] = None,
        lookback_days: Optional[int] = None,
        limit: int = 50,
        format_type: Literal["dict", "markdown", "both"] = "markdown"
    ) -> Dict[str, Any] | str:
        """
        Get global/market news (not specific to any company).
        
        Args:
            end_date: End date (inclusive)
            start_date: Start date (use either this OR lookback_days, not both)
            lookback_days: Days to look back from end_date (use either this OR start_date, not both)
            limit: Maximum number of articles to return (max: 50 per request)
            format_type: Output format ('dict', 'markdown', or 'both')
        
        Returns:
            News data in requested format
        
        Raises:
            ValueError: If both start_date and lookback_days are provided, or if neither is provided
        """
        # Validate date parameters
        if start_date and lookback_days:
            raise ValueError("Provide either start_date OR lookback_days, not both")
        if not start_date and not lookback_days:
            raise ValueError("Must provide either start_date or lookback_days")
        
        # Calculate date range
        if lookback_days:
            lookback_days = validate_lookback_days(lookback_days, max_lookback=365)
            start_date, end_date = calculate_date_range(end_date, lookback_days)
        else:
            start_date, end_date = validate_date_range(start_date, end_date, max_days=365)
        
        # Limit to max 50 per Alpaca API constraints
        limit = min(limit, 50)
        
        logger.info(
            f"Fetching global news from {start_date.date()} to {end_date.date()} "
            f"(limit={limit})"
        )
        
        try:
            # Create news request without symbol filter for global news
            request = NewsRequest(
                start=start_date,
                end=end_date,
                limit=limit,
                sort="desc"  # Most recent first
            )
            
            # Fetch news from Alpaca
            news_response = self.client.get_news(request)
            
            # Convert to dict format
            articles = []
            for article in news_response.data.get("news", []):
                articles.append({
                    "title": article.headline,
                    "summary": article.summary,
                    "source": article.source,
                    "author": article.author if hasattr(article, "author") else None,
                    "published_at": article.created_at.isoformat() if article.created_at else None,
                    "url": article.url,
                    "image_url": article.images[0].url if article.images else None,
                    "symbols": article.symbols if article.symbols else [],
                })
            
            result = {
                "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat(),
                "article_count": len(articles),
                "articles": articles
            }
            
            logger.info(f"Retrieved {len(articles)} global news articles")
            
            # Format output
            if format_type == "dict":
                return result
            elif format_type == "both":
                return {
                    "text": self._format_as_markdown(result),
                    "data": result
                }
            else:  # markdown
                return self._format_as_markdown(result)
            
        except Exception as e:
            logger.error(f"Error fetching global news: {e}", exc_info=True)
            raise
