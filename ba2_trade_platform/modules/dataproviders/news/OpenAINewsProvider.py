"""
OpenAI News Provider

Provides news using OpenAI's web search capabilities.
"""

from typing import Dict, Any, Literal, Optional
from datetime import datetime, timedelta
from openai import OpenAI

from ba2_trade_platform.core.interfaces import MarketNewsInterface
from ba2_trade_platform.core.provider_utils import (
    validate_date_range,
    validate_lookback_days,
    calculate_date_range
)
from ba2_trade_platform.config import get_app_setting
from ba2_trade_platform.logger import logger


class OpenAINewsProvider(MarketNewsInterface):
    """
    OpenAI News Provider.
    
    Uses OpenAI's web search capabilities to retrieve news articles
    and social media discussions about companies and global markets.
    """
    
    def __init__(self):
        """Initialize the OpenAI News Provider."""
        super().__init__()
        
        # Get OpenAI configuration
        self.backend_url = get_app_setting("OPENAI_BACKEND_URL", "https://api.openai.com/v1")
        self.model = get_app_setting("OPENAI_QUICK_THINK_LLM", "gpt-4")
        
        self.client = OpenAI(base_url=self.backend_url)
        logger.info("OpenAINewsProvider initialized successfully")
    
    def get_provider_name(self) -> str:
        """Get the provider name."""
        return "openai"
    
    def get_supported_features(self) -> Dict[str, Any]:
        """Get supported features of this provider."""
        return {
            "company_news": True,
            "global_news": True,
            "sentiment_analysis": False,  # OpenAI doesn't provide structured sentiment
            "full_article_content": False,  # Returns summaries
            "image_urls": False,
            "source_attribution": True,  # OpenAI includes sources
            "max_lookback_days": 365,
            "rate_limits": {
                "requests_per_minute": 60,
                "notes": "Depends on OpenAI API tier and usage limits"
            }
        }
    
    def get_company_news(
        self,
        symbol: str,
        end_date: datetime,
        start_date: Optional[datetime] = None,
        lookback_days: Optional[int] = None,
        limit: int = 50,
        format_type: Literal["dict", "markdown"] = "markdown"
    ) -> Dict[str, Any] | str:
        """
        Get news articles for a specific company using OpenAI web search.
        
        Args:
            symbol: Stock ticker symbol
            end_date: End date (inclusive)
            start_date: Start date (use either this OR lookback_days, not both)
            lookback_days: Days to look back from end_date
            limit: Maximum number of articles
            format_type: Output format ('dict' or 'markdown')
        
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
            f"(limit={limit}) using OpenAI"
        )
        
        try:
            # Format dates for the prompt
            start_str = start_date.strftime("%Y-%m-%d")
            end_str = end_date.strftime("%Y-%m-%d")
            
            # Call OpenAI API with web search
            response = self.client.responses.create(
                model=self.model,
                input=[
                    {
                        "role": "system",
                        "content": [
                            {
                                "type": "input_text",
                                "text": f"Can you search Social Media and news sources for {symbol} from {start_str} to {end_str}? Make sure you only get the data posted during that period. Limit to {limit} articles.",
                            }
                        ],
                    }
                ],
                text={"format": {"type": "text"}},
                reasoning={},
                tools=[
                    {
                        "type": "web_search_preview",
                        "user_location": {"type": "approximate"},
                        "search_context_size": "low",
                    }
                ],
                temperature=1,
                max_output_tokens=4096,
                top_p=1,
                store=True,
            )
            
            # Extract the response text
            news_text = response.output[1].content[0].text
            
            result = {
                "symbol": symbol,
                "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat(),
                "content": news_text,
                "source": "OpenAI Web Search"
            }
            
            logger.info(f"Retrieved news for {symbol} from OpenAI")
            
            # Format output
            if format_type == "dict":
                return result
            else:
                return self._format_as_markdown(result, is_company_news=True)
            
        except Exception as e:
            logger.error(f"Error fetching company news for {symbol} from OpenAI: {e}", exc_info=True)
            raise
    
    def get_global_news(
        self,
        end_date: datetime,
        start_date: Optional[datetime] = None,
        lookback_days: Optional[int] = None,
        limit: int = 50,
        format_type: Literal["dict", "markdown"] = "markdown"
    ) -> Dict[str, Any] | str:
        """
        Get global/market news using OpenAI web search.
        
        Args:
            end_date: End date (inclusive)
            start_date: Start date (use either this OR lookback_days, not both)
            lookback_days: Days to look back from end_date
            limit: Maximum number of articles
            format_type: Output format ('dict' or 'markdown')
        
        Returns:
            Global news data in requested format
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
            f"Fetching global news from {start_date.date()} to {end_date.date()} "
            f"(limit={limit}) using OpenAI"
        )
        
        try:
            # Format dates for the prompt
            start_str = start_date.strftime("%Y-%m-%d")
            end_str = end_date.strftime("%Y-%m-%d")
            days_back = (end_date - start_date).days
            
            # Call OpenAI API with web search
            response = self.client.responses.create(
                model=self.model,
                input=[
                    {
                        "role": "system",
                        "content": [
                            {
                                "type": "input_text",
                                "text": f"Can you search global or macroeconomics news from {days_back} days before {end_str} to {end_str} that would be informative for trading purposes? Make sure you only get the data posted during that period. Limit the results to {limit} articles.",
                            }
                        ],
                    }
                ],
                text={"format": {"type": "text"}},
                reasoning={},
                tools=[
                    {
                        "type": "web_search_preview",
                        "user_location": {"type": "approximate"},
                        "search_context_size": "low",
                    }
                ],
                temperature=1,
                max_output_tokens=4096,
                top_p=1,
                store=True,
            )
            
            # Extract the response text
            news_text = response.output[1].content[0].text
            
            result = {
                "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat(),
                "content": news_text,
                "source": "OpenAI Web Search"
            }
            
            logger.info(f"Retrieved global news from OpenAI")
            
            # Format output
            if format_type == "dict":
                return result
            else:
                return self._format_as_markdown(result, is_company_news=False)
            
        except Exception as e:
            logger.error(f"Error fetching global news from OpenAI: {e}", exc_info=True)
            raise
    
    def _format_as_markdown(self, data: Dict[str, Any], is_company_news: bool) -> str:
        """Format news data as markdown."""
        lines = []
        
        # Header
        if is_company_news:
            lines.append(f"# News for {data['symbol']}")
        else:
            lines.append("# Global Market News")
        
        lines.append(f"**Period:** {data['start_date'][:10]} to {data['end_date'][:10]}")
        lines.append(f"**Source:** {data['source']}")
        lines.append("")
        lines.append(data['content'])
        
        return "\n".join(lines)
