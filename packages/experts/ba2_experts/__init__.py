"""ba2_experts — trading expert implementations.

TradingAgents/TradingAgentsUI (the multi-agent LLM framework) stay in the live
BA2TradePlatform and are intentionally NOT part of this package, so importing
ba2_experts never pulls langchain.
"""
__version__ = "0.1.0"

from .FinnHubRating import FinnHubRating
from .FMPRating import FMPRating
from .FMPSenateTraderWeight import FMPSenateTraderWeight
from .FMPSenateTraderCopy import FMPSenateTraderCopy
from .FMPInsiderClusterBuy import FMPInsiderClusterBuy
from .FMPEarningsDrift import FMPEarningsDrift
from .PennyMomentumTrader import PennyMomentumTrader
from .FactorRanker import FactorRanker

experts = [FinnHubRating, FMPRating, FMPSenateTraderWeight, FMPSenateTraderCopy,
           FMPInsiderClusterBuy, FMPEarningsDrift, PennyMomentumTrader, FactorRanker]


def get_expert_class(expert_type):
    """Get the expert class by type name."""
    for expert_class in experts:
        if expert_class.__name__ == expert_type:
            return expert_class
    return None


# ---------------------------------------------------------------------------
# Instrument auto-adder seam.
#
# The live BA2TradePlatform's InstrumentAutoAdder (core/InstrumentAutoAdder.py)
# is live-platform infra. ba2_experts must not import it, so screening exposes an
# optional host-provided hook. Default is None (no-op) — e.g. in a backtest.
# The host installs a hook via set_instrument_auto_adder_hook(fn) at startup; fn
# receives the list of tradeable symbols to queue for addition.
# ---------------------------------------------------------------------------
_instrument_auto_adder_hook = None


def set_instrument_auto_adder_hook(fn):
    """Install the host's instrument-auto-adder callback. fn(symbols: list[str])."""
    global _instrument_auto_adder_hook
    _instrument_auto_adder_hook = fn


def get_instrument_auto_adder_hook():
    """Return the installed auto-adder hook, or None if the host set none."""
    return _instrument_auto_adder_hook
