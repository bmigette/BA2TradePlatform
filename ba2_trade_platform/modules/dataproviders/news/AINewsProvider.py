"""
AI News Provider - Uses LLM with web search to fetch and summarize news.
Supports any model with websearch capability via the centralized do_llm_call_with_websearch function.
"""

from datetime import datetime, timedelta
from typing import Any, Dict, List, Literal, Optional, Annotated

from ba2_trade_platform.core.ModelFactory import ModelFactory
from ba2_trade_platform.core.interfaces.MarketNewsInterface import MarketNewsInterface
from ba2_trade_platform.logger import logger


class AINewsProvider(MarketNewsInterface):
    """
    News provider using LLM web search capabilities.
    
    Supports OpenAI (Responses API), NagaAI, xAI (Grok), and Google (Gemini) models
    with web search enabled via the centralized do_llm_call_with_websearch function.
    """

    def __init__(self, model: str = None):
        """
        Initialize the AI news provider.
        
        Args:
            model: Model identifier in format "Provider/model_key" (e.g., "OpenAI/gpt4o")
                   If None, will use default from settings.
        """
        self.model_string = model
        logger.debug(f"AINewsProvider initialized with model: {model}")

    def get_company_news(
        self,
        symbol: Annotated[str, "Stock ticker symbol"],
        end_date: Annotated[datetime, "End date for news (inclusive)"],
        start_date: Annotated[Optional[datetime], "Start date for news (mutually exclusive with lookback_days)"] = None,
        lookback_days: Annotated[Optional[int], "Days to look back from end_date (mutually exclusive with start_date)"] = None,
        limit: Annotated[int, "Maximum number of articles"] = 50,
        format_type: Literal["dict", "markdown", "both"] = "markdown"
    ) -> Dict[str, Any] | str:
        """
        Get news articles for a specific company using LLM web search.
        
        Args:
            symbol: Stock ticker symbol (e.g., 'AAPL', 'MSFT')
            end_date: End date (inclusive)
            start_date: Start date (use either this OR lookback_days, not both)
            lookback_days: Days to look back from end_date (use either this OR start_date, not both)
            limit: Maximum number of articles to return
            format_type: Output format ('dict', 'markdown', or 'both')
        
        Returns:
            News data in requested format
        """
        # Calculate lookback_days if start_date provided
        if start_date is not None:
            lookback_days = (end_date - start_date).days
        elif lookback_days is None:
            lookback_days = 7  # Default
        
        return self._fetch_news(
            symbol=symbol,
            news_type="company",
            lookback_days=lookback_days,
            max_articles=limit,
            format_type=format_type
        )

    def get_global_news(
        self,
        end_date: Annotated[datetime, "End date for news (inclusive)"],
        start_date: Annotated[Optional[datetime], "Start date for news (mutually exclusive with lookback_days)"] = None,
        lookback_days: Annotated[Optional[int], "Days to look back from end_date (mutually exclusive with start_date)"] = None,
        limit: Annotated[int, "Maximum number of articles"] = 50,
        format_type: Literal["dict", "markdown", "both"] = "markdown"
    ) -> Dict[str, Any] | str:
        """
        Get global market and macroeconomic news using LLM web search.
        
        Args:
            end_date: End date (inclusive)
            start_date: Start date (use either this OR lookback_days, not both)
            lookback_days: Days to look back from end_date (use either this OR start_date, not both)
            limit: Maximum number of articles to return
            format_type: Output format ('dict', 'markdown', or 'both')
        
        Returns:
            News data in requested format
        """
        # Calculate lookback_days if start_date provided
        if start_date is not None:
            lookback_days = (end_date - start_date).days
        elif lookback_days is None:
            lookback_days = 7  # Default
        
        return self._fetch_news(
            symbol=None,
            news_type="global",
            lookback_days=lookback_days,
            max_articles=limit,
            format_type=format_type
        )

    def _fetch_news(
        self,
        symbol: Optional[str],
        news_type: str,
        lookback_days: int,
        max_articles: int,
        format_type: Literal["dict", "markdown", "both"],
    ) -> Dict[str, Any] | str:
        """
        Fetch news using LLM web search.
        
        Args:
            symbol: Stock ticker symbol (None for global news)
            news_type: "company" or "global"
            lookback_days: Number of days to look back for news
            max_articles: Maximum number of articles to return
            format_type: Output format - "dict", "markdown", or "both"
            
        Returns:
            News data in requested format
        """
        try:
            # Build the search prompt
            if news_type == "global":
                prompt = self._build_global_news_prompt(lookback_days, max_articles)
            else:
                prompt = self._build_company_news_prompt(symbol, lookback_days, max_articles)
            
            # Call the centralized web search function
            response_text = ModelFactory.do_llm_call_with_websearch(
                model_selection=self.model_string,
                prompt=prompt,
                max_tokens=4096,
                temperature=0.3,
            )
            
            if not response_text:
                logger.warning(f"Empty response from LLM for {symbol or 'global'} news")
                return self._format_empty_response(symbol, format_type)
            
            # Parse the response
            return self._format_response(symbol, response_text, format_type)
            
        except Exception as e:
            logger.error(f"Error fetching news for {symbol or 'global'}: {e}", exc_info=True)
            return self._format_error_response(symbol, str(e), format_type)

    def _build_company_news_prompt(
        self,
        symbol: str,
        lookback_days: int,
        max_articles: int,
    ) -> str:
        """Build the prompt for company news search."""
        end_date = datetime.now()
        start_date = end_date - timedelta(days=lookback_days)
        
        prompt = f"""Search for the latest news about {symbol} stock from the past {lookback_days} days (from {start_date.strftime('%Y-%m-%d')} to {end_date.strftime('%Y-%m-%d')}).

Find up to {max_articles} relevant news articles and provide:
1. A brief market sentiment summary (bullish/bearish/neutral with reasoning)
2. Key news items with:
   - Headline
   - Source name
   - Publication date (if available)
   - Brief summary (1-2 sentences)
   - Sentiment impact (positive/negative/neutral)

Focus on:
- Earnings reports and financial results
- Product launches and business developments
- Management changes
- Market analysis and analyst ratings
- Regulatory news
- Industry trends affecting the company

Format the response clearly with sections for:
## Market Sentiment Summary
[Overall sentiment and key drivers]

## Recent News
[List of news items with details]

## Key Takeaways
[3-5 bullet points of most important insights for traders]
"""
        return prompt

    def _build_global_news_prompt(
        self,
        lookback_days: int,
        max_articles: int,
    ) -> str:
        """Build the prompt for global market news search."""
        end_date = datetime.now()
        start_date = end_date - timedelta(days=lookback_days)
        
        prompt = f"""Search for the latest global market and macroeconomic news from the past {lookback_days} days (from {start_date.strftime('%Y-%m-%d')} to {end_date.strftime('%Y-%m-%d')}).

Find up to {max_articles} relevant news articles about:
- US stock market trends (S&P 500, NASDAQ, Dow Jones)
- Federal Reserve policy and interest rates
- Economic indicators (GDP, unemployment, inflation, CPI)
- Geopolitical events affecting markets
- Major sector movements
- Global economic trends

Provide:
1. A brief overall market sentiment summary (bullish/bearish/neutral with reasoning)
2. Key news items with:
   - Headline
   - Source name
   - Publication date (if available)
   - Brief summary (1-2 sentences)
   - Market impact (positive/negative/neutral)

Format the response clearly with sections for:
## Market Sentiment Summary
[Overall sentiment and key market drivers]

## Recent News
[List of news items with details]

## Key Takeaways
[3-5 bullet points of most important insights for traders]
"""
        return prompt

    def _format_response(
        self,
        symbol: str,
        response_text: str,
        format_type: Literal["dict", "markdown", "both"],
    ) -> Dict[str, Any] | str:
        """Format the LLM response based on format_type."""
        
        # Build structured data
        structured_data = {
            "symbol": symbol,
            "timestamp": datetime.now().isoformat(),
            "model": self.model_string,
            "content": response_text,
            "success": True,
        }
        
        # Build markdown
        markdown_text = f"# News for {symbol}\n\n{response_text}\n\n---\n*Generated at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} using {self.model_string}*"
        
        if format_type == "dict":
            return structured_data
        elif format_type == "both":
            return {"text": markdown_text, "data": structured_data}
        else:  # markdown
            return markdown_text

    def _format_empty_response(
        self,
        symbol: str,
        format_type: Literal["dict", "markdown", "both"],
    ) -> Dict[str, Any] | str:
        """Format response when no data is available."""
        
        structured_data = {
            "symbol": symbol,
            "timestamp": datetime.now().isoformat(),
            "model": self.model_string,
            "content": None,
            "success": False,
            "error": "No news data available",
        }
        
        markdown_text = f"# News for {symbol}\n\nNo news data available at this time."
        
        if format_type == "dict":
            return structured_data
        elif format_type == "both":
            return {"text": markdown_text, "data": structured_data}
        else:
            return markdown_text

    def _format_error_response(
        self,
        symbol: str,
        error: str,
        format_type: Literal["dict", "markdown", "both"],
    ) -> Dict[str, Any] | str:
        """Format response when an error occurred."""
        
        structured_data = {
            "symbol": symbol,
            "timestamp": datetime.now().isoformat(),
            "model": self.model_string,
            "content": None,
            "success": False,
            "error": error,
        }
        
        markdown_text = f"# News for {symbol}\n\n⚠️ Error fetching news: {error}"
        
        if format_type == "dict":
            return structured_data
        elif format_type == "both":
            return {"text": markdown_text, "data": structured_data}
        else:
            return markdown_text
