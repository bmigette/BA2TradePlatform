"""
Finnhub News Provider

Provides company-specific and general market news using Finnhub API.

API Documentation: https://finnhub.io/docs/api/company-news
"""

from typing import Dict, Any, Literal, Optional
from datetime import datetime, timedelta, timezone
import requests

from ba2_trade_platform.core.interfaces import MarketNewsInterface
from ba2_trade_platform.core.provider_utils import calculate_date_range, log_provider_call
from ba2_trade_platform.config import get_app_setting
from ba2_trade_platform.logger import logger


class FinnhubNewsProvider(MarketNewsInterface):
    """
    Finnhub News Provider.
    
    Provides access to news articles from Finnhub API, including:
    - Company-specific news articles
    - General market news
    - Article summaries and metadata
    - Source attribution
    
    Requires Finnhub API key (free tier available at https://finnhub.io).
    """
    
    def __init__(self):
        """Initialize the Finnhub News Provider with API key."""
        super().__init__()
        
        # Get API key from settings
        self.api_key = get_app_setting("finnhub_api_key")
        
        if not self.api_key:
            raise ValueError(
                "Finnhub API key not configured. "
                "Please set 'finnhub_api_key' in AppSetting table."
            )
        
        logger.info("FinnhubNewsProvider initialized successfully")
    
    def get_provider_name(self) -> str:
        """Get the provider name."""
        return "finnhub"
    
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
            limit: Maximum number of articles to return (Finnhub doesn't enforce limit, we filter)
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
            f"Fetching Finnhub company news for {symbol} from "
            f"{actual_start_date.strftime('%Y-%m-%d')} to {end_date.strftime('%Y-%m-%d')}"
        )
        
        try:
            # Finnhub API endpoint for company news
            # API expects dates in YYYY-MM-DD format
            url = "https://finnhub.io/api/v1/company-news"
            params = {
                "symbol": symbol.upper(),
                "from": actual_start_date.strftime("%Y-%m-%d"),
                "to": end_date.strftime("%Y-%m-%d"),
                "token": self.api_key
            }
            
            # Make API request with timeout and retry logic
            max_retries = 3
            timeout = 60
            response = None
            
            for attempt in range(1, max_retries + 1):
                try:
                    response = requests.get(url, params=params, timeout=timeout)
                    response.raise_for_status()
                    break  # Success
                except requests.exceptions.ReadTimeout:
                    if attempt < max_retries:
                        logger.warning(f"Finnhub API timeout for {symbol} (attempt {attempt}/{max_retries}), retrying...")
                        continue
                    else:
                        error_msg = f"Failed to fetch news for {symbol} after {max_retries} attempts (timeout)"
                        logger.error(error_msg)
                        raise ValueError(error_msg)
                except requests.exceptions.RequestException as e:
                    error_msg = f"Failed to fetch Finnhub news for {symbol}: {e}"
                    logger.error(error_msg)
                    raise ValueError(error_msg) from e
            
            news_data = response.json()
            
            if not news_data or not isinstance(news_data, list):
                logger.warning(f"No news data returned from Finnhub for {symbol}")
                return self._format_empty_response(symbol, actual_start_date, end_date, format_type)
            
            # Limit results if needed
            filtered_articles = news_data[:limit] if limit else news_data
            
            # Build dict response
            dict_response = {
                "symbol": symbol,
                "start_date": actual_start_date.isoformat(),
                "end_date": end_date.isoformat(),
                "article_count": len(filtered_articles),
                "articles": [
                    {
                        "title": article.get("headline", ""),
                        "summary": article.get("summary", ""),
                        "source": article.get("source", ""),
                        "published_at": datetime.fromtimestamp(
                            article.get("datetime", 0), tz=timezone.utc
                        ).isoformat() if article.get("datetime") else "",
                        "url": article.get("url", ""),
                        "image_url": article.get("image", ""),
                        "category": article.get("category", ""),
                        "related_symbols": article.get("related", "")
                    }
                    for article in filtered_articles
                ]
            }
            
            # Build markdown response
            markdown = f"# News for {symbol}\n\n"
            markdown += f"**Period:** {actual_start_date.strftime('%Y-%m-%d')} to {end_date.strftime('%Y-%m-%d')}\n"
            markdown += f"**Articles:** {len(filtered_articles)}\n\n"
            
            for i, article in enumerate(filtered_articles, 1):
                markdown += f"## {i}. {article.get('headline', 'No Title')}\n\n"
                markdown += f"**Source:** {article.get('source', 'Unknown')}"
                
                # Format timestamp
                if article.get('datetime'):
                    dt = datetime.fromtimestamp(article['datetime'], tz=timezone.utc)
                    markdown += f" | **Published:** {dt.strftime('%Y-%m-%d %H:%M:%S')} UTC"
                
                if article.get('category'):
                    markdown += f" | **Category:** {article['category']}"
                
                markdown += "\n\n"
                
                # Summary
                summary = article.get('summary', 'No summary available.')
                if summary:
                    markdown += f"{summary}\n\n"
                
                # Related symbols
                if article.get('related'):
                    related = article['related']
                    if isinstance(related, list):
                        related = ", ".join(related)
                    markdown += f"**Related Symbols:** {related}\n\n"
                
                # URL
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
                
        except ValueError:
            # Re-raise ValueError (from our error handling)
            raise
        except Exception as e:
            error_msg = f"Error fetching Finnhub company news for {symbol}: {e}"
            logger.error(error_msg)
            raise ValueError(error_msg) from e
    
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
        Get global/market news (general news category).
        
        Finnhub provides general market news through the same API endpoint
        using 'general' as the category parameter.
        
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
            f"Fetching Finnhub global news from "
            f"{actual_start_date.strftime('%Y-%m-%d')} to {end_date.strftime('%Y-%m-%d')}"
        )
        
        try:
            # Finnhub API endpoint for general/market news
            url = "https://finnhub.io/api/v1/news"
            params = {
                "category": "general",
                "token": self.api_key
            }
            
            # Make API request with timeout and retry logic
            max_retries = 3
            timeout = 60
            response = None
            
            for attempt in range(1, max_retries + 1):
                try:
                    response = requests.get(url, params=params, timeout=timeout)
                    response.raise_for_status()
                    break  # Success
                except requests.exceptions.ReadTimeout:
                    if attempt < max_retries:
                        logger.warning(f"Finnhub global news API timeout (attempt {attempt}/{max_retries}), retrying...")
                        continue
                    else:
                        error_msg = f"Failed to fetch global news after {max_retries} attempts (timeout)"
                        logger.error(error_msg)
                        raise ValueError(error_msg)
                except requests.exceptions.RequestException as e:
                    error_msg = f"Failed to fetch Finnhub global news: {e}"
                    logger.error(error_msg)
                    raise ValueError(error_msg) from e
            
            news_data = response.json()
            
            if not news_data or not isinstance(news_data, list):
                logger.warning("No global news data returned from Finnhub")
                return self._format_empty_response(None, actual_start_date, end_date, format_type)
            
            # Filter by date range (Finnhub general news doesn't support date filtering, so we do it manually)
            filtered_articles = []
            for article in news_data:
                if article.get('datetime'):
                    article_date = datetime.fromtimestamp(article['datetime'], tz=timezone.utc)
                    # Ensure all dates are timezone-aware for comparison
                    if actual_start_date.tzinfo is None:
                        actual_start_date = actual_start_date.replace(tzinfo=timezone.utc)
                    if end_date.tzinfo is None:
                        end_date = end_date.replace(tzinfo=timezone.utc)
                    
                    if actual_start_date <= article_date <= end_date:
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
                        "title": article.get("headline", ""),
                        "summary": article.get("summary", ""),
                        "source": article.get("source", ""),
                        "published_at": datetime.fromtimestamp(
                            article.get("datetime", 0), tz=timezone.utc
                        ).isoformat() if article.get("datetime") else "",
                        "url": article.get("url", ""),
                        "image_url": article.get("image", ""),
                        "category": article.get("category", "")
                    }
                    for article in filtered_articles
                ]
            }
            
            # Build markdown response
            markdown = f"# General Market News\n\n"
            markdown += f"**Period:** {actual_start_date.strftime('%Y-%m-%d')} to {end_date.strftime('%Y-%m-%d')}\n"
            markdown += f"**Articles:** {len(filtered_articles)}\n\n"
            
            for i, article in enumerate(filtered_articles, 1):
                markdown += f"## {i}. {article.get('headline', 'No Title')}\n\n"
                markdown += f"**Source:** {article.get('source', 'Unknown')}"
                
                # Format timestamp
                if article.get('datetime'):
                    dt = datetime.fromtimestamp(article['datetime'], tz=timezone.utc)
                    markdown += f" | **Published:** {dt.strftime('%Y-%m-%d %H:%M:%S')} UTC"
                
                if article.get('category'):
                    markdown += f" | **Category:** {article['category']}"
                
                markdown += "\n\n"
                
                # Summary
                summary = article.get('summary', 'No summary available.')
                if summary:
                    markdown += f"{summary}\n\n"
                
                # URL
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
                
        except ValueError:
            # Re-raise ValueError (from our error handling)
            raise
        except Exception as e:
            error_msg = f"Error fetching Finnhub global news: {e}"
            logger.error(error_msg)
            raise ValueError(error_msg) from e
    
    def _format_empty_response(
        self, 
        symbol: Optional[str], 
        start_date: datetime, 
        end_date: datetime,
        format_type: Literal["dict", "markdown", "both"]
    ) -> Dict[str, Any] | str:
        """Format an empty response when no news is found."""
        dict_result = {
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "article_count": 0,
            "articles": []
        }
        if symbol:
            dict_result["symbol"] = symbol
        
        if format_type == "dict":
            return dict_result
        elif format_type == "both":
            title = f"News for {symbol}" if symbol else "General Market News"
            markdown = f"# {title}\n\nNo news articles found for the specified period.\n"
            return {
                "text": markdown,
                "data": dict_result
            }
        else:  # markdown
            title = f"News for {symbol}" if symbol else "General Market News"
            return f"# {title}\n\nNo news articles found for the specified period.\n"
