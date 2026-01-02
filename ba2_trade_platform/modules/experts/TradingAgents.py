from typing import Any, Dict, List, Optional
from datetime import datetime, timezone
import json
from sqlmodel import select

from ...core.interfaces import MarketExpertInterface, SmartRiskExpertInterface
from ...core.models import ExpertInstance, MarketAnalysis, AnalysisOutput, ExpertRecommendation
from ...core.db import get_db, get_instance, update_instance, add_instance
from ...core.types import MarketAnalysisStatus, OrderRecommendation, RiskLevel, TimeHorizon, AnalysisUseCase
from ...logger import get_expert_logger
from ...thirdparties.TradingAgents.tradingagents.graph.trading_graph import TradingAgentsGraph
from ...thirdparties.TradingAgents.tradingagents.default_config import DEFAULT_CONFIG
from ...thirdparties.TradingAgents.tradingagents.db_storage import update_market_analysis_status


class TradingAgents(MarketExpertInterface, SmartRiskExpertInterface):
    """
    TradingAgents Expert Implementation
    
    Multi-agent AI system for market analysis and trading recommendations.
    Integrates news, technical, fundamental, and macro-economic analysis
    through specialized AI agents with debate-based decision making.
    """
    
    @classmethod
    def description(cls) -> str:
        return "Multi-agent AI trading system with debate-based analysis and risk assessment"
    
    @classmethod
    def _get_timeframe_valid_values(cls) -> List[str]:
        """Get valid timeframe values from TimeInterval enum."""
        from ...core.types import TimeInterval
        return TimeInterval.get_all_intervals()
    
    def __init__(self, id: int):
        """Initialize TradingAgents expert with database instance."""
        super().__init__(id)
        
        self._setup_api_keys()
        self._load_expert_instance(id)
        
        # Initialize expert-specific logger
        self.logger = get_expert_logger("TradingAgents", id)
    
    def _setup_api_keys(self) -> None:
        """Setup API keys from database configuration."""
        try:
            from ...thirdparties.TradingAgents.tradingagents.dataflows.config import set_environment_variables_from_database
            set_environment_variables_from_database()
        except Exception as e:
            # Logger not initialized yet, will log in __init__ after logger is set up
            self._api_key_error = str(e)
    
    def _load_expert_instance(self, id: int) -> None:
        """Load and validate expert instance from database."""
        self.instance = get_instance(ExpertInstance, id)
        if not self.instance:
            raise ValueError(f"ExpertInstance with ID {id} not found")
        
        # Log API key setup error if it occurred
        if hasattr(self, '_api_key_error'):
            self.logger.warning(f"Could not load API keys from database: {self._api_key_error}")
    
    @classmethod
    def get_settings_definitions(cls) -> Dict[str, Any]:
        """Define configurable settings for TradingAgents expert."""
        return {
            # Analysis Configuration
            "debates_new_positions": {
                "type": "float", "required": True, "default": 3.0,
                "description": "Number of debate rounds for new position analysis",
                "tooltip": "Controls how many debate rounds the AI agents will conduct when analyzing potential new positions. More rounds = more thorough analysis but takes longer. Recommended: 2-4 rounds."
            },
            "debates_existing_positions": {
                "type": "float", "required": True, "default": 3.0,
                "description": "Number of debate rounds for existing position analysis",
                "tooltip": "Controls how many debate rounds the AI agents will conduct when reviewing existing open positions. More rounds = more thorough analysis. Recommended: 2-3 rounds for faster real-time decisions."
            },
            "timeframe": {
                "type": "str", "required": True, "default": "1h",
                "description": "Analysis timeframe for market data",
                "valid_values": cls._get_timeframe_valid_values(),
                "tooltip": "The time interval used for technical analysis charts and indicators. Shorter timeframes (1m, 5m) are for day trading, medium (1h, 4h, 1d) for swing trading, longer (1wk, 1mo) for position trading."
            },
            
            # LLM Models (Format: Provider/ModelName - e.g., OpenAI/gpt-4o-mini or NagaAI/gemini-2.5-flash:free)
            "deep_think_llm": {
                "type": "str", "required": True, "default": "OpenAI/gpt-4o-mini",
                "description": "LLM model for complex reasoning and deep analysis",
                "ui_editor_type": "ModelSelector",
                "tooltip": "The AI model used for in-depth analysis requiring complex reasoning, such as fundamental analysis and debate arbitration. Use the model selector to browse available models."
            },
            "quick_think_llm": {
                "type": "str", "required": True, "default": "OpenAI/gpt-4o-mini",
                "description": "LLM model for quick analysis and real-time decisions",
                "ui_editor_type": "ModelSelector",
                "help": "For more information, see [OpenAI Models](https://platform.openai.com/docs/models) and [Naga AI Models](https://naga.ac/models)",
                "tooltip": "The AI model used for faster analysis tasks like technical indicators and quick data summarization. Use the model selector to browse available models."
            },
            "dataprovider_websearch_model": {
                "type": "str", "required": True, "default": "OpenAI/gpt4o",
                "description": "Model for data provider web searches",
                "ui_editor_type": "ModelSelector",
                "required_labels": ["websearch"],  # Only show models with websearch capability
                "help": "For more information, see [OpenAI Docs](https://platform.openai.com/docs/models), [Naga AI Web Search](https://docs.naga.ac/features/web-search), and [Gemini Thinking](https://ai.google.dev/gemini-api/docs/thinking)",
                "tooltip": "The model used by data providers for web search and data gathering. Only models with web search capability are shown."
            },
            "embedding_model": {
                "type": "str", "required": True, "default": "OpenAI/text-embedding-3-small",
                "description": "Model for generating embeddings for memories/vector storage",
                "valid_values": [
                    # OpenAI embeddings (direct)
                    "OpenAI/text-embedding-3-small",
                    "OpenAI/text-embedding-3-large",
                    # NagaAI embeddings (same models via Naga AI)
                    "NagaAI/text-embedding-3-small",
                    "NagaAI/text-embedding-3-large",
                ],
                "allow_custom": True,
                "help": "For more information, see [OpenAI Embeddings](https://platform.openai.com/docs/guides/embeddings) and [Naga AI Embeddings](https://docs.naga.ac/models/embeddings)",
                "tooltip": "The model used to generate embeddings for storing and retrieving memories in vector databases. Format: Provider/ModelName. text-embedding-3-small is faster and cheaper, text-embedding-3-large provides better accuracy."
            },
            
            # Data Lookback Periods
            "news_lookback_days": {
                "type": "int", "required": True, "default": 7,
                "description": "Days of news data to analyze",
                "tooltip": "How many days back to search for news articles about the symbol. More days = broader context but may include outdated information. Recommended: 3-7 days for active stocks, 14-30 for slower-moving positions."
            },
            "market_history_days": {
                "type": "int", "required": True, "default": 90,
                "description": "Days of market history for technical analysis",
                "tooltip": "Historical price data window for calculating technical indicators (moving averages, RSI, MACD, etc.). 90 days provides good context for most indicators. Increase to 180-365 for longer-term trend analysis."
            },
            "economic_data_days": {
                "type": "int", "required": True, "default": 90,
                "description": "Days of economic data to consider",
                "tooltip": "Lookback period for macroeconomic indicators (inflation, GDP, interest rates, etc.). 90 days captures recent economic trends. Increase to 180-365 for broader economic cycle analysis."
            },
            "social_sentiment_days": {
                "type": "int", "required": True, "default": 3,
                "description": "Days of social sentiment data to analyze",
                "tooltip": "How many days of social media and Reddit sentiment to analyze. Social sentiment changes rapidly, so 1-7 days is typical. Shorter periods (1-3 days) capture current buzz, longer periods (7-14 days) smooth out noise."
            },
            
            # Data Vendor Settings
            "vendor_stock_data": {
                "type": "list", "required": True, "default": ["yfinance"],
                "description": "Data vendor(s) for OHLCV stock price data",
                "valid_values": ["yfinance", "alpaca", "alpha_vantage", "fmp"],
                "multiple": True,
                "tooltip": "Select one or more data providers for historical stock prices (Open, High, Low, Close, Volume). Multiple vendors enable automatic fallback. Order matters: first vendor is tried first. YFinance is free and reliable. Alpaca provides real-time and historical data. Alpha Vantage requires API key. FMP provides daily and intraday data (1min to 4hour intervals)."
            },
            "vendor_indicators": {
                "type": "list", "required": True, "default": ["pandas"],
                "description": "Data vendor(s) for technical indicators",
                "valid_values": ["pandas", "alpha_vantage"],
                "multiple": True,
                "tooltip": "Select one or more data providers for technical indicators (RSI, MACD, Bollinger Bands, etc.). Multiple vendors enable automatic fallback. Pandas calculates indicators locally using configured OHLCV provider. Alpha Vantage provides pre-calculated indicators."
            },
            "vendor_fundamentals": {
                "type": "list", "required": True, "default": ["alpha_vantage"],
                "description": "Data vendor(s) for company fundamentals overview",
                "valid_values": ["alpha_vantage", "ai", "fmp"],
                "multiple": True,
                "tooltip": "Select one or more data providers for company overview and key metrics (market cap, P/E ratio, beta, industry, sector, etc.). Multiple vendors enable automatic fallback. Alpha Vantage provides comprehensive company overviews. AI uses OpenAI (direct) or NagaAI models for web search to gather latest company information. FMP provides detailed company profiles including valuation metrics and company information."
            },
            "vendor_fundamentals_details": {
                "type": "list", "required": True, "default": ["yfinance"],
                "description": "Data vendor(s) for company fundamentals details (balance sheet, cash flow, income statement)",
                "valid_values": ["yfinance", "fmp", "alpha_vantage"],
                "multiple": True,
                "tooltip": "Select one or more data providers for detailed financial statements (balance sheet, cash flow statement, income statement). Multiple vendors enable automatic fallback. YFinance provides quarterly/annual data. FMP provides comprehensive financial data. Alpha Vantage offers similar data."
            },
            "vendor_news": {
                "type": "list", "required": True, "default": ["ai", "alpaca"],
                "description": "Data vendor(s) for company news",
                "valid_values": ["ai", "alpaca", "alpha_vantage", "fmp", "finnhub"],
                "multiple": True,
                "tooltip": "Select one or more data providers for company news articles. Multiple vendors enable automatic fallback. AI uses OpenAI (direct) or NagaAI models for web search across social media/news. Alpaca provides news from multiple sources. Alpha Vantage provides news sentiment API. FMP provides company-specific news articles. Finnhub provides comprehensive news from multiple financial sources."
            },
            "vendor_global_news": {
                "type": "list", "required": True, "default": ["ai"],
                "description": "Data vendor(s) for global/macro news",
                "valid_values": ["ai", "fmp", "finnhub"],
                "multiple": True,
                "tooltip": "Select one or more data providers for global macroeconomic news (interest rates, inflation, geopolitics, etc.). Multiple vendors enable automatic fallback. AI uses OpenAI (direct) or NagaAI models for web search to find latest news. FMP provides general market news. Finnhub provides general market news from multiple financial sources."
            },
            "vendor_insider": {
                "type": "list", "required": True, "default": ["fmp"],
                "description": "Data vendor(s) for insider trading data (transactions and sentiment)",
                "valid_values": ["fmp"],
                "multiple": True,
                "tooltip": "Select one or more data providers for insider trading data. FMP provides both insider transactions (buys/sells by executives) and calculated sentiment scores from SEC filings."
            },
            "vendor_social_media": {
                "type": "list", "required": True, "default": ["ai"],
                "description": "Data vendor(s) for social media sentiment analysis",
                "valid_values": ["ai"],
                "multiple": True,
                "tooltip": "Select one or more data providers for social media sentiment analysis. Crawls Twitter/X, Reddit, StockTwits, forums, and other public sources. AI uses OpenAI (direct) or NagaAI models for web search to analyze sentiment across multiple platforms."
            },
            
            # Provider Configuration
            "alpha_vantage_source": {
                "type": "str", "required": True, "default": "trading_agents",
                "description": "Source identifier for Alpha Vantage API calls",
                "tooltip": "This identifier is sent with Alpha Vantage API requests for usage tracking and analytics. Default 'trading_agents' helps differentiate TradingAgents usage from other platform components. Can be customized for advanced tracking scenarios."
            },
            
            # Analyst Selection
            "enable_market_analyst": {
                "type": "bool", "required": True, "default": True,
                "description": "Enable Market/Technical Analyst",
                "tooltip": "The Market Analyst analyzes price charts, technical indicators (RSI, MACD, Moving Averages, etc.), and trading patterns. Essential for technical analysis and timing entry/exit points. Highly recommended to keep enabled."
            },
            "enable_social_analyst": {
                "type": "bool", "required": True, "default": True,
                "description": "Enable Social Media Analyst",
                "tooltip": "The Social Media Analyst monitors sentiment from Reddit, Twitter, and other social platforms. Useful for gauging retail investor sentiment and detecting trending stocks. Can be disabled if social sentiment is not relevant to your strategy."
            },
            "enable_news_analyst": {
                "type": "bool", "required": True, "default": True,
                "description": "Enable News Analyst",
                "tooltip": "The News Analyst gathers and analyzes recent company news, press releases, and media coverage. Critical for event-driven trading and understanding market-moving catalysts. Recommended to keep enabled."
            },
            "enable_fundamentals_analyst": {
                "type": "bool", "required": True, "default": True,
                "description": "Enable Fundamentals Analyst",
                "tooltip": "The Fundamentals Analyst evaluates company financials, earnings reports, valuation metrics (P/E, P/B, ROE), and business health. Essential for value investing and long-term positions. Highly recommended to keep enabled."
            },
            "enable_macro_analyst": {
                "type": "bool", "required": True, "default": True,
                "description": "Enable Macro/Economic Analyst",
                "tooltip": "The Macro Analyst monitors economic indicators (inflation, GDP, interest rates, unemployment), Federal Reserve policy, and global economic trends. Important for understanding market-wide forces. Can be disabled for pure stock-picking strategies."
            },
            
            "debug_mode": {
                "type": "bool", "required": True, "default": True,
                "description": "Enable debug mode with detailed console output",
                "tooltip": "When enabled, outputs detailed logs of the AI agent's thinking process, data gathering, and decision-making steps. Useful for understanding why recommendations were made. Disable for cleaner logs in production."
            },
            "use_memory": {
                "type": "bool", "required": True, "default": True,
                "description": "Use memory system for context-aware analysis",
                "tooltip": "When enabled, agents retrieve past experiences and recommendations from memory to inform current decisions. Memories are always stored but only used when this is enabled. Disabling may be useful for fresh analysis without historical bias."
            },
            "parallel_tool_calls": {
                "type": "bool", "required": False, "default": True,
                "description": "TradingAgents Parallel Tool Calls",
                "help": "Enable parallel tool calls for all TradingAgents analysts. May cause issues with some LLM providers (e.g., GPT-4.5/5.1 reasoning modes).",
                "tooltip": "Allows analysts to call multiple tools simultaneously for faster execution. Disable if experiencing corrupted tool names or call_id errors with certain LLM models (especially GPT-4.5/5.1 with reasoning). When disabled, tools execute sequentially which is slower but more stable."
            },
            "enable_streaming": {
                "type": "bool", "required": False, "default": True,
                "description": "Enable LLM Streaming",
                "help": "Enable streaming responses from LLM API. Prevents Cloudflare timeouts on long operations but causes tool call bugs with NagaAI/Grok. See [NagaAI Streaming Bug](docs/NAGAAI_STREAMING_TOOL_CALL_BUG.md).",
                "tooltip": "When enabled, LLM responses stream incrementally which prevents timeouts on long operations. DISABLE for NagaAI/Grok models as streaming causes tool call arguments to be lost and tool names to get concatenated."
            },
            
            # Dynamic Instrument Selection
            "max_instruments": {
                "type": "int", "required": True, "default": 30,
                "description": "Maximum number of instruments for dynamic AI selection",
                "tooltip": "When using dynamic AI-driven instrument selection (e.g., for DYNAMIC symbol analysis), this setting limits the number of instruments returned by the AI selector. This prevents excessive analysis queue buildup. Default: 30 instruments per dynamic selection."
            }
        }

    def _get_current_timestamp(self) -> str:
        """Get current UTC timestamp in ISO format."""
        return datetime.now(timezone.utc).isoformat()
    
    def _create_tradingagents_config(self, subtype: str) -> Dict[str, Any]:
        """Create TradingAgents configuration from expert settings."""
        config = DEFAULT_CONFIG.copy()
        
        # Get settings definitions for default values
        settings_def = self.get_settings_definitions()
        
        # Choose debate settings based on analysis subtype
        if subtype == AnalysisUseCase.ENTER_MARKET:
            # For new position analysis, use debates_new_positions setting
            max_debate_rounds = int(self.settings.get('debates_new_positions') or settings_def['debates_new_positions']['default'])
            max_risk_discuss_rounds = int(self.settings.get('debates_new_positions') or settings_def['debates_new_positions']['default'])
        elif subtype == AnalysisUseCase.OPEN_POSITIONS:
            # For existing position analysis, use debates_existing_positions setting
            max_debate_rounds = int(self.settings.get('debates_existing_positions') or settings_def['debates_existing_positions']['default'])
            max_risk_discuss_rounds = int(self.settings.get('debates_existing_positions') or settings_def['debates_existing_positions']['default'])
        else:
            # Default fallback
            max_debate_rounds = int(self.settings.get('debates_new_positions') or settings_def['debates_new_positions']['default'])
            max_risk_discuss_rounds = int(self.settings.get('debates_existing_positions') or settings_def['debates_existing_positions']['default'])
        
        # Build tool_vendors mapping from individual vendor settings
        # Convert list-type vendor settings to comma-separated strings
        def _get_vendor_string(key: str) -> str:
            """Get vendor setting as comma-separated string."""
            value = self.settings.get(key) or settings_def[key]['default']
            # If value is already a list, join with commas; otherwise return as-is
            if isinstance(value, list):
                return ','.join(value)
            return value
        
        tool_vendors = {
            'get_stock_data': _get_vendor_string('vendor_stock_data'),
            'get_indicators': _get_vendor_string('vendor_indicators'),
            'get_fundamentals': _get_vendor_string('vendor_fundamentals'),
            'get_balance_sheet': _get_vendor_string('vendor_fundamentals_details'),
            'get_cashflow': _get_vendor_string('vendor_fundamentals_details'),
            'get_income_statement': _get_vendor_string('vendor_fundamentals_details'),
            'get_news': _get_vendor_string('vendor_news'),
            'get_global_news': _get_vendor_string('vendor_global_news'),
            'get_insider_sentiment': _get_vendor_string('vendor_insider'),
            'get_insider_transactions': _get_vendor_string('vendor_insider'),
        }
        
        # Get model selection strings - pass them directly to trading_graph for ModelFactory
        deep_think_model_str = self.settings.get('deep_think_llm') or settings_def['deep_think_llm']['default']
        quick_think_model_str = self.settings.get('quick_think_llm') or settings_def['quick_think_llm']['default']
        embedding_model_str = self.settings.get('embedding_model') or settings_def['embedding_model']['default']
        
        # Apply user settings with defaults from settings definitions
        # Pass full model selection strings - ModelFactory will handle parsing
        config.update({
            'max_debate_rounds': max_debate_rounds,
            'max_risk_discuss_rounds': max_risk_discuss_rounds,
            'deep_think_llm': deep_think_model_str,  # Full selection string (e.g., "nagaai/gpt5")
            'quick_think_llm': quick_think_model_str,  # Full selection string (e.g., "nagaai/gpt5_mini")
            'embedding_model': embedding_model_str,  # Full selection string
            'news_lookback_days': int(self.settings.get('news_lookback_days') or settings_def['news_lookback_days']['default']),
            'market_history_days': int(self.settings.get('market_history_days') or settings_def['market_history_days']['default']),
            'economic_data_days': int(self.settings.get('economic_data_days') or settings_def['economic_data_days']['default']),
            'social_sentiment_days': int(self.settings.get('social_sentiment_days') or settings_def['social_sentiment_days']['default']),
            'timeframe': self.settings.get('timeframe') or settings_def['timeframe']['default'],
            'tool_vendors': tool_vendors,  # Add tool_vendors to config
            'parallel_tool_calls': self.settings.get('parallel_tool_calls', settings_def['parallel_tool_calls']['default']),
            'enable_streaming': self.settings.get('enable_streaming', settings_def['enable_streaming']['default']),
        })
        
        return config
    
    def _build_provider_map(self) -> Dict[str, List[type]]:
        """
        Build provider_map for new Toolkit from expert settings.
        
        Maps vendor settings to actual BA2 provider classes for each data category.
        
        Returns:
            Dict mapping category names to lists of provider classes:
            {
                "news": [NewsProviderClass1, NewsProviderClass2, ...],
                "insider": [InsiderProviderClass1, ...],
                "macro": [MacroProviderClass1, ...],
                "fundamentals_details": [FundProviderClass1, ...],
                "ohlcv": [OHLCVProviderClass1, ...],
                "indicators": [IndicatorProviderClass1, ...]
            }
        """
        from ...modules.dataproviders import (
            OHLCV_PROVIDERS,
            INDICATORS_PROVIDERS,
            FUNDAMENTALS_OVERVIEW_PROVIDERS,
            FUNDAMENTALS_DETAILS_PROVIDERS,
            NEWS_PROVIDERS,
            MACRO_PROVIDERS,
            INSIDER_PROVIDERS,
            SOCIALMEDIA_PROVIDERS
        )
        
        settings_def = self.get_settings_definitions()
        
        # Helper to get vendor list from settings
        def _get_vendor_list(setting_key: str) -> List[str]:
            """Get list of vendors from setting (handles both list and string formats)."""
            value = self.settings.get(setting_key, settings_def[setting_key]['default'])
            if isinstance(value, list):
                return value
            elif isinstance(value, str):
                return [v.strip() for v in value.split(',') if v.strip()]
            return []
        
        # Build provider_map by looking up provider classes from registries
        provider_map = {}
        
        # News providers (aggregated)
        news_vendors = _get_vendor_list('vendor_news')
        provider_map['news'] = []
        for vendor in news_vendors:
            if vendor in NEWS_PROVIDERS:
                provider_map['news'].append(NEWS_PROVIDERS[vendor])
            else:
                self.logger.warning(f"News provider '{vendor}' not found in NEWS_PROVIDERS registry")
        
        # Social media providers (aggregated)
        # Uses dedicated social media providers with sentiment analysis capabilities
        social_media_vendors = _get_vendor_list('vendor_social_media')
        provider_map['social_media'] = []
        for vendor in social_media_vendors:
            if vendor in SOCIALMEDIA_PROVIDERS:
                provider_map['social_media'].append(SOCIALMEDIA_PROVIDERS[vendor])
            else:
                self.logger.warning(f"Social media provider '{vendor}' not found in SOCIALMEDIA_PROVIDERS registry")
        
        # Global news uses same providers as company news
        # The toolkit will call get_global_news() on each provider
        
        # Insider providers (aggregated) - for both transactions and sentiment
        insider_vendors = _get_vendor_list('vendor_insider')
        provider_map['insider'] = []
        for vendor in insider_vendors:
            if vendor in INSIDER_PROVIDERS:
                provider_map['insider'].append(INSIDER_PROVIDERS[vendor])
            else:
                self.logger.warning(f"Insider provider '{vendor}' not found in INSIDER_PROVIDERS registry")
        
        # Macro providers (aggregated) - for economic indicators, yield curve, Fed calendar
        # Note: We don't have a vendor_macro setting yet, so we'll default to 'fred'
        provider_map['macro'] = [MACRO_PROVIDERS['fred']] if 'fred' in MACRO_PROVIDERS else []
        
        # Fundamentals details providers (aggregated) - for balance sheet, income stmt, cash flow
        # Use consolidated vendor_fundamentals_details setting
        fund_vendors = _get_vendor_list('vendor_fundamentals_details')
        
        provider_map['fundamentals_details'] = []
        for vendor in fund_vendors:
            if vendor in FUNDAMENTALS_DETAILS_PROVIDERS:
                provider_map['fundamentals_details'].append(FUNDAMENTALS_DETAILS_PROVIDERS[vendor])
            else:
                self.logger.warning(f"Fundamentals details provider '{vendor}' not found in FUNDAMENTALS_DETAILS_PROVIDERS registry")
        
        # Fundamentals overview providers (aggregated) - for company overview, key metrics
        # Use vendor_fundamentals setting (merged with old vendor_fundamentals_overview)
        overview_vendors = _get_vendor_list('vendor_fundamentals')
        
        provider_map['fundamentals_overview'] = []
        for vendor in overview_vendors:
            if vendor in FUNDAMENTALS_OVERVIEW_PROVIDERS:
                provider_map['fundamentals_overview'].append(FUNDAMENTALS_OVERVIEW_PROVIDERS[vendor])
            else:
                self.logger.warning(f"Fundamentals overview provider '{vendor}' not found in FUNDAMENTALS_OVERVIEW_PROVIDERS registry")
        
        # OHLCV providers (fallback) - tries first, then second, etc.
        ohlcv_vendors = _get_vendor_list('vendor_stock_data')
        provider_map['ohlcv'] = []
        for vendor in ohlcv_vendors:
            if vendor in OHLCV_PROVIDERS:
                provider_map['ohlcv'].append(OHLCV_PROVIDERS[vendor])
            else:
                self.logger.warning(f"OHLCV provider '{vendor}' not found in OHLCV_PROVIDERS registry")
        
        # Indicators providers (fallback) - tries first, then second, etc.
        indicator_vendors = _get_vendor_list('vendor_indicators')
        provider_map['indicators'] = []
        for vendor in indicator_vendors:
            if vendor in INDICATORS_PROVIDERS:
                provider_map['indicators'].append(INDICATORS_PROVIDERS[vendor])
            else:
                self.logger.warning(f"Indicators provider '{vendor}' not found in INDICATORS_PROVIDERS registry")
        
        self.logger.debug(f"Built provider_map with {len(provider_map)} categories: {list(provider_map.keys())}")
        return provider_map
    
    def _extract_recommendation_data(self, final_state: Dict, processed_signal: str, symbol: str) -> Dict[str, Any]:
        """Extract recommendation data from TradingAgents analysis results."""
        expert_recommendation = final_state.get('expert_recommendation', {})
        
        if expert_recommendation:
            # Get price_at_date from recommendation
            graph_price = expert_recommendation.get('price_at_date', 0.0)
            
            # Always fetch account price for validation and logging
            account_price = None
            try:
                from ...core.utils import get_account_instance_from_id
                account = get_account_instance_from_id(self.instance.account_id)
                if account:
                    account_price = account.get_instrument_current_price(symbol)
                    if account_price and account_price > 0:
                        self.logger.debug(f"Account price for {symbol}: ${account_price:.2f}")
                    else:
                        self.logger.warning(f"Account returned invalid price for {symbol}: {account_price}")
                else:
                    self.logger.error(f"Could not get account instance for account_id {self.instance.account_id}")
            except Exception as e:
                self.logger.error(f"Error fetching account price for {symbol}: {e}", exc_info=True)
            
            # Determine which price to use and log decision
            if graph_price > 0:
                # Graph provided a price - validate against account price if available
                if account_price and account_price > 0:
                    price_diff_pct = abs(graph_price - account_price) / account_price * 100
                    
                    if price_diff_pct > 10:  # More than 10% difference
                        self.logger.warning(
                            f"⚠️ PRICE DISCREPANCY for {symbol}: "
                            f"Graph=${graph_price:.2f} vs Account=${account_price:.2f} "
                            f"(diff: {price_diff_pct:.1f}%). Using graph price. "
                            f"Data providers: {self.get_setting_with_interface_default('vendor_stock_data', log_warning=False) or 'unknown'}"
                        )
                    else:
                        self.logger.info(f"Price validation OK for {symbol}: Graph=${graph_price:.2f}, Account=${account_price:.2f} (diff: {price_diff_pct:.1f}%)")
                else:
                    self.logger.info(f"Using graph price for {symbol}: ${graph_price:.2f} (account price unavailable for validation)")
                
                price_at_date = graph_price
                price_source = "graph"
            else:
                # Graph price missing or zero - use account price
                if account_price and account_price > 0:
                    price_at_date = account_price
                    price_source = "account_fallback"
                    self.logger.info(f"Using account fallback price for {symbol}: ${price_at_date:.2f} (graph price was missing/zero)")
                else:
                    price_at_date = 0.0
                    price_source = "none"
                    self.logger.error(f"No valid price available for {symbol} - both graph and account failed!")
            
            return {
                'signal': expert_recommendation.get('recommended_action', OrderRecommendation.ERROR),
                'confidence': expert_recommendation.get('confidence', 0.0),
                'expected_profit': expert_recommendation.get('expected_profit_percent', 0.0),
                'details': expert_recommendation.get('details', 'TradingAgents analysis completed'),
                'price_at_date': price_at_date,
                'risk_level': expert_recommendation.get('risk_level', RiskLevel.MEDIUM),
                'time_horizon': expert_recommendation.get('time_horizon', TimeHorizon.SHORT_TERM)
            }
        else:
            # Fallback to processed signal - fetch current price from account
            self.logger.warning(f"No expert_recommendation in graph output for {symbol} - using fallback path")
            price_at_date = 0.0
            price_source = "none"
            
            try:
                from ...core.utils import get_account_instance_from_id
                account = get_account_instance_from_id(self.instance.account_id)
                if account:
                    current_price = account.get_instrument_current_price(symbol)
                    if current_price and current_price > 0:
                        price_at_date = current_price
                        price_source = "account_fallback"
                        self.logger.info(f"Using account price for {symbol}: ${price_at_date:.2f} (fallback path - no graph recommendation)")
                    else:
                        self.logger.error(f"Account returned invalid price for {symbol} in fallback path: {current_price}")
                else:
                    self.logger.error(f"Could not get account instance for account_id {self.instance.account_id} in fallback path")
            except Exception as e:
                self.logger.error(f"Error fetching account price for {symbol} in fallback path: {e}", exc_info=True)
            
            return {
                'signal': processed_signal if processed_signal in ['BUY', 'SELL', 'HOLD'] else OrderRecommendation.ERROR,
                'confidence': 0.0,
                'expected_profit': 0.0,
                'details': f"TradingAgents analysis: {processed_signal}",
                'price_at_date': price_at_date,
                'risk_level': RiskLevel.MEDIUM,
                'time_horizon': TimeHorizon.SHORT_TERM
            }
    
    def _create_expert_recommendation(self, recommendation_data: Dict[str, Any], symbol: str, market_analysis_id: int) -> int:
        """Create ExpertRecommendation record in database."""
        try:
            expert_recommendation = ExpertRecommendation(
                instance_id=self.id,
                symbol=symbol,
                recommended_action=recommendation_data['signal'],
                expected_profit_percent=round(recommendation_data['expected_profit'], 2),
                price_at_date=recommendation_data['price_at_date'],
                details=recommendation_data['details'][:100000] if recommendation_data['details'] else None,
                confidence=round(recommendation_data['confidence'], 3),
                risk_level=recommendation_data['risk_level'],
                time_horizon=recommendation_data['time_horizon'],
                market_analysis_id=market_analysis_id,
                created_at=datetime.now(timezone.utc)
            )
            
            recommendation_id = add_instance(expert_recommendation)
            self.logger.info(f"[SUCCESS] Created ExpertRecommendation (ID: {recommendation_id}) for {symbol}: "
                       f"{recommendation_data['signal']} with {recommendation_data['confidence']:.1f}% confidence")
            return recommendation_id
            
        except Exception as e:
            self.logger.error(f"[ERROR] Failed to create ExpertRecommendation for {symbol}: {e}", exc_info=True)
            raise
    
    def _store_analysis_outputs(self, market_analysis_id: int, symbol: str, 
                               recommendation_data: Dict[str, Any], final_state: Dict, 
                               expert_settings: Dict) -> None:
        """Store detailed analysis outputs in database."""
        session = get_db()
        
        try:
            # Store reasoning
            if recommendation_data.get('details'):
                reasoning_output = AnalysisOutput(
                    market_analysis_id=market_analysis_id,
                    name="Trading Recommendation Reasoning",
                    type="trading_recommendation_reasoning",
                    text=recommendation_data['details']
                )
                session.add(reasoning_output)
            
            # Store full TradingAgents state
            if final_state:
                state_output = AnalysisOutput(
                    market_analysis_id=market_analysis_id,
                    name="TradingAgents Full State",
                    type="tradingagents_full_state",
                    text=json.dumps(final_state, indent=2, default=str)
                )
                session.add(state_output)
            
            # Store analysis summary
            summary_text = self._create_analysis_summary(symbol, recommendation_data, expert_settings)
            summary_output = AnalysisOutput(
                market_analysis_id=market_analysis_id,
                name="Analysis Summary",
                type="tradingagents_analysis_summary",
                text=summary_text
            )
            session.add(summary_output)
            
            session.commit()
            
        except Exception as e:
            session.rollback()
            self.logger.error(f"Failed to store analysis outputs: {e}", exc_info=True)
            raise
        finally:
            session.close()
    
    def _create_analysis_summary(self, symbol: str, recommendation_data: Dict[str, Any], 
                                expert_settings: Dict) -> str:
        """Create formatted analysis summary text."""
        return f"""TradingAgents Analysis Summary for {symbol}:

Signal: {recommendation_data.get('signal', 'UNKNOWN')}
Confidence: {recommendation_data.get('confidence', 0.0):.1f}%
Expected Profit: {recommendation_data.get('expected_profit', 0.0):.2f}%
Risk Level: {recommendation_data.get('risk_level', 'UNKNOWN')}
Time Horizon: {recommendation_data.get('time_horizon', 'UNKNOWN')}
Expert ID: {self.id}

Configuration:
- Deep Think LLM: {expert_settings.get('deep_think_llm') or 'Unknown'}
- Quick Think LLM: {expert_settings.get('quick_think_llm') or 'Unknown'}
- News Lookback: {expert_settings.get('news_lookback_days') or 0} days
- Market History: {expert_settings.get('market_history_days') or 0} days
- Economic Data: {expert_settings.get('economic_data_days') or 0} days
- Social Sentiment: {expert_settings.get('social_sentiment_days') or 0} days
- Timeframe: {expert_settings.get('timeframe', 'Unknown')}
- Debates (New/Existing): {expert_settings.get('debates_new_positions', 0)}/{expert_settings.get('debates_existing_positions', 0)}

Analysis completed at: {self._get_current_timestamp()}"""

    def run_analysis(self, symbol: str, market_analysis: MarketAnalysis) -> None:
        """
        Run TradingAgents analysis for a symbol and create ExpertRecommendation.
        
        Args:
            symbol: Financial instrument symbol to analyze
            market_analysis: MarketAnalysis instance to update with results
        """
        self.logger.info(f"[START] Starting TradingAgents analysis for {symbol} (Analysis ID: {market_analysis.id})")
        
        try:
            # Update status to running
            market_analysis.status = MarketAnalysisStatus.RUNNING
            update_instance(market_analysis)
            
            # Execute TradingAgents analysis
            final_state, processed_signal = self._execute_tradingagents_analysis(symbol, market_analysis.id, market_analysis.subtype)
            
            # Check if analysis encountered an error (expert_recommendation would be empty from summarization error)
            if not final_state.get('expert_recommendation'):
                # Analysis failed - let exception handler mark as FAILED
                raise Exception(f"TradingAgents analysis returned no recommendation for {symbol}")
            
            # Extract recommendation data
            recommendation_data = self._extract_recommendation_data(final_state, processed_signal, symbol)
            
            # Create ExpertRecommendation record
            recommendation_id = self._create_expert_recommendation(recommendation_data, symbol, market_analysis.id)
            
            # Store analysis state and outputs
            self._store_analysis_state(market_analysis, recommendation_data, final_state, processed_signal, recommendation_id)
            self._store_analysis_outputs(market_analysis.id, symbol, recommendation_data, final_state, self.settings)
            
            # Mark analysis as completed
            market_analysis.status = MarketAnalysisStatus.COMPLETED
            # Ensure state is initialized (protect against None from database)
            if market_analysis.state is None:
                market_analysis.state = {}
            market_analysis.state['trading_agent_graph'] = self._clean_state_for_json_storage(final_state)
            # Explicitly mark the state field as modified for SQLAlchemy
            from sqlalchemy.orm import attributes
            attributes.flag_modified(market_analysis, "state")
            update_instance(market_analysis)

            self.logger.info(f"[COMPLETE] TradingAgents analysis completed for {symbol}: "
                       f"{recommendation_data['signal']} ({recommendation_data['confidence']:.1f}% confidence)")
            
            # Trigger Trade Manager to process the recommendation (if automatic trading is enabled)
            self._notify_trade_manager(recommendation_id, symbol)

        except Exception as e:
            # Check if this is a network error that was already retried
            error_str = str(e).lower()
            is_network_error = any(keyword in error_str for keyword in [
                'connection', 'timeout', 'incomplete chunked read', 
                'peer closed', 'remote protocol error', 'network'
            ])
            retry_note = " (Transient network error - LLM retries were attempted)" if is_network_error else ""
            
            self.logger.error(f"[FAILED] TradingAgents analysis failed for {symbol}: {e}{retry_note}", exc_info=True)
            self._handle_analysis_error(market_analysis, symbol, str(e))
            raise
    
    def _build_selected_analysts(self) -> List[str]:
        """Build list of selected analysts based on settings.
        
        Returns:
            List of analyst names (e.g., ["market", "news", "fundamentals"])
        """
        settings_def = self.get_settings_definitions()
        selected_analysts = []
        
        # Check each analyst setting and add to list if enabled
        analyst_mapping = {
            'enable_market_analyst': 'market',
            'enable_social_analyst': 'social',
            'enable_news_analyst': 'news',
            'enable_fundamentals_analyst': 'fundamentals',
            'enable_macro_analyst': 'macro'
        }
        
        for setting_key, analyst_name in analyst_mapping.items():
            # Get setting value, default to True (all enabled by default)
            is_enabled = self.settings.get(setting_key, settings_def[setting_key]['default'])
            if is_enabled:
                selected_analysts.append(analyst_name)
        
        # Ensure at least one analyst is selected
        if not selected_analysts:
            self.logger.warning("No analysts selected! Defaulting to all analysts enabled.")
            selected_analysts = ['market', 'social', 'news', 'fundamentals', 'macro']
        
        self.logger.info(f"Selected analysts: {', '.join(selected_analysts)}")
        return selected_analysts
    
    def _execute_tradingagents_analysis(self, symbol: str, market_analysis_id: int, subtype: str) -> tuple:
        """Execute the core TradingAgents analysis."""
        # Create configuration
        config = self._create_tradingagents_config(subtype)
        
        # Build provider_map for new toolkit
        provider_map = self._build_provider_map()
        
        # Build provider_args for OpenAI and Alpha Vantage providers
        settings_def = self.get_settings_definitions()
        
        # Get dataprovider_websearch_model with backward compatibility for old setting name
        websearch_model = self.settings.get('dataprovider_websearch_model')
        if not websearch_model:
            # Backward compatibility: check for old setting name
            websearch_model = self.settings.get('openai_provider_model')
        if not websearch_model:
            # Use default value
            websearch_model = settings_def['dataprovider_websearch_model']['default']
        
        # Get settings with proper None-aware fallback to defaults
        # (settings can be None if not configured in database)
        alpha_vantage_source = self.settings.get('alpha_vantage_source') or settings_def['alpha_vantage_source']['default']
        economic_data_days = int(self.settings.get('economic_data_days') or settings_def['economic_data_days']['default'])
        news_lookback_days = int(self.settings.get('news_lookback_days') or settings_def['news_lookback_days']['default'])
        social_sentiment_days = int(self.settings.get('social_sentiment_days') or settings_def['social_sentiment_days']['default'])
        provider_args = {
            'websearch_model': websearch_model,
            'alpha_vantage_source': alpha_vantage_source,
            'economic_data_days': economic_data_days,  # Pass expert setting for default lookback_days
            'news_lookback_days': news_lookback_days,  # Pass expert setting for news tools
            'social_sentiment_days': social_sentiment_days  # Pass expert setting for social media tools
        }
        
        # Log provider_map configuration
        self.logger.info(f"=== TradingAgents Provider Configuration ===")
        for category, providers in provider_map.items():
            provider_names = [p.__name__ for p in providers] if providers else ["None"]
            self.logger.info(f"  {category}: {', '.join(provider_names)}")
        self.logger.info(f"  provider_args: {provider_args}")
        self.logger.info(f"============================================")
        
        # Initialize TradingAgents graph
        # Get debug mode from settings (defaults to True for detailed logging)
        debug_mode = bool(self.get_setting_with_interface_default('debug_mode'))
        
        # Build selected_analysts list based on settings
        selected_analysts = self._build_selected_analysts()
        
        ta_graph = TradingAgentsGraph(
            selected_analysts=selected_analysts,
            debug=debug_mode,
            config=config,
            market_analysis_id=market_analysis_id,
            expert_instance_id=self.id,  # Pass expert instance ID for persistent memory
            provider_map=provider_map,  # Pass provider_map for new toolkit
            provider_args=provider_args  # Pass provider_args for provider instantiation
        )
        
        # Run analysis
        trade_date = datetime.now().strftime("%Y-%m-%d")
        self.logger.debug(f"Running TradingAgents propagation for {symbol} on {trade_date}")
        
        final_state, processed_signal = ta_graph.propagate(symbol, trade_date)
        self.logger.debug(f"TradingAgents propagation completed for {symbol}")
        
        return final_state, processed_signal
    
    def _store_analysis_state(self, market_analysis: MarketAnalysis, recommendation_data: Dict[str, Any], 
                             final_state: Dict, processed_signal: str, recommendation_id: int) -> None:
        """Store analysis results in MarketAnalysis state using proper state merging."""
        # Import database update function
        
        prediction_result = {
            'instrument': market_analysis.symbol,
            'signal': recommendation_data['signal'],
            'confidence': round(recommendation_data['confidence'], 3),
            'expected_profit_percent': round(recommendation_data['expected_profit'], 2),
            'price_target': recommendation_data['price_at_date'] if recommendation_data['price_at_date'] > 0 else None,
            'reasoning': recommendation_data['details'][:500] if recommendation_data['details'] else 'Analysis completed',
            'timestamp': self._get_current_timestamp(),
            'expert_id': self.id,
            'expert_type': 'TradingAgents',
            'market_analysis_id': market_analysis.id,
            'expert_recommendation_id': recommendation_id,
            'analysis_method': 'tradingagents_full'
        }
        
        # Use proper state merging under 'trading_agent_graph' key
        trading_agent_state = {
            'prediction_result': prediction_result,
            'analysis_timestamp': self._get_current_timestamp(),
            'expert_settings': self.settings,
            'final_state': self._clean_state_for_json_storage(final_state),
            'processed_signal': processed_signal,
            'tradingagents_mode': True
        }
        
        # Update using the db_storage function which properly merges state
        update_market_analysis_status(
            analysis_id=market_analysis.id,
            status=market_analysis.status,
            state=trading_agent_state
        )
    
    def _handle_analysis_error(self, market_analysis: MarketAnalysis, symbol: str, error_message: str) -> None:
        """Handle analysis errors by storing error state and creating error output."""
        # Store error in market analysis with ID for traceability
        market_analysis.state = {
            'error': error_message,
            'error_timestamp': self._get_current_timestamp(),
            'analysis_failed': True,
            'analysis_method': 'tradingagents_full',
            'analysis_id': market_analysis.id
        }
        market_analysis.status = MarketAnalysisStatus.FAILED
        update_instance(market_analysis)
        
        # Create error output with analysis ID for traceability
        try:
            session = get_db()
            error_output = AnalysisOutput(
                market_analysis_id=market_analysis.id,
                name="Analysis Error",
                type="error",
                text=f"TradingAgents analysis failed (ID {market_analysis.id}) for {symbol}: {error_message}"
            )
            session.add(error_output)
            session.commit()
            session.close()
        except Exception as db_error:
            self.logger.error(f"Failed to store error output: {db_error}", exc_info=True)
    
    def render_market_analysis(self, market_analysis: MarketAnalysis) -> None:
        """
        Render comprehensive TradingAgents market analysis results using the TradingAgentsUI class.
        
        Args:
            market_analysis (MarketAnalysis): The market analysis instance to render.
        """
        try:
            # Import and use the dedicated UI class
            from .TradingAgentsUI import TradingAgentsUI
            
            # Create UI instance and render directly
            trading_ui = TradingAgentsUI(market_analysis)
            trading_ui.render()
            
        except Exception as e:
            self.logger.error(f"Error rendering market analysis {market_analysis.id}: {e}", exc_info=True)
            # Fallback to error display
            from nicegui import ui
            with ui.card().classes('w-full p-8 text-center'):
                ui.icon('error', size='3rem', color='negative').classes('mb-4')
                ui.label('Rendering Error').classes('text-h5 text-negative')
                ui.label(f'Failed to render analysis: {str(e)}').classes('text-grey-7')
    
    def _render_in_progress_analysis(self, market_analysis: MarketAnalysis) -> str:
        """Render analysis in progress state with partial results and running tabs."""
        try:
            # Load analysis outputs for this analysis (even if still running)
            from ...core.db import get_db
            from sqlmodel import select
            
            session = get_db()
            statement = select(AnalysisOutput).where(AnalysisOutput.market_analysis_id == market_analysis.id).order_by(AnalysisOutput.created_at)
            analysis_outputs = session.exec(statement).all()
            session.close()
            
            # Get state data (might have partial data)
            state = market_analysis.state if market_analysis.state else {}
            trading_state = state.get('trading_agent_graph', {}) if isinstance(state, dict) else {}
            
            # Build in-progress HTML content with tabs showing partial results
            return self._build_in_progress_html_tabs(market_analysis, trading_state, analysis_outputs)
            
        except Exception as e:
            self.logger.error(f"Error rendering in-progress analysis: {e}", exc_info=True)
            return self._render_basic_in_progress()
    
    def _render_basic_in_progress(self) -> str:
        """Render basic in-progress message as fallback."""
        return """⏳ **Analysis in Progress**

The TradingAgents multi-agent analysis is currently running. This includes:
- News sentiment analysis
- Technical indicator analysis  
- Fundamental analysis
- Risk assessment
- Multi-agent debate and consensus

Please check back in a few minutes for results."""
    
    def _render_cancelled_analysis(self, market_analysis: MarketAnalysis) -> str:
        """Render cancelled analysis state."""
        return "❌ **Analysis Cancelled**\n\nThe TradingAgents analysis was cancelled before completion."
    
    def _render_failed_analysis(self, market_analysis: MarketAnalysis) -> str:
        """Render failed analysis state."""
        summary = "⚠️ **TradingAgents Analysis Failed**\n\n"
        if market_analysis.state and isinstance(market_analysis.state, dict):
            error_info = market_analysis.state.get('error', 'Unknown error occurred during analysis.')
            summary += f"**Error Details:** {error_info}\n\n"
        
        summary += "The multi-agent analysis system encountered an error during execution. Please try running the analysis again."
        return summary
    
    def _render_basic_completion(self, market_analysis: MarketAnalysis) -> str:
        """Render basic completion without detailed state."""
        return "✅ **Analysis Completed**\n\nTradingAgents analysis completed successfully but no detailed results are available."
    
    def _render_completed_analysis_comprehensive(self, market_analysis: MarketAnalysis) -> str:
        """Render comprehensive completed analysis with all details, tabs, and interactive content."""
        try:
            # Load analysis outputs for this analysis
            from ...core.db import get_db
            from sqlmodel import select
            
            session = get_db()
            statement = select(AnalysisOutput).where(AnalysisOutput.market_analysis_id == market_analysis.id).order_by(AnalysisOutput.created_at)
            analysis_outputs = session.exec(statement).all()
            session.close()
            
            # Get state data
            state = market_analysis.state if market_analysis.state else {}
            trading_state = state.get('trading_agent_graph', {}) if isinstance(state, dict) else {}
            
            # Build comprehensive HTML content with tabs
            html_content = self._build_analysis_html_tabs(market_analysis, trading_state, analysis_outputs)
            
            return html_content
            
        except Exception as e:
            self.logger.error(f"Error rendering comprehensive analysis: {e}", exc_info=True)
            return self._render_fallback_analysis(market_analysis)
    
    def _build_analysis_html_tabs(self, market_analysis: MarketAnalysis, trading_state: dict, analysis_outputs: list) -> str:
        """Build HTML content with tabs for comprehensive analysis display."""
        # Extract key data
        recommendation_data = self._extract_recommendation_from_state(trading_state)
        agent_communications = self._extract_llm_outputs_from_state(trading_state)
        grouped_outputs = self._group_analysis_outputs(analysis_outputs)
        
        # Build comprehensive markdown with collapsible sections
        content = self._build_summary_section(market_analysis, recommendation_data, trading_state)
        
        # Agent Communications Section  
        if agent_communications:
            content += self._build_agent_communications_section(agent_communications)
        
        # Tool Outputs Section
        if grouped_outputs:
            content += self._build_tool_outputs_section(grouped_outputs)
        
        # Individual Agent Sections
        agent_names = set()
        if agent_communications:
            agent_names.update(agent_communications.keys())
        if grouped_outputs:
            agent_names.update(grouped_outputs.keys())
        
        for agent_name in sorted(agent_names):
            content += self._build_individual_agent_section(
                agent_name, 
                grouped_outputs.get(agent_name, []), 
                agent_communications.get(agent_name, [])
            )
        
        return content
    
    def _build_summary_section(self, market_analysis: MarketAnalysis, recommendation_data: dict, trading_state: dict) -> str:
        """Build the analysis summary section."""
        content = '# ✅ TradingAgents Analysis Completed\n\n'
        
        # Recommendation section
        if recommendation_data:
            content += '## 🎯 Final Recommendation\n\n'
            content += f"**Action:** {recommendation_data.get('action', 'N/A')}  \n"
            content += f"**Confidence:** {recommendation_data.get('confidence', 'N/A')}  \n"
            if recommendation_data.get('reasoning'):
                content += f"**Reasoning:** {recommendation_data['reasoning']}  \n\n"
        
        # Agent summary
        agent_summaries = self._extract_agent_summaries(trading_state)
        if agent_summaries:
            content += '## 🤖 Agent Analysis Summary\n\n'
            for agent_name, summary in agent_summaries.items():
                content += f"- **{agent_name}:** {summary}\n"
            content += '\n'
        
        # Metadata
        content += '## 📊 Analysis Metadata\n\n'
        content += f"**Symbol:** {market_analysis.symbol}  \n"
        content += f"**Analysis Method:** TradingAgents Multi-Agent System  \n"
        content += f"**Completed:** {market_analysis.created_at.strftime('%Y-%m-%d %H:%M:%S') if market_analysis.created_at else 'Unknown'}  \n"
        content += f"**Expert ID:** {self.id}  \n\n"
        
        content += '*Detailed agent communications and tool outputs are shown below.*\n\n'
        
        return content
    
    def _build_agent_communications_section(self, agent_communications: dict) -> str:
        """Build agent communications section with collapsible details."""
        content = '## 💬 Agent Communications\n\n'
        
        for agent_name, messages in agent_communications.items():
            content += f'<details>\n<summary><strong>{agent_name} ({len(messages)} messages)</strong></summary>\n\n'
            
            for i, message in enumerate(messages):
                message_type = message.get('type', 'unknown')
                msg_content = message.get('content', '')
                
                # Format message based on type
                icon = '🤖' if message_type == 'ai' else '👤' if message_type == 'human' else '⚙️'
                content += f'### {icon} {message_type.title()} Message {i+1}\n\n'
                
                if isinstance(msg_content, str):
                    content += f'```\n{msg_content}\n```\n\n'
                else:
                    content += f'```json\n{json.dumps(msg_content, indent=2)}\n```\n\n'
            
            content += '</details>\n\n'
        
        return content
    
    def _build_tool_outputs_section(self, grouped_outputs: dict) -> str:
        """Build tool outputs section with collapsible details."""
        content = '## 🔧 Tool Execution Outputs\n\n'
        
        for agent_name, outputs in grouped_outputs.items():
            content += f'<details>\n<summary><strong>{agent_name} Tools ({len(outputs)} outputs)</strong></summary>\n\n'
            
            for output in outputs:
                tool_name = output.name or "Unknown Tool"
                timestamp = output.created_at.strftime("%H:%M:%S") if output.created_at else "Unknown time"
                
                content += f'### 🔠 {tool_name} - {timestamp}\n\n'
                
                # Tool parameters
                if hasattr(output, 'tool_parameters') and output.tool_parameters:
                    content += '**Parameters:**\n\n'
                    try:
                        if isinstance(output.tool_parameters, str):
                            params = json.loads(output.tool_parameters)
                        else:
                            params = output.tool_parameters
                        content += f'```json\n{json.dumps(params, indent=2)}\n```\n\n'
                    except:
                        content += f'```\n{str(output.tool_parameters)}\n```\n\n'
                
                # Tool output
                content += '**Output:**\n\n'
                if output.text:
                    try:
                        # Try to format as JSON
                        parsed_json = json.loads(output.text)
                        formatted_output = json.dumps(parsed_json, indent=2, ensure_ascii=False)
                        content += f'```json\n{formatted_output}\n```\n\n'
                    except:
                        # Use as plain text
                        content += f'```\n{output.text}\n```\n\n'
                elif output.blob:
                    content += f'*Binary data ({len(output.blob)} bytes)*\n\n'
                else:
                    content += '*No output content*\n\n'
            
            content += '</details>\n\n'
        
        return content
    
    def _build_individual_agent_section(self, agent_name: str, tool_outputs: list, llm_outputs: list) -> str:
        """Build individual agent section."""
        if not tool_outputs and not llm_outputs:
            return ''
        
        content = f'## 🤖 {agent_name} - Detailed View\n\n'
        
        # LLM Communications
        if llm_outputs:
            content += f'### 💬 {agent_name} Communications\n\n'
            for i, message in enumerate(llm_outputs):
                message_type = message.get('type', 'unknown')
                msg_content = message.get('content', '')
                icon = '🤖' if message_type == 'ai' else '👤' if message_type == 'human' else '⚙️'
                
                content += f'<details>\n<summary><strong>{icon} {message_type.title()} Message {i+1}</strong></summary>\n\n'
                
                if isinstance(msg_content, str):
                    content += f'```\n{msg_content}\n```\n\n'
                else:
                    content += f'```json\n{json.dumps(msg_content, indent=2)}\n```\n\n'
                
                content += '</details>\n\n'
        
        # Tool Outputs
        if tool_outputs:
            content += f'### 🔧 {agent_name} Tool Outputs\n\n'
            for output in tool_outputs:
                tool_name = output.name or "Unknown Tool"
                timestamp = output.created_at.strftime("%H:%M:%S") if output.created_at else "Unknown time"
                
                content += f'<details>\n<summary><strong>🔠 {tool_name} - {timestamp}</strong></summary>\n\n'
                
                if output.text:
                    try:
                        parsed_json = json.loads(output.text)
                        formatted_output = json.dumps(parsed_json, indent=2, ensure_ascii=False)
                        content += f'```json\n{formatted_output}\n```\n\n'
                    except:
                        content += f'```\n{output.text}\n```\n\n'
                elif output.blob:
                    content += f'*Binary data ({len(output.blob)} bytes)*\n\n'
                else:
                    content += '*No output content*\n\n'
                
                content += '</details>\n\n'
        
        return content
    
    def _build_in_progress_html_tabs(self, market_analysis: MarketAnalysis, trading_state: dict, analysis_outputs: list) -> str:
        """Build HTML content with tabs showing in-progress analysis and partial results."""
        # Extract available data
        agent_communications = self._extract_llm_outputs_from_state(trading_state)
        grouped_outputs = self._group_analysis_outputs(analysis_outputs)
        
        # Build in-progress content with status indicators
        content = f'# ⏳ TradingAgents Analysis in Progress - {market_analysis.symbol}\n\n'
        
        # Progress summary
        content += '## 📊 Analysis Progress\n\n'
        content += 'The multi-agent analysis is currently running. Below you can see partial results as they become available:\n\n'
        
        # Show completed and running sections
        sections_status = self._determine_sections_status(trading_state, grouped_outputs, agent_communications)
        for section_name, status in sections_status.items():
            icon = '✅' if status == 'completed' else '⏳' if status == 'running' else '⌛'
            content += f"- {icon} **{section_name}:** {status.title()}\n"
        content += '\n'
        
        # Show any available recommendation (even if partial)
        recommendation_data = self._extract_recommendation_from_state(trading_state)
        if recommendation_data:
            content += '## 🎯 Preliminary Recommendation\n\n'
            content += f"**Action:** {recommendation_data.get('action', 'Analyzing...')}  \n"
            content += f"**Confidence:** {recommendation_data.get('confidence', 'Calculating...')}  \n"
            if recommendation_data.get('reasoning'):
                content += f"**Reasoning:** {recommendation_data['reasoning']}  \n\n"
        else:
            content += '## ⏳ Final Recommendation\n\n'
            content += '*Final recommendation will appear here once analysis is complete...*\n\n'
        
        # Agent Communications Section (with running indicator if empty)
        if agent_communications:
            content += '## 💬 Agent Communications\n\n'
            for agent_name, messages in agent_communications.items():
                content += f'<details>\n<summary><strong>✅ {agent_name} ({len(messages)} messages completed)</strong></summary>\n\n'
                
                for i, message in enumerate(messages):
                    message_type = message.get('type', 'unknown')
                    msg_content = message.get('content', '')
                    
                    icon = '🤖' if message_type == 'ai' else '👤' if message_type == 'human' else '⚙️'
                    content += f'### {icon} {message_type.title()} Message {i+1}\n\n'
                    
                    if isinstance(msg_content, str):
                        content += f'```\n{msg_content}\n```\n\n'
                    else:
                        content += f'```json\n{json.dumps(msg_content, indent=2)}\n```\n\n'
                
                content += '</details>\n\n'
        else:
            content += '## ⏳ Agent Communications\n\n'
            content += '*Agent communications will appear here as the analysis progresses...*\n\n'
        
        # Tool Outputs Section (with running indicators)
        if grouped_outputs:
            content += '## 🔧 Tool Execution Outputs\n\n'
            for agent_name, outputs in grouped_outputs.items():
                content += f'<details>\n<summary><strong>✅ {agent_name} Tools ({len(outputs)} outputs completed)</strong></summary>\n\n'
                
                for output in outputs:
                    tool_name = output.name or "Unknown Tool"
                    timestamp = output.created_at.strftime("%H:%M:%S") if output.created_at else "Unknown time"
                    
                    content += f'### 🔄 {tool_name} - {timestamp}\n\n'
                    
                    # Tool parameters
                    if hasattr(output, 'tool_parameters') and output.tool_parameters:
                        content += '**Parameters:**\n\n'
                        try:
                            if isinstance(output.tool_parameters, str):
                                params = json.loads(output.tool_parameters)
                            else:
                                params = output.tool_parameters
                            content += f'```json\n{json.dumps(params, indent=2)}\n```\n\n'
                        except:
                            content += f'```\n{str(output.tool_parameters)}\n```\n\n'
                    
                    # Tool output
                    content += '**Output:**\n\n'
                    if output.text:
                        try:
                            parsed_json = json.loads(output.text)
                            formatted_output = json.dumps(parsed_json, indent=2, ensure_ascii=False)
                            content += f'```json\n{formatted_output}\n```\n\n'
                        except:
                            content += f'```\n{output.text}\n```\n\n'
                    elif output.blob:
                        content += f'*Binary data ({len(output.blob)} bytes)*\n\n'
                    else:
                        content += '*No output content*\n\n'
                
                content += '</details>\n\n'
        else:
            content += '## ⏳ Tool Execution Outputs\n\n'
            content += '*Tool execution results will appear here as agents complete their analysis...*\n\n'
        
        # Show expected agents that are still running
        expected_agents = ['News Agent', 'Technical Agent', 'Fundamental Agent', 'Risk Agent', 'Portfolio Agent']
        running_agents = set(expected_agents) - set(agent_communications.keys()) - set(grouped_outputs.keys())
        
        if running_agents:
            content += '## ⏳ Running Agents\n\n'
            content += 'The following agents are currently analyzing:\n\n'
            for agent in sorted(running_agents):
                content += f'- ⏳ **{agent}:** Analysis in progress...\n'
            content += '\n'
        
        # Refresh notice
        content += '---\n\n'
        content += '**💡 Tip:** This page will automatically update as the analysis progresses. '
        content += 'Refresh the page to see the latest results.\n\n'
        
        return content
    
    def _determine_sections_status(self, trading_state: dict, grouped_outputs: dict, agent_communications: dict) -> dict:
        """Determine the status of different analysis sections."""
        status = {}
        
        # Check recommendation status
        if self._extract_recommendation_from_state(trading_state):
            status['Final Recommendation'] = 'completed'
        else:
            status['Final Recommendation'] = 'running'
        
        # Check agent communications
        if agent_communications:
            status['Agent Communications'] = 'completed'
        else:
            status['Agent Communications'] = 'running'
        
        # Check tool outputs
        if grouped_outputs:
            status['Tool Execution'] = 'completed'
        else:
            status['Tool Execution'] = 'running'
        
        # Check individual agents
        expected_agents = ['News Analysis', 'Technical Analysis', 'Fundamental Analysis', 'Risk Assessment', 'Portfolio Analysis']
        for agent in expected_agents:
            if any(agent.lower().replace(' ', '_') in key.lower() for key in trading_state.keys()):
                status[agent] = 'completed'
            else:
                status[agent] = 'running'
        
        return status
    
    def _render_fallback_analysis(self, market_analysis: MarketAnalysis) -> str:
        """Render a fallback analysis when comprehensive rendering fails."""
        summary = "✅ **TradingAgents Analysis Completed**\n\n"
        
        if market_analysis.state and isinstance(market_analysis.state, dict):
            trading_state = market_analysis.state.get('trading_agent_graph', {})
            if trading_state:
                # Try to extract basic recommendation
                recommendation_data = self._extract_recommendation_from_state(trading_state)
                if recommendation_data:
                    summary += "## 🎯 Final Recommendation\n\n"
                    summary += f"**Action:** {recommendation_data.get('action', 'N/A')}  \n"
                    summary += f"**Confidence:** {recommendation_data.get('confidence', 'N/A')}  \n"
                    if recommendation_data.get('reasoning'):
                        summary += f"**Reasoning:** {recommendation_data['reasoning'][:200]}...  \n\n"
        
        summary += "*Detailed analysis results are available in the database.*\n"
        return summary
    
    def _is_status(self, market_analysis: MarketAnalysis, *statuses: MarketAnalysisStatus) -> bool:
        """Check if market analysis status matches any of the provided statuses (case-insensitive)."""
        if not market_analysis.status:
            return False
        
        current_status = market_analysis.status
        return current_status in statuses
    
    def _format_agent_name_from_key(self, key: str) -> str:
        """Format agent name from state key for display."""
        # Convert keys like "news_agent" or "newsAgent" to "News Agent"
        if '_' in key:
            parts = key.split('_')
            return ' '.join(word.title() for word in parts)
        
        # Handle camelCase
        import re
        camel_case_parts = re.findall(r'[A-Z][a-z]*', key)
        if camel_case_parts:
            return ' '.join(camel_case_parts)
        
        return key.title()
    
    def _extract_recommendation_from_state(self, trading_state: dict) -> dict:
        """Extract recommendation data from trading state."""
        recommendation_data = {}
        
        # Look for prediction_result first
        if 'prediction_result' in trading_state:
            pred_result = trading_state['prediction_result']
            if isinstance(pred_result, dict):
                recommendation_data['action'] = pred_result.get('signal', 'N/A')
                recommendation_data['confidence'] = f"{pred_result.get('confidence', 0):.1f}%" if pred_result.get('confidence') else 'N/A'
                recommendation_data['reasoning'] = pred_result.get('reasoning', '')
                return recommendation_data
        
        # Look for other recommendation formats
        for key in ['final_recommendation', 'recommendation', 'expert_recommendation']:
            if key in trading_state:
                rec = trading_state[key]
                if isinstance(rec, dict):
                    recommendation_data['action'] = rec.get('action', rec.get('signal', 'N/A'))
                    confidence = rec.get('confidence', 0)
                    if isinstance(confidence, (int, float)):
                        recommendation_data['confidence'] = f"{confidence:.1f}%"
                    else:
                        recommendation_data['confidence'] = str(confidence)
                    recommendation_data['reasoning'] = rec.get('reasoning', rec.get('details', ''))
                    return recommendation_data
        
        return recommendation_data
    
    def _extract_llm_outputs_from_state(self, trading_state: dict) -> dict:
        """Extract LLM outputs from trading state."""
        llm_outputs = {}
        
        for key, value in trading_state.items():
            if isinstance(value, dict) and 'messages' in value:
                messages = value['messages']
                if isinstance(messages, list) and messages:
                    agent_name = self._format_agent_name_from_key(key)
                    llm_outputs[agent_name] = messages
        
        return llm_outputs
    
    def _group_analysis_outputs(self, analysis_outputs: list) -> dict:
        """Group analysis outputs by agent name."""
        grouped = {}
        for output in analysis_outputs:
            agent_name = self._extract_agent_name_from_output(output)
            if agent_name not in grouped:
                grouped[agent_name] = []
            grouped[agent_name].append(output)
        return grouped
    
    def _extract_agent_name_from_output(self, output) -> str:
        """Extract agent name from analysis output name."""
        name = output.name or "Unknown"
        
        # Handle patterns like "agent_name_tool_name" or "AgentName: tool_name"
        if '_' in name:
            parts = name.split('_')
            if len(parts) >= 2:
                return parts[0].title()
        
        if ':' in name:
            return name.split(':')[0].strip()
        
        # Handle patterns like "NewsAgentTool" -> "News Agent"
        import re
        camel_case_pattern = re.findall(r'[A-Z][a-z]*', name)
        if camel_case_pattern and len(camel_case_pattern) >= 2:
            return ' '.join(camel_case_pattern[:-1])  # Exclude "Tool" suffix
        
        return name
    
    def _extract_agent_summaries(self, trading_state: dict) -> dict:
        """Extract agent summaries from trading state."""
        agent_summaries = {}
        
        for key, value in trading_state.items():
            if isinstance(value, dict) and 'messages' in value:
                messages = value['messages']
                if isinstance(messages, list) and messages:
                    agent_name = self._format_agent_name_from_key(key)
                    
                    # Get the last AI message as summary
                    for message in reversed(messages):
                        if isinstance(message, dict) and message.get('type') == 'ai':
                            content = message.get('content', '')
                            if content:
                                # Truncate long content
                                summary = str(content)[:150]
                                if len(str(content)) > 150:
                                    summary += "..."
                                agent_summaries[agent_name] = summary
                                break
        
        return agent_summaries

    def _clean_state_for_json_storage(self, state: Dict[str, Any]) -> Dict[str, Any]:
        """Clean state data to make it JSON serializable by removing non-serializable objects."""
        cleaned_state = {}
        
        for key, value in state.items():
            if key == 'messages':
                # Store message summary instead of full HumanMessage objects
                if isinstance(value, list):
                    cleaned_state['messages_summary'] = {
                        'count': len(value),
                        'types': [msg.__class__.__name__ for msg in value if hasattr(msg, '__class__')]
                    }
                else:
                    cleaned_state['messages_summary'] = {'count': 0, 'types': []}
            elif key in ['investment_debate_state', 'risk_debate_state']:
                # Keep debate states as they are crucial for UI display
                if isinstance(value, dict):
                    cleaned_value = {}
                    for debate_key, debate_value in value.items():
                        # Ensure all values are JSON serializable
                        if isinstance(debate_value, (str, int, float, bool, type(None))):
                            cleaned_value[debate_key] = debate_value
                        elif isinstance(debate_value, list):
                            # Clean list items
                            cleaned_list = []
                            for item in debate_value:
                                if isinstance(item, (str, int, float, bool, type(None))):
                                    cleaned_list.append(item)
                                else:
                                    cleaned_list.append(str(item))
                            cleaned_value[debate_key] = cleaned_list
                        else:
                            cleaned_value[debate_key] = str(debate_value)
                    cleaned_state[key] = cleaned_value
                else:
                    cleaned_state[key] = str(value) if value is not None else ""
            elif isinstance(value, (str, int, float, bool, type(None))):
                # Keep simple types as-is
                cleaned_state[key] = value
            elif isinstance(value, (dict, list)):
                # Try to keep dictionaries and lists, but convert complex objects to strings
                try:
                    json.dumps(value)  # Test if it's JSON serializable
                    cleaned_state[key] = value
                except (TypeError, ValueError):
                    # If not serializable, convert to string representation
                    cleaned_state[key] = str(value)
            else:
                # Convert everything else to string
                cleaned_state[key] = str(value)
        
        return cleaned_state
    
    def _notify_trade_manager(self, recommendation_id: int, symbol: str) -> None:
        """
        Notify the Trade Manager about a new recommendation and trigger order creation if enabled.
        
        Args:
            recommendation_id: The ID of the created ExpertRecommendation
            symbol: The trading symbol
        """
        try:
            # Check if automatic trade opening is enabled for this expert
            allow_automated_trade_opening = self.get_setting_with_interface_default('allow_automated_trade_opening')
            # Also check legacy setting for backward compatibility
            legacy_automatic_trading = self.settings.get('automatic_trading', False)  # Legacy setting, keep hardcoded default
            
            if not allow_automated_trade_opening and not legacy_automatic_trading:
                self.logger.debug(f"[TRADE MANAGER] Automatic trade opening disabled for expert {self.id}, skipping order creation for {symbol}")
                return
            
            # Get the recommendation from database
            from ...core.models import ExpertRecommendation
            from ...core.db import get_instance
            from ...core.TradeManager import get_trade_manager
            
            recommendation = get_instance(ExpertRecommendation, recommendation_id)
            if not recommendation:
                self.logger.error(f"[TRADE MANAGER] ExpertRecommendation {recommendation_id} not found")
                return
            
            # Skip HOLD recommendations as they don't require orders
            if recommendation.recommended_action == OrderRecommendation.HOLD:
                self.logger.debug(f"[TRADE MANAGER] HOLD recommendation for {symbol}, no order needed")
                return
            
            # Get the trade manager and process the recommendation
            trade_manager = get_trade_manager()
            placed_order = trade_manager.process_recommendation(recommendation)
            
            if placed_order:
                self.logger.info(f"[TRADE MANAGER] Successfully created order {placed_order.id} for {symbol} "
                           f"({recommendation.recommended_action.value}) based on recommendation {recommendation_id}")
            else:
                self.logger.info(f"[TRADE MANAGER] No order created for {symbol} recommendation {recommendation_id} "
                           f"(may be filtered by rules or permissions)")
                
        except Exception as e:
            self.logger.error(f"[TRADE MANAGER] Error notifying trade manager for recommendation {recommendation_id}, symbol {symbol}: {e}", exc_info=True)
    
    @classmethod
    def get_expert_actions(cls) -> List[Dict[str, Any]]:
        """Define expert-specific actions for TradingAgents."""
        return [
            {
                "name": "clear_memory",
                "label": "Clear Memory",
                "description": "Delete stored memory collections for this expert instance",
                "icon": "delete_forever",
                "callback": "clear_memory_action"
            }
        ]
    
    def clear_memory_action(self) -> Dict[str, Any]:
        """
        Clear memory collections for this expert instance.
        
        Returns:
            Dict with status and message about the operation
        """
        try:
            import chromadb
            import os
            from ...config import CACHE_FOLDER
            
            # Get the persist directory for this expert (check all symbol subdirectories)
            expert_base_dir = os.path.join(CACHE_FOLDER, "chromadb", f"expert_{self.id}")
            
            if not os.path.exists(expert_base_dir):
                return {
                    "status": "info",
                    "message": "No memory collections found for this expert."
                }
            
            # Collect all collections from all symbol subdirectories
            collection_info = []
            
            # Check base directory (old format without symbol subdirs)
            if os.path.isfile(os.path.join(expert_base_dir, "chroma.sqlite3")):
                try:
                    client = chromadb.PersistentClient(path=expert_base_dir)
                    collections = client.list_collections()
                    for collection in collections:
                        count = collection.count()
                        collection_info.append({
                            "name": collection.name,
                            "count": count,
                            "path": expert_base_dir
                        })
                except Exception as e:
                    self.logger.debug(f"Could not read collections from {expert_base_dir}: {e}")
            
            # Check symbol subdirectories
            for symbol_dir in os.listdir(expert_base_dir):
                symbol_path = os.path.join(expert_base_dir, symbol_dir)
                if os.path.isdir(symbol_path):
                    try:
                        client = chromadb.PersistentClient(path=symbol_path)
                        collections = client.list_collections()
                        for collection in collections:
                            count = collection.count()
                            collection_info.append({
                                "name": f"{symbol_dir}/{collection.name}",
                                "count": count,
                                "path": symbol_path
                            })
                    except Exception as e:
                        self.logger.debug(f"Could not read collections from {symbol_path}: {e}")
            
            if not collection_info:
                return {
                    "status": "info",
                    "message": "No memory collections found for this expert."
                }
            
            # Return list of collections for user to confirm
            total_count = sum(c["count"] for c in collection_info)
            return {
                "status": "confirm",
                "message": f"Found {len(collection_info)} memory collection(s) with {total_count} total memories. This action will permanently delete all stored memories.",
                "collections": collection_info,
                "action": "confirm_clear_memory"
            }
            
        except Exception as e:
            self.logger.error(f"Error listing memory collections for expert {self.id}: {e}", exc_info=True)
            return {
                "status": "error",
                "message": f"Error accessing memory collections: {str(e)}"
            }
    
    def confirm_clear_memory(self, collection_names: Optional[List[str]] = None) -> Dict[str, Any]:
        """
        Confirm and execute memory clearing for selected collections.
        
        Args:
            collection_names: List of collection names to delete. If None, deletes all.
        
        Returns:
            Dict with status and message about the operation
        """
        try:
            import chromadb
            import os
            import shutil
            from ...config import CACHE_FOLDER
            
            # Get the persist directory for this expert (base directory with symbol subdirs)
            expert_base_dir = os.path.join(CACHE_FOLDER, "chromadb", f"expert_{self.id}")
            
            if not os.path.exists(expert_base_dir):
                return {
                    "status": "info",
                    "message": "No memory collections found."
                }
            
            deleted_count = 0
            errors = []
            
            # Delete from base directory (old format)
            if os.path.isfile(os.path.join(expert_base_dir, "chroma.sqlite3")):
                try:
                    client = chromadb.PersistentClient(path=expert_base_dir)
                    all_collections = client.list_collections()
                    
                    for collection in all_collections:
                        if collection_names and collection.name not in collection_names:
                            continue
                        try:
                            client.delete_collection(collection.name)
                            deleted_count += 1
                            self.logger.info(f"Deleted memory collection '{collection.name}' for expert {self.id}")
                        except Exception as e:
                            errors.append(f"Failed to delete collection '{collection.name}': {str(e)}")
                except Exception as e:
                    self.logger.debug(f"Could not access collections in {expert_base_dir}: {e}")
            
            # Delete from symbol subdirectories
            for symbol_dir in os.listdir(expert_base_dir):
                symbol_path = os.path.join(expert_base_dir, symbol_dir)
                if os.path.isdir(symbol_path):
                    try:
                        client = chromadb.PersistentClient(path=symbol_path)
                        all_collections = client.list_collections()
                        
                        for collection in all_collections:
                            collection_full_name = f"{symbol_dir}/{collection.name}"
                            if collection_names and collection_full_name not in collection_names:
                                continue
                            try:
                                client.delete_collection(collection.name)
                                deleted_count += 1
                                self.logger.info(f"Deleted memory collection '{collection_full_name}' for expert {self.id}")
                            except Exception as e:
                                errors.append(f"Failed to delete collection '{collection_full_name}': {str(e)}")
                        
                        # If symbol directory is now empty, remove it
                        remaining = client.list_collections()
                        if not remaining:
                            try:
                                shutil.rmtree(symbol_path)
                                self.logger.info(f"Removed empty symbol directory: {symbol_path}")
                            except Exception as e:
                                self.logger.warning(f"Could not remove symbol directory {symbol_path}: {e}")
                    except Exception as e:
                        self.logger.debug(f"Could not access collections in {symbol_path}: {e}")
            
            # If expert directory is now empty, remove it
            if not os.listdir(expert_base_dir) or (len(os.listdir(expert_base_dir)) == 1 and os.listdir(expert_base_dir)[0] == 'chroma.sqlite3'):
                try:
                    shutil.rmtree(expert_base_dir)
                    self.logger.info(f"Removed memory directory for expert {self.id}")
                except Exception as e:
                    self.logger.warning(f"Could not remove expert directory: {e}")
            
            # Build result message
            if errors:
                return {
                    "status": "warning",
                    "message": f"Deleted {deleted_count} collection(s) with {len(errors)} error(s).",
                    "errors": errors
                }
            else:
                return {
                    "status": "success",
                    "message": f"Successfully deleted {deleted_count} memory collection(s)."
                }
            
        except Exception as e:
            self.logger.error(f"Error clearing memory collections for expert {self.id}: {e}", exc_info=True)
            return {
                "status": "error",
                "message": f"Error clearing memory collections: {str(e)}"
            }
    
    # ==================== SmartRiskExpertInterface Implementation ====================
    
    def get_analysis_summary(self, market_analysis_id: int) -> str:
        """
        Get a concise summary of a market analysis for Smart Risk Manager.
        
        Returns structured format that can be parsed by SmartRiskManagerGraph:
        - Symbol: SYMBOL (current price: bid: XXX / ask: XXX)
        - Action: BUY/SELL/HOLD
        - Confidence: XX.X%
        - Expected Profit: XX.X% (if applicable)
        - Time Horizon: SHORT_TERM/MEDIUM_TERM/LONG_TERM
        - Key Insight: Brief description
        
        Args:
            market_analysis_id: ID of the MarketAnalysis record
            
        Returns:
            str: Structured summary formatted for SmartRiskManager parsing
        """
        try:
            with get_db() as session:
                analysis = session.get(MarketAnalysis, market_analysis_id)
                if not analysis:
                    return f"Analysis {market_analysis_id} not found"
                
                # Get recommendation if exists
                recommendation = None
                if analysis.expert_recommendations:
                    recommendation = analysis.expert_recommendations[0]
                
                # Build summary
                symbol = analysis.symbol
                status = analysis.status.value if hasattr(analysis.status, 'value') else str(analysis.status)
                
                # Get current market price (bid/ask)
                price_info = ""
                try:
                    from ...core.utils import get_account_instance_from_id
                    from ...core.models import ExpertInstance
                    
                    # Get account_id from expert instance
                    expert_instance = session.get(ExpertInstance, analysis.expert_instance_id)
                    if expert_instance:
                        account = get_account_instance_from_id(expert_instance.account_id)
                        if account:
                            bid_price = account.get_instrument_current_price(symbol, price_type='bid')
                            ask_price = account.get_instrument_current_price(symbol, price_type='ask')
                            if bid_price and ask_price:
                                price_info = f" (current price: bid: {bid_price:.2f} / ask: {ask_price:.2f})"
                            elif bid_price:
                                price_info = f" (current price: {bid_price:.2f})"
                except Exception as price_err:
                    self.logger.debug(f"Could not fetch current price for {symbol}: {price_err}")
                
                if recommendation:
                    action = recommendation.recommended_action.value if hasattr(recommendation.recommended_action, 'value') else str(recommendation.recommended_action)
                    confidence = recommendation.confidence
                    
                    # Extract key insight from details field
                    details = recommendation.details or ""
                    key_insight = details.split('.')[0] if details else "No additional details"
                    
                    # Get time horizon (default to MEDIUM_TERM if not specified)
                    time_horizon = getattr(recommendation, 'time_horizon', 'MEDIUM_TERM')
                    if hasattr(time_horizon, 'value'):
                        time_horizon = time_horizon.value
                    
                    # Get expected profit if available (field is named expected_profit_percent in database)
                    expected_profit = recommendation.expected_profit_percent
                    
                    # Build structured summary (format for SmartRiskManager parsing)
                    lines = [
                        f"Symbol: {symbol}{price_info}",
                        f"Action: {action}",
                        f"Confidence: {confidence:.1f}%",
                    ]
                    
                    if expected_profit is not None and action != "HOLD":
                        lines.append(f"Expected Profit: {expected_profit:.2f}%")
                    
                    lines.extend([
                        f"Time Horizon: {time_horizon}",
                        f"Status: {status}",
                        f"Key Insight: {key_insight}"
                    ])
                    
                    return "\n".join(lines)
                else:
                    return (
                        f"Symbol: {symbol}{price_info}\n"
                        f"Action: HOLD\n"
                        f"Status: {status}\n"
                        f"Note: No recommendation available yet"
                    )
                    
        except Exception as e:
            self.logger.error(f"Error getting analysis summary for {market_analysis_id}: {e}", exc_info=True)
            return f"Error retrieving summary for analysis {market_analysis_id}: {str(e)}"
    
    def get_available_outputs(self, market_analysis_id: int) -> Dict[str, str]:
        """
        List available agent outputs from the analysis (matching UI tabs structure).
        
        Returns agent-level outputs, not raw tool outputs. For debates, all rounds are
        included in a single output with speaker indications.
        
        Args:
            market_analysis_id: ID of the MarketAnalysis record
            
        Returns:
            Dict[str, str]: Map of output_key -> description
        """
        try:
            with get_db() as session:
                analysis = session.get(MarketAnalysis, market_analysis_id)
                if not analysis:
                    return {}
                
                # Get state data
                state = analysis.state if analysis.state else {}
                trading_state = state.get('trading_agent_graph', {}) if isinstance(state, dict) else {}
                
                # Build output map matching UI tabs structure
                output_map = {}
                
                # 1. Overall analysis summary (from AnalysisOutput)
                summary_output = session.exec(
                    select(AnalysisOutput)
                    .where(AnalysisOutput.market_analysis_id == market_analysis_id)
                    .where(AnalysisOutput.type == 'tradingagents_analysis_summary')
                ).first()
                
                if summary_output:
                    output_map['analysis_summary'] = "TradingAgents Analysis Summary"
                
                # 2. Individual analyst reports (matching UI tabs)
                analyst_keys = {
                    'market_report': 'Market Analysis (Technical Indicators: MACD, RSI, EMA, SMA, ATR, Bollinger Bands, support/resistance levels, volume analysis, price patterns)',
                    'sentiment_report': 'Social Sentiment Analysis (Social media mentions, sentiment scores, trending topics, community engagement metrics)',
                    'news_report': 'News Analysis (Recent news articles, sentiment scores, market-moving events, press releases)',
                    'fundamentals_report': 'Fundamental Analysis (Earnings calls, cash flow statements, balance sheet data, income statements, valuation ratios, insider transactions)',
                    'macro_report': 'Macroeconomic Analysis (GDP trends, inflation indicators, interest rates, Fed policy, unemployment data, economic calendar events)'
                }
                
                for key, description in analyst_keys.items():
                    if key in trading_state and trading_state[key]:
                        output_map[key] = description
                
                # 3. Investment debate (combined with speaker indications)
                if 'investment_debate_state' in trading_state:
                    debate_state = trading_state['investment_debate_state']
                    if isinstance(debate_state, dict) and debate_state:
                        # Check for both new format (bull_messages/bear_messages) and legacy format (bull_history/bear_history)
                        bull_messages = debate_state.get('bull_messages', [])
                        bear_messages = debate_state.get('bear_messages', [])
                        
                        # Legacy format fallback
                        if not bull_messages and not bear_messages:
                            bull_history = debate_state.get('bull_history', '')
                            bear_history = debate_state.get('bear_history', '')
                            if bull_history or bear_history or debate_state.get('history'):
                                output_map['investment_debate'] = 'Investment Research Debate (Bull vs Bear)'
                        elif bull_messages or bear_messages:
                            output_map['investment_debate'] = 'Investment Research Debate (Bull vs Bear)'
                
                # 4. Research manager summary
                if 'investment_plan' in trading_state and trading_state['investment_plan']:
                    output_map['investment_plan'] = 'Research Manager Summary'
                
                # 5. Trader investment plan
                if 'trader_investment_plan' in trading_state and trading_state['trader_investment_plan']:
                    output_map['trader_investment_plan'] = 'Trader Investment Plan'
                
                # 6. Risk debate (combined with speaker indications)
                if 'risk_debate_state' in trading_state:
                    debate_state = trading_state['risk_debate_state']
                    if isinstance(debate_state, dict) and debate_state:
                        # Check for both new format and legacy format
                        risky_messages = debate_state.get('risky_messages', [])
                        safe_messages = debate_state.get('safe_messages', [])
                        neutral_messages = debate_state.get('neutral_messages', [])
                        
                        # Legacy format fallback
                        if not risky_messages and not safe_messages and not neutral_messages:
                            if debate_state.get('history') or debate_state.get('current_response'):
                                output_map['risk_debate'] = 'Risk Management Debate (Risky/Safe/Neutral)'
                        elif risky_messages or safe_messages or neutral_messages:
                            output_map['risk_debate'] = 'Risk Management Debate (Risky/Safe/Neutral)'
                
                # 7. Final trading decision
                if 'final_trade_decision' in trading_state and trading_state['final_trade_decision']:
                    output_map['final_trade_decision'] = 'Final Trading Decision'
                
                self.logger.debug(f"Found {len(output_map)} agent outputs for analysis {market_analysis_id}")
                return output_map
                
        except Exception as e:
            self.logger.error(f"Error getting available outputs for {market_analysis_id}: {e}", exc_info=True)
            return {}
    
    def get_output_detail(self, market_analysis_id: int, output_key: str) -> str:
        """
        Get the full content of a specific analysis output or agent summary.
        
        For debate outputs, formats all messages with speaker indications (bull/bear, risky/safe/neutral).
        Implements truncation at ~300K characters (~100K tokens) with <truncated> marker.
        
        Args:
            market_analysis_id: ID of the MarketAnalysis record
            output_key: Key of the output to retrieve (from get_available_outputs)
            
        Returns:
            str: Complete output content (truncated if > 300K chars)
            
        Raises:
            KeyError: If output_key is not valid for this analysis
        """
        MAX_CHARS = 300_000  # Approximately 100K tokens
        
        try:
            with get_db() as session:
                analysis = session.get(MarketAnalysis, market_analysis_id)
                if not analysis:
                    raise KeyError(f"Analysis {market_analysis_id} not found")
                
                state = analysis.state if analysis.state else {}
                trading_state = state.get('trading_agent_graph', {}) if isinstance(state, dict) else {}
                
                # Handle analysis summary from AnalysisOutput
                if output_key == 'analysis_summary':
                    summary_output = session.exec(
                        select(AnalysisOutput)
                        .where(AnalysisOutput.market_analysis_id == market_analysis_id)
                        .where(AnalysisOutput.type == 'tradingagents_analysis_summary')
                    ).first()
                    
                    if summary_output and summary_output.text:
                        result = summary_output.text
                        if len(result) > MAX_CHARS:
                            result = result[:MAX_CHARS] + "\n\n<truncated>"
                        return result
                    else:
                        raise KeyError("Analysis summary not found")
                
                # Handle analyst reports (simple text from state)
                analyst_keys = [
                    'market_report', 'sentiment_report', 'news_report',
                    'fundamentals_report', 'macro_report', 'investment_plan',
                    'trader_investment_plan', 'final_trade_decision'
                ]
                
                if output_key in analyst_keys:
                    if output_key not in trading_state:
                        raise KeyError(f"Output not found for key: {output_key}")
                    
                    content = trading_state[output_key]
                    
                    # Handle case where content is a list (multimodal messages)
                    if isinstance(content, list):
                        # Join list elements, filtering for strings
                        content = "\n".join(str(item) for item in content if item)
                    
                    if not content or not content.strip():
                        raise KeyError(f"Output is empty for key: {output_key}")
                    
                    # Truncate if necessary
                    if len(content) > MAX_CHARS:
                        content = content[:MAX_CHARS] + "\n\n<truncated>"
                    
                    return content
                
                # Handle investment debate (format with speaker indications)
                if output_key == 'investment_debate':
                    if 'investment_debate_state' not in trading_state:
                        raise KeyError("Investment debate not found")
                    
                    debate_state = trading_state['investment_debate_state']
                    if not isinstance(debate_state, dict):
                        raise KeyError("Investment debate state is invalid")
                    
                    # Try new format first (bull_messages/bear_messages)
                    bull_messages = debate_state.get('bull_messages', [])
                    bear_messages = debate_state.get('bear_messages', [])
                    judge_decision = debate_state.get('judge_decision', '')
                    
                    # If new format has data, use it
                    if bull_messages or bear_messages:
                        # Format debate with speaker indications
                        debate_output = "# Investment Research Debate (Bull vs Bear)\n\n"
                        debate_output += f"**Total Messages:** {len(bull_messages) + len(bear_messages)}\n\n"
                        debate_output += "---\n\n"
                        
                        # Interleave messages: Bull speaks first, then alternates
                        max_len = max(len(bull_messages), len(bear_messages))
                        for i in range(max_len):
                            if i < len(bull_messages):
                                debate_output += f"## 🐂 Bull Researcher - Message {i+1}\n\n"
                                debate_output += f"{bull_messages[i]}\n\n"
                                debate_output += "---\n\n"
                            
                            if i < len(bear_messages):
                                debate_output += f"## 🐻 Bear Researcher - Message {i+1}\n\n"
                                debate_output += f"{bear_messages[i]}\n\n"
                                debate_output += "---\n\n"
                        
                        # Add judge decision if available
                        if judge_decision:
                            debate_output += "## ⚖️ Judge Decision\n\n"
                            debate_output += f"{judge_decision}\n\n"
                    else:
                        # Legacy format fallback (bull_history/bear_history or combined history)
                        debate_output = "# Investment Research Debate (Bull vs Bear)\n\n"
                        
                        bull_history = debate_state.get('bull_history', '')
                        bear_history = debate_state.get('bear_history', '')
                        combined_history = debate_state.get('history', '')
                        
                        if bull_history:
                            debate_output += "## 🐂 Bull Researcher History\n\n"
                            debate_output += f"{bull_history}\n\n"
                            debate_output += "---\n\n"
                        
                        if bear_history:
                            debate_output += "## 🐻 Bear Researcher History\n\n"
                            debate_output += f"{bear_history}\n\n"
                            debate_output += "---\n\n"
                        
                        if combined_history and not bull_history and not bear_history:
                            debate_output += "## Debate History\n\n"
                            debate_output += f"{combined_history}\n\n"
                            debate_output += "---\n\n"
                        
                        # Add judge decision if available
                        if judge_decision:
                            debate_output += "## ⚖️ Judge Decision\n\n"
                            debate_output += f"{judge_decision}\n\n"
                        
                        # Add current response if available (legacy format)
                        current_response = debate_state.get('current_response', '')
                        if current_response:
                            debate_output += "## Current Response\n\n"
                            debate_output += f"{current_response}\n\n"
                    
                    # Truncate if necessary
                    if len(debate_output) > MAX_CHARS:
                        debate_output = debate_output[:MAX_CHARS] + "\n\n<truncated>"
                    
                    return debate_output
                
                # Handle risk debate (format with speaker indications)
                if output_key == 'risk_debate':
                    if 'risk_debate_state' not in trading_state:
                        raise KeyError("Risk debate not found")
                    
                    debate_state = trading_state['risk_debate_state']
                    if not isinstance(debate_state, dict):
                        raise KeyError("Risk debate state is invalid")
                    
                    # Try new format first (risky_messages/safe_messages/neutral_messages)
                    risky_messages = debate_state.get('risky_messages', [])
                    safe_messages = debate_state.get('safe_messages', [])
                    neutral_messages = debate_state.get('neutral_messages', [])
                    judge_decision = debate_state.get('judge_decision', '')
                    
                    # If new format has data, use it
                    if risky_messages or safe_messages or neutral_messages:
                        # Format debate with speaker indications
                        debate_output = "# Risk Management Debate (Risky/Safe/Neutral)\n\n"
                        debate_output += f"**Total Messages:** {len(risky_messages) + len(safe_messages) + len(neutral_messages)}\n\n"
                        debate_output += "---\n\n"
                        
                        # Interleave messages: Risky → Safe → Neutral cycle
                        max_len = max(len(risky_messages), len(safe_messages), len(neutral_messages))
                        for i in range(max_len):
                            if i < len(risky_messages):
                                debate_output += f"## ⚡ Risky Analyst - Message {i+1}\n\n"
                                debate_output += f"{risky_messages[i]}\n\n"
                                debate_output += "---\n\n"
                            
                            if i < len(safe_messages):
                                debate_output += f"## 🛡️ Safe Analyst - Message {i+1}\n\n"
                                debate_output += f"{safe_messages[i]}\n\n"
                                debate_output += "---\n\n"
                            
                            if i < len(neutral_messages):
                                debate_output += f"## ⚖️ Neutral Analyst - Message {i+1}\n\n"
                                debate_output += f"{neutral_messages[i]}\n\n"
                                debate_output += "---\n\n"
                        
                        # Add judge decision if available
                        if judge_decision:
                            debate_output += "## ⚖️ Judge Decision\n\n"
                            debate_output += f"{judge_decision}\n\n"
                    else:
                        # Legacy format fallback (combined history)
                        debate_output = "# Risk Management Debate (Risky/Safe/Neutral)\n\n"
                        
                        history = debate_state.get('history', '')
                        current_risky = debate_state.get('current_risky_response', '')
                        current_safe = debate_state.get('current_safe_response', '')
                        current_neutral = debate_state.get('current_neutral_response', '')
                        
                        if history:
                            debate_output += "## Debate History\n\n"
                            debate_output += f"{history}\n\n"
                            debate_output += "---\n\n"
                        
                        if current_risky:
                            debate_output += "## ⚡ Current Risky Response\n\n"
                            debate_output += f"{current_risky}\n\n"
                            debate_output += "---\n\n"
                        
                        if current_safe:
                            debate_output += "## 🛡️ Current Safe Response\n\n"
                            debate_output += f"{current_safe}\n\n"
                            debate_output += "---\n\n"
                        
                        if current_neutral:
                            debate_output += "## ⚖️ Current Neutral Response\n\n"
                            debate_output += f"{current_neutral}\n\n"
                            debate_output += "---\n\n"
                        
                        # Add judge decision if available
                        if judge_decision:
                            debate_output += "## ⚖️ Judge Decision\n\n"
                            debate_output += f"{judge_decision}\n\n"
                        
                        # Add current response if available (legacy format)
                        current_response = debate_state.get('current_response', '')
                        if current_response:
                            debate_output += "## Current Response\n\n"
                            debate_output += f"{current_response}\n\n"
                    
                    # Truncate if necessary
                    if len(debate_output) > MAX_CHARS:
                        debate_output = debate_output[:MAX_CHARS] + "\n\n<truncated>"
                    
                    return debate_output
                
                raise KeyError(f"Invalid output_key: {output_key}")
                
        except KeyError:
            raise
        except Exception as e:
            self.logger.error(f"Error getting output detail: {e}", exc_info=True)
            raise KeyError(f"Error retrieving output: {str(e)}")
    
    def supports_smart_risk_manager(self) -> bool:
        """
        Check if this expert implements SmartRiskExpertInterface.
        
        Returns:
            True (TradingAgents fully implements the interface)
        """
        return True
    
    def get_expert_specific_instructions(self, node_name: str) -> str:
        """
        Get TradingAgents-specific instructions for Smart Risk Manager nodes.
        
        Args:
            node_name: Name of the node requesting instructions
            
        Returns:
            Expert-specific guidance for the node
        """
        if node_name == "research_node":
            return """
**TradingAgents Analysis Structure:**

When analyzing TradingAgents outputs, follow this recommended approach:

1. **Start with Overview**: Use `get_analysis_outputs_batch()` to fetch all 'final_trade_decision' outputs across all recent analyses. This gives you a portfolio-wide view of the expert's recommendations.

2. **Dive Deeper as Needed**: If you need more context on specific recommendations, retrieve additional analysis outputs:
   - `market_report`: Technical analysis and price action
   - `sentiment_report`: Market sentiment and news analysis
   - `fundamentals_report`: Company fundamentals and valuation
   - `news_report`: Recent news and events
   - `macro_report`: Macroeconomic factors
   - `investment_plan`: Strategic investment plan
   - `trader_investment_plan`: Tactical trading plan

3. **Batch Fetching**: Use the batch tool to fetch multiple outputs efficiently and stay within token limits.

**Key Outputs:**
- `final_trade_decision`: The expert's final recommendation (BUY/SELL/HOLD with confidence)
- `analysis_summary`: Concise summary of the complete analysis
"""
        
        return ""
