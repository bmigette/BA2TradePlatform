from .TradingAgents import TradingAgents
from .FinnHubRating import FinnHubRating

experts = [TradingAgents, FinnHubRating]

def get_expert_class(expert_type):
    """Get the expert class by type name."""
    for expert_class in experts:
        if expert_class.__name__ == expert_type:
            return expert_class
    return None