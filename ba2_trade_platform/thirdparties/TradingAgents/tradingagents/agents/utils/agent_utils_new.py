"""
New Toolkit for TradingAgents - Direct BA2 Provider Integration

This module provides a completely rewritten toolkit that:
1. Uses BA2 providers directly instead of interface.py routing
2. Accepts provider_map dict for thread-safe multi-configuration support
3. Aggregates results from multiple providers for news, insider, macro, fundamentals
4. Uses fallback logic for OHLCV and indicators (first provider, then fallback)
5. Maintains comprehensive Annotated type hints for LLM tool usage

The Toolkit class methods are designed to be wrapped with @tool decorator by the graph.

IMPORTANT: This toolkit does NOT use ProviderWithPersistence wrapper. All tool calls
and results are logged by LoggingToolNode in db_storage.py, which creates AnalysisOutput
entries. Using both would create duplicate database records.
"""

from typing import Annotated, Dict, Type, List, Optional
from datetime import datetime
from ba2_trade_platform.logger import logger
from ba2_trade_platform.core.interfaces import (
    MarketNewsInterface,
    CompanyInsiderInterface,
    MacroEconomicsInterface,
    CompanyFundamentalsDetailsInterface,
    DataProviderInterface,
    MarketIndicatorsInterface
)


class Toolkit:
    """
    New Toolkit for TradingAgents with direct BA2 provider integration.
    
    This toolkit eliminates the interface.py routing layer and works directly with
    BA2 providers. It accepts a provider_map configuration that can be different
    per thread/instance, enabling parallel execution with different configs.
    
    Provider Map Structure:
    {
        "news": [NewsProviderClass1, NewsProviderClass2, ...],       # All called, results aggregated
        "insider": [InsiderProviderClass1, ...],                      # All called, results aggregated
        "macro": [MacroProviderClass1, ...],                          # All called, results aggregated
        "fundamentals_details": [FundProviderClass1, ...],            # All called, results aggregated
        "ohlcv": [OHLCVProviderClass1, OHLCVProviderClass2, ...],    # Fallback: try first, then second, etc.
        "indicators": [IndProviderClass1, IndProviderClass2, ...]     # Fallback: try first, then second, etc.
    }
    
    Provider Args Structure (optional):
    {
        "openai_model": "gpt-5"  # Model to use for OpenAI providers
    }
    """
    
    def __init__(self, provider_map: Dict[str, List[Type[DataProviderInterface]]], provider_args: Dict[str, any] = None):
        """
        Initialize toolkit with provider configuration.
        
        Args:
            provider_map: Dictionary mapping provider categories to list of provider classes
            provider_args: Optional dictionary with arguments for provider instantiation
                          (e.g., {"websearch_model": "gpt-5"})
        """
        self.provider_map = provider_map
        self.provider_args = provider_args or {}
        logger.debug(f"Toolkit initialized with provider_map keys: {list(provider_map.keys())}")
        
        # Log model configuration for AI providers
        if self.provider_args and 'websearch_model' in self.provider_args:
            model = self.provider_args['websearch_model']
            # Parse provider from model string (e.g., "OpenAI/gpt-5" or "NagaAI/grok-4")
            provider_prefix = ""
            if '/' in model:
                provider_prefix = f"({model.split('/')[0]}) "
            logger.info(f"[TRADING_AGENTS_CONFIG] Data Provider Model: {provider_prefix}{model}")
        
        if self.provider_args:
            logger.debug(f"Provider args: {self.provider_args}")
    
    def _instantiate_provider(self, provider_class: Type[DataProviderInterface]) -> DataProviderInterface:
        """
        Instantiate a provider with appropriate arguments.
        
        Args:
            provider_class: Provider class to instantiate
            
        Returns:
            Instantiated provider
        """
        provider_name = provider_class.__name__
        
        # Check if this is a MarketIndicatorsInterface that needs OHLCV provider
        from ba2_trade_platform.core.interfaces import MarketIndicatorsInterface
        if issubclass(provider_class, MarketIndicatorsInterface):
            # Get the first OHLCV provider from the provider_map
            ohlcv_provider = self._get_ohlcv_provider()
            if ohlcv_provider is None:
                raise ValueError(f"Cannot instantiate {provider_name}: No OHLCV provider configured")
            logger.debug(f"Instantiating {provider_name} with OHLCV provider: {ohlcv_provider.__class__.__name__}")
            return provider_class(ohlcv_provider=ohlcv_provider)
        
        # Check if this is an AI provider that supports model parameter (OpenAI or NagaAI)
        # Includes: AINewsProvider, AICompanyOverviewProvider, AISocialMediaSentiment, etc.
        elif (provider_name.startswith('AI') or 'OpenAI' in provider_name) and 'websearch_model' in self.provider_args:
            model = self.provider_args['websearch_model']
            logger.debug(f"Instantiating {provider_name} with model={model}")
            return provider_class(model=model)
        
        # Check if this is an Alpha Vantage provider that needs source argument
        elif 'AlphaVantage' in provider_name and 'alpha_vantage_source' in self.provider_args:
            source = self.provider_args['alpha_vantage_source']
            logger.debug(f"Instantiating {provider_name} with source={source}")
            return provider_class(source=source)
        else:
            # Standard instantiation with no arguments
            return provider_class()
    
    def _get_ohlcv_provider(self) -> Optional[DataProviderInterface]:
        """
        Get the first available OHLCV provider instance.
        
        Returns:
            Instantiated OHLCV provider or None if not configured
        """
        if "ohlcv" not in self.provider_map or not self.provider_map["ohlcv"]:
            return None
        
        # Instantiate the first OHLCV provider
        ohlcv_provider_class = self.provider_map["ohlcv"][0]
        provider_name = ohlcv_provider_class.__name__
        
        # Check if AI provider needs model argument
        if (provider_name.startswith('AI') or 'OpenAI' in provider_name) and 'websearch_model' in self.provider_args:
            model = self.provider_args['websearch_model']
            return ohlcv_provider_class(model=model)
        # Check if Alpha Vantage provider needs source argument
        elif 'AlphaVantage' in provider_name and 'alpha_vantage_source' in self.provider_args:
            source = self.provider_args['alpha_vantage_source']
            return ohlcv_provider_class(source=source)
        else:
            return ohlcv_provider_class()
    
    def _format_ohlcv_dataframe(self, df, symbol: str) -> tuple:
        """
        Convert OHLCV DataFrame to markdown text and JSON dict.
        
        Args:
            df: DataFrame with columns: Date, Open, High, Low, Close, Volume
            symbol: Stock symbol (for metadata)
        
        Returns:
            Tuple of (markdown_text, json_dict)
        """
        import pandas as pd
        
        try:
            # Ensure Date is datetime
            if 'Date' in df.columns:
                df['Date'] = pd.to_datetime(df['Date'])
            
            # Create markdown representation
            markdown_lines = [f"# OHLCV Data for {symbol}"]
            markdown_lines.append(f"**Records**: {len(df)}")
            markdown_lines.append(f"**Date Range**: {df['Date'].min()} to {df['Date'].max()}")
            markdown_lines.append("")
            
            # Check if data contains intraday (has time component)
            has_intraday = any(
                hasattr(d, 'hour') and (d.hour != 0 or d.minute != 0 or d.second != 0)
                for d in df['Date']
            )
            
            if has_intraday:
                markdown_lines.append("| DateTime | Open | High | Low | Close | Volume |")
                markdown_lines.append("|----------|------|------|-----|-------|--------|")
            else:
                markdown_lines.append("| Date | Open | High | Low | Close | Volume |")
                markdown_lines.append("|------|------|------|-----|-------|--------|")
            
            # Show first 10, last 10 rows in markdown (limit output)
            rows_to_show = min(10, len(df))
            for idx in range(rows_to_show):
                row = df.iloc[idx]
                if has_intraday:
                    date_str = row['Date'].strftime('%Y-%m-%d %H:%M:%S')
                else:
                    date_str = row['Date'].strftime('%Y-%m-%d')
                markdown_lines.append(
                    f"| {date_str} | {row['Open']:.2f} | {row['High']:.2f} | {row['Low']:.2f} | {row['Close']:.2f} | {int(row['Volume'])} |"
                )
            
            if len(df) > 20:
                markdown_lines.append("| ... | ... | ... | ... | ... | ... |")
                for idx in range(len(df) - rows_to_show, len(df)):
                    row = df.iloc[idx]
                    if has_intraday:
                        date_str = row['Date'].strftime('%Y-%m-%d %H:%M:%S')
                    else:
                        date_str = row['Date'].strftime('%Y-%m-%d')
                    markdown_lines.append(
                        f"| {date_str} | {row['Open']:.2f} | {row['High']:.2f} | {row['Low']:.2f} | {row['Close']:.2f} | {int(row['Volume'])} |"
                    )
            
            markdown_text = "\n".join(markdown_lines)
            
            # Create JSON structure (clean, JSON-serializable)
            # Preserve full timestamp for intraday data (time component)
            dates_list = []
            for date in df['Date']:
                # Use isoformat to preserve timestamp precision
                if hasattr(date, 'isoformat'):
                    dates_list.append(date.isoformat())
                else:
                    # Fallback to date-only format if not a datetime
                    dates_list.append(str(date)[:10])
            
            json_dict = {
                "symbol": symbol,
                "dates": dates_list,
                "opens": df['Open'].round(4).tolist(),
                "highs": df['High'].round(4).tolist(),
                "lows": df['Low'].round(4).tolist(),
                "closes": df['Close'].round(4).tolist(),
                "volumes": df['Volume'].astype(int).tolist(),
                "metadata": {
                    "total_records": len(df),
                    "start_date": df['Date'].min().strftime('%Y-%m-%d'),
                    "end_date": df['Date'].max().strftime('%Y-%m-%d')
                }
            }
            
            return markdown_text, json_dict
            
        except Exception as e:
            logger.error(f"Error formatting OHLCV DataFrame: {e}", exc_info=True)
            return f"Error formatting data: {str(e)}", {"error": str(e)}
    
    def _call_provider_with_both_format(self, provider, method_name: str, **kwargs) -> tuple:
        """
        Call a provider method with format_type="both" and handle response extraction.
        
        Args:
            provider: Instantiated provider instance
            method_name: Name of the method to call (e.g., 'get_company_news')
            **kwargs: Arguments to pass to the method (will add format_type="both")
        
        Returns:
            Tuple of (markdown_text, data_dict or None)
        """
        try:
            # Add format_type="both" to kwargs
            kwargs["format_type"] = "both"
            
            # Call the provider method
            method = getattr(provider, method_name)
            result = method(**kwargs)
            
            # Handle result that has both text and data
            if isinstance(result, dict):
                if "text" in result and "data" in result:
                    return result["text"], result["data"]
                else:
                    # Result doesn't have expected structure, return as is
                    logger.warning(f"Provider result missing 'text' or 'data' keys: {type(result)}")
                    return str(result), None
            else:
                # Result is plain string, no structured data
                return str(result), None
                
        except Exception as e:
            logger.warning(f"Provider call failed with format_type='both': {e}", exc_info=True)
            # Fallback: try with format_type="markdown" only
            try:
                kwargs["format_type"] = "markdown"
                method = getattr(provider, method_name)
                result = method(**kwargs)
                return str(result), None
            except Exception as e2:
                logger.error(f"Provider call also failed with format_type='markdown': {e2}", exc_info=True)
                return None, None
    
    
    # NEWS PROVIDERS - Aggregate results from all providers
    # ========================================================================
    
    def get_company_news(
        self,
        symbol: Annotated[str, "Stock ticker symbol (e.g., 'AAPL', 'MSFT', 'TSLA')"],
        end_date: Annotated[str, "End date for news in YYYY-MM-DD format (e.g., '2024-03-15')"],
        lookback_days: Annotated[
            Optional[int],
            "Number of days to look back for news. If not provided, defaults to news_lookback_days from config (typically 7 days). "
            "You can specify a custom value to get more or less historical news (e.g., 3 for recent news, 30 for broader context)."
        ] = None
    ) -> str:
        """
        Retrieve news articles about a specific company from multiple news providers.
        
        This function calls ALL configured news providers and aggregates their results,
        giving you comprehensive news coverage from multiple sources (OpenAI, Alpha Vantage, Google, etc.).
        
        Args:
            symbol: Stock ticker symbol for the company you're interested in
            end_date: End date in YYYY-MM-DD format
            lookback_days: How many days of news history to retrieve (default from config)
        
        Returns:
            str: Aggregated markdown-formatted news from all providers, with provider attribution
        
        Example:
            >>> news = get_company_news("AAPL", "2024-03-15", lookback_days=7)
            >>> # Returns news from all configured providers about Apple
        """
        if "news" not in self.provider_map or not self.provider_map["news"]:
            logger.warning(f"No news providers configured for get_company_news")
            return "**No Provider Available**\n\nNo news providers are currently configured. Please configure at least one news provider in the expert settings to retrieve company news."
        
        try:
            end_dt = datetime.strptime(end_date, "%Y-%m-%d")
            
            # Default lookback_days from provider_args (expert settings) if not provided
            if lookback_days is None:
                lookback_days = self.provider_args.get("news_lookback_days", 7)
            
            results = []
            for provider_class in self.provider_map["news"]:
                try:
                    provider = self._instantiate_provider(provider_class)
                    provider_name = provider.__class__.__name__
                    
                    logger.debug(f"Fetching company news from {provider_name} for {symbol}")
                    
                    # Call provider with format_type="both" to get both markdown and structured data
                    markdown_data, data_dict = self._call_provider_with_both_format(
                        provider,
                        "get_company_news",
                        symbol=symbol,
                        end_date=end_dt,
                        lookback_days=lookback_days
                    )
                    
                    if markdown_data is None:
                        results.append(f"## {provider_name} - Error\n\nFailed to fetch news")
                        continue
                    
                    results.append(f"## News from {provider_name.upper()}\n\n{markdown_data}")
                    
                except Exception as e:
                    logger.error(f"Error fetching company news from {provider_class.__name__}: {e}")
                    results.append(f"## {provider_class.__name__} - Error\n\nFailed to fetch news: {str(e)}")
            
            return "\n\n---\n\n".join(results) if results else "No news data available"
            
        except Exception as e:
            logger.error(f"Error in get_company_news: {e}")
            return f"Error retrieving company news: {str(e)}"
    
    def get_global_news(
        self,
        end_date: Annotated[str, "End date for news in YYYY-MM-DD format (e.g., '2024-03-15')"],
        lookback_days: Annotated[
            Optional[int],
            "Number of days to look back for global/macroeconomic news. If not provided, defaults to news_lookback_days from config (typically 7 days). "
            "Specify a custom value to get more or less historical context (e.g., 3 for recent events, 14 for broader macro trends)."
        ] = None
    ) -> str:
        """
        Retrieve global market and macroeconomic news from multiple providers.
        
        Gets news about overall market trends, economic indicators, Federal Reserve actions,
        geopolitical events, and other macro factors that affect trading decisions.
        Aggregates results from all configured providers.
        
        Args:
            end_date: End date in YYYY-MM-DD format
            lookback_days: How many days of news history to retrieve (default from config)
        
        Returns:
            str: Aggregated markdown-formatted global news from all providers
        
        Example:
            >>> global_news = get_global_news("2024-03-15", lookback_days=7)
            >>> # Returns macro/market news from all configured providers
        """
        if "news" not in self.provider_map or not self.provider_map["news"]:
            logger.warning(f"No news providers configured for get_global_news")
            return "**No Provider Available**\n\nNo news providers are currently configured. Please configure at least one news provider in the expert settings to retrieve global news."
        
        try:
            end_dt = datetime.strptime(end_date, "%Y-%m-%d")
            
            # Default lookback_days from provider_args (expert settings) if not provided
            if lookback_days is None:
                lookback_days = self.provider_args.get("news_lookback_days", 7)
            
            results = []
            for provider_class in self.provider_map["news"]:
                try:
                    provider = self._instantiate_provider(provider_class)
                    provider_name = provider.__class__.__name__
                    
                    logger.debug(f"Fetching global news from {provider_name}")
                    
                    # Call provider with format_type="both" to get both markdown and structured data
                    markdown_data, data_dict = self._call_provider_with_both_format(
                        provider,
                        "get_global_news",
                        end_date=end_dt,
                        lookback_days=lookback_days
                    )
                    
                    if markdown_data is None:
                        results.append(f"## {provider_name} - Error\n\nFailed to fetch news")
                        continue
                    
                    results.append(f"## Global News from {provider_name.upper()}\n\n{markdown_data}")
                    
                except Exception as e:
                    logger.error(f"Error fetching global news from {provider_class.__name__}: {e}")
                    results.append(f"## {provider_class.__name__} - Error\n\nFailed to fetch global news: {str(e)}")
            
            return "\n\n---\n\n".join(results) if results else "No global news data available"
            
        except Exception as e:
            logger.error(f"Error in get_global_news: {e}")
            return f"Error retrieving global news: {str(e)}"
    
    def extract_web_content(
        self,
        urls: Annotated[List[str], "List of news article URLs to extract full content from. Provide URLs from news articles to get complete article text for deeper analysis."],
        max_tokens: Annotated[int, "Maximum total tokens to extract across all URLs. Default is 128000 (128K tokens). Content extraction stops when this limit is reached."] = 128000
    ) -> str:
        """
        Extract full article content from multiple news URLs in parallel.
        
        This tool fetches and extracts the main content from news article URLs,
        filtering out ads, navigation, and boilerplate to provide clean article text.
        Ideal for deep analysis of specific news articles beyond summaries.
        
        The extraction:
        - Runs in parallel for fast processing (up to 5 URLs simultaneously)
        - Automatically manages token limits (stops at max_tokens)
        - Returns clean markdown format for LLM analysis
        - Skips URLs that would exceed the token limit
        - Handles errors gracefully (blocked sites, timeouts, etc.)
        
        Use this when you need full article details beyond the summary provided by
        news APIs. Particularly useful for analyzing detailed earnings reports,
        in-depth investigative pieces, or comprehensive market analysis articles.
        
        Args:
            urls: List of article URLs to extract (e.g., from get_company_news results)
            max_tokens: Maximum total tokens across all articles (default: 128000)
        
        Returns:
            str: Markdown-formatted content with all extracted articles and metadata
        
        Example:
            >>> # Get news URLs first
            >>> news = get_company_news("AAPL", "2024-03-15", lookback_days=3)
            >>> # Extract full articles (you would parse URLs from news first)
            >>> content = extract_web_content(
            ...     urls=["https://example.com/article1", "https://example.com/article2"],
            ...     max_tokens=50000
            ... )
            >>> # Returns full article text for deeper analysis
        
        Note:
            - Some sites block automated access (403/401 errors) - these will be skipped
            - Extraction stops automatically when token limit is reached
            - Results are logged as Analysis Output in the database
        """
        from ...utils.web_content_extractor import extract_urls_parallel
        
        # Defensive check: ensure urls is a list, not a string
        if isinstance(urls, str):
            logger.warning(f"extract_web_content received a string instead of list, wrapping: {urls[:100]}")
            urls = [urls]
        
        if not urls:
            logger.warning("extract_web_content called with empty URL list")
            return "**No URLs Provided**\n\nPlease provide at least one URL to extract content from."
        
        logger.info(f"Extracting web content from {len(urls)} URLs (max_tokens={max_tokens})")
        
        try:
            # Extract content in parallel
            result = extract_urls_parallel(
                urls=urls,
                max_workers=5,
                max_tokens=max_tokens
            )
            
            if not result["success"]:
                error_msg = result.get("error", "Unknown error")
                logger.error(f"Web content extraction failed: {error_msg}")
                return f"**Extraction Failed**\n\n{error_msg}"
            
            # Log success metrics
            logger.info(
                f"Web content extraction complete: {result['extracted_count']}/{len(urls)} URLs, "
                f"{result['total_tokens']:,} tokens in {result['duration']:.2f}s"
            )
            
            # Return markdown content
            return result["content_markdown"]
            
        except Exception as e:
            logger.error(f"Error in extract_web_content: {e}", exc_info=True)
            return f"**Error Extracting Web Content**\n\n{str(e)}"
    
    # ========================================================================
    # SOCIAL MEDIA PROVIDERS - Aggregate results from all providers
    # ========================================================================
    
    def get_social_media_sentiment(
        self,
        symbol: Annotated[str, "Stock ticker symbol (e.g., 'AAPL', 'MSFT', 'TSLA')"],
        end_date: Annotated[str, "End date for social media data in YYYY-MM-DD format (e.g., '2024-03-15')"],
        lookback_days: Annotated[
            Optional[int],
            "Number of days to look back for social media sentiment and discussions. If not provided, defaults to news_lookback_days from config (typically 7 days). "
            "You can specify a custom value for different time horizons (e.g., 3 for recent buzz, 14 for trend analysis)."
        ] = None
    ) -> str:
        """
        Retrieve social media sentiment and discussions about a specific company.
        
        This function aggregates social media data, community discussions, and sentiment
        from platforms like Reddit, Twitter/X, and other sources. It provides insights into
        retail investor sentiment, trending topics, and community perception.
        
        NOTE: This uses the 'social_media' provider category, which should be mapped to
        specific news providers in the expert configuration (e.g., Reddit-focused providers,
        social sentiment APIs, etc.).
        
        Args:
            symbol: Stock ticker symbol for the company
            end_date: End date in YYYY-MM-DD format
            lookback_days: How many days of social media history to analyze (default from config)
        
        Returns:
            str: Aggregated markdown-formatted social media sentiment from all providers
        
        Example:
            >>> sentiment = get_social_media_sentiment("TSLA", "2024-03-15", lookback_days=7)
            >>> # Returns social media discussions and sentiment about Tesla
        """
        if "social_media" not in self.provider_map or not self.provider_map["social_media"]:
            logger.warning(f"No social_media providers configured for get_social_media_sentiment")
            return "**No Provider Available**\n\nNo social media providers are currently configured. Please configure at least one social media provider in the expert settings to retrieve sentiment data."
        
        try:
            end_dt = datetime.strptime(end_date, "%Y-%m-%d")
            
            # Default lookback_days from provider_args (expert settings) if not provided
            if lookback_days is None:
                lookback_days = self.provider_args.get("social_sentiment_days", 3)
            
            results = []
            for provider_class in self.provider_map["social_media"]:
                try:
                    provider = self._instantiate_provider(provider_class)
                    provider_name = provider.__class__.__name__
                    
                    logger.debug(f"Fetching social media sentiment from {provider_name} for {symbol}")
                    
                    # Call provider with format_type="both" to get both markdown and structured data
                    markdown_data, data_dict = self._call_provider_with_both_format(
                        provider,
                        "get_social_media_sentiment",
                        symbol=symbol,
                        end_date=end_dt,
                        lookback_days=lookback_days
                    )
                    
                    if markdown_data is None:
                        results.append(f"## {provider_name} - Error\n\nFailed to fetch sentiment")
                        continue
                    
                    results.append(f"## Social Media Sentiment from {provider_name.upper()}\n\n{markdown_data}")
                    
                except Exception as e:
                    logger.error(f"Error fetching social media sentiment from {provider_class.__name__}: {e}")
                    results.append(f"## {provider_class.__name__} - Error\n\nFailed to fetch sentiment: {str(e)}")
            
            return "\n\n---\n\n".join(results) if results else "No social media sentiment data available"
            
        except Exception as e:
            logger.error(f"Error in get_social_media_sentiment: {e}")
            return f"Error retrieving social media sentiment: {str(e)}"
    
    # ========================================================================
    # INSIDER PROVIDERS - Aggregate results from all providers
    # ========================================================================
    
    def get_insider_transactions(
        self,
        symbol: Annotated[str, "Stock ticker symbol (e.g., 'AAPL', 'MSFT')"],
        end_date: Annotated[str, "End date for transactions in YYYY-MM-DD format"],
        lookback_days: Annotated[
            Optional[int],
            "Number of days to look back for insider transaction data. If not provided, defaults to economic_data_days from config (typically 90 days). "
            "Specify a custom value to analyze shorter or longer periods (e.g., 30 for recent activity, 180 for longer-term patterns)."
        ] = None
    ) -> str:
        """
        Retrieve insider trading transactions (buys/sells by executives and directors).
        
        Provides detailed information about insider trades including transaction types,
        shares traded, prices, and insider roles. Aggregates data from all configured providers.
        Useful for identifying insider confidence or concerns.
        
        Args:
            symbol: Stock ticker symbol
            end_date: End date in YYYY-MM-DD format
            lookback_days: Days to look back (default from config, typically 90 days)
        
        Returns:
            str: Aggregated markdown-formatted insider transaction data
        
        Example:
            >>> transactions = get_insider_transactions("AAPL", "2024-03-15", lookback_days=90)
            >>> # Shows all insider buys/sells for Apple over the past 90 days
        """
        if "insider" not in self.provider_map or not self.provider_map["insider"]:
            return "Error: No insider data providers configured"
        
        try:
            end_dt = datetime.strptime(end_date, "%Y-%m-%d")
            
            # Default lookback_days from provider_args (expert settings) if not provided
            if lookback_days is None:
                lookback_days = self.provider_args.get("economic_data_days", 90)
            
            results = []
            for provider_class in self.provider_map["insider"]:
                try:
                    provider = self._instantiate_provider(provider_class)
                    provider_name = provider.__class__.__name__
                    
                    logger.debug(f"Fetching insider transactions from {provider_name} for {symbol}")
                    
                    # Call provider with format_type="both" to get both markdown and structured data
                    markdown_data, data_dict = self._call_provider_with_both_format(
                        provider,
                        "get_insider_transactions",
                        symbol=symbol,
                        end_date=end_dt,
                        lookback_days=lookback_days
                    )
                    
                    if markdown_data is None:
                        results.append(f"## {provider_name} - Error\n\nFailed to fetch transactions")
                        continue
                    
                    results.append(f"## Insider Transactions from {provider_name.upper()}\n\n{markdown_data}")
                    
                except Exception as e:
                    logger.error(f"Error fetching insider transactions from {provider_class.__name__}: {e}")
                    results.append(f"## {provider_class.__name__} - Error\n\nFailed to fetch transactions: {str(e)}")
            
            return "\n\n---\n\n".join(results) if results else "No insider transaction data available"
            
        except Exception as e:
            logger.error(f"Error in get_insider_transactions: {e}")
            return f"Error retrieving insider transactions: {str(e)}"
    
    def get_insider_sentiment(
        self,
        symbol: Annotated[str, "Stock ticker symbol (e.g., 'AAPL', 'MSFT')"],
        end_date: Annotated[str, "End date for sentiment calculation in YYYY-MM-DD format"],
        lookback_days: Annotated[
            Optional[int],
            "Number of days to look back for calculating insider sentiment. If not provided, defaults to economic_data_days from config (typically 90 days). "
            "Longer periods provide more stable sentiment signals but may miss recent shifts."
        ] = None
    ) -> str:
        """
        Retrieve aggregated insider sentiment metrics (bullish/bearish indicators).
        
        Calculates sentiment scores based on insider buying vs selling activity, weighted by
        insider role (CEO/CFO trades weighted more heavily). Includes metrics like MSPR (Monthly
        Share Purchase Ratio) and net transaction values. Aggregates from all configured providers.
        
        Args:
            symbol: Stock ticker symbol
            end_date: End date in YYYY-MM-DD format
            lookback_days: Days to look back for sentiment calculation (default 90)
        
        Returns:
            str: Aggregated markdown-formatted insider sentiment analysis
        
        Example:
            >>> sentiment = get_insider_sentiment("AAPL", "2024-03-15", lookback_days=90)
            >>> # Shows bullish/bearish sentiment from insider trading patterns
        """
        if "insider" not in self.provider_map or not self.provider_map["insider"]:
            return "Error: No insider data providers configured"
        
        try:
            end_dt = datetime.strptime(end_date, "%Y-%m-%d")
            
            # Default lookback_days from provider_args (expert settings) if not provided
            if lookback_days is None:
                lookback_days = self.provider_args.get("economic_data_days", 90)
            
            results = []
            for provider_class in self.provider_map["insider"]:
                try:
                    provider = self._instantiate_provider(provider_class)
                    provider_name = provider.__class__.__name__
                    
                    logger.debug(f"Fetching insider sentiment from {provider_name} for {symbol}")
                    
                    # Call provider with format_type="both" to get both markdown and structured data
                    markdown_data, data_dict = self._call_provider_with_both_format(
                        provider,
                        "get_insider_sentiment",
                        symbol=symbol,
                        end_date=end_dt,
                        lookback_days=lookback_days
                    )
                    
                    if markdown_data is None:
                        results.append(f"## {provider_name} - Error\n\nFailed to fetch sentiment")
                        continue
                    
                    results.append(f"## Insider Sentiment from {provider_name.upper()}\n\n{markdown_data}")
                    
                except Exception as e:
                    logger.error(f"Error fetching insider sentiment from {provider_class.__name__}: {e}")
                    results.append(f"## {provider_class.__name__} - Error\n\nFailed to fetch sentiment: {str(e)}")
            
            return "\n\n---\n\n".join(results) if results else "No insider sentiment data available"
            
        except Exception as e:
            logger.error(f"Error in get_insider_sentiment: {e}")
            return f"Error retrieving insider sentiment: {str(e)}"
    
    # ========================================================================
    # FUNDAMENTALS PROVIDERS - Aggregate results from all providers
    # ========================================================================
    
    def get_balance_sheet(
        self,
        symbol: Annotated[str, "Stock ticker symbol (e.g., 'AAPL', 'MSFT')"],
        frequency: Annotated[
            str,
            "Reporting frequency: 'quarterly' for most recent quarterly data or 'annual' for yearly data. "
            "Quarterly provides more timely information but annual shows longer-term trends."
        ],
        end_date: Annotated[str, "End date in YYYY-MM-DD format - retrieves most recent statement as of this date"],
        lookback_periods: Annotated[
            int,
            "Number of periods to retrieve. For quarterly, 4 periods = 1 year of data. For annual, 3-5 periods shows multi-year trends. "
            "Default is 4 periods if not specified."
        ] = 4
    ) -> str:
        """
        Retrieve balance sheet(s) showing company assets, liabilities, and equity.
        
        The balance sheet shows a company's financial position at a point in time:
        - Assets: What the company owns (cash, inventory, property, investments)
        - Liabilities: What the company owes (debt, payables)
        - Equity: Shareholder ownership value (assets - liabilities)
        
        Aggregates data from all configured providers for comprehensive coverage.
        
        Args:
            symbol: Stock ticker symbol
            frequency: 'quarterly' or 'annual'
            end_date: End date in YYYY-MM-DD format
            lookback_periods: Number of statements to retrieve (default 4)
        
        Returns:
            str: Aggregated markdown-formatted balance sheet data
        
        Example:
            >>> balance_sheet = get_balance_sheet("AAPL", "quarterly", "2024-03-15", lookback_periods=4)
            >>> # Shows Apple's last 4 quarterly balance sheets (1 year of data)
        """
        if "fundamentals_details" not in self.provider_map or not self.provider_map["fundamentals_details"]:
            return "Error: No fundamentals details providers configured"
        
        try:
            end_dt = datetime.strptime(end_date, "%Y-%m-%d")
            
            results = []
            for provider_class in self.provider_map["fundamentals_details"]:
                try:
                    provider = self._instantiate_provider(provider_class)
                    provider_name = provider.__class__.__name__
                    
                    logger.debug(f"Fetching balance sheet from {provider_name} for {symbol}")
                    
                    # Call provider with format_type="both" to get both markdown and structured data
                    markdown_data, data_dict = self._call_provider_with_both_format(
                        provider,
                        "get_balance_sheet",
                        symbol=symbol,
                        frequency=frequency,
                        end_date=end_dt,
                        lookback_periods=lookback_periods
                    )
                    
                    if markdown_data is None:
                        results.append(f"## {provider_name} - Error\n\nFailed to fetch balance sheet")
                        continue
                    
                    results.append(f"## Balance Sheet from {provider_name.upper()}\n\n{markdown_data}")
                    
                except Exception as e:
                    logger.error(f"Error fetching balance sheet from {provider_class.__name__}: {e}")
                    results.append(f"## {provider_class.__name__} - Error\n\nFailed to fetch balance sheet: {str(e)}")
            
            return "\n\n---\n\n".join(results) if results else "No balance sheet data available"
            
        except Exception as e:
            logger.error(f"Error in get_balance_sheet: {e}")
            return f"Error retrieving balance sheet: {str(e)}"
    
    def get_income_statement(
        self,
        symbol: Annotated[str, "Stock ticker symbol (e.g., 'AAPL', 'MSFT')"],
        frequency: Annotated[
            str,
            "Reporting frequency: 'quarterly' for most recent quarterly data or 'annual' for yearly data. "
            "Quarterly provides more timely earnings information."
        ],
        end_date: Annotated[str, "End date in YYYY-MM-DD format - retrieves most recent statement as of this date"],
        lookback_periods: Annotated[
            int,
            "Number of periods to retrieve. For quarterly, 4 periods = 1 year of earnings. For annual, 3-5 periods shows multi-year trends. "
            "Default is 4 periods if not specified."
        ] = 4
    ) -> str:
        """
        Retrieve income statement(s) showing company revenues, expenses, and profitability.
        
        The income statement shows financial performance over a period:
        - Revenue: Total sales and income
        - Costs: Cost of goods sold, operating expenses, R&D
        - Profitability: Gross profit, operating income, net income, EPS
        - Margins: Gross margin, operating margin, net margin
        
        Aggregates data from all configured providers for comprehensive coverage.
        
        Args:
            symbol: Stock ticker symbol
            frequency: 'quarterly' or 'annual'
            end_date: End date in YYYY-MM-DD format
            lookback_periods: Number of statements to retrieve (default 4)
        
        Returns:
            str: Aggregated markdown-formatted income statement data
        
        Example:
            >>> income_stmt = get_income_statement("AAPL", "quarterly", "2024-03-15", lookback_periods=4)
            >>> # Shows Apple's last 4 quarterly earnings reports (1 year of data)
        """
        if "fundamentals_details" not in self.provider_map or not self.provider_map["fundamentals_details"]:
            return "Error: No fundamentals details providers configured"
        
        try:
            end_dt = datetime.strptime(end_date, "%Y-%m-%d")
            
            results = []
            for provider_class in self.provider_map["fundamentals_details"]:
                try:
                    provider = self._instantiate_provider(provider_class)
                    provider_name = provider.__class__.__name__
                    
                    logger.debug(f"Fetching income statement from {provider_name} for {symbol}")
                    
                    # Call provider with format_type="both" to get both markdown and structured data
                    markdown_data, data_dict = self._call_provider_with_both_format(
                        provider,
                        "get_income_statement",
                        symbol=symbol,
                        frequency=frequency,
                        end_date=end_dt,
                        lookback_periods=lookback_periods
                    )
                    
                    if markdown_data is None:
                        results.append(f"## {provider_name} - Error\n\nFailed to fetch income statement")
                        continue
                    
                    results.append(f"## Income Statement from {provider_name.upper()}\n\n{markdown_data}")
                    
                except Exception as e:
                    logger.error(f"Error fetching income statement from {provider_class.__name__}: {e}")
                    results.append(f"## {provider_class.__name__} - Error\n\nFailed to fetch income statement: {str(e)}")
            
            return "\n\n---\n\n".join(results) if results else "No income statement data available"
            
        except Exception as e:
            logger.error(f"Error in get_income_statement: {e}")
            return f"Error retrieving income statement: {str(e)}"
    
    def get_cashflow_statement(
        self,
        symbol: Annotated[str, "Stock ticker symbol (e.g., 'AAPL', 'MSFT')"],
        frequency: Annotated[
            str,
            "Reporting frequency: 'quarterly' for most recent quarterly data or 'annual' for yearly data. "
            "Quarterly shows recent cash generation trends."
        ],
        end_date: Annotated[str, "End date in YYYY-MM-DD format - retrieves most recent statement as of this date"],
        lookback_periods: Annotated[
            int,
            "Number of periods to retrieve. For quarterly, 4 periods = 1 year of cash flow. For annual, 3-5 periods shows multi-year trends. "
            "Default is 4 periods if not specified."
        ] = 4
    ) -> str:
        """
        Retrieve cash flow statement(s) showing how company generates and uses cash.
        
        The cash flow statement tracks actual cash movements (not accounting profits):
        - Operating Activities: Cash from core business operations
        - Investing Activities: Capital expenditures, acquisitions, investments
        - Financing Activities: Debt, dividends, stock buybacks
        - Free Cash Flow: Operating cash flow - capital expenditures
        
        Aggregates data from all configured providers for comprehensive coverage.
        
        Args:
            symbol: Stock ticker symbol
            frequency: 'quarterly' or 'annual'
            end_date: End date in YYYY-MM-DD format
            lookback_periods: Number of statements to retrieve (default 4)
        
        Returns:
            str: Aggregated markdown-formatted cash flow statement data
        
        Example:
            >>> cashflow = get_cashflow_statement("AAPL", "quarterly", "2024-03-15", lookback_periods=4)
            >>> # Shows Apple's last 4 quarterly cash flow statements (1 year of data)
        """
        if "fundamentals_details" not in self.provider_map or not self.provider_map["fundamentals_details"]:
            return "Error: No fundamentals details providers configured"
        
        try:
            end_dt = datetime.strptime(end_date, "%Y-%m-%d")
            
            results = []
            for provider_class in self.provider_map["fundamentals_details"]:
                try:
                    provider = self._instantiate_provider(provider_class)
                    provider_name = provider.__class__.__name__
                    
                    logger.debug(f"Fetching cash flow statement from {provider_name} for {symbol}")
                    
                    # Call provider with format_type="both" to get both markdown and structured data
                    markdown_data, data_dict = self._call_provider_with_both_format(
                        provider,
                        "get_cashflow_statement",
                        symbol=symbol,
                        frequency=frequency,
                        end_date=end_dt,
                        lookback_periods=lookback_periods
                    )
                    
                    if markdown_data is None:
                        results.append(f"## {provider_name} - Error\n\nFailed to fetch cash flow")
                        continue
                    
                    results.append(f"## Cash Flow Statement from {provider_name.upper()}\n\n{markdown_data}")
                    
                except Exception as e:
                    logger.error(f"Error fetching cash flow from {provider_class.__name__}: {e}")
                    results.append(f"## {provider_class.__name__} - Error\n\nFailed to fetch cash flow: {str(e)}")
            
            return "\n\n---\n\n".join(results) if results else "No cash flow data available"
            
        except Exception as e:
            logger.error(f"Error in get_cashflow_statement: {e}")
            return f"Error retrieving cash flow statement: {str(e)}"
    
    def get_past_earnings(
        self,
        symbol: Annotated[str, "Stock ticker symbol"],
        end_date: Annotated[str, "End date in YYYY-MM-DD format"],
        lookback_periods: Annotated[int, "Number of periods to look back (default 8 quarters = 2 years)"] = 8,
        frequency: Annotated[str, "Reporting frequency: 'quarterly' or 'annual'"] = "quarterly"
    ) -> str:
        """
        Retrieve historical earnings data showing actual vs estimated EPS.
        
        Earnings data helps assess:
        - Earnings quality and consistency
        - Whether company beats/misses analyst estimates
        - Earnings surprise trends (positive surprises indicate strength)
        - EPS growth trajectory over time
        
        The surprise percentage shows how much the company outperformed or underperformed
        analyst expectations - a key indicator of company performance vs market expectations.
        
        Aggregates data from all configured providers for comprehensive coverage.
        
        Args:
            symbol: Stock ticker symbol
            end_date: End date in YYYY-MM-DD format
            lookback_periods: Number of periods to look back (default 8 quarters = 2 years)
            frequency: 'quarterly' or 'annual'
        
        Returns:
            str: Aggregated markdown-formatted past earnings data
        
        Example:
            >>> earnings = get_past_earnings("AAPL", "2024-03-15", lookback_periods=8)
            >>> # Shows Apple's last 8 quarters of earnings (2 years)
        """
        if "fundamentals_details" not in self.provider_map or not self.provider_map["fundamentals_details"]:
            return "Error: No fundamentals details providers configured"
        
        try:
            end_dt = datetime.strptime(end_date, "%Y-%m-%d")
            
            results = []
            for provider_class in self.provider_map["fundamentals_details"]:
                try:
                    provider = self._instantiate_provider(provider_class)
                    provider_name = provider.__class__.__name__
                    
                    logger.debug(f"Fetching past earnings from {provider_name} for {symbol}")
                    
                    # Call provider with format_type="both" to get both markdown and structured data
                    markdown_data, data_dict = self._call_provider_with_both_format(
                        provider,
                        "get_past_earnings",
                        symbol=symbol,
                        frequency=frequency,
                        end_date=end_dt,
                        lookback_periods=lookback_periods
                    )
                    
                    if markdown_data is None:
                        results.append(f"## {provider_name} - Error\n\nFailed to fetch past earnings")
                        continue
                    
                    results.append(f"## Past Earnings from {provider_name.upper()}\n\n{markdown_data}")
                    
                except Exception as e:
                    logger.error(f"Error fetching past earnings from {provider_class.__name__}: {e}")
                    results.append(f"## {provider_class.__name__} - Error\n\nFailed to fetch past earnings: {str(e)}")
            
            return "\n\n---\n\n".join(results) if results else "No past earnings data available"
            
        except Exception as e:
            logger.error(f"Error in get_past_earnings: {e}")
            return f"Error retrieving past earnings: {str(e)}"
    
    def get_earnings_estimates(
        self,
        symbol: Annotated[str, "Stock ticker symbol"],
        as_of_date: Annotated[str, "Date for estimates in YYYY-MM-DD format"],
        lookback_periods: Annotated[int, "Number of future periods to retrieve (default 4 quarters)"] = 4,
        frequency: Annotated[str, "Reporting frequency: 'quarterly' or 'annual'"] = "quarterly"
    ) -> str:
        """
        Retrieve forward-looking earnings estimates from analysts.
        
        Earnings estimates help assess:
        - Future growth expectations
        - Analyst consensus and confidence (tight range = high confidence)
        - Potential for earnings surprises (compare to actual results later)
        - Market expectations for the company's performance
        
        The number of analysts provides confidence: more analysts = more reliable estimates.
        A wide range (high - low) suggests uncertainty about future performance.
        
        Aggregates data from all configured providers for comprehensive coverage.
        
        Args:
            symbol: Stock ticker symbol
            as_of_date: Date for estimates in YYYY-MM-DD format
            lookback_periods: Number of future periods to retrieve (default 4 quarters)
            frequency: 'quarterly' or 'annual'
        
        Returns:
            str: Aggregated markdown-formatted earnings estimates
        
        Example:
            >>> estimates = get_earnings_estimates("AAPL", "2024-03-15", lookback_periods=4)
            >>> # Shows next 4 quarters of analyst EPS estimates for Apple
        """
        if "fundamentals_details" not in self.provider_map or not self.provider_map["fundamentals_details"]:
            return "Error: No fundamentals details providers configured"
        
        try:
            as_of_dt = datetime.strptime(as_of_date, "%Y-%m-%d")
            
            results = []
            for provider_class in self.provider_map["fundamentals_details"]:
                try:
                    provider = self._instantiate_provider(provider_class)
                    provider_name = provider.__class__.__name__
                    
                    logger.debug(f"Fetching earnings estimates from {provider_name} for {symbol}")
                    
                    # Call provider with format_type="both" to get both markdown and structured data
                    markdown_data, data_dict = self._call_provider_with_both_format(
                        provider,
                        "get_earnings_estimates",
                        symbol=symbol,
                        frequency=frequency,
                        as_of_date=as_of_dt,
                        lookback_periods=lookback_periods
                    )
                    
                    if markdown_data is None:
                        results.append(f"## {provider_name} - Error\n\nFailed to fetch earnings estimates")
                        continue
                    
                    results.append(f"## Earnings Estimates from {provider_name.upper()}\n\n{markdown_data}")
                    
                except Exception as e:
                    logger.error(f"Error fetching earnings estimates from {provider_class.__name__}: {e}")
                    results.append(f"## {provider_class.__name__} - Error\n\nFailed to fetch earnings estimates: {str(e)}")
            
            return "\n\n---\n\n".join(results) if results else "No earnings estimates available"
            
        except Exception as e:
            logger.error(f"Error in get_earnings_estimates: {e}")
            return f"Error retrieving earnings estimates: {str(e)}"
    
    # ========================================================================
    # OHLCV PROVIDERS - Fallback logic (try first, then second, etc.)
    # ========================================================================
    
    def get_ohlcv_data(
        self,
        symbol: Annotated[str, "Stock ticker symbol (e.g., 'AAPL', 'MSFT', 'TSLA')"],
        start_date: Annotated[Optional[str], "Start date in YYYY-MM-DD format (e.g., '2024-01-01'). Optional - defaults to 30 days ago."] = None,
        end_date: Annotated[Optional[str], "End date in YYYY-MM-DD format (e.g., '2024-03-15'). Optional - defaults to today."] = None,
        interval: Annotated[
            Optional[str],
            "Data interval/timeframe: '1d' (daily), '1h' (hourly), '1m' (1-minute), '5m' (5-minute), etc. "
            "Shorter intervals (1m, 5m) for day trading, medium (1h, 1d) for swing trading, longer (1wk, 1mo) for position trading. "
            "If not provided, uses timeframe from expert config."
        ] = None,
        lookback_days: Annotated[int, "Days to look back if dates not provided. Default: 30 days."] = 30
    ) -> str:
        """
        Retrieve OHLCV (Open, High, Low, Close, Volume) stock price data.
        
        OHLCV data is fundamental for technical analysis and price charting:
        - Open: Opening price for the period
        - High: Highest price during the period
        - Low: Lowest price during the period
        - Close: Closing price for the period
        - Volume: Number of shares traded
        
        OPTIONAL DATE LOGIC:
        - If both start_date and end_date are provided: uses them as-is
        - If end_date is None/empty: defaults to current date
        - If start_date is None/empty: defaults to end_date - lookback_days (default 30 days)
        
        Uses FALLBACK logic: tries first provider, if it fails, tries second provider, etc.
        Only one provider's data is returned (the first successful one).
        
        Args:
            symbol: Stock ticker symbol
            start_date: Start date in YYYY-MM-DD format (optional)
            end_date: End date in YYYY-MM-DD format (optional)
            interval: Data interval (default from expert config)
            lookback_days: Days to look back if dates not provided (default: 30)
        
        Returns:
            str: Markdown-formatted OHLCV data from first successful provider
        
        Example:
            >>> ohlcv = get_ohlcv_data("AAPL", "2024-01-01", "2024-03-15", interval="1d")
            >>> # Returns daily price data for Apple for the specified period
            >>> ohlcv = get_ohlcv_data("AAPL", interval="1d")
            >>> # Returns last 30 days of daily data
        """
        if "ohlcv" not in self.provider_map or not self.provider_map["ohlcv"]:
            return "Error: No OHLCV providers configured"
        
        try:
            from datetime import datetime as dt_class, timezone
            from ba2_trade_platform.core.provider_utils import validate_date_range
            
            # Treat empty strings as None
            if isinstance(start_date, str) and not start_date.strip():
                start_date = None
            if isinstance(end_date, str) and not end_date.strip():
                end_date = None
            
            # Convert date strings to datetime objects if provided
            start_dt = None
            end_dt = None
            
            if start_date:
                try:
                    start_dt = datetime.strptime(start_date, "%Y-%m-%d")
                except ValueError:
                    return f"Error: Invalid start_date format. Expected YYYY-MM-DD, got '{start_date}'"
            
            if end_date:
                try:
                    end_dt = datetime.strptime(end_date, "%Y-%m-%d")
                except ValueError:
                    return f"Error: Invalid end_date format. Expected YYYY-MM-DD, got '{end_date}'"
            
            # Validate and normalize dates with smart defaults
            start_dt, end_dt = validate_date_range(start_dt, end_dt, lookback_days)
            
            # Get interval from config if not provided
            if interval is None:
                from ...dataflows.config import get_config
                config = get_config()
                interval = config["timeframe"]
            
            # Try each provider in order until one succeeds (fallback logic)
            for provider_class in self.provider_map["ohlcv"]:
                try:
                    provider = self._instantiate_provider(provider_class)
                    provider_name = provider.__class__.__name__
                    
                    logger.debug(f"Trying OHLCV provider {provider_name} for {symbol}")
                    
                    # Call provider's get_ohlcv_data method (returns DataFrame)
                    # Note: OHLCV providers do NOT support format_type parameter
                    df = provider.get_ohlcv_data(
                        symbol=symbol,
                        start_date=start_dt,
                        end_date=end_dt,
                        interval=interval
                    )
                    
                    # Check if data was retrieved successfully
                    if df is None or df.empty:
                        logger.warning(f"OHLCV provider {provider_name} returned empty data, trying next provider...")
                        continue
                    
                    logger.info(f"Successfully retrieved OHLCV data from {provider_name}")
                    
                    # Convert DataFrame to markdown and JSON using helper method
                    markdown_text, json_data = self._format_ohlcv_dataframe(df, symbol)
                    
                    # Format original dates for storage
                    orig_start = start_date if start_date else start_dt.strftime("%Y-%m-%d")
                    orig_end = end_date if end_date else end_dt.strftime("%Y-%m-%d")
                    
                    # Return structured format for LoggingToolNode to store both text and data
                    # This allows the database to store complete OHLCV data in JSON format
                    return {
                        "_internal": True,
                        "text_for_agent": markdown_text,
                        "json_for_storage": {
                            "tool": "get_ohlcv_data",
                            "symbol": symbol,
                            "start_date": orig_start,
                            "end_date": orig_end,
                            "interval": interval,
                            "provider": provider_name,
                            "data": json_data
                        }
                    }
                    
                except Exception as e:
                    logger.warning(f"OHLCV provider {provider_class.__name__} failed: {e}, trying next provider...")
                    continue
            
            return "Error: All OHLCV providers failed to retrieve data"
            
        except Exception as e:
            logger.error(f"Error in get_ohlcv_data: {e}", exc_info=True)
            return f"Error retrieving OHLCV data: {str(e)}"
    
    # ========================================================================
    # INDICATORS PROVIDERS - Fallback logic (try first, then second, etc.)
    # ========================================================================
    
    def get_indicator_data(
        self,
        symbol: Annotated[str, "Stock ticker symbol (e.g., 'AAPL', 'MSFT')"],
        indicator: Annotated[
            str,
            "Technical indicator name. Available indicators: "
            "MOVING AVERAGES: 'close_50_sma' (50-day Simple Moving Average - medium-term trend), "
            "'close_200_sma' (200-day SMA - long-term trend), 'close_10_ema' (10-day EMA - fast short-term). "
            "MACD: 'macd' (MACD line - momentum), 'macds' (Signal line - smoother MACD), 'macdh' (Histogram - momentum strength). "
            "MOMENTUM: 'rsi' (Relative Strength Index - overbought/oversold 0-100 scale). "
            "VOLATILITY: 'boll' (Bollinger middle band), 'boll_ub' (Bollinger upper band), 'boll_lb' (Bollinger lower band), "
            "'atr' (Average True Range - volatility measure). "
            "VOLUME: 'vwma' (Volume Weighted MA), 'mfi' (Money Flow Index - volume + price pressure). "
            "USAGE NOTES: RSI extremes (>70 or <30) signal overbought/oversold. MACD crossovers signal trend changes. "
            "Bollinger bands signal volatility extremes. ATR helps size positions based on volatility."
        ],
        start_date: Annotated[Optional[str], "Start date in YYYY-MM-DD format. Optional - defaults to 30 days ago."] = None,
        end_date: Annotated[Optional[str], "End date in YYYY-MM-DD format. Optional - defaults to today."] = None,
        interval: Annotated[
            Optional[str],
            "Data interval/timeframe: '1d' (daily), '1h' (hourly), etc. "
            "Must match the timeframe you're analyzing. If not provided, uses timeframe from expert config."
        ] = None,
        lookback_days: Annotated[int, "Days to look back if dates not provided. Default: 30 days."] = 30
    ) -> str:
        """
        Retrieve technical indicator data for analysis.
        
        Technical indicators help identify trends, momentum, volatility, and potential trading signals:
        - Trend: SMA, EMA (moving averages)
        - Momentum: RSI, MACD, MFI
        - Volatility: Bollinger Bands, ATR
        - Volume: VWMA
        
        OPTIONAL DATE LOGIC:
        - If both start_date and end_date are provided: uses them as-is
        - If end_date is None/empty: defaults to current date
        - If start_date is None/empty: defaults to end_date - lookback_days (default 30 days)
        
        Uses FALLBACK logic: tries first provider, if it fails, tries second provider, etc.
        Only one provider's data is returned (the first successful one).
        
        Args:
            symbol: Stock ticker symbol
            indicator: Indicator name (e.g., 'rsi', 'macd', 'close_50_sma')
            start_date: Start date in YYYY-MM-DD format (optional)
            end_date: End date in YYYY-MM-DD format (optional)
            interval: Data interval (default from expert config)
            lookback_days: Days to look back if dates not provided (default: 30)
        
        Returns:
            str: Markdown-formatted indicator data from first successful provider
        
        Example:
            >>> rsi = get_indicator_data("AAPL", "rsi", "2024-01-01", "2024-03-15", interval="1d")
            >>> # Returns RSI indicator values for Apple
            >>> rsi = get_indicator_data("AAPL", "rsi", interval="1d")
            >>> # Returns RSI for last 30 days
        """
        if "indicators" not in self.provider_map or not self.provider_map["indicators"]:
            return "Error: No indicator providers configured"
        
        try:
            import json as json_module
            from ba2_trade_platform.core.provider_utils import validate_date_range
            
            # Treat empty strings as None
            if isinstance(start_date, str) and not start_date.strip():
                start_date = None
            if isinstance(end_date, str) and not end_date.strip():
                end_date = None
            
            # Convert date strings to datetime objects if provided
            start_dt = None
            end_dt = None
            
            if start_date:
                try:
                    start_dt = datetime.strptime(start_date, "%Y-%m-%d")
                except ValueError:
                    return f"Error: Invalid start_date format. Expected YYYY-MM-DD, got '{start_date}'"
            
            if end_date:
                try:
                    end_dt = datetime.strptime(end_date, "%Y-%m-%d")
                except ValueError:
                    return f"Error: Invalid end_date format. Expected YYYY-MM-DD, got '{end_date}'"
            
            # Validate and normalize dates with smart defaults
            start_dt, end_dt = validate_date_range(start_dt, end_dt, lookback_days)
            
            # Get interval from config if not provided
            if interval is None:
                from ...dataflows.config import get_config
                config = get_config()
                interval = config["timeframe"]
            
            # Try each provider in order until one succeeds (fallback logic)
            for provider_class in self.provider_map["indicators"]:
                try:
                    provider = self._instantiate_provider(provider_class)
                    provider_name = provider.__class__.__name__
                    
                    logger.debug(f"Trying indicator provider {provider_name} for {symbol} - {indicator}")
                    
                    # Get both markdown (for LLM) and structured data (for storage) in single call
                    # format_type="both" returns {"text": markdown_str, "data": dict}
                    result = provider.get_indicator(
                        symbol=symbol,
                        indicator=indicator,
                        start_date=start_dt,
                        end_date=end_dt,
                        interval=interval,
                        format_type="both"
                    )
                    
                    # Extract markdown and data from result
                    if isinstance(result, dict) and "text" in result and "data" in result:
                        markdown_data = result["text"]
                        indicator_data = result["data"]
                    else:
                        raise ValueError(f"Provider did not return expected format with 'text' and 'data' keys. Got: {type(result)}")
                    
                    logger.info(f"Successfully retrieved indicator data from {provider_name}")
                    
                    # Return structured format for LoggingToolNode storage
                    text_for_agent = f"## {indicator.upper()} from {provider_name.upper()}\n\n{markdown_data}"
                    
                    # Format original dates for storage
                    orig_start = start_date if start_date else start_dt.strftime("%Y-%m-%d")
                    orig_end = end_date if end_date else end_dt.strftime("%Y-%m-%d")
                    
                    json_for_storage = {
                        "tool": "get_indicator_data",
                        "symbol": symbol,
                        "indicator": indicator,
                        "start_date": orig_start,
                        "end_date": orig_end,
                        "interval": interval,
                        "provider": provider_name,
                        "data": indicator_data if isinstance(indicator_data, dict) else {"raw": str(indicator_data)}
                    }
                    
                    return {
                        "_internal": True,
                        "text_for_agent": text_for_agent,
                        "json_for_storage": json_for_storage
                    }
                    
                except Exception as e:
                    logger.warning(f"Indicator provider {provider_class.__name__} failed: {e}, trying next provider...")
                    continue
            
            return f"Error: All indicator providers failed to retrieve {indicator} data"
            
        except Exception as e:
            logger.error(f"Error in get_indicator_data: {e}", exc_info=True)
            return f"Error retrieving indicator data: {str(e)}"
    
    # ========================================================================
    # MACRO PROVIDERS - Aggregate results from all providers
    # ========================================================================
    
    def get_economic_indicators(
        self,
        end_date: Annotated[str, "End date in YYYY-MM-DD format"],
        lookback_days: Annotated[
            Optional[int],
            "Number of days to look back for economic data. If not provided, defaults to economic_data_days from config (typically 90 days). "
            "Longer periods show economic trends, shorter periods show recent changes."
        ] = None,
        indicators: Annotated[
            Optional[List[str]],
            "List of specific indicator codes to retrieve (e.g., ['GDP', 'UNRATE', 'CPIAUCSL']). "
            "If None, retrieves all available major economic indicators. Common codes: "
            "GDP (Gross Domestic Product), UNRATE (Unemployment Rate), CPIAUCSL (CPI/Inflation), "
            "FEDFUNDS (Fed Funds Rate), DGS10 (10-Year Treasury), PAYEMS (Nonfarm Payrolls)."
        ] = None
    ) -> str:
        """
        Retrieve economic indicators (GDP, unemployment, inflation, etc.).
        
        Economic indicators help understand the broader economic environment affecting markets:
        - Growth: GDP, industrial production
        - Employment: Unemployment rate, nonfarm payrolls, job openings
        - Inflation: CPI, PPI, PCE
        - Monetary Policy: Fed funds rate, money supply
        
        Aggregates data from all configured providers for comprehensive coverage.
        
        Args:
            end_date: End date in YYYY-MM-DD format
            lookback_days: Days to look back (default from config, typically 90 days)
            indicators: Specific indicators to retrieve (None = all available)
        
        Returns:
            str: Aggregated markdown-formatted economic indicator data
        
        Example:
            >>> econ = get_economic_indicators("2024-03-15", lookback_days=90)
            >>> # Returns key economic indicators for the past 90 days
        """
        if "macro" not in self.provider_map or not self.provider_map["macro"]:
            return "Error: No macro providers configured"
        
        try:
            end_dt = datetime.strptime(end_date, "%Y-%m-%d")
            
            # Default lookback_days from provider_args (expert settings) if not provided
            if lookback_days is None:
                lookback_days = self.provider_args.get("economic_data_days", 90)
            
            results = []
            for provider_class in self.provider_map["macro"]:
                try:
                    provider = self._instantiate_provider(provider_class)
                    provider_name = provider.__class__.__name__
                    
                    logger.debug(f"Fetching economic indicators from {provider_name}")
                    
                    # Call provider with format_type="both" to get both markdown and structured data
                    markdown_data, data_dict = self._call_provider_with_both_format(
                        provider,
                        "get_economic_indicators",
                        end_date=end_dt,
                        lookback_days=lookback_days,
                        indicators=indicators
                    )
                    
                    if markdown_data is None:
                        results.append(f"## {provider_name} - Error\n\nFailed to fetch indicators")
                        continue
                    
                    results.append(f"## Economic Indicators from {provider_name.upper()}\n\n{markdown_data}")
                    
                except Exception as e:
                    logger.error(f"Error fetching economic indicators from {provider_class.__name__}: {e}")
                    results.append(f"## {provider_class.__name__} - Error\n\nFailed to fetch indicators: {str(e)}")
            
            return "\n\n---\n\n".join(results) if results else "No economic indicator data available"
            
        except Exception as e:
            logger.error(f"Error in get_economic_indicators: {e}")
            return f"Error retrieving economic indicators: {str(e)}"
    
    def get_yield_curve(
        self,
        end_date: Annotated[str, "End date in YYYY-MM-DD format"],
        lookback_days: Annotated[
            Optional[int],
            "Number of days to look back for yield curve history. If not provided, defaults to economic_data_days from config (typically 90 days). "
            "Historical data shows yield curve inversions (recession indicators)."
        ] = None
    ) -> str:
        """
        Retrieve Treasury yield curve data and inversion analysis.
        
        The yield curve plots interest rates across different Treasury maturities (1-month to 30-year).
        - Normal curve: Long-term rates > short-term rates (healthy economy)
        - Flat curve: Similar rates across maturities (uncertainty)
        - Inverted curve: Short-term rates > long-term rates (potential recession signal)
        
        Tracks 10Y-2Y spread and inversion status - historically reliable recession predictor.
        Aggregates data from all configured providers.
        
        Args:
            end_date: End date in YYYY-MM-DD format
            lookback_days: Days to look back for historical curve data (default 90)
        
        Returns:
            str: Aggregated markdown-formatted yield curve data with inversion analysis
        
        Example:
            >>> yield_curve = get_yield_curve("2024-03-15", lookback_days=90)
            >>> # Returns current yield curve and historical trend
        """
        if "macro" not in self.provider_map or not self.provider_map["macro"]:
            return "Error: No macro providers configured"
        
        try:
            end_dt = datetime.strptime(end_date, "%Y-%m-%d")
            
            # Default lookback_days from provider_args (expert settings) if not provided
            if lookback_days is None:
                lookback_days = self.provider_args.get("economic_data_days", 90)
            
            results = []
            for provider_class in self.provider_map["macro"]:
                try:
                    provider = self._instantiate_provider(provider_class)
                    provider_name = provider.__class__.__name__
                    
                    logger.debug(f"Fetching yield curve from {provider_name}")
                    
                    # Call provider with format_type="both" to get both markdown and structured data
                    markdown_data, data_dict = self._call_provider_with_both_format(
                        provider,
                        "get_yield_curve",
                        end_date=end_dt,
                        lookback_days=lookback_days
                    )
                    
                    if markdown_data is None:
                        results.append(f"## {provider_name} - Error\n\nFailed to fetch yield curve")
                        continue
                    
                    results.append(f"## Yield Curve from {provider_name.upper()}\n\n{markdown_data}")
                    
                except Exception as e:
                    logger.error(f"Error fetching yield curve from {provider_class.__name__}: {e}")
                    results.append(f"## {provider_class.__name__} - Error\n\nFailed to fetch yield curve: {str(e)}")
            
            return "\n\n---\n\n".join(results) if results else "No yield curve data available"
            
        except Exception as e:
            logger.error(f"Error in get_yield_curve: {e}")
            return f"Error retrieving yield curve: {str(e)}"
    
    def get_fed_calendar(
        self,
        end_date: Annotated[str, "End date in YYYY-MM-DD format"],
        lookback_days: Annotated[
            Optional[int],
            "Number of days to look back for Fed events. If not provided, defaults to economic_data_days from config (typically 90 days). "
            "Captures recent FOMC meetings, statements, and speeches."
        ] = None
    ) -> str:
        """
        Retrieve Federal Reserve calendar including FOMC meetings, statements, and speeches.
        
        Fed events are critical market drivers:
        - FOMC Meetings: Interest rate decisions (affects all asset prices)
        - Meeting Minutes: Detailed policy discussions and economic outlook
        - Statements: Policy guidance and forward-looking commentary
        - Speeches: Fed Chair and governor speeches on policy direction
        
        Rate changes, QE/QT programs, and forward guidance significantly impact trading decisions.
        Aggregates data from all configured providers.
        
        Args:
            end_date: End date in YYYY-MM-DD format
            lookback_days: Days to look back for Fed events (default 90)
        
        Returns:
            str: Aggregated markdown-formatted Fed calendar with key decisions
        
        Example:
            >>> fed_cal = get_fed_calendar("2024-03-15", lookback_days=90)
            >>> # Returns recent FOMC meetings and policy decisions
        """
        if "macro" not in self.provider_map or not self.provider_map["macro"]:
            return "Error: No macro providers configured"
        
        try:
            end_dt = datetime.strptime(end_date, "%Y-%m-%d")
            
            # Default lookback_days from provider_args (expert settings) if not provided
            if lookback_days is None:
                lookback_days = self.provider_args.get("economic_data_days", 90)
                logger.debug(f"Using default lookback_days from expert settings: {lookback_days}")
            
            # Ensure lookback_days is not None
            if lookback_days is None:
                logger.error("lookback_days is None even after expert settings lookup")
                return "Error: Unable to determine lookback_days for Fed calendar"
            
            results = []
            for provider_class in self.provider_map["macro"]:
                try:
                    provider = self._instantiate_provider(provider_class)
                    provider_name = provider.__class__.__name__
                    
                    logger.debug(f"Fetching Fed calendar from {provider_name} with lookback_days={lookback_days}")
                    fed_data = provider.get_fed_calendar(
                        end_date=end_dt,
                        lookback_days=lookback_days,
                        format_type="markdown"
                    )
                    
                    results.append(f"## Fed Calendar from {provider_name.upper()}\n\n{fed_data}")
                    
                except Exception as e:
                    logger.error(f"Error fetching Fed calendar from {provider_class.__name__}: {e}")
                    results.append(f"## {provider_class.__name__} - Error\n\nFailed to fetch Fed calendar: {str(e)}")
            
            return "\n\n---\n\n".join(results) if results else "No Fed calendar data available"
            
        except Exception as e:
            logger.error(f"Error in get_fed_calendar: {e}")
            return f"Error retrieving Fed calendar: {str(e)}"


def create_msg_delete():
    """
    Create a function to clear messages and add placeholder for Anthropic compatibility.
    
    This is used in the agent graph to manage message history and avoid context overflow.
    """
    from langchain_core.messages import RemoveMessage, HumanMessage
    
    def delete_messages(state):
        """Clear messages and add placeholder for Anthropic compatibility"""
        messages = state["messages"]
        
        # Remove all messages
        removal_operations = [RemoveMessage(id=m.id) for m in messages]
        
        # Add a minimal placeholder message
        placeholder = HumanMessage(content="Continue")
        
        return {"messages": removal_operations + [placeholder]}
    
    return delete_messages
