from .utils.agent_utils import Toolkit, create_msg_delete
from .utils.agent_states import AgentState, InvestDebateState, RiskDebateState
from .utils.memory import FinancialSituationMemory

from .analysts.fundamentals_analyst import create_fundamentals_analyst
from .analysts.market_analyst import create_market_analyst
from .analysts.news_analyst import create_news_analyst
from .analysts.social_media_analyst import create_social_media_analyst
from .analysts.macro_analyst import create_macro_analyst

# Try to import recommendation_agent, provide fallback if missing
try:
    from .analysts.recommendation_agent import create_recommendation_agent
except ImportError:
    # Create a fallback recommendation agent function
    def create_recommendation_agent(*args, **kwargs):
        """Fallback recommendation agent when module is missing"""
        from .analysts.market_analyst import create_market_analyst
        # Use market analyst as fallback since it's similar functionality
        agent = create_market_analyst(*args, **kwargs)
        # Modify the name to indicate it's a fallback
        if hasattr(agent, 'name'):
            agent.name = "Recommendation Agent (Fallback)"
        return agent

from .researchers.bear_researcher import create_bear_researcher
from .researchers.bull_researcher import create_bull_researcher

from .risk_mgmt.aggresive_debator import create_risky_debator
from .risk_mgmt.conservative_debator import create_safe_debator
from .risk_mgmt.neutral_debator import create_neutral_debator

from .managers.research_manager import create_research_manager
from .managers.risk_manager import create_risk_manager

from .trader.trader import create_trader

__all__ = [
    "FinancialSituationMemory",
    "Toolkit",
    "AgentState",
    "create_msg_delete",
    "InvestDebateState",
    "RiskDebateState",
    "create_bear_researcher",
    "create_bull_researcher",
    "create_research_manager",
    "create_fundamentals_analyst",
    "create_market_analyst",
    "create_macro_analyst",
    "create_neutral_debator",
    "create_news_analyst",
    "create_recommendation_agent",
    "create_risky_debator",
    "create_risk_manager",
    "create_safe_debator",
    "create_social_media_analyst",
    "create_trader",
]
