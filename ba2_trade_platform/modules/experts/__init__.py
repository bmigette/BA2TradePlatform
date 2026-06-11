from .TradingAgents import TradingAgents
from .FinnHubRating import FinnHubRating
from .FMPRating import FMPRating
from .FMPSenateTraderWeight import FMPSenateTraderWeight
from .FMPSenateTraderCopy import FMPSenateTraderCopy
from .FMPInsiderClusterBuy import FMPInsiderClusterBuy
from .FMPEarningsDrift import FMPEarningsDrift
from .PennyMomentumTrader import PennyMomentumTrader
from .FactorRanker import FactorRanker
#from .FinRobotExpert import FinRobotExpert

experts = [TradingAgents, FinnHubRating, FMPRating, FMPSenateTraderWeight, FMPSenateTraderCopy,
           FMPInsiderClusterBuy, FMPEarningsDrift, PennyMomentumTrader, FactorRanker]  # , FinRobotExpert]

def get_expert_class(expert_type):
    """Get the expert class by type name."""
    for expert_class in experts:
        if expert_class.__name__ == expert_type:
            return expert_class
    return None
