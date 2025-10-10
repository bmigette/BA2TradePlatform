"""
Alpha Vantage News Provider

Provides company-specific news using Alpha Vantage News Sentiment API.
"""

from typing import Dict, Any, Literal, Optional
from datetime import datetime, timezone
import json

from ba2_trade_platform.core.interfaces import MarketNewsInterface
from ba2_trade_platform.core.provider_utils import (
    validate_date_range,
    validate_lookback_days,
    calculate_date_range,
    log_provider_call
)
from ba2_trade_platform.modules.dataproviders.alpha_vantage_common import (
    AlphaVantageBaseProvider,
    format_datetime_for_api,
    AlphaVantageRateLimitError
)
from ba2_trade_platform.logger import logger


class AlphaVantageNewsProvider(AlphaVantageBaseProvider, MarketNewsInterface):
    """
    Alpha Vantage News Sentiment Provider.
    
    Provides access to live and historical market news & sentiment data from
    premier news outlets worldwide. Covers stocks, cryptocurrencies, forex,
    and topics like fiscal policy, mergers & acquisitions, IPOs.
    """
    
    def __init__(self, source: str = "ba2_trade_platform"):
        """
        Initialize the Alpha Vantage News Provider with API credentials.
        
        Args:
            source: Source identifier for API tracking (e.g., 'ba2_trade_platform', 'trading_agents')
        """
        AlphaVantageBaseProvider.__init__(self, source)
        MarketNewsInterface.__init__(self)
        logger.info(f"AlphaVantageNewsProvider initialized successfully with source: {source}")
    
    def get_provider_name(self) -> str:
        """Get the provider name."""
        return "alphavantage"
    
    def get_supported_features(self) -> list[str]:
        """Get supported features of this provider."""
        return ["company_news", "sentiment_analysis"]
    
    def validate_config(self) -> bool:
        """
        Validate provider configuration.
        
        Returns:
            bool: True if configuration is valid (Alpha Vantage handled by common module)
        """
        # Alpha Vantage API key is managed by alpha_vantage_common.make_api_request
        # which validates the key on each request
        return True
    
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
        lines = []
        
        # Header
        if "symbol" in data:
            lines.append(f"# News for {data['symbol']}")
        else:
            lines.append("# News")
        
        lines.append(f"**Period:** {data['start_date'][:10]} to {data['end_date'][:10]}")
        lines.append(f"**Articles:** {data['article_count']}")
        lines.append("")
        
        # Articles
        for i, article in enumerate(data.get("articles", []), 1):
            lines.append(f"## {i}. {article['title']}")
            lines.append(f"**Source:** {article['source']}")
            
            if article.get('authors'):
                lines.append(f"**Authors:** {', '.join(article['authors'])}")
            
            if article.get('published_at'):
                pub_date = article['published_at'][:19].replace('T', ' ')
                lines.append(f"**Published:** {pub_date}")
            
            if article.get('sentiment'):
                sentiment = article['sentiment']
                lines.append(f"**Sentiment:** {sentiment['label']} (score: {sentiment['score']:.2f})")
            
            if article.get('topics'):
                topics_str = ", ".join([f"{t['topic']} ({t['relevance']:.1%})" for t in article['topics'][:3]])
                lines.append(f"**Topics:** {topics_str}")
            
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
            symbol: Stock ticker symbol
            end_date: End date (inclusive)
            start_date: Start date (use either this OR lookback_days, not both)
            lookback_days: Days to look back from end_date
            limit: Maximum number of articles (Alpha Vantage limit: 50)
            format_type: Output format ('dict', 'markdown', or 'both')
        
        Returns:
            News data in requested format
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
        
        logger.info(
            f"Fetching news for {symbol} from {start_date.date()} to {end_date.date()} "
            f"(limit={limit})"
        )
        
        try:
            # Call Alpha Vantage API
            params = {
                "tickers": symbol,
                "time_from": format_datetime_for_api(start_date),
                "time_to": format_datetime_for_api(end_date),
                "sort": "LATEST",
                "limit": str(min(limit, 50)),
            }
            
            raw_data = self.make_api_request("NEWS_SENTIMENT", params)
            
            # Parse the response
            if isinstance(raw_data, str):
                import json
                raw_data = json.loads(raw_data)
            
            # Extract articles
            articles = []
            feed = raw_data.get("feed", [])
            
            for item in feed[:limit]:
                article = {
                    "title": item.get("title", ""),
                    "summary": item.get("summary", ""),
                    "source": item.get("source", ""),
                    "author": item.get("authors", [None])[0] if item.get("authors") else None,
                    "published_at": item.get("time_published", ""),
                    "url": item.get("url", ""),
                    "image_url": item.get("banner_image"),
                    "sentiment": self._parse_sentiment(item.get("overall_sentiment_label")),
                    "sentiment_score": item.get("overall_sentiment_score"),
                    "symbols": [ticker.get("ticker") for ticker in item.get("ticker_sentiment", [])]
                }
                articles.append(article)
            
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
    
    def get_global_news(
        self,
        end_date: datetime,
        start_date: Optional[datetime] = None,
        lookback_days: Optional[int] = None,
        limit: int = 50,
        format_type: Literal["dict", "markdown", "both"] = "markdown"
    ) -> Dict[str, Any] | str:
        """
        Get global/market news.
        
        Note: Alpha Vantage API is focused on company-specific news.
        This method returns general market news without ticker filter.
        """
        # Alpha Vantage doesn't have a pure "global news" endpoint
        # We'll call without ticker filter and return general news
        logger.warning("Alpha Vantage doesn't have dedicated global news - returning general market news")
        
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
        
        try:
            params = {
                "time_from": format_datetime_for_api(start_date),
                "time_to": format_datetime_for_api(end_date),
                "sort": "LATEST",
                "limit": str(min(limit, 50)),
            }
            
            raw_data = self.make_api_request("NEWS_SENTIMENT", params)
            
            # Parse the response
            if isinstance(raw_data, str):
                import json
                raw_data = json.loads(raw_data)
            
            # Extract articles
            articles = []
            feed = raw_data.get("feed", [])
            
            for item in feed[:limit]:
                article = {
                    "title": item.get("title", ""),
                    "summary": item.get("summary", ""),
                    "source": item.get("source", ""),
                    "author": item.get("authors", [None])[0] if item.get("authors") else None,
                    "published_at": item.get("time_published", ""),
                    "url": item.get("url", ""),
                    "image_url": item.get("banner_image"),
                    "sentiment": self._parse_sentiment(item.get("overall_sentiment_label")),
                    "sentiment_score": item.get("overall_sentiment_score"),
                    "symbols": [ticker.get("ticker") for ticker in item.get("ticker_sentiment", [])]
                }
                articles.append(article)
            
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
    
    def _parse_sentiment(self, sentiment_label: Optional[str]) -> Optional[str]:
        """Parse sentiment label to standard format."""
        if not sentiment_label:
            return None
        
        label = sentiment_label.lower()
        if "bullish" in label or "positive" in label:
            return "positive"
        elif "bearish" in label or "negative" in label:
            return "negative"
        else:
            return "neutral"
