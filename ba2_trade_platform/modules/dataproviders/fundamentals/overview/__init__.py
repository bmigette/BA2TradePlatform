"""Company Fundamentals Overview Providers (Phase 6 merge-shim).

The non-AI overview providers come from the package (single source of truth):
``ba2_providers.fundamentals.overview.{AlphaVantageCompanyOverviewProvider,
FMPCompanyOverviewProvider}``.
The AI overview provider (AICompanyOverviewProvider) is live-only (it needs the
live ModelFactory LLM stack), so it stays in this in-tree file and is re-exported.
"""
# Non-AI providers from the package:
from ba2_providers.fundamentals.overview import (  # noqa: F401
    AlphaVantageCompanyOverviewProvider,
    FMPCompanyOverviewProvider,
)

# Live-only AI provider (stays in BA2TradePlatform; needs ModelFactory):
from .AICompanyOverviewProvider import AICompanyOverviewProvider  # noqa: F401

__all__ = [
    "AlphaVantageCompanyOverviewProvider",
    "AICompanyOverviewProvider",
    "FMPCompanyOverviewProvider",
]
