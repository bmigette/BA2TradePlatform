# TradingAgents/graph/propagation.py

from typing import Dict, Any
from ..agents.utils.agent_states import (
    AgentState,
    InvestDebateState,
    RiskDebateState,
)


class Propagator:
    """Handles state initialization and propagation through the graph."""

    def __init__(self, max_recur_limit=100):
        """Initialize with configuration parameters."""
        self.max_recur_limit = max_recur_limit

    def create_initial_state(
        self, company_name: str, trade_date: str, market_analysis_id: int = None
    ) -> Dict[str, Any]:
        """Create the initial state for the agent graph.
        
        Args:
            company_name: Symbol to analyze
            trade_date: Date of analysis
            market_analysis_id: Database ID for this analysis (enables tool JSON storage)
        """
        return {
            "messages": [("human", company_name)],
            "company_of_interest": company_name,
            "trade_date": str(trade_date),
            "market_analysis_id": market_analysis_id,
            "investment_debate_state": InvestDebateState(
                {
                    "history": "", 
                    "current_response": "", 
                    "count": 0,
                    "bull_history": "",
                    "bear_history": "",
                    "bull_messages": [],
                    "bear_messages": [],
                    "judge_decision": ""
                }
            ),
            "risk_debate_state": RiskDebateState(
                {
                    "history": "",
                    "current_risky_response": "",
                    "current_safe_response": "",
                    "current_neutral_response": "",
                    "count": 0,
                    "risky_history": "",
                    "safe_history": "",
                    "neutral_history": "",
                    "risky_messages": [],
                    "safe_messages": [],
                    "neutral_messages": [],
                    "latest_speaker": "",
                    "judge_decision": ""
                }
            ),
            "market_report": "",
            "fundamentals_report": "",
            "sentiment_report": "",
            "news_report": "",
        }

    def get_graph_args(self) -> Dict[str, Any]:
        """Get arguments for the graph invocation."""
        return {
            "stream_mode": "values",
            "config": {"recursion_limit": self.max_recur_limit},
        }
