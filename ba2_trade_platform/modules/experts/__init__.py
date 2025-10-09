from .TradingAgents import TradingAgents
from .FinnHubRating import FinnHubRating
from .FMPRating import FMPRating
from .FMPSenateTrade import FMPSenateTrade
#from .FinRobotExpert import FinRobotExpert

experts = [TradingAgents, FinnHubRating, FMPRating, FMPSenateTrade] #, FinRobotExpert]

def get_expert_class(expert_type):
    """Get the expert class by type name."""
    for expert_class in experts:
        if expert_class.__name__ == expert_type:
            return expert_class
    return None