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
    calculate_date_range,
    log_provider_call,
)
from ba2_trade_platform import config
from ba2_trade_platform.config import get_app_setting
from ba2_trade_platform.logger import logger


class OpenAINewsProvider(MarketNewsInterface):
    """
    OpenAI News Provider.
    
    Uses OpenAI's web search capabilities to retrieve news articles
    and social media discussions about companies and global markets.
    """
    
    def __init__(self, model: str = None):
        """
        Initialize the OpenAI News Provider.
        
        Args:
            model: OpenAI model to use (e.g., 'gpt-4', 'gpt-4o-mini').
                   If not provided, uses OPENAI_QUICK_THINK_LLM from app settings (default: 'gpt-4')
        """
        super().__init__()
        
        # Get OpenAI configuration
        self.backend_url = config.OPENAI_BACKEND_URL
        self.model = model 
        
        self.client = OpenAI(base_url=self.backend_url)
        logger.info(f"OpenAINewsProvider initialized with model={self.model}, backend_url={self.backend_url}")
    
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
        Get news articles for a specific company using OpenAI web search.
        
        Args:
            symbol: Stock ticker symbol
            end_date: End date (inclusive)
            start_date: Start date (use either this OR lookback_days, not both)
            lookback_days: Days to look back from end_date
            limit: Maximum number of articles
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
            
            # Extract the response text - handle different response structures
            news_text = ""
            
            try:
                # Try to get the output from the response
                if hasattr(response, 'output') and response.output:
                    # Iterate through output items to find text content
                    for item in response.output:
                        # Check if item has content attribute
                        if hasattr(item, 'content'):
                            if isinstance(item.content, list):
                                for content_item in item.content:
                                    if hasattr(content_item, 'text'):
                                        news_text += content_item.text + "\n\n"
                            elif hasattr(item.content, 'text'):
                                news_text += item.content.text + "\n\n"
                        # Check if item has text attribute directly
                        elif hasattr(item, 'text'):
                            news_text += item.text + "\n\n"
                        # Check if item is a string
                        elif isinstance(item, str):
                            news_text += item + "\n\n"
                
                # If no text found, try to get reasoning or other content
                if not news_text:
                    if hasattr(response, 'reasoning') and response.reasoning:
                        news_text = str(response.reasoning)
                    else:
                        # Last resort - convert entire response to string
                        news_text = f"Response received but could not extract text content. Raw response type: {type(response)}"
                        logger.warning(f"Could not extract text from OpenAI response for {symbol}. Response attributes: {dir(response)}")
                
            except Exception as extract_error:
                logger.error(f"Error extracting text from OpenAI response: {extract_error}")
                news_text = f"Error extracting news content: {extract_error}"
            
            result = {
                "symbol": symbol,
                "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat(),
                "content": news_text.strip(),
                "source": "OpenAI Web Search"
            }
            
            logger.info(f"Retrieved news for {symbol} from OpenAI")
            
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
            logger.error(f"Error fetching company news for {symbol} from OpenAI: {e}", exc_info=True)
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
        Get global/market news using OpenAI web search.
        
        Args:
            end_date: End date (inclusive)
            start_date: Start date (use either this OR lookback_days, not both)
            lookback_days: Days to look back from end_date
            limit: Maximum number of articles
            format_type: Output format ('dict', 'markdown', or 'both')
        
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
            
            # Extract the response text - handle different response structures
            news_text = ""
            
            try:
                # Try to get the output from the response
                if hasattr(response, 'output') and response.output:
                    # Iterate through output items to find text content
                    for item in response.output:
                        # Check if item has content attribute
                        if hasattr(item, 'content'):
                            if isinstance(item.content, list):
                                for content_item in item.content:
                                    if hasattr(content_item, 'text'):
                                        news_text += content_item.text + "\n\n"
                            elif hasattr(item.content, 'text'):
                                news_text += item.content.text + "\n\n"
                        # Check if item has text attribute directly
                        elif hasattr(item, 'text'):
                            news_text += item.text + "\n\n"
                        # Check if item is a string
                        elif isinstance(item, str):
                            news_text += item + "\n\n"
                
                # If no text found, try to get reasoning or other content
                if not news_text:
                    if hasattr(response, 'reasoning') and response.reasoning:
                        news_text = str(response.reasoning)
                    else:
                        # Last resort - convert entire response to string
                        news_text = f"Response received but could not extract text content. Raw response type: {type(response)}"
                        logger.warning(f"Could not extract text from OpenAI response for global news. Response attributes: {dir(response)}")
                
            except Exception as extract_error:
                logger.error(f"Error extracting text from OpenAI response: {extract_error}")
                news_text = f"Error extracting news content: {extract_error}"
            
            result = {
                "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat(),
                "content": news_text.strip(),
                "source": "OpenAI Web Search"
            }
            
            logger.info(f"Retrieved global news from OpenAI")
            
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
            logger.error(f"Error fetching global news from OpenAI: {e}", exc_info=True)
            raise
    
    def validate_config(self) -> bool:
        """Validate provider configuration."""
        try:
            # Check if OpenAI client is properly initialized
            return self.client is not None and bool(self.model)
        except Exception:
            return False
    
    def _format_as_dict(self, data: Any) -> Dict[str, Any]:
        """Format data as structured dictionary."""
        if isinstance(data, dict):
            return data
        return {"content": str(data)}
    
    def _format_as_markdown(self, data: Any) -> str:
        """Format data as markdown."""
        # Handle the standard interface signature
        if isinstance(data, str):
            return data
        
        if not isinstance(data, dict):
            return str(data)
        
        # Determine if it's company or global news
        is_company_news = 'symbol' in data
        
        lines = []
        
        # Header
        if is_company_news:
            lines.append(f"# News for {data.get('symbol', 'Unknown')}")
        else:
            lines.append("# Global Market News")
        
        lines.append(f"**Period:** {data.get('start_date', 'N/A')[:10]} to {data.get('end_date', 'N/A')[:10]}")
        lines.append(f"**Source:** {data.get('source', 'OpenAI Web Search')}")
        lines.append("")
        lines.append(data.get('content', ''))
        
        return "\n".join(lines)
