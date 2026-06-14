"""Live expert registry (Phase 6 merge-shim).

The clean experts now live in ``ba2_experts`` (single source of truth):
FinnHubRating, FMPRating, FMPSenateTraderWeight, FMPSenateTraderCopy,
FMPInsiderClusterBuy, FMPEarningsDrift, PennyMomentumTrader, FactorRanker — plus
the instrument-auto-adder hook seam (set/get_instrument_auto_adder_hook).

TradingAgents/TradingAgentsUI (the multi-agent LLM framework) are live-only (they
pull langchain) and stay in BA2TradePlatform. ``get_expert_class`` therefore
special-cases 'TradingAgents' to the live class and delegates everything else to
``ba2_experts.get_expert_class``.

IMPORTANT (do not ``from ba2_experts import *`` here): the per-expert in-tree
modules (e.g. ``modules/experts/FinnHubRating.py``) are ALIAS shims whose
``sys.modules`` entry IS the package module — so that ``unittest.mock.patch`` /
``inspect.getsource`` targeting the in-tree path operate on the real package
module. Binding the expert *classes* onto this package namespace (which ``import *``
would do) would shadow those aliased submodules and make ``patch(
"ba2_trade_platform.modules.experts.FinnHubRating.<name>")`` resolve to the class
instead of the module. So we bring in ONLY the registry helpers + hooks at module
scope and resolve the expert classes locally.
"""
# Seam hooks + the package registry helper (no expert-class names at module scope).
from ba2_experts import (  # noqa: F401
    get_expert_class as _pkg_get_expert_class,
    set_instrument_auto_adder_hook,
    get_instrument_auto_adder_hook,
)

# Live-only expert (needs langchain / the live TradingAgents framework):
from .TradingAgents import TradingAgents  # noqa: F401


def _build_experts_list():
    """Full live registry list — TradingAgents first, then the package experts
    (same order as the original in-tree __init__). Imported locally so the expert
    class names are not bound at module scope (see module docstring)."""
    from ba2_experts import (
        FinnHubRating, FMPRating, FMPSenateTraderWeight, FMPSenateTraderCopy,
        FMPInsiderClusterBuy, FMPEarningsDrift, PennyMomentumTrader, FactorRanker,
    )
    return [
        TradingAgents,
        FinnHubRating,
        FMPRating,
        FMPSenateTraderWeight,
        FMPSenateTraderCopy,
        FMPInsiderClusterBuy,
        FMPEarningsDrift,
        PennyMomentumTrader,
        FactorRanker,
    ]


# Public registry list (ui/pages/settings.py imports ``experts``).
experts = _build_experts_list()


def get_expert_class(expert_type):
    """Get the expert class by type name.

    'TradingAgents' resolves to the live class; everything else delegates to the
    package registry (single source of truth).
    """
    if expert_type == "TradingAgents":
        return TradingAgents
    return _pkg_get_expert_class(expert_type)
