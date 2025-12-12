"""
AI Social Media Sentiment Provider

Provides social media sentiment analysis using AI web search capabilities.
Crawls public social media sources (Twitter/X, Reddit, StockTwits, forums, etc.)
to analyze sentiment and discussions about a specific stock symbol.

Uses the centralized do_llm_call_with_websearch function for provider-agnostic web search.
"""

from typing import Dict, Any, Literal, Annotated
from datetime import datetime, timedelta

from ba2_trade_platform.core.interfaces import SocialMediaDataProviderInterface
from ba2_trade_platform.core.provider_utils import log_provider_call
from ba2_trade_platform.core.ModelFactory import ModelFactory
from ba2_trade_platform.logger import logger


class AISocialMediaSentiment(SocialMediaDataProviderInterface):
    """
    AI social media sentiment provider.
    
    Uses AI web search capabilities to crawl and analyze social media sentiment
    from Twitter/X, Reddit, StockTwits, financial forums, and other public sources.
    
    Supports OpenAI, NagaAI, xAI, and Google models with web search via the
    centralized do_llm_call_with_websearch function.
    
    Model format: "Provider/ModelName" (e.g., "OpenAI/gpt5", "NagaAI/grok3")
    """
    
    def __init__(self, model: str = None):
        """
        Initialize AI social media sentiment provider.
        
        Args:
            model: AI model to use in format "Provider/ModelName"
                   (e.g., "OpenAI/gpt4o", "NagaAI/grok3").
                   REQUIRED - must be provided by caller.
        """
        super().__init__()
        
        if not model:
            raise ValueError("model parameter is required for AISocialMediaSentiment - no default fallback allowed")
        
        self.model_string = model
        logger.debug(f"Initialized AISocialMediaSentiment with model={self.model_string}")
    
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
Provide specific examples with dates when possible.

IMPORTANT: Respond in English only."""

            # Call centralized web search function
            sentiment_text = ModelFactory.do_llm_call_with_websearch(
                model_selection=self.model_string,
                prompt=prompt,
                max_tokens=4096,
                temperature=0.3,
            )
            
            if not sentiment_text:
                sentiment_text = "Error: Could not retrieve sentiment analysis."
            
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
        return True  # No client to validate, centralized function handles it
    
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
