"""
OpenAI Social Media Sentiment Provider

Provides social media sentiment analysis using OpenAI's web search capabilities.
Crawls public social media sources (Twitter/X, Reddit, StockTwits, forums, etc.)
to analyze sentiment and discussions about a specific stock symbol.
"""

from typing import Dict, Any, Literal, Annotated
from datetime import datetime, timedelta
from openai import OpenAI

from ba2_trade_platform.core.interfaces import SocialMediaDataProviderInterface
from ba2_trade_platform.core.provider_utils import log_provider_call
from ba2_trade_platform.logger import logger
from ba2_trade_platform import config


class OpenAISocialMediaSentiment(SocialMediaDataProviderInterface):
    """
    OpenAI social media sentiment provider.
    
    Uses OpenAI's web search capabilities to crawl and analyze social media sentiment
    from Twitter/X, Reddit, StockTwits, financial forums, and other public sources.
    """
    
    def __init__(self, model: str = None):
        """
        Initialize OpenAI social media sentiment provider.
        
        Args:
            model: OpenAI model to use (e.g., 'gpt-4', 'gpt-4o-mini').
                   REQUIRED - must be provided by caller.
        """
        super().__init__()
        
        if not model:
            raise ValueError("model parameter is required for OpenAISocialMediaSentiment - no default fallback allowed")
        
        # Get OpenAI configuration
        self.backend_url = config.OPENAI_BACKEND_URL
        self.model = model
        
        # Get API key from database settings
        api_key = config.get_app_setting('openai_api_key')
        if not api_key:
            api_key = config.OPENAI_API_KEY or "dummy-key-not-used"
            logger.warning("OpenAI API key not found in database settings, using config or dummy key")
        
        # Initialize OpenAI client with web search capabilities
        self.client = OpenAI(
            base_url=self.backend_url,
            api_key=api_key
        )
        logger.debug(f"Initialized OpenAISocialMediaSentiment with model={self.model}, backend_url={self.backend_url}")
    
    @log_provider_call
    def get_social_media_sentiment(
        self,
        symbol: Annotated[str, "Stock ticker symbol"],
        end_date: Annotated[datetime, "End date for sentiment analysis"],
        lookback_days: Annotated[int, "Number of days to look back for sentiment data"],
        format_type: Literal["dict", "markdown", "both"] = "markdown"
    ) -> Dict[str, Any] | str:
        """
        Get social media sentiment analysis using OpenAI web search.
        
        Args:
            symbol: Stock ticker symbol
            end_date: End date for sentiment analysis
            lookback_days: Number of days to analyze
            format_type: Output format - 'dict' for structured data, 'markdown' for text
            
        Returns:
            Social media sentiment analysis in requested format
        """
        logger.debug(f"Fetching social media sentiment for {symbol} (end_date={end_date.date()}, lookback={lookback_days} days) using OpenAI")
        
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

            # Call OpenAI API with web search enabled
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
            # Fall back to iterating through output items (output_text is often empty, need to check output array)
            elif hasattr(response, 'output') and response.output:
                logger.debug(f"Iterating through {len(response.output)} output items")
                for item in response.output:
                    # Check for ResponseOutputMessage with content
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
                logger.error(f"Could not extract text from OpenAI response for {symbol}")
                logger.error(f"Response has output_text: {hasattr(response, 'output_text')}, type: {type(response.output_text) if hasattr(response, 'output_text') else 'N/A'}, value: {repr(response.output_text)[:200] if hasattr(response, 'output_text') else 'N/A'}")
                sentiment_text = "Error: Could not extract sentiment analysis from OpenAI response."
            
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
                "source": "OpenAI Web Search",
                "search_period": f"{start_str} to {end_str}",
                "timestamp": datetime.now().isoformat()
            }
            
            # Build markdown response
            lines = []
            lines.append(f"# Social Media Sentiment Analysis for {symbol}")
            lines.append(f"**Analysis Period:** {start_str} to {end_str} ({lookback_days} days)")
            lines.append(f"**Analysis Date:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
            lines.append(f"**Source:** OpenAI Web Search across multiple platforms")
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
            logger.error(f"Failed to get social media sentiment for {symbol} from OpenAI: {e}", exc_info=True)
            raise

    def get_provider_name(self) -> str:
        """Get the provider name."""
        return "openai"
    
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
