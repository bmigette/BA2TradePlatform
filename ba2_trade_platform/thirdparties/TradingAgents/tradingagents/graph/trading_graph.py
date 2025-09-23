# TradingAgents/graph/trading_graph.py

import os
from pathlib import Path
import json
from datetime import date
from typing import Dict, Any, Tuple, List, Optional

from langchain_openai import ChatOpenAI
from langchain_anthropic import ChatAnthropic
from langchain_google_genai import ChatGoogleGenerativeAI

from langgraph.prebuilt import ToolNode

from ..agents import *
from ..default_config import DEFAULT_CONFIG
from ..agents.utils.memory import FinancialSituationMemory
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
from ..db_storage import DatabaseStorageMixin
from .. import logger as ta_logger
from .summarization import create_expert_recommendation_summary


class TradingAgentsGraph(DatabaseStorageMixin):
    """Main class that orchestrates the trading agents framework."""

    def __init__(
        self,
        selected_analysts=["market", "social", "news", "fundamentals", "macro"],
        debug=False,
        config: Dict[str, Any] = None,
        expert_instance_id: Optional[int] = None,
    ):
        """Initialize the trading agents graph and components.

        Args:
            selected_analysts: List of analyst types to include
            debug: Whether to run in debug mode
            config: Configuration dictionary. If None, uses default config
            expert_instance_id: Expert instance ID for database storage and logging
        """
        super().__init__()
        self.debug = debug
        self.config = config or DEFAULT_CONFIG
        self.expert_instance_id = expert_instance_id

        # Initialize logger
        ta_logger.init_logger(expert_instance_id, self.config.get("log_dir", "."))
        ta_logger.info(f"Initializing TradingAgentsGraph with expert_instance_id={expert_instance_id}")

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

        # Initialize memories
        self.bull_memory = FinancialSituationMemory("bull_memory", self.config)
        self.bear_memory = FinancialSituationMemory("bear_memory", self.config)
        self.trader_memory = FinancialSituationMemory("trader_memory", self.config)
        self.invest_judge_memory = FinancialSituationMemory("invest_judge_memory", self.config)
        self.risk_manager_memory = FinancialSituationMemory("risk_manager_memory", self.config)

        # Create tool nodes
        self.tool_nodes = self._create_tool_nodes()

        # Initialize components
        self.conditional_logic = ConditionalLogic()
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

        self.propagator = Propagator()
        self.reflector = Reflector(self.quick_thinking_llm)
        self.signal_processor = SignalProcessor(self.quick_thinking_llm)

        # State tracking
        self.curr_state = None
        self.ticker = None
        self.log_states_dict = {}  # date to full state dict

        # Set up the graph
        self.graph = self.graph_setup.setup_graph(selected_analysts)

    def _create_tool_nodes(self) -> Dict[str, ToolNode]:
        """Create tool nodes for different data sources."""
        return {
            "market": ToolNode(
                [
                    # online tools
                    self.toolkit.get_YFin_data_online,
                    self.toolkit.get_stockstats_indicators_report_online,
                    # offline tools
                    self.toolkit.get_YFin_data,
                    self.toolkit.get_stockstats_indicators_report,
                ]
            ),
            "social": ToolNode(
                [
                    # online tools
                    self.toolkit.get_stock_news_openai,
                    # offline tools
                    self.toolkit.get_reddit_stock_info,
                ]
            ),
            "news": ToolNode(
                [
                    # online tools
                    self.toolkit.get_global_news_openai,
                    self.toolkit.get_google_news,
                    # offline tools
                    self.toolkit.get_finnhub_news,
                    self.toolkit.get_reddit_news,
                ]
            ),
            "fundamentals": ToolNode(
                [
                    # online tools
                    self.toolkit.get_fundamentals_openai,
                    # offline tools
                    self.toolkit.get_finnhub_company_insider_sentiment,
                    self.toolkit.get_finnhub_company_insider_transactions,
                    self.toolkit.get_simfin_balance_sheet,
                    self.toolkit.get_simfin_cashflow,
                    self.toolkit.get_simfin_income_stmt,
                ]
            ),
            "macro": ToolNode(
                [
                    # FRED economic data tools
                    self.toolkit.get_fred_series_data,
                    self.toolkit.get_economic_calendar,
                    self.toolkit.get_treasury_yield_curve,
                    self.toolkit.get_inflation_data,
                    self.toolkit.get_employment_data,
                ]
            ),
        }

    def propagate(self, company_name, trade_date):
        """Run the trading agents graph for a company on a specific date."""

        self.ticker = company_name
        
        # Initialize database storage if expert_instance_id is provided
        if self.expert_instance_id:
            ta_logger.info(f"Starting analysis for {company_name} on {trade_date}")
            market_analysis_id = self.initialize_market_analysis(company_name, self.expert_instance_id)
            ta_logger.info(f"Created MarketAnalysis record with ID: {market_analysis_id}")
        else:
            # Running without database - use terminal output
            ta_logger.info(f"Starting standalone analysis for {company_name} on {trade_date}")
            ta_logger.info("No expert instance ID provided - results will be shown in terminal only")

        # Initialize state
        init_agent_state = self.propagator.create_initial_state(
            company_name, trade_date
        )
        args = self.propagator.get_graph_args()

        try:
            if self.debug:
                # Debug mode with tracing
                trace = []
                for chunk in self.graph.stream(init_agent_state, **args):
                    if len(chunk["messages"]) == 0:
                        pass
                    else:
                        chunk["messages"][-1].pretty_print()
                        trace.append(chunk)

                final_state = trace[-1]
            else:
                # Standard mode without tracing
                final_state = self.graph.invoke(init_agent_state, **args)

            # Store current state for reflection
            self.curr_state = final_state

            # Generate expert recommendation or print to terminal
            if self.expert_instance_id:
                self._generate_expert_recommendation(final_state, company_name)
            else:
                self._print_terminal_summary(final_state, company_name)

            # Log state
            self._log_state(trade_date, final_state)

            # Update analysis status to completed
            if self.expert_instance_id:
                self.update_analysis_status("completed", {"final_state": "success"})
                ta_logger.info(f"Analysis for {company_name} completed successfully")
            else:
                ta_logger.info(f"Standalone analysis for {company_name} completed successfully")

            # Return decision and processed signal
            return final_state, self.process_signal(final_state["final_trade_decision"])
            
        except Exception as e:
            ta_logger.error(f"Error during analysis for {company_name}: {str(e)}")
            if self.expert_instance_id:
                self.update_analysis_status("failed", {"error": str(e)})
            raise

    def _generate_expert_recommendation(self, final_state: Dict[str, Any], symbol: str):
        """Generate and store expert recommendation in database"""
        try:
            # Get current price if available from the state
            current_price = final_state.get("current_price", 0.0)
            
            # Generate recommendation summary
            recommendation = create_expert_recommendation_summary(final_state, symbol, current_price)
            
            # Store recommendation in database
            from ba2_trade_platform.core.db import add_instance
            from ba2_trade_platform.core.models import ExpertRecommendation
            from ba2_trade_platform.core.types import OrderDirection
            
            # Map recommendation action to OrderDirection enum
            action_mapping = {
                "BUY": OrderDirection.BUY,
                "SELL": OrderDirection.SELL,
                "HOLD": OrderDirection.HOLD
            }
            
            expert_rec = ExpertRecommendation(
                instance_id=self.expert_instance_id,
                symbol=recommendation["symbol"],
                recommended_action=action_mapping.get(recommendation["recommended_action"], OrderDirection.HOLD),
                expected_profit_percent=recommendation["expected_profit_percent"],
                price_at_date=recommendation["price_at_date"],
                details=recommendation["details"],
                confidence=recommendation["confidence"]
            )
            
            rec_id = add_instance(expert_rec)
            ta_logger.info(f"Created ExpertRecommendation record with ID: {rec_id}")
            
            # Also store as analysis output
            if self.market_analysis_id:
                import json
                self.store_analysis_output(
                    market_analysis_id=self.market_analysis_id,
                    name="expert_recommendation",
                    output_type="recommendation",
                    text=json.dumps(recommendation, indent=2)
                )
                
        except Exception as e:
            ta_logger.error(f"Error generating expert recommendation: {str(e)}")

    def _print_terminal_summary(self, final_state: Dict[str, Any], symbol: str):
        """Print a formatted summary to terminal when running without database"""
        try:
            # Get current price if available from the state
            current_price = final_state.get("current_price", 0.0)
            
            # Generate recommendation summary
            from .summarization import create_expert_recommendation_summary
            recommendation = create_expert_recommendation_summary(final_state, symbol, current_price)
            
            # Print formatted summary to terminal
            ta_logger.info("="*70)
            ta_logger.info(f"TRADING ANALYSIS SUMMARY FOR {symbol}")
            ta_logger.info("="*70)
            ta_logger.info(f"Recommended Action: {recommendation['recommended_action']}")
            ta_logger.info(f"Expected Profit: {recommendation['expected_profit_percent']:.2f}%")
            ta_logger.info(f"Price at Analysis: ${recommendation['price_at_date']:.2f}")
            ta_logger.info(f"Confidence Level: {recommendation['confidence']:.1f}%")
            ta_logger.info("Analysis Details:")
            ta_logger.info(f"   {recommendation['details']}")
            ta_logger.info("="*70)
            
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
                
        except Exception as e:
            ta_logger.error(f"Error printing terminal summary: {str(e)}")

    def _log_state(self, trade_date, final_state):
        """Log the final state to a JSON file."""
        self.log_states_dict[str(trade_date)] = {
            "company_of_interest": final_state["company_of_interest"],
            "trade_date": final_state["trade_date"],
            "market_report": final_state["market_report"],
            "sentiment_report": final_state["sentiment_report"],
            "news_report": final_state["news_report"],
            "fundamentals_report": final_state["fundamentals_report"],
            "investment_debate_state": {
                "bull_history": final_state["investment_debate_state"]["bull_history"],
                "bear_history": final_state["investment_debate_state"]["bear_history"],
                "history": final_state["investment_debate_state"]["history"],
                "current_response": final_state["investment_debate_state"][
                    "current_response"
                ],
                "judge_decision": final_state["investment_debate_state"][
                    "judge_decision"
                ],
            },
            "trader_investment_decision": final_state["trader_investment_plan"],
            "risk_debate_state": {
                "risky_history": final_state["risk_debate_state"]["risky_history"],
                "safe_history": final_state["risk_debate_state"]["safe_history"],
                "neutral_history": final_state["risk_debate_state"]["neutral_history"],
                "history": final_state["risk_debate_state"]["history"],
                "judge_decision": final_state["risk_debate_state"]["judge_decision"],
            },
            "investment_plan": final_state["investment_plan"],
            "final_trade_decision": final_state["final_trade_decision"],
        }

        # Save to file
        directory = Path(f"eval_results/{self.ticker}/TradingAgentsStrategy_logs/")
        directory.mkdir(parents=True, exist_ok=True)

        with open(
            f"eval_results/{self.ticker}/TradingAgentsStrategy_logs/full_states_log_{trade_date}.json",
            "w",
        ) as f:
            json.dump(self.log_states_dict, f, indent=4)

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
