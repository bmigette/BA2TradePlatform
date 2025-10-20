"""
AI News Provider

Provides news using AI web search capabilities.
Supports both OpenAI (direct) and NagaAI models with automatic API selection.
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


class AINewsProvider(MarketNewsInterface):
    """
    AI News Provider.
    
    Uses AI web search capabilities to retrieve news articles and social media discussions.
    Supports both OpenAI Responses API (for OpenAI/ prefixed models) and NagaAI Chat Completions API 
    (for NagaAI/ prefixed models and other providers).
    
    Model format: "Provider/ModelName" (e.g., "OpenAI/gpt-5", "NagaAI/grok-4-fast-reasoning")
    """
    
    def __init__(self, model: str = None):
        """
        Initialize the AI News Provider.
        
        Args:
            model: AI model to use in format "Provider/ModelName" 
                   (e.g., "OpenAI/gpt-4o", "NagaAI/grok-4-fast-reasoning").
                   If not provided, uses OpenAI models from config.
        """
        super().__init__()
        
        # Parse model string to determine provider and API type
        self.model_string = model or f"OpenAI/{config.OPENAI_MODEL}"
        self._parse_model_config()
        
        # Initialize appropriate client
        self.client = OpenAI(
            base_url=self.backend_url,
            api_key=self.api_key
        )
        logger.info(f"AINewsProvider initialized with model={self.model_string}, api_type={self.api_type}, backend_url={self.backend_url}")
    
    def _parse_model_config(self):
        """Parse model string to determine provider, API type, and configuration."""
        # Handle legacy format (no provider prefix) - default to OpenAI
        if '/' not in self.model_string:
            self.provider = 'OpenAI'
            self.model = self.model_string
            self.api_type = 'responses'  # OpenAI uses Responses API
            self.backend_url = config.OPENAI_BACKEND_URL
            self.api_key = get_app_setting('openai_api_key') or config.OPENAI_API_KEY or "dummy-key"
            return
        
        # Parse Provider/Model format
        self.provider, self.model = self.model_string.split('/', 1)
        
        if self.provider == 'OpenAI':
            # OpenAI models use Responses API directly from OpenAI
            self.api_type = 'responses'
            self.backend_url = config.OPENAI_BACKEND_URL
            self.api_key = get_app_setting('openai_api_key') or config.OPENAI_API_KEY or "dummy-key"
        elif self.provider == 'NagaAI':
            # NagaAI uses Chat Completions API with web_search_options
            self.api_type = 'chat'
            self.backend_url = 'https://api.naga.ac/v1'
            self.api_key = get_app_setting('naga_ai_api_key') or "dummy-key"
        else:
            # Unknown provider - default to NagaAI Chat Completions API
            logger.warning(f"Unknown provider '{self.provider}', defaulting to NagaAI Chat Completions API")
            self.api_type = 'chat'
            self.backend_url = 'https://api.naga.ac/v1'
            self.api_key = get_app_setting('naga_ai_api_key') or "dummy-key"
    
    def _call_openai_responses_api(self, prompt: str) -> str:
        """Call OpenAI Responses API with web search."""
        response = self.client.responses.create(
            model=self.model,
            input=[
                {
                    "role": "system",
                    "content": [
                        {
                            "type": "input_text",
                            "text": prompt,
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
        
        # Extract text from response
        news_text = ""
        try:
            if hasattr(response, 'output') and response.output:
                for item in response.output:
                    if hasattr(item, 'content'):
                        if isinstance(item.content, list):
                            for content_item in item.content:
                                if hasattr(content_item, 'text'):
                                    news_text += content_item.text + "\n\n"
                        elif hasattr(item.content, 'text'):
                            news_text += item.content.text + "\n\n"
                    elif hasattr(item, 'text'):
                        news_text += item.text + "\n\n"
                    elif isinstance(item, str):
                        news_text += item + "\n\n"
            
            if not news_text and hasattr(response, 'reasoning') and response.reasoning:
                news_text = str(response.reasoning)
        except Exception as extract_error:
            logger.error(f"Error extracting text from OpenAI Responses API: {extract_error}")
            news_text = f"Error extracting news content: {extract_error}"
        
        return news_text.strip()
    
    def _call_nagaai_chat_api(self, prompt: str) -> str:
        """Call NagaAI Chat Completions API with web_search_options."""
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {
                    "role": "user",
                    "content": prompt
                }
            ],
            web_search_options={
                # Enable web search with default options
                # For Grok models, optionally add: "return_citations": True
            },
            temperature=1.0,
            max_tokens=4096,
            top_p=1.0
        )
        
        # Extract text from chat completion response
        try:
            if hasattr(response, 'choices') and response.choices:
                choice = response.choices[0]
                if hasattr(choice, 'message') and hasattr(choice.message, 'content'):
                    return choice.message.content
        except Exception as extract_error:
            logger.error(f"Error extracting text from NagaAI Chat API: {extract_error}")
            return f"Error extracting news content: {extract_error}"
        
        return ""
    
    def _call_ai_api(self, prompt: str) -> str:
        """Call appropriate AI API based on provider type."""
        try:
            if self.api_type == 'responses':
                return self._call_openai_responses_api(prompt)
            else:  # chat
                return self._call_nagaai_chat_api(prompt)
        except Exception as e:
            logger.error(f"Error calling {self.api_type} API for {self.model_string}: {e}", exc_info=True)
            raise
    
    def get_provider_name(self) -> str:
        """Get the provider name."""
        return "ai"
    
    def get_supported_features(self) -> Dict[str, Any]:
        """Get supported features of this provider."""
        return {
            "company_news": True,
            "global_news": True,
            "sentiment_analysis": False,  # AI doesn't provide structured sentiment
            "full_article_content": False,  # Returns summaries
            "image_urls": False,
            "source_attribution": True,  # AI includes sources
            "max_lookback_days": 365,
            "rate_limits": {
                "requests_per_minute": 60,
                "notes": "Depends on API tier and usage limits"
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
        Get news articles for a specific company using AI web search.
        
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
            f"(limit={limit}) using {self.model_string}"
        )
        
        try:
            # Format dates for the prompt
            start_str = start_date.strftime("%Y-%m-%d")
            end_str = end_date.strftime("%Y-%m-%d")
            
            # Build prompt
            prompt = (
                f"Can you search Social Media and news sources for {symbol} from {start_str} to {end_str}? "
                f"Make sure you only get the data posted during that period. Limit to {limit} articles."
            )
            
            # Call appropriate AI API
            news_text = self._call_ai_api(prompt)
            
            if not news_text:
                news_text = f"No news content retrieved for {symbol}"
                logger.warning(f"Empty response from AI API for {symbol}")
            
            result = {
                "symbol": symbol,
                "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat(),
                "content": news_text,
                "source": f"AI Web Search ({self.model_string})"
            }
            
            logger.info(f"Retrieved news for {symbol} using {self.model_string}")
            
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
            logger.error(f"Error fetching company news for {symbol} using {self.model_string}: {e}", exc_info=True)
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
        Get global/market news using AI web search.
        
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
            f"(limit={limit}) using {self.model_string}"
        )
        
        try:
            # Format dates for the prompt
            start_str = start_date.strftime("%Y-%m-%d")
            end_str = end_date.strftime("%Y-%m-%d")
            days_back = (end_date - start_date).days
            
            # Build prompt
            prompt = (
                f"Can you search global or macroeconomics news from {days_back} days before {end_str} to {end_str} "
                f"that would be informative for trading purposes? Make sure you only get the data posted during that period. "
                f"Limit the results to {limit} articles."
            )
            
            # Call appropriate AI API
            news_text = self._call_ai_api(prompt)
            
            if not news_text:
                news_text = "No global news content retrieved"
                logger.warning(f"Empty response from AI API for global news")
            
            result = {
                "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat(),
                "content": news_text,
                "source": f"AI Web Search ({self.model_string})"
            }
            
            logger.info(f"Retrieved global news using {self.model_string}")
            
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
            logger.error(f"Error fetching global news using {self.model_string}: {e}", exc_info=True)
            raise
    
    def validate_config(self) -> bool:
        """Validate provider configuration."""
        try:
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
        lines.append(f"**Source:** {data.get('source', 'AI Web Search')}")
        lines.append("")
        lines.append(data.get('content', ''))
        
        return "\n".join(lines)
