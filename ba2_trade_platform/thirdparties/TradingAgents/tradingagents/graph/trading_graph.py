# TradingAgents/graph/trading_graph.py

import os
from pathlib import Path
import json
from datetime import date, datetime, timezone
from typing import Dict, Any, Tuple, List, Optional

from langchain_openai import ChatOpenAI
from langchain_anthropic import ChatAnthropic
from langchain_google_genai import ChatGoogleGenerativeAI

from langgraph.prebuilt import ToolNode

from ..agents import *
from ..default_config import DEFAULT_CONFIG
from ..agents.utils.memory import FinancialSituationMemory
from ..agents.utils.agent_utils_new import Toolkit  # Use new toolkit
from ..agents.utils.agent_states import (
    AgentState,
    InvestDebateState,
    RiskDebateState,
)
from ..dataflows.config import set_config  # Use config module instead of deprecated interface

from .conditional_logic import ConditionalLogic
from .setup import GraphSetup
from .propagation import Propagator
from .reflection import Reflector
from .signal_processing import SignalProcessor

# Import our new modules
from ..db_storage import DatabaseStorageMixin, update_market_analysis_status
from .. import logger  # Import the TradingAgents logger module with per-expert file logging


class TradingAgentsGraph(DatabaseStorageMixin):
    """Main class that orchestrates the trading agents framework."""

    def __init__(
        self,
        selected_analysts=["market", "social", "news", "fundamentals", "macro"],
        debug=False,
        config: Dict[str, Any] = None,
        market_analysis_id: Optional[int] = None,
        expert_instance_id: Optional[int] = None,
        provider_map: Optional[Dict[str, List[type]]] = None,
        provider_args: Optional[Dict[str, Any]] = None,
    ):
        """Initialize the trading agents graph and components.

        Args:
            selected_analysts: List of analyst types to include
            debug: Whether to run in debug mode
            config: Configuration dictionary. If None, uses default config
            market_analysis_id: Existing MarketAnalysis ID to use (prevents creating a new one)
            expert_instance_id: Expert instance ID for persistent memory storage
            provider_map: BA2 provider map for data access (required for new toolkit)
            provider_args: Optional arguments for provider instantiation (e.g., {"websearch_model": "gpt-5"})
        """
        super().__init__()
        self.debug = debug
        self.config = config or DEFAULT_CONFIG
        self.market_analysis_id = market_analysis_id
        self.expert_instance_id = expert_instance_id
        self.provider_map = provider_map or {}  # Store provider_map
        self.provider_args = provider_args or {}  # Store provider_args

        # Initialize logger
        # Use LOG_FOLDER from BA2 platform config
        from ba2_trade_platform import config as ba2_config
        
        # Get expert_instance_id from market_analysis if available (for logging)
        expert_instance_id_for_logging = None
        if market_analysis_id:
            try:
                from ba2_trade_platform.core.db import get_instance
                from ba2_trade_platform.core.models import MarketAnalysis
                market_analysis = get_instance(MarketAnalysis, market_analysis_id)
                if market_analysis and hasattr(market_analysis, 'expert_instance_id'):
                    expert_instance_id_for_logging = market_analysis.expert_instance_id
            except Exception as e:
                logger.warning(f"Could not get expert_instance_id from market_analysis: {e}")
        
        # Initialize TradingAgents logger
        logger.init_logger(expert_instance_id_for_logging, ba2_config.LOG_FOLDER)
        logger.info(f"Initializing TradingAgentsGraph with market_analysis_id={market_analysis_id}, expert_instance_id={expert_instance_id_for_logging}")

        # Update the interface's config
        set_config(self.config)

        # Create necessary directories
        os.makedirs(
            os.path.join(self.config["project_dir"], "dataflows/data_cache"),
            exist_ok=True,
        )

        # Initialize LLMs
        if self.config["llm_provider"].lower() == "openai" or self.config["llm_provider"] == "ollama" or self.config["llm_provider"] == "openrouter":
            from ..dataflows.config import get_api_key_from_database
            # Get API key based on the provider (defaults to openai_api_key for backward compatibility)
            api_key_setting = self.config.get("api_key_setting", "openai_api_key")
            api_key = get_api_key_from_database(api_key_setting)
            
            # Check if streaming is enabled in config
            from ba2_trade_platform import config as ba2_config
            streaming_enabled = ba2_config.OPENAI_ENABLE_STREAMING
            
            # Get model-specific parameters (e.g., reasoning_effort for GPT-5 and Gemini)
            deep_think_kwargs = self.config.get("deep_think_llm_kwargs", {})
            quick_think_kwargs = self.config.get("quick_think_llm_kwargs", {})
            
            # Extract parameters that should be passed directly to ChatOpenAI (not in model_kwargs)
            # These are special parameters that ChatOpenAI recognizes at the class level
            direct_params = ['reasoning', 'temperature', 'max_tokens', 'top_p', 'frequency_penalty', 'presence_penalty']
            
            # Build ChatOpenAI initialization parameters for deep thinking model
            deep_think_params = {
                "model": self.config["deep_think_llm"],
                "base_url": self.config["backend_url"],
                "api_key": api_key,
                "streaming": streaming_enabled
            }
            
            # Separate direct params from model_kwargs for deep_think
            deep_think_model_kwargs = {}
            for key, value in deep_think_kwargs.items():
                if key in direct_params:
                    deep_think_params[key] = value
                else:
                    deep_think_model_kwargs[key] = value
            
            if deep_think_model_kwargs:
                deep_think_params["model_kwargs"] = deep_think_model_kwargs
            
            # Build ChatOpenAI initialization parameters for quick thinking model
            quick_think_params = {
                "model": self.config["quick_think_llm"],
                "base_url": self.config["backend_url"],
                "api_key": api_key,
                "streaming": streaming_enabled
            }
            
            # Separate direct params from model_kwargs for quick_think
            quick_think_model_kwargs = {}
            for key, value in quick_think_kwargs.items():
                if key in direct_params:
                    quick_think_params[key] = value
                else:
                    quick_think_model_kwargs[key] = value
            
            if quick_think_model_kwargs:
                quick_think_params["model_kwargs"] = quick_think_model_kwargs
            
            self.deep_thinking_llm = ChatOpenAI(**deep_think_params)
            self.quick_thinking_llm = ChatOpenAI(**quick_think_params)
        elif self.config["llm_provider"].lower() == "anthropic":
            self.deep_thinking_llm = ChatAnthropic(model=self.config["deep_think_llm"], base_url=self.config["backend_url"])
            self.quick_thinking_llm = ChatAnthropic(model=self.config["quick_think_llm"], base_url=self.config["backend_url"])
        elif self.config["llm_provider"].lower() == "google":
            self.deep_thinking_llm = ChatGoogleGenerativeAI(model=self.config["deep_think_llm"])
            self.quick_thinking_llm = ChatGoogleGenerativeAI(model=self.config["quick_think_llm"])
        else:
            raise ValueError(f"Unsupported LLM provider: {self.config['llm_provider']}")
        
        # Log model configuration
        logger.info("=" * 80)
        logger.info("TradingAgents Model Configuration:")
        logger.info(f"  Provider: {self.config['llm_provider']}")
        logger.info(f"  Backend URL: {self.config.get('backend_url', 'N/A')}")
        logger.info(f"  Deep Think Model: {self.config['deep_think_llm']}")
        if deep_think_kwargs:
            logger.info(f"  Deep Think Model Parameters: {deep_think_kwargs}")
        logger.info(f"  Quick Think Model: {self.config['quick_think_llm']}")
        if quick_think_kwargs:
            logger.info(f"  Quick Think Model Parameters: {quick_think_kwargs}")
        logger.info(f"  WebSearch Model: {self.provider_args.get('websearch_model', 'N/A')}")
        logger.info(f"  Embedding Model: {self.config.get('embedding_model', 'N/A')}")
        logger.info("=" * 80)
        
        # Initialize new toolkit with provider_map
        if not self.provider_map:
            raise ValueError("provider_map is required for TradingAgentsGraph initialization")
        self.toolkit = Toolkit(provider_map=self.provider_map, provider_args=self.provider_args)

        # Store market_analysis_id for state initialization
        self._market_analysis_id = market_analysis_id
        
        # Initialize memories (will be created with unique names in propagate method)
        self.bull_memory = None
        self.bear_memory = None
        self.trader_memory = None
        self.invest_judge_memory = None
        self.risk_manager_memory = None

        # Create tool nodes
        self.tool_nodes = self._create_tool_nodes()

        # Initialize components with config values
        self.conditional_logic = ConditionalLogic(
            max_debate_rounds=self.config.get('max_debate_rounds', 1),
            max_risk_discuss_rounds=self.config.get('max_risk_discuss_rounds', 1)
        )
        
        # Store selected_analysts for later graph setup
        self.selected_analysts = selected_analysts
        
        # Initialize GraphSetup but don't create the graph yet (memories are None)
        self.graph_setup = None
        self.graph = None

        # Get recursion limit from config (default 100, can be increased for complex analyses)
        max_recur_limit = self.config.get('max_recur_limit', 100)
        self.propagator = Propagator(max_recur_limit=max_recur_limit)
        self.reflector = Reflector(self.quick_thinking_llm)
        self.signal_processor = SignalProcessor(self.quick_thinking_llm)

        # State tracking
        self.curr_state = None
        self.ticker = None
        self.log_states_dict = {}  # date to full state dict

        logger.info("TradingAgentsGraph initialization completed")

    def _sync_state_to_market_analysis(self, graph_state: Dict[str, Any], step_name: str = None):
        """Synchronize current graph state to MarketAnalysis database record.
        
        Args:
            graph_state: Current state of the TradingAgents graph
            step_name: Optional name of the current step/stage for context
        """
        if not self.market_analysis_id:
            return
            
        try:
            # Create a clean state snapshot for database storage
            state_snapshot = {
                'current_step': step_name or 'unknown',
                'timestamp': datetime.now(timezone.utc).isoformat(),
                'messages_count': len(graph_state.get('messages', [])),
                'ticker': getattr(self, 'ticker', None)
            }
            
            # Add key state information (avoid storing large message content)
            for key, value in graph_state.items():
                if key == 'messages':
                    # Store message count and types instead of full content
                    state_snapshot['message_types'] = [msg.__class__.__name__ for msg in value] if value else []
                elif key in ['expert_recommendation', 'final_trade_decision', 'risk_assessment']:
                    # Store important decision states
                    state_snapshot[key] = value
                elif key in ['investment_debate_state', 'risk_debate_state']:
                    # Always store debate states with their message lists - these are crucial for UI display
                    if isinstance(value, dict):
                        if key == 'investment_debate_state':
                            state_snapshot[key] = {
                                'bull_history': value.get('bull_history', ''),
                                'bear_history': value.get('bear_history', ''),
                                'bull_messages': self._clean_message_list(value.get('bull_messages', [])),
                                'bear_messages': self._clean_message_list(value.get('bear_messages', [])),
                                'history': value.get('history', ''),
                                'current_response': value.get('current_response', ''),
                                'judge_decision': value.get('judge_decision', ''),
                                'count': value.get('count', 0)
                            }
                        elif key == 'risk_debate_state':
                            state_snapshot[key] = {
                                'risky_history': value.get('risky_history', ''),
                                'safe_history': value.get('safe_history', ''),
                                'neutral_history': value.get('neutral_history', ''),
                                'risky_messages': self._clean_message_list(value.get('risky_messages', [])),
                                'safe_messages': self._clean_message_list(value.get('safe_messages', [])),
                                'neutral_messages': self._clean_message_list(value.get('neutral_messages', [])),
                                'history': value.get('history', ''),
                                'judge_decision': value.get('judge_decision', ''),
                                'count': value.get('count', 0),
                                'latest_speaker': value.get('latest_speaker', ''),
                                'current_risky_response': value.get('current_risky_response', ''),
                                'current_safe_response': value.get('current_safe_response', ''),
                                'current_neutral_response': value.get('current_neutral_response', '')
                            }
                    else:
                        state_snapshot[key] = value
                elif isinstance(value, (str, int, float, bool, type(None))):
                    # Store simple types directly
                    state_snapshot[key] = value
                elif isinstance(value, (dict, list)) and len(str(value)) < 1000:
                    # Store small complex objects, but clean them first
                    state_snapshot[key] = self._clean_any_object(value)
            
            # Final pass: recursively clean the entire state snapshot to ensure no HumanMessage objects remain
            cleaned_state_snapshot = self._clean_any_object(state_snapshot)
            
            # Update MarketAnalysis state using the proper merging function
            from ba2_trade_platform.core.types import MarketAnalysisStatus
            update_market_analysis_status(
                analysis_id=self.market_analysis_id,
                status=MarketAnalysisStatus.RUNNING,  # Keep status as running during execution
                state=cleaned_state_snapshot
            )
            
            logger.debug(f"Synced graph state to MarketAnalysis {self.market_analysis_id} at step: {step_name}")
            
        except Exception as e:
            logger.error(f"Failed to sync state to MarketAnalysis {self.market_analysis_id}: {e}", exc_info=True)

    def _clean_message_list(self, messages: list) -> list:
        """Clean a list of messages to make them JSON serializable.
        
        Args:
            messages: List that may contain HumanMessage or other non-serializable objects
            
        Returns:
            List of JSON-serializable message representations
        """
        cleaned_messages = []
        for msg in messages:
            cleaned_msg = self._clean_any_object(msg)
            cleaned_messages.append(cleaned_msg)
        
        return cleaned_messages

    def _clean_any_object(self, obj) -> Any:
        """Recursively clean any object to make it JSON serializable.
        
        Args:
            obj: Any object that might contain non-serializable elements
            
        Returns:
            JSON-serializable representation of the object
        """
        if obj is None or isinstance(obj, (str, int, float, bool)):
            # Simple types are already serializable
            return obj
        elif hasattr(obj, 'content') and hasattr(obj, '__class__'):
            # This is likely a langchain message object
            return {
                'type': obj.__class__.__name__,
                'content': str(obj.content) if obj.content else ''
            }
        elif isinstance(obj, dict):
            # Recursively clean dictionary values
            cleaned_dict = {}
            for key, value in obj.items():
                try:
                    cleaned_dict[str(key)] = self._clean_any_object(value)
                except Exception:
                    cleaned_dict[str(key)] = str(value)
            return cleaned_dict
        elif isinstance(obj, (list, tuple)):
            # Recursively clean list/tuple elements
            cleaned_list = []
            for item in obj:
                try:
                    cleaned_list.append(self._clean_any_object(item))
                except Exception:
                    cleaned_list.append(str(item))
            return cleaned_list
        else:
            # Test if object is already JSON serializable
            try:
                import json
                json.dumps(obj)
                return obj
            except (TypeError, ValueError):
                # Convert to string if not serializable
                return {'content': str(obj), 'type': obj.__class__.__name__}

    def _create_tool_nodes(self) -> Dict[str, ToolNode]:
        """Create tool nodes for different data sources using new BA2 provider toolkit."""
        # Import LoggingToolNode for database logging
        from ..db_storage import LoggingToolNode
        from langchain_core.tools import tool
        
        # Extract model info from provider_args if available
        model_info = None
        if self.provider_args and 'websearch_model' in self.provider_args:
            model_info = self.provider_args['websearch_model']
        
        # Wrap toolkit methods with @tool decorator for LangChain compatibility
        # This creates proper Tool objects from instance methods
        # NOTE: Parameters that are Optional in the toolkit should have defaults here too
        # to prevent "Field required" errors when AI omits optional parameters
        
        @tool
        def get_ohlcv_data(
            symbol: str,
            start_date: str = None,
            end_date: str = None,
            interval: str = None
        ) -> str:
            """Get OHLCV (Open, High, Low, Close, Volume) stock price data.
            
            Args:
                symbol: REQUIRED. Stock ticker symbol (e.g., 'AAPL', 'NVDA', 'TSLA'). This must be provided.
                start_date: Optional. Start date for data range. Defaults to 30 days ago if not provided.
                end_date: Optional. End date for data range. Defaults to today if not provided.
                interval: Optional. Data interval. Defaults to configured timeframe.
            
            Returns:
                str: OHLCV price data for the specified symbol.
            """
            return self.toolkit.get_ohlcv_data(symbol, start_date, end_date, interval)
        
        @tool
        def get_indicator_data(
            symbol: str,
            indicator: str,
            start_date: str = None,
            end_date: str = None,
            interval: str = None
        ) -> str:
            """Get technical indicator data for a stock.
            
            Args:
                symbol: REQUIRED. Stock ticker symbol (e.g., 'AAPL', 'NVDA', 'TSLA'). This must be provided.
                indicator: REQUIRED. Technical indicator name (e.g., 'rsi', 'macd', 'boll', 'atr', 'close_50_sma').
                start_date: Optional. Start date for data range. Defaults to 30 days ago if not provided.
                end_date: Optional. End date for data range. Defaults to today if not provided.
                interval: Optional. Data interval. Defaults to configured timeframe.
            
            Returns:
                str: Technical indicator data for the specified symbol.
            """
            return self.toolkit.get_indicator_data(symbol, indicator, start_date, end_date, interval)
        
        @tool
        def get_company_news(
            symbol: str,
            end_date: str,
            lookback_days: int = None
        ) -> str:
            """Get news articles about a specific company.
            
            Args:
                symbol: REQUIRED. Stock ticker symbol (e.g., 'AAPL', 'NVDA', 'TSLA'). This must be provided.
                end_date: REQUIRED. End date for news search (format: YYYY-MM-DD).
                lookback_days: Optional. Number of days to look back. Defaults to configured value.
            
            Returns:
                str: News articles about the specified company.
            """
            return self.toolkit.get_company_news(symbol, end_date, lookback_days)
        
        @tool
        def get_global_news(
            end_date: str,
            lookback_days: int = None
        ) -> str:
            """Get global market and macroeconomic news."""
            return self.toolkit.get_global_news(end_date, lookback_days)
        
        @tool
        def extract_web_content(
            url: str
        ) -> str:
            """Extract full content from a web page URL for detailed article reading."""
            # Ensure url is wrapped in a list if passed as string
            urls = [url] if isinstance(url, str) else url
            return self.toolkit.extract_web_content(urls)
        
        @tool
        def get_social_media_sentiment(
            symbol: str,
            end_date: str,
            lookback_days: int = None
        ) -> str:
            """Retrieve social media sentiment and discussions about a specific company.
            
            Args:
                symbol: REQUIRED. Stock ticker symbol (e.g., 'AAPL', 'NVDA', 'TSLA'). This must be provided.
                end_date: REQUIRED. End date for sentiment search (format: YYYY-MM-DD).
                lookback_days: Optional. Number of days to look back. Defaults to configured value.
            
            Returns:
                str: Social media sentiment data for the specified company.
            """
            return self.toolkit.get_social_media_sentiment(symbol, end_date, lookback_days)
        
        @tool
        def get_balance_sheet(
            symbol: str,
            frequency: str,
            end_date: str,
            lookback_periods: int = 4
        ) -> str:
            """Get company balance sheet data."""
            return self.toolkit.get_balance_sheet(symbol, frequency, end_date, lookback_periods)
        
        @tool
        def get_income_statement(
            symbol: str,
            frequency: str,
            end_date: str,
            lookback_periods: int = 4
        ) -> str:
            """Get company income statement data."""
            return self.toolkit.get_income_statement(symbol, frequency, end_date, lookback_periods)
        
        @tool
        def get_cashflow_statement(
            symbol: str,
            frequency: str,
            end_date: str,
            lookback_periods: int = 4
        ) -> str:
            """Get company cash flow statement data."""
            return self.toolkit.get_cashflow_statement(symbol, frequency, end_date, lookback_periods)
        
        @tool
        def get_insider_transactions(
            symbol: str,
            end_date: str,
            lookback_days: int = None
        ) -> str:
            """Get insider trading transactions."""
            return self.toolkit.get_insider_transactions(symbol, end_date, lookback_days)
        
        @tool
        def get_insider_sentiment(
            symbol: str,
            end_date: str,
            lookback_days: int = None
        ) -> str:
            """Get aggregated insider sentiment metrics."""
            return self.toolkit.get_insider_sentiment(symbol, end_date, lookback_days)
        
        @tool
        def get_earnings_estimates(
            symbol: str,
            as_of_date: str,
            lookback_periods: int = 4,
            frequency: str = "quarterly"
        ) -> str:
            """Get forward earnings estimates from analysts for the next periods."""
            return self.toolkit.get_earnings_estimates(symbol, as_of_date, lookback_periods, frequency)
        
        @tool
        def get_economic_indicators(
            end_date: str,
            lookback_days: int = None,
            indicators: list = None
        ) -> str:
            """Get economic indicators (GDP, unemployment, inflation, etc.)."""
            return self.toolkit.get_economic_indicators(end_date, lookback_days, indicators)
        
        @tool
        def get_yield_curve(
            end_date: str,
            lookback_days: int = None
        ) -> str:
            """Get Treasury yield curve data."""
            return self.toolkit.get_yield_curve(end_date, lookback_days)
        
        @tool
        def get_fed_calendar(
            end_date: str,
            lookback_days: int = None
        ) -> str:
            """Get Federal Reserve calendar and meetings."""
            return self.toolkit.get_fed_calendar(end_date, lookback_days)
        
        return {
            "market": LoggingToolNode(
                [
                    get_ohlcv_data,
                    get_indicator_data,
                ],
                self.market_analysis_id,
                model_info=model_info
            ),
            "social": LoggingToolNode(
                [
                    get_social_media_sentiment,  # For social media sentiment and discussions
                ],
                self.market_analysis_id,
                model_info=model_info
            ),
            "news": LoggingToolNode(
                [
                    get_company_news,  # For company-specific news
                    get_global_news,  # For global/macro news
                    extract_web_content,  # For extracting full article content from URLs
                ],
                self.market_analysis_id,
                model_info=model_info
            ),
            "fundamentals": LoggingToolNode(
                [
                    get_balance_sheet,
                    get_income_statement,
                    get_cashflow_statement,
                    get_insider_transactions,
                    get_insider_sentiment,
                    get_earnings_estimates,
                ],
                self.market_analysis_id,
                model_info=model_info
            ),
            "macro": LoggingToolNode(
                [
                    get_economic_indicators,
                    get_yield_curve,
                    get_fed_calendar,
                ],
                self.market_analysis_id,
                model_info=model_info
            ),
        }

    def _initialize_memories_and_graph(self, symbol: str):
        """Initialize memory collections and create the graph with properly initialized memories"""
        from ..agents.utils.memory import FinancialSituationMemory
        
        self.bull_memory = FinancialSituationMemory("bull_memory", self.config, symbol, self.market_analysis_id, self.expert_instance_id)
        self.bear_memory = FinancialSituationMemory("bear_memory", self.config, symbol, self.market_analysis_id, self.expert_instance_id)
        self.trader_memory = FinancialSituationMemory("trader_memory", self.config, symbol, self.market_analysis_id, self.expert_instance_id)
        self.invest_judge_memory = FinancialSituationMemory("invest_judge_memory", self.config, symbol, self.market_analysis_id, self.expert_instance_id)
        self.risk_manager_memory = FinancialSituationMemory("risk_manager_memory", self.config, symbol, self.market_analysis_id, self.expert_instance_id)
        
        # Create GraphSetup with the newly initialized memories
        self.graph_setup = GraphSetup(
            self.quick_thinking_llm,
            self.deep_thinking_llm,
            self.toolkit,
            self.tool_nodes,
            self.bull_memory,
            self.bear_memory,
            self.trader_memory,
            self.invest_judge_memory,
            self.risk_manager_memory,
            self.conditional_logic,
            self.config,
        )
        
        # Now create the graph with properly initialized memories
        self.graph = self.graph_setup.setup_graph(self.selected_analysts)
        
        logger.debug(f"Initialized memory collections and graph for symbol: {symbol}, market_analysis_id: {self.market_analysis_id}")

    def propagate(self, company_name, trade_date):
        """Run the trading agents graph for a company on a specific date."""

        self.ticker = company_name
        
        # Initialize memories and create graph if not already done
        if self.graph is None:
            self._initialize_memories_and_graph(company_name)
        
        if self.market_analysis_id:
            logger.info(f"Starting analysis for {company_name} on {trade_date}")
            logger.info(f"Using existing MarketAnalysis record with ID: {self.market_analysis_id}")
        else:
            # Running without database - use terminal output
            logger.info(f"Starting standalone analysis for {company_name} on {trade_date}")
            logger.info("No market analysis ID provided - results will be shown in terminal only")

        # Initialize state
        init_agent_state = self.propagator.create_initial_state(
            company_name, trade_date, self.market_analysis_id
        )
        args = self.propagator.get_graph_args()
        
        # Sync initial state to MarketAnalysis
        self._sync_state_to_market_analysis(init_agent_state, "initialization")

        try:
            if self.debug:
                # Debug mode with tracing
                trace = []
                step_count = 0
                accumulated_state = init_agent_state.copy()  # Start with initial state
                
                for chunk in self.graph.stream(init_agent_state, **args):
                    # Check if analysis has been marked as FAILED (e.g., by tool error callback)
                    if self.market_analysis_id:
                        from ..db_storage import get_market_analysis_status
                        current_status = get_market_analysis_status(self.market_analysis_id)
                        if current_status == "FAILED":
                            logger.error(f"Analysis {self.market_analysis_id} marked as FAILED, stopping graph execution")
                            raise Exception("Analysis failed due to critical tool error - stopping graph execution")
                    
                    if len(chunk["messages"]) == 0:
                        pass
                    else:
                        # Log message to logger instead of printing to console
                        self._log_message(chunk["messages"][-1])
                        trace.append(chunk)
                        step_count += 1
                        
                        # Update accumulated state with chunk data
                        accumulated_state.update(chunk)
                        
                        # Sync state after each step in debug mode
                        self._sync_state_to_market_analysis(accumulated_state, f"debug_step_{step_count}")

                final_state = accumulated_state  # Use accumulated state instead of just the last chunk
            else:
                # Standard mode without tracing
                final_state = self.graph.invoke(init_agent_state, **args)

            # Store current state for reflection
            self.curr_state = final_state
            
            # Sync final state to MarketAnalysis
            self._sync_state_to_market_analysis(final_state, "completion")

            # Handle expert recommendation - now generated by the Final Summarization agent in the graph
            if self.market_analysis_id:
                self._store_expert_recommendation_from_graph(final_state, company_name)
            else:
                self._print_terminal_summary_from_graph(final_state, company_name)

            # Log state
            self._log_state(trade_date, final_state)

            # Update analysis status to completed
            if self.market_analysis_id:
                self.update_analysis_status("completed", {"final_state": "success"})
                logger.info(f"Analysis for {company_name} completed successfully")
            else:
                logger.info(f"Standalone analysis for {company_name} completed successfully")

            # Return decision and processed signal
            return final_state, self.process_signal(final_state["final_trade_decision"])
            
        except Exception as e:
            logger.error(f"Error during analysis for {company_name}: {str(e)}", exc_info=True)
            if self.market_analysis_id:
                # Sync error state to MarketAnalysis
                error_state = {
                    "error": str(e),
                    "error_timestamp": datetime.now(timezone.utc).isoformat(),
                    "failed_step": "execution",
                    "ticker": company_name
                }
                self._sync_state_to_market_analysis(error_state, "error")
                self.update_analysis_status("failed", {"error": str(e)})
            raise

    def _store_expert_recommendation_from_graph(self, final_state: Dict[str, Any], symbol: str):
        """Store expert recommendation generated by the Final Summarization agent in the graph"""
        try:
            # Get the recommendation from the graph's final summarization agent
            recommendation = final_state.get("expert_recommendation")
            
            if not recommendation:
                logger.warning(f"No expert_recommendation found in final_state for {symbol}")
                return
                
            # Log the complete JSON recommendation for debugging and audit trail
            import json
            logger.info(f"Graph-Generated Recommendation JSON for {symbol}: {json.dumps(recommendation, indent=2)}")
            
            # Store recommendation in database
            # Note: ExpertRecommendation creation is handled by the higher-level TradingAgents.py
            # to avoid duplicate database entries. The recommendation data is returned via the final_state
            # and processed by TradingAgents._create_expert_recommendation()
            
            # Get expert_instance_id from market_analysis for logging purposes
            expert_instance_id = None
            if self.market_analysis_id:
                try:
                    from ba2_trade_platform.core.db import get_instance
                    from ba2_trade_platform.core.models import MarketAnalysis
                    market_analysis = get_instance(MarketAnalysis, self.market_analysis_id)
                    if market_analysis and hasattr(market_analysis, 'expert_instance_id'):
                        expert_instance_id = market_analysis.expert_instance_id
                except Exception as e:
                    logger.warning(f"Could not get expert_instance_id from market_analysis: {e}")
            
            if expert_instance_id:
                logger.info(f"Expert recommendation data will be processed by TradingAgents.py for expert_instance_id: {expert_instance_id}")
            else:
                logger.warning("No expert_instance_id available, ExpertRecommendation creation will be handled by TradingAgents.py")
                
            # Also store as analysis output
            if self.market_analysis_id:
                self.store_analysis_output(
                    market_analysis_id=self.market_analysis_id,
                    name="expert_recommendation",
                    output_type="recommendation",
                    text=json.dumps(recommendation, indent=2)
                )
                
        except Exception as e:
            logger.error(f"Error storing expert recommendation from graph: {str(e)}", exc_info=True)

    def _print_terminal_summary_from_graph(self, final_state: Dict[str, Any], symbol: str):
        """Print formatted summary using recommendation generated by the Final Summarization agent"""
        try:
            # Get the recommendation from the graph's final summarization agent
            recommendation = final_state.get("expert_recommendation")
            
            if recommendation:
                # Log the complete JSON recommendation for debugging and audit trail
                import json
                logger.info(f"Graph-Generated Recommendation JSON for {symbol}: {json.dumps(recommendation, indent=2)}")
                
                # Print formatted summary to terminal
                logger.info("="*70)
                logger.info(f"TRADING ANALYSIS SUMMARY FOR {symbol}")
                logger.info("="*70)
                logger.info(f"Recommended Action: {recommendation['recommended_action']}")
                logger.info(f"Expected Profit: {recommendation['expected_profit_percent']:.2f}%")
                logger.info(f"Price at Analysis: ${recommendation['price_at_date']:.2f}")
                logger.info(f"Confidence Level: {recommendation['confidence']:.1f}%")
                logger.info(f"Risk Level: {recommendation.get('risk_level', 'UNKNOWN')}")
                logger.info(f"Time Horizon: {recommendation.get('time_horizon', 'UNKNOWN')}")
                
                # Print key factors if available
                key_factors = recommendation.get('key_factors', [])
                if key_factors:
                    logger.info("Key Factors:")
                    for factor in key_factors:
                        logger.info(f"   • {factor}")
                
                logger.info("Analysis Details:")
                logger.info(f"   {recommendation['details']}")
                logger.info("="*70)
                
                # Print analysis summary if available
                analysis_summary = recommendation.get('analysis_summary', {})
                if analysis_summary:
                    logger.info("Analysis Summary:")
                    logger.info(f"   Market Trend: {analysis_summary.get('market_trend', 'Unknown')}")
                    logger.info(f"   Fundamental Strength: {analysis_summary.get('fundamental_strength', 'Unknown')}")
                    logger.info(f"   Sentiment Score: {analysis_summary.get('sentiment_score', 0)}")
                    logger.info(f"   Macro Environment: {analysis_summary.get('macro_environment', 'Unknown')}")
                    logger.info(f"   Technical Signals: {analysis_summary.get('technical_signals', 'Unknown')}")
                    logger.info("="*70)
            else:
                # Fallback to basic summary if no recommendation available  
                logger.warning(f"No expert_recommendation found in final_state for {symbol}")
                self._print_basic_terminal_summary(final_state, symbol)
                
        except Exception as e:
            logger.error(f"Error printing terminal summary from graph: {str(e)}", exc_info=True)
            self._print_basic_terminal_summary(final_state, symbol)

    def _print_basic_terminal_summary(self, final_state: Dict[str, Any], symbol: str):
        """Print basic terminal summary as fallback"""
        logger.info("="*70)
        logger.info(f"BASIC TRADING ANALYSIS SUMMARY FOR {symbol}")
        logger.info("="*70)
        logger.info(f"Final Decision: {final_state.get('final_trade_decision', 'Unknown')}")
        logger.info(f"Investment Plan: {final_state.get('investment_plan', 'Unknown')}")
        
        # Print key reports if available
        if final_state.get("market_report"):
            logger.info("Market Analysis:")
            logger.info(f"   {final_state['market_report'][:200]}...")
        
        if final_state.get("news_report"):
            logger.info("News Analysis:")
            logger.info(f"   {final_state['news_report'][:200]}...")
        
        if final_state.get("fundamentals_report"):
            logger.info("Fundamentals Analysis:")
            logger.info(f"   {final_state['fundamentals_report'][:200]}...")
            
        if final_state.get("macro_report"):
            logger.info("Macro Economic Analysis:")
            logger.info(f"   {final_state['macro_report'][:200]}...")
        
        logger.info("="*70)

    def _log_state(self, trade_date, final_state):
        """Store the final state in database if MarketAnalysis is available, otherwise just log it."""
        # Prepare the state data
        state_data = {
            "company_of_interest": final_state["company_of_interest"],
            "trade_date": final_state["trade_date"],
            "market_report": final_state["market_report"],
            "sentiment_report": final_state["sentiment_report"],
            "news_report": final_state["news_report"],
            "fundamentals_report": final_state["fundamentals_report"],
            "investment_debate_state": {
                "bull_history": final_state["investment_debate_state"]["bull_history"],
                "bear_history": final_state["investment_debate_state"]["bear_history"],
                "bull_messages": final_state["investment_debate_state"].get("bull_messages", []),
                "bear_messages": final_state["investment_debate_state"].get("bear_messages", []),
                "history": final_state["investment_debate_state"]["history"],
                "current_response": final_state["investment_debate_state"][
                    "current_response"
                ],
                "judge_decision": final_state["investment_debate_state"][
                    "judge_decision"
                ],
                "count": final_state["investment_debate_state"].get("count", 0),
            },
            "trader_investment_decision": final_state["trader_investment_plan"],
            "risk_debate_state": {
                "risky_history": final_state["risk_debate_state"]["risky_history"],
                "safe_history": final_state["risk_debate_state"]["safe_history"],
                "neutral_history": final_state["risk_debate_state"]["neutral_history"],
                "risky_messages": final_state["risk_debate_state"].get("risky_messages", []),
                "safe_messages": final_state["risk_debate_state"].get("safe_messages", []),
                "neutral_messages": final_state["risk_debate_state"].get("neutral_messages", []),
                "history": final_state["risk_debate_state"]["history"],
                "judge_decision": final_state["risk_debate_state"]["judge_decision"],
                "count": final_state["risk_debate_state"].get("count", 0),
                "latest_speaker": final_state["risk_debate_state"].get("latest_speaker", ""),
            },
            "investment_plan": final_state["investment_plan"],
            "final_trade_decision": final_state["final_trade_decision"],
        }

        if hasattr(self, 'market_analysis_id') and self.market_analysis_id:
            # Store in database using final_state key
            try:
                import json
                self.store_analysis_output(
                    market_analysis_id=self.market_analysis_id,
                    name="final_state",
                    output_type="analysis_state",
                    text=json.dumps(state_data, indent=2)
                )
                logger.info(f"Stored final state in database for analysis ID: {self.market_analysis_id}")
            except Exception as e:
                logger.error(f"Error storing final state in database: {str(e)}", exc_info=True)
                # Fallback to logging only
                logger.info(f"Final state data: {json.dumps(state_data, indent=2)}")
        else:
            # No MarketAnalysis - just log the state data
            import json
            logger.info(f"Final state for {trade_date}: {json.dumps(state_data, indent=2)}")
            
        # Keep state in memory for potential reflection operations
        self.log_states_dict[str(trade_date)] = state_data

    def reflect_and_remember(self, returns_losses):
        """Reflect on decisions and update memory based on returns."""
        self.reflector.reflect_bull_researcher(
            self.curr_state, returns_losses, self.bull_memory
        )
        self.reflector.reflect_bear_researcher(
            self.curr_state, returns_losses, self.bear_memory
        )
        self.reflector.reflect_trader(
            self.curr_state, returns_losses, self.trader_memory
        )
        self.reflector.reflect_invest_judge(
            self.curr_state, returns_losses, self.invest_judge_memory
        )
        self.reflector.reflect_risk_manager(
            self.curr_state, returns_losses, self.risk_manager_memory
        )

    def process_signal(self, full_signal):
        """Process a signal to extract the core decision."""
        return self.signal_processor.process_signal(full_signal)

    def _log_message(self, message) -> None:
        """
        Log a LangChain message object to the logger instead of printing to stdout.
        
        This replaces pretty_print() to capture Tool Messages and all LLM communication
        in the log files.
        
        Args:
            message: A LangChain BaseMessage object to log
        """
        try:
            from langchain_core.messages import (
                HumanMessage, AIMessage, ToolMessage, SystemMessage, BaseMessage
            )
            
            if isinstance(message, ToolMessage):
                # Format Tool Messages
                logger.info(f"{'=' * 80}")
                logger.info(f"Tool Message")
                logger.info(f"{'=' * 80}")
                logger.info(f"Tool: {message.name if hasattr(message, 'name') else 'Unknown'}")
                logger.info(f"Tool ID: {message.tool_call_id if hasattr(message, 'tool_call_id') else 'N/A'}")
                if hasattr(message, 'content') and message.content:
                    content = message.content if isinstance(message.content, str) else str(message.content)
                    # Log as error if content starts with "Error:"
                    if content.startswith("Error:"):
                        logger.error(f"Result: {content}")
                    else:
                        logger.info(f"Result: {content}")
                logger.info(f"{'=' * 80}")
            elif isinstance(message, AIMessage):
                # Format AI Messages
                logger.info(f"{'=' * 80}")
                logger.info(f"AI Message")
                logger.info(f"{'=' * 80}")
                if hasattr(message, 'content') and message.content:
                    content = message.content if isinstance(message.content, str) else str(message.content)
                    logger.info(f"Content: {content}")
                if hasattr(message, 'tool_calls') and message.tool_calls:
                    logger.info(f"Tool Calls: {len(message.tool_calls)}")
                    for i, tc in enumerate(message.tool_calls, 1):
                        tool_args = tc.get('args', {})
                        logger.info(f"  {i}. {tc.get('name', 'Unknown')} - {tc.get('id', 'Unknown')} - args: {tool_args}")
                logger.info(f"{'=' * 80}")
            elif isinstance(message, HumanMessage):
                # Format Human Messages
                logger.debug(f"Human Message: {str(message.content)}")
            elif isinstance(message, SystemMessage):
                # Format System Messages
                logger.debug(f"System Message: {str(message.content)}")
            else:
                # Generic message handling
                msg_type = message.__class__.__name__
                logger.debug(f"{msg_type}: {str(message)}")
                
        except Exception as e:
            logger.warning(f"Error logging message: {e}")
            try:
                logger.info(f"Message (fallback): {str(message)}")
            except:
                pass

