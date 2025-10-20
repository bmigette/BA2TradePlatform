"""
AI Social Media Sentiment Provider

Provides social media sentiment analysis using AI web search capabilities.
Crawls public social media sources (Twitter/X, Reddit, StockTwits, forums, etc.)
to analyze sentiment and discussions about a specific stock symbol.

Supports both OpenAI (direct) and NagaAI models with automatic API selection.
"""

from typing import Dict, Any, Literal, Annotated
from datetime import datetime, timedelta
from openai import OpenAI

from ba2_trade_platform.core.interfaces import SocialMediaDataProviderInterface
from ba2_trade_platform.core.provider_utils import log_provider_call
from ba2_trade_platform.logger import logger
from ba2_trade_platform import config
from ba2_trade_platform.config import get_app_setting


class AISocialMediaSentiment(SocialMediaDataProviderInterface):
    """
    AI social media sentiment provider.
    
    Uses AI web search capabilities to crawl and analyze social media sentiment
    from Twitter/X, Reddit, StockTwits, financial forums, and other public sources.
    
    Supports both OpenAI Responses API (for OpenAI/ prefixed models) and NagaAI
    Chat Completions API (for NagaAI/ prefixed models and other providers).
    
    Model format: "Provider/ModelName" (e.g., "OpenAI/gpt-5", "NagaAI/grok-4-fast-reasoning")
    """
    
    def __init__(self, model: str = None):
        """
        Initialize AI social media sentiment provider.
        
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
        logger.debug(f"Initialized AISocialMediaSentiment with model={self.model_string}, api_type={self.api_type}, backend_url={self.backend_url}")
    
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
            max_output_tokens=65535,
            top_p=1,
            store=True,
        )
        
        # Extract text from response using robust parsing
        sentiment_text = ""
        
        # Try response.output_text first (simple accessor for full text)
        if hasattr(response, 'output_text') and response.output_text and isinstance(response.output_text, str) and len(response.output_text.strip()) > 0:
            sentiment_text = response.output_text
            logger.debug(f"Extracted text via response.output_text: {len(sentiment_text)} chars")
        # Fall back to iterating through output items
        elif hasattr(response, 'output') and response.output:
            logger.debug(f"Iterating through {len(response.output)} output items")
            for item in response.output:
                if hasattr(item, 'content') and isinstance(item.content, list):
                    logger.debug(f"Found item with content list ({len(item.content)} items)")
                    for content_item in item.content:
                        if hasattr(content_item, 'text'):
                            text_value = str(content_item.text)
                            logger.debug(f"Found text in content item: {len(text_value)} chars")
                            sentiment_text += text_value + "\n\n"
            sentiment_text = sentiment_text.strip()
            logger.debug(f"Extracted text via output iteration: {len(sentiment_text)} chars")
        
        if not sentiment_text:
            logger.error(f"Could not extract text from OpenAI Responses API")
            sentiment_text = "Error: Could not extract sentiment analysis from AI response."
        
        return sentiment_text
    
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
                # For Grok models, can optionally add: "return_citations": True
            },
            temperature=1.0,
            max_tokens=65535,
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
            return f"Error extracting sentiment analysis: {extract_error}"
        
        return "Error: Could not extract sentiment analysis from AI response."
    
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
    
    @log_provider_call
    def get_social_media_sentiment(
        self,
        symbol: Annotated[str, "Stock ticker symbol"],
        end_date: Annotated[datetime, "End date for sentiment analysis"],
        lookback_days: Annotated[int, "Number of days to look back for sentiment data"],
        format_type: Literal["dict", "markdown", "both"] = "markdown"
    ) -> Dict[str, Any] | str:
        """
        Get social media sentiment analysis using AI web search.
        
        Args:
            symbol: Stock ticker symbol
            end_date: End date for sentiment analysis
            lookback_days: Number of days to analyze
            format_type: Output format - 'dict' for structured data, 'markdown' for text
            
        Returns:
            Social media sentiment analysis in requested format
        """
        logger.debug(f"Fetching social media sentiment for {symbol} (end_date={end_date.date()}, lookback={lookback_days} days) using {self.model_string}")
        
        try:
            # Calculate date range for the search
            start_date = end_date - timedelta(days=lookback_days)
            
            # Format dates for the prompt
            start_str = start_date.strftime("%Y-%m-%d")
            end_str = end_date.strftime("%Y-%m-%d")
            
            # Build comprehensive prompt for social media analysis
            prompt = f"""Search and analyze social media sentiment and discussions for {symbol} from {start_str} to {end_str}.

Please crawl and analyze public social media sources including:
- Twitter/X posts and threads
- Reddit discussions (r/wallstreetbets, r/stocks, r/investing, etc.)
- StockTwits sentiment and posts
- Financial news comment sections
- Investment forums and message boards
- Any other relevant public discussions

Provide a comprehensive sentiment analysis including:
1. **Overall Sentiment**: Bullish, Bearish, or Neutral with confidence score
2. **Sentiment Score**: A numerical score from -1.0 (extremely bearish) to +1.0 (extremely bullish)
3. **Key Themes**: Main topics and concerns being discussed
4. **Notable Mentions**: Specific impactful posts or discussions with examples
5. **Source Breakdown**: Sentiment from each major platform (Twitter/X, Reddit, StockTwits, etc.)
6. **Volume Analysis**: How much discussion volume compared to normal
7. **Influencer Activity**: Notable accounts or users discussing the stock
8. **Examples**: Quote 3-5 representative posts/comments that capture the sentiment

Focus on posts and discussions that occurred within the specified date range ({start_str} to {end_str}).
Provide specific examples with dates when possible."""

            # Call appropriate AI API
            sentiment_text = self._call_ai_api(prompt)
            
            logger.debug(f"Received sentiment analysis for {symbol}: {len(sentiment_text)} chars")
            
            # Build dict response (always build it for "both" format support)
            dict_response = {
                "symbol": symbol.upper(),
                "analysis_period": {
                    "start_date": start_date.isoformat(),
                    "end_date": end_date.isoformat(),
                    "days": lookback_days
                },
                "content": sentiment_text,
                "source": f"AI Web Search ({self.model_string})",
                "search_period": f"{start_str} to {end_str}",
                "timestamp": datetime.now().isoformat()
            }
            
            # Build markdown response
            lines = []
            lines.append(f"# Social Media Sentiment Analysis for {symbol}")
            lines.append(f"**Analysis Period:** {start_str} to {end_str} ({lookback_days} days)")
            lines.append(f"**Analysis Date:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
            lines.append(f"**Source:** AI Web Search ({self.model_string})")
            lines.append("")
            lines.append(sentiment_text)
            markdown = "\n".join(lines)
            
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
            logger.error(f"Failed to get social media sentiment for {symbol} using {self.model_string}: {e}", exc_info=True)
            raise

    def get_provider_name(self) -> str:
        """Get the provider name."""
        return "ai"
    
    def get_supported_features(self) -> list[str]:
        """Get supported features of this provider."""
        return ["social_media_sentiment", "sentiment_analysis"]
    
    def validate_config(self) -> bool:
        """
        Validate provider configuration.
        
        Returns:
            bool: True if configuration is valid
        """
        return self.client is not None
    
    def _format_as_dict(self, data: Any) -> Dict[str, Any]:
        """
        Format data as a structured dictionary.
        
        Args:
            data: Provider data
            
        Returns:
            Dict[str, Any]: Structured dictionary
        """
        if isinstance(data, dict):
            return data
        return {"data": data}
    
    def _format_as_markdown(self, data: Any) -> str:
        """
        Format data as markdown for LLM consumption.
        
        Args:
            data: Provider data
            
        Returns:
            str: Markdown-formatted string
        """
        if isinstance(data, dict):
            md = "# Data\n\n"
            for key, value in data.items():
                if isinstance(value, (list, dict)):
                    md += f"**{key}**: (complex data)\n"
                else:
                    md += f"**{key}**: {value}\n"
            return md
        return str(data)
