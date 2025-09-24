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
from ..agents.utils.agent_utils import Toolkit
from ..agents.utils.agent_states import (
    AgentState,
    InvestDebateState,
    RiskDebateState,
)
from ..dataflows.interface import set_config

from .conditional_logic import ConditionalLogic
from .setup import GraphSetup
from .propagation import Propagator
from .reflection import Reflector
from .signal_processing import SignalProcessor

# Import our new modules
from ..db_storage import DatabaseStorageMixin, update_market_analysis_status
from .. import logger as ta_logger


class TradingAgentsGraph(DatabaseStorageMixin):
    """Main class that orchestrates the trading agents framework."""

    def __init__(
        self,
        selected_analysts=["market", "social", "news", "fundamentals", "macro"],
        debug=False,
        config: Dict[str, Any] = None,
        market_analysis_id: Optional[int] = None,
    ):
        """Initialize the trading agents graph and components.

        Args:
            selected_analysts: List of analyst types to include
            debug: Whether to run in debug mode
            config: Configuration dictionary. If None, uses default config
            market_analysis_id: Existing MarketAnalysis ID to use (prevents creating a new one)
        """
        super().__init__()
        self.debug = debug
        self.config = config or DEFAULT_CONFIG
        self.market_analysis_id = market_analysis_id

        # Initialize logger
        # Use BA2 platform logs directory by default
        try:
            from ba2_trade_platform import config as ba2_config
            default_log_dir = os.path.join(ba2_config.HOME, "logs")
        except ImportError:
            default_log_dir = "."
        
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
                ta_logger.warning(f"Could not get expert_instance_id from market_analysis: {e}")
        
        ta_logger.init_logger(expert_instance_id_for_logging, self.config.get("log_dir", default_log_dir))
        ta_logger.info(f"Initializing TradingAgentsGraph with market_analysis_id={market_analysis_id}, expert_instance_id={expert_instance_id_for_logging}")

        # Update the interface's config
        set_config(self.config)

        # Create necessary directories
        os.makedirs(
            os.path.join(self.config["project_dir"], "dataflows/data_cache"),
            exist_ok=True,
        )

        # Initialize LLMs
        if self.config["llm_provider"].lower() == "openai" or self.config["llm_provider"] == "ollama" or self.config["llm_provider"] == "openrouter":
            from ..dataflows.config import get_openai_api_key
            api_key = get_openai_api_key()
            self.deep_thinking_llm = ChatOpenAI(model=self.config["deep_think_llm"], base_url=self.config["backend_url"], api_key=api_key)
            self.quick_thinking_llm = ChatOpenAI(model=self.config["quick_think_llm"], base_url=self.config["backend_url"], api_key=api_key)
        elif self.config["llm_provider"].lower() == "anthropic":
            self.deep_thinking_llm = ChatAnthropic(model=self.config["deep_think_llm"], base_url=self.config["backend_url"])
            self.quick_thinking_llm = ChatAnthropic(model=self.config["quick_think_llm"], base_url=self.config["backend_url"])
        elif self.config["llm_provider"].lower() == "google":
            self.deep_thinking_llm = ChatGoogleGenerativeAI(model=self.config["deep_think_llm"])
            self.quick_thinking_llm = ChatGoogleGenerativeAI(model=self.config["quick_think_llm"])
        else:
            raise ValueError(f"Unsupported LLM provider: {self.config['llm_provider']}")
        
        
        self.toolkit = Toolkit(config=self.config)

        # Initialize memories (will be created with unique names in propagate method)
        self.bull_memory = None
        self.bear_memory = None
        self.trader_memory = None
        self.invest_judge_memory = None
        self.risk_manager_memory = None

        # Create tool nodes
        self.tool_nodes = self._create_tool_nodes()

        # Initialize components
        self.conditional_logic = ConditionalLogic()
        
        # Store selected_analysts for later graph setup
        self.selected_analysts = selected_analysts
        
        # Initialize GraphSetup but don't create the graph yet (memories are None)
        self.graph_setup = None
        self.graph = None

        self.propagator = Propagator()
        self.reflector = Reflector(self.quick_thinking_llm)
        self.signal_processor = SignalProcessor(self.quick_thinking_llm)

        # State tracking
        self.curr_state = None
        self.ticker = None
        self.log_states_dict = {}  # date to full state dict

        ta_logger.info("TradingAgentsGraph initialization completed")

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
                    # Store small complex objects
                    state_snapshot[key] = value
            
            # Update MarketAnalysis state using the proper merging function
            from ba2_trade_platform.core.types import MarketAnalysisStatus
            update_market_analysis_status(
                analysis_id=self.market_analysis_id,
                status=MarketAnalysisStatus.RUNNING,  # Keep status as running during execution
                state=state_snapshot
            )
            
            ta_logger.debug(f"Synced graph state to MarketAnalysis {self.market_analysis_id} at step: {step_name}")
            
        except Exception as e:
            ta_logger.error(f"Failed to sync state to MarketAnalysis {self.market_analysis_id}: {e}")

    def _clean_message_list(self, messages: list) -> list:
        """Clean a list of messages to make them JSON serializable.
        
        Args:
            messages: List that may contain HumanMessage or other non-serializable objects
            
        Returns:
            List of JSON-serializable message representations
        """
        cleaned_messages = []
        for msg in messages:
            if hasattr(msg, 'content') and hasattr(msg, '__class__'):
                # This is likely a langchain message object
                cleaned_messages.append({
                    'type': msg.__class__.__name__,
                    'content': str(msg.content) if msg.content else ''
                })
            elif isinstance(msg, dict):
                # Already a dictionary, check if it's serializable
                try:
                    import json
                    json.dumps(msg)
                    cleaned_messages.append(msg)
                except (TypeError, ValueError):
                    # Convert to string if not serializable
                    cleaned_messages.append({'content': str(msg), 'type': 'dict'})
            elif isinstance(msg, (str, int, float, bool, type(None))):
                # Simple types
                cleaned_messages.append({'content': str(msg), 'type': 'simple'})
            else:
                # Convert everything else to string
                cleaned_messages.append({'content': str(msg), 'type': 'unknown'})
        
        return cleaned_messages

    def _create_tool_nodes(self) -> Dict[str, ToolNode]:
        """Create tool nodes for different data sources."""
        # Import LoggingToolNode for database logging
        from ..db_storage import LoggingToolNode
        
        return {
            "market": LoggingToolNode(
                [
                    # online tools
                    self.toolkit.get_YFin_data_online,
                    self.toolkit.get_stockstats_indicators_report_online,
                    # offline tools
                    self.toolkit.get_YFin_data,
                    self.toolkit.get_stockstats_indicators_report,
                ],
                self.market_analysis_id
            ),
            "social": LoggingToolNode(
                [
                    # online tools
                    self.toolkit.get_stock_news_openai,
                    # offline tools
                    self.toolkit.get_reddit_stock_info,
                ],
                self.market_analysis_id
            ),
            "news": LoggingToolNode(
                [
                    # online tools
                    self.toolkit.get_global_news_openai,
                    self.toolkit.get_google_news,
                    # offline tools
                    self.toolkit.get_finnhub_news,
                    self.toolkit.get_reddit_news,
                ],
                self.market_analysis_id
            ),
            "fundamentals": LoggingToolNode(
                [
                    # online tools
                    self.toolkit.get_fundamentals_openai,
                    # offline tools
                    self.toolkit.get_finnhub_company_insider_sentiment,
                    self.toolkit.get_finnhub_company_insider_transactions,
                    self.toolkit.get_simfin_balance_sheet,
                    self.toolkit.get_simfin_cashflow,
                    self.toolkit.get_simfin_income_stmt,
                ],
                self.market_analysis_id
            ),
            "macro": LoggingToolNode(
                [
                    # FRED economic data tools
                    self.toolkit.get_fred_series_data,
                    self.toolkit.get_economic_calendar,
                    self.toolkit.get_treasury_yield_curve,
                    self.toolkit.get_inflation_data,
                    self.toolkit.get_employment_data,
                ],
                self.market_analysis_id
            ),
        }

    def _initialize_memories_and_graph(self, symbol: str):
        """Initialize memory collections and create the graph with properly initialized memories"""
        from ..agents.utils.memory import FinancialSituationMemory
        
        self.bull_memory = FinancialSituationMemory("bull_memory", self.config, symbol, self.market_analysis_id)
        self.bear_memory = FinancialSituationMemory("bear_memory", self.config, symbol, self.market_analysis_id)
        self.trader_memory = FinancialSituationMemory("trader_memory", self.config, symbol, self.market_analysis_id)
        self.invest_judge_memory = FinancialSituationMemory("invest_judge_memory", self.config, symbol, self.market_analysis_id)
        self.risk_manager_memory = FinancialSituationMemory("risk_manager_memory", self.config, symbol, self.market_analysis_id)
        
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
        )
        
        # Now create the graph with properly initialized memories
        self.graph = self.graph_setup.setup_graph(self.selected_analysts)
        
        ta_logger.debug(f"Initialized memory collections and graph for symbol: {symbol}, market_analysis_id: {self.market_analysis_id}")

    def propagate(self, company_name, trade_date):
        """Run the trading agents graph for a company on a specific date."""

        self.ticker = company_name
        
        # Initialize memories and create graph if not already done
        if self.graph is None:
            self._initialize_memories_and_graph(company_name)
        
        if self.market_analysis_id:
            ta_logger.info(f"Starting analysis for {company_name} on {trade_date}")
            ta_logger.info(f"Using existing MarketAnalysis record with ID: {self.market_analysis_id}")
        else:
            # Running without database - use terminal output
            ta_logger.info(f"Starting standalone analysis for {company_name} on {trade_date}")
            ta_logger.info("No market analysis ID provided - results will be shown in terminal only")

        # Initialize state
        init_agent_state = self.propagator.create_initial_state(
            company_name, trade_date
        )
        args = self.propagator.get_graph_args()
        
        # Sync initial state to MarketAnalysis
        self._sync_state_to_market_analysis(init_agent_state, "initialization")

        try:
            if self.debug:
                # Debug mode with tracing
                trace = []
                step_count = 0
                for chunk in self.graph.stream(init_agent_state, **args):
                    if len(chunk["messages"]) == 0:
                        pass
                    else:
                        chunk["messages"][-1].pretty_print()
                        trace.append(chunk)
                        step_count += 1
                        # Sync state after each step in debug mode
                        self._sync_state_to_market_analysis(chunk, f"debug_step_{step_count}")

                final_state = trace[-1]
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
                ta_logger.info(f"Analysis for {company_name} completed successfully")
            else:
                ta_logger.info(f"Standalone analysis for {company_name} completed successfully")

            # Return decision and processed signal
            return final_state, self.process_signal(final_state["final_trade_decision"])
            
        except Exception as e:
            ta_logger.error(f"Error during analysis for {company_name}: {str(e)}")
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
                ta_logger.warning(f"No expert_recommendation found in final_state for {symbol}")
                return
                
            # Log the complete JSON recommendation for debugging and audit trail
            import json
            ta_logger.info(f"Graph-Generated Recommendation JSON for {symbol}: {json.dumps(recommendation, indent=2)}")
            
            # Store recommendation in database
            from ba2_trade_platform.core.db import add_instance
            from ba2_trade_platform.core.models import ExpertRecommendation
            from ba2_trade_platform.core.types import OrderRecommendation, RiskLevel, TimeHorizon
            
            # Map recommendation action to OrderRecommendation enum
            action_mapping = {
                "BUY": OrderRecommendation.BUY,
                "SELL": OrderRecommendation.SELL,
                "HOLD": OrderRecommendation.HOLD,
                "ERROR": OrderRecommendation.ERROR
            }
            
            # Get expert_instance_id from market_analysis
            expert_instance_id = None
            if self.market_analysis_id:
                try:
                    from ba2_trade_platform.core.db import get_instance
                    from ba2_trade_platform.core.models import MarketAnalysis
                    market_analysis = get_instance(MarketAnalysis, self.market_analysis_id)
                    if market_analysis and hasattr(market_analysis, 'expert_instance_id'):
                        expert_instance_id = market_analysis.expert_instance_id
                except Exception as e:
                    ta_logger.warning(f"Could not get expert_instance_id from market_analysis: {e}")
            
            if expert_instance_id:
                # Map risk level and time horizon from recommendation
                risk_level_mapping = {
                    "LOW": RiskLevel.LOW,
                    "MEDIUM": RiskLevel.MEDIUM,
                    "HIGH": RiskLevel.HIGH
                }
                time_horizon_mapping = {
                    "SHORT_TERM": TimeHorizon.SHORT_TERM,
                    "MEDIUM_TERM": TimeHorizon.MEDIUM_TERM,
                    "LONG_TERM": TimeHorizon.LONG_TERM
                }
                
                expert_rec = ExpertRecommendation(
                    instance_id=expert_instance_id,
                    market_analysis_id=self.market_analysis_id,
                    symbol=recommendation["symbol"],
                    recommended_action=action_mapping.get(recommendation["recommended_action"], OrderRecommendation.HOLD),
                    expected_profit_percent=recommendation["expected_profit_percent"],
                    price_at_date=recommendation["price_at_date"],
                    details=recommendation["details"],
                    confidence=recommendation["confidence"],
                    risk_level=risk_level_mapping.get(recommendation.get("risk_level", "MEDIUM"), RiskLevel.MEDIUM),
                    time_horizon=time_horizon_mapping.get(recommendation.get("time_horizon", "MEDIUM_TERM"), TimeHorizon.MEDIUM_TERM)
                )
                
                rec_id = add_instance(expert_rec)
                ta_logger.info(f"Created ExpertRecommendation record with ID: {rec_id}")
            else:
                ta_logger.warning("No expert_instance_id available, skipping ExpertRecommendation creation")
                
            # Also store as analysis output
            if self.market_analysis_id:
                self.store_analysis_output(
                    market_analysis_id=self.market_analysis_id,
                    name="expert_recommendation",
                    output_type="recommendation",
                    text=json.dumps(recommendation, indent=2)
                )
                
        except Exception as e:
            ta_logger.error(f"Error storing expert recommendation from graph: {str(e)}")

    def _print_terminal_summary_from_graph(self, final_state: Dict[str, Any], symbol: str):
        """Print formatted summary using recommendation generated by the Final Summarization agent"""
        try:
            # Get the recommendation from the graph's final summarization agent
            recommendation = final_state.get("expert_recommendation")
            
            if recommendation:
                # Log the complete JSON recommendation for debugging and audit trail
                import json
                ta_logger.info(f"Graph-Generated Recommendation JSON for {symbol}: {json.dumps(recommendation, indent=2)}")
                
                # Print formatted summary to terminal
                ta_logger.info("="*70)
                ta_logger.info(f"TRADING ANALYSIS SUMMARY FOR {symbol}")
                ta_logger.info("="*70)
                ta_logger.info(f"Recommended Action: {recommendation['recommended_action']}")
                ta_logger.info(f"Expected Profit: {recommendation['expected_profit_percent']:.2f}%")
                ta_logger.info(f"Price at Analysis: ${recommendation['price_at_date']:.2f}")
                ta_logger.info(f"Confidence Level: {recommendation['confidence']:.1f}%")
                ta_logger.info(f"Risk Level: {recommendation.get('risk_level', 'UNKNOWN')}")
                ta_logger.info(f"Time Horizon: {recommendation.get('time_horizon', 'UNKNOWN')}")
                
                # Print key factors if available
                key_factors = recommendation.get('key_factors', [])
                if key_factors:
                    ta_logger.info("Key Factors:")
                    for factor in key_factors:
                        ta_logger.info(f"   • {factor}")
                
                ta_logger.info("Analysis Details:")
                ta_logger.info(f"   {recommendation['details']}")
                ta_logger.info("="*70)
                
                # Print analysis summary if available
                analysis_summary = recommendation.get('analysis_summary', {})
                if analysis_summary:
                    ta_logger.info("Analysis Summary:")
                    ta_logger.info(f"   Market Trend: {analysis_summary.get('market_trend', 'Unknown')}")
                    ta_logger.info(f"   Fundamental Strength: {analysis_summary.get('fundamental_strength', 'Unknown')}")
                    ta_logger.info(f"   Sentiment Score: {analysis_summary.get('sentiment_score', 0)}")
                    ta_logger.info(f"   Macro Environment: {analysis_summary.get('macro_environment', 'Unknown')}")
                    ta_logger.info(f"   Technical Signals: {analysis_summary.get('technical_signals', 'Unknown')}")
                    ta_logger.info("="*70)
            else:
                # Fallback to basic summary if no recommendation available  
                ta_logger.warning(f"No expert_recommendation found in final_state for {symbol}")
                self._print_basic_terminal_summary(final_state, symbol)
                
        except Exception as e:
            ta_logger.error(f"Error printing terminal summary from graph: {str(e)}")
            self._print_basic_terminal_summary(final_state, symbol)

    def _print_basic_terminal_summary(self, final_state: Dict[str, Any], symbol: str):
        """Print basic terminal summary as fallback"""
        ta_logger.info("="*70)
        ta_logger.info(f"BASIC TRADING ANALYSIS SUMMARY FOR {symbol}")
        ta_logger.info("="*70)
        ta_logger.info(f"Final Decision: {final_state.get('final_trade_decision', 'Unknown')}")
        ta_logger.info(f"Investment Plan: {final_state.get('investment_plan', 'Unknown')}")
        
        # Print key reports if available
        if final_state.get("market_report"):
            ta_logger.info("Market Analysis:")
            ta_logger.info(f"   {final_state['market_report'][:200]}...")
        
        if final_state.get("news_report"):
            ta_logger.info("News Analysis:")
            ta_logger.info(f"   {final_state['news_report'][:200]}...")
        
        if final_state.get("fundamentals_report"):
            ta_logger.info("Fundamentals Analysis:")
            ta_logger.info(f"   {final_state['fundamentals_report'][:200]}...")
            
        if final_state.get("macro_report"):
            ta_logger.info("Macro Economic Analysis:")
            ta_logger.info(f"   {final_state['macro_report'][:200]}...")
        
        ta_logger.info("="*70)

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
                ta_logger.info(f"Stored final state in database for analysis ID: {self.market_analysis_id}")
            except Exception as e:
                ta_logger.error(f"Error storing final state in database: {str(e)}")
                # Fallback to logging only
                ta_logger.info(f"Final state data: {json.dumps(state_data, indent=2)}")
        else:
            # No MarketAnalysis - just log the state data
            import json
            ta_logger.info(f"Final state for {trade_date}: {json.dumps(state_data, indent=2)}")
            
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
