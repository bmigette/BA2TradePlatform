"""
Financial Modeling Prep (FMP) News Provider

This provider uses the FMP API to retrieve company-specific and general market news.

API Documentation: https://site.financialmodelingprep.com/developer/docs#stock-news
"""

from typing import Dict, Any, Literal, Optional
from datetime import datetime, timedelta, timezone

import fmpsdk

from ba2_trade_platform.core.interfaces import MarketNewsInterface
from ba2_trade_platform.core.provider_utils import calculate_date_range, log_provider_call
from ba2_trade_platform.config import get_app_setting
from ba2_trade_platform.logger import logger


class FMPNewsProvider(MarketNewsInterface):
    """
    Financial Modeling Prep News Provider.
    
    Provides access to news articles from FMP API, including:
    - Company-specific news articles
    - General market news
    - Article summaries and metadata
    - Source attribution
    
    Requires FMP API key (free tier available).
    """
    
    def __init__(self):
        """Initialize the FMP News Provider with API key."""
        super().__init__()
        
        # Get API key from settings
        self.api_key = get_app_setting("FMP_API_KEY")
        
        if not self.api_key:
            raise ValueError(
                "FMP API key not configured. "
                "Please set 'FMP_API_KEY' in AppSetting table."
            )
        
        logger.info("FMPNewsProvider initialized successfully")
    
    def get_provider_name(self) -> str:
        """Get the provider name."""
        return "fmp"
    
    def get_supported_features(self) -> list[str]:
        """Get supported features of this provider."""
        return ["company_news", "global_news"]
    
    def validate_config(self) -> bool:
        """Validate provider configuration."""
        return bool(self.api_key)
    
    def _format_as_dict(self, data: Any) -> Dict[str, Any]:
        """Format news data as dictionary."""
        if isinstance(data, dict):
            return data
        return {"data": data}
    
    def _format_as_markdown(self, data: Any) -> str:
        """Format news data as markdown."""
        if isinstance(data, str):
            return data
        if isinstance(data, dict) and "markdown" in data:
            return data["markdown"]
        return str(data)
    
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
            limit: Maximum number of articles to return
            format_type: Output format ('dict', 'markdown', or 'both')
        
        Returns:
            If format_type='dict': Dictionary with news data
            If format_type='markdown': Formatted markdown string
            If format_type='both': Dict with keys 'text' (markdown) and 'data' (dict)
        
        Raises:
            ValueError: If both start_date and lookback_days are provided, or if neither is provided
        """
        # Validate date parameters
        if start_date and lookback_days:
            raise ValueError("Provide either start_date OR lookback_days, not both")
        if not start_date and not lookback_days:
            raise ValueError("Must provide either start_date or lookback_days")
        
        # Calculate start_date if using lookback_days
        if lookback_days:
            actual_start_date, end_date = calculate_date_range(end_date, lookback_days)
        else:
            actual_start_date = start_date
        
        logger.debug(
            f"Fetching FMP company news for {symbol} from "
            f"{actual_start_date.strftime('%Y-%m-%d')} to {end_date.strftime('%Y-%m-%d')}"
        )
        
        try:
            # FMP stock_news returns list of news articles
            # Note: FMP doesn't support date filtering in API, we filter manually
            news_data = fmpsdk.stock_news(
                apikey=self.api_key,
                tickers=symbol,
                limit=limit * 2  # Get extra to account for filtering
            )
            
            if not news_data:
                logger.warning(f"No news data returned from FMP for {symbol}")
                return self._format_empty_response(symbol, actual_start_date, end_date, format_type)
            
            # Filter by date range (FMP returns publishedDate as string)
            filtered_articles = []
            for article in news_data:
                if "publishedDate" in article:
                    pub_date = datetime.fromisoformat(article["publishedDate"].replace("Z", "+00:00"))
                    # Ensure all dates are timezone-aware for comparison
                    if pub_date.tzinfo is None:
                        pub_date = pub_date.replace(tzinfo=timezone.utc)
                    if actual_start_date <= pub_date <= end_date:
                        filtered_articles.append(article)
                        if len(filtered_articles) >= limit:
                            break
            
            # Build dict response
            dict_response = {
                "symbol": symbol,
                "start_date": actual_start_date.isoformat(),
                "end_date": end_date.isoformat(),
                "article_count": len(filtered_articles),
                "articles": [
                    {
                        "title": article.get("title", ""),
                        "summary": article.get("text", ""),
                        "source": article.get("site", ""),
                        "published_at": article.get("publishedDate", ""),
                        "url": article.get("url", ""),
                        "image_url": article.get("image", ""),
                        "symbol": article.get("symbol", symbol)
                    }
                    for article in filtered_articles
                ]
            }
            
            # Build markdown response
            markdown = f"# News for {symbol}\n\n"
            markdown += f"**Period:** {actual_start_date.strftime('%Y-%m-%d')} to {end_date.strftime('%Y-%m-%d')}\n"
            markdown += f"**Articles:** {len(filtered_articles)}\n\n"
            
            for i, article in enumerate(filtered_articles, 1):
                markdown += f"## {i}. {article.get('title', 'No Title')}\n\n"
                markdown += f"**Source:** {article.get('site', 'Unknown')} | "
                markdown += f"**Published:** {article.get('publishedDate', 'Unknown')}\n\n"
                markdown += f"{article.get('text', 'No summary available.')}\n\n"
                if article.get('url'):
                    markdown += f"[Read more]({article['url']})\n\n"
                markdown += "---\n\n"
            
            # Return based on format_type
            if format_type == "dict":
                return dict_response
            elif format_type == "both":
                return {
                    "text": markdown,
                    "data": dict_response
                }
            else:  # markdown
                return markdown
                
        except Exception as e:
            logger.error(f"Error fetching FMP company news for {symbol}: {e}")
            return f"Error fetching news: {str(e)}"
    
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
            limit: Maximum number of articles to return
            format_type: Output format ('dict', 'markdown', or 'both')
        
        Returns:
            If format_type='dict': Dictionary with news data
            If format_type='markdown': Formatted markdown string
            If format_type='both': Dict with keys 'text' (markdown) and 'data' (dict)
        
        Raises:
            ValueError: If both start_date and lookback_days are provided, or if neither is provided
        """
        # Validate date parameters
        if start_date and lookback_days:
            raise ValueError("Provide either start_date OR lookback_days, not both")
        if not start_date and not lookback_days:
            raise ValueError("Must provide either start_date or lookback_days")
        
        # Calculate start_date if using lookback_days
        if lookback_days:
            actual_start_date, end_date = calculate_date_range(end_date, lookback_days)
        else:
            actual_start_date = start_date
        
        logger.debug(
            f"Fetching FMP general news from "
            f"{actual_start_date.strftime('%Y-%m-%d')} to {end_date.strftime('%Y-%m-%d')}"
        )
        
        try:
            # FMP general_news returns latest market news
            news_data = fmpsdk.general_news(
                apikey=self.api_key,
                page=0  # Get first page
            )
            
            if not news_data:
                logger.warning("No general news data returned from FMP")
                return self._format_empty_response(None, actual_start_date, end_date, format_type)
            
            # Filter by date range and limit
            filtered_articles = []
            for article in news_data:
                if "publishedDate" in article:
                    pub_date = datetime.fromisoformat(article["publishedDate"].replace("Z", "+00:00"))
                    # Ensure all dates are timezone-aware for comparison
                    if pub_date.tzinfo is None:
                        pub_date = pub_date.replace(tzinfo=timezone.utc)
                    if actual_start_date <= pub_date <= end_date:
                        filtered_articles.append(article)
                        if len(filtered_articles) >= limit:
                            break
            
            # Build dict response
            dict_response = {
                "start_date": actual_start_date.isoformat(),
                "end_date": end_date.isoformat(),
                "article_count": len(filtered_articles),
                "articles": [
                    {
                        "title": article.get("title", ""),
                        "summary": article.get("text", ""),
                        "source": article.get("site", ""),
                        "published_at": article.get("publishedDate", ""),
                        "url": article.get("url", ""),
                        "image_url": article.get("image", "")
                    }
                    for article in filtered_articles
                ]
            }
            
            # Build markdown response
            markdown = f"# General Market News\n\n"
            markdown += f"**Period:** {actual_start_date.strftime('%Y-%m-%d')} to {end_date.strftime('%Y-%m-%d')}\n"
            markdown += f"**Articles:** {len(filtered_articles)}\n\n"
            
            for i, article in enumerate(filtered_articles, 1):
                markdown += f"## {i}. {article.get('title', 'No Title')}\n\n"
                markdown += f"**Source:** {article.get('site', 'Unknown')} | "
                markdown += f"**Published:** {article.get('publishedDate', 'Unknown')}\n\n"
                markdown += f"{article.get('text', 'No summary available.')}\n\n"
                if article.get('url'):
                    markdown += f"[Read more]({article['url']})\n\n"
                markdown += "---\n\n"
            
            # Return based on format_type
            if format_type == "dict":
                return dict_response
            elif format_type == "both":
                return {
                    "text": markdown,
                    "data": dict_response
                }
            else:  # markdown
                return markdown
                
        except Exception as e:
            logger.error(f"Error fetching FMP general news: {e}")
            return f"Error fetching news: {str(e)}"
    
    def _format_empty_response(
        self, 
        symbol: Optional[str], 
        start_date: datetime, 
        end_date: datetime,
        format_type: Literal["dict", "markdown"]
    ) -> Dict[str, Any] | str:
        """Format an empty response when no news is found."""
        if format_type == "dict":
            result = {
                "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat(),
                "article_count": 0,
                "articles": []
            }
            if symbol:
                result["symbol"] = symbol
            return result
        else:
            title = f"News for {symbol}" if symbol else "General Market News"
            return f"# {title}\n\nNo news articles found for the specified period.\n"
