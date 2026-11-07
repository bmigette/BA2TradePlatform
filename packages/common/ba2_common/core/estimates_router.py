"""``fundamentals_details()`` with a configurable ESTIMATES source.

``EstimatesRoutedDetails`` delegates every attribute to the base details provider (FMP) EXCEPT the estimate
methods (``ESTIMATE_METHODS``), which go to the provider configured for estimates. With ``estimates_source ==
'fmp'`` the estimates provider IS the base, so the router is a transparent pass-through.

Pure of provider imports: the providers are handed in (the bundle's ``get_provider`` builds them).
"""
from __future__ import annotations

from typing import Any

#: The methods answered by the estimates provider; everything else stays on the base provider.
ESTIMATE_METHODS = ("get_earnings_estimates", "get_quarterly_eps_consensus")


class EstimatesRoutedDetails:
    def __init__(self, base: Any, estimates: Any, source: str):
        self._base = base
        self._estimates = estimates
        self.estimates_source = source

    @property
    def base_provider(self) -> Any:
        """The provider everything except the estimate methods goes to (FMP): for ``isinstance`` checks."""
        return self._base

    def __getattr__(self, name: str):
        # Only called for names not found normally: the router's own methods are below.
        return getattr(self._base, name)

    def get_earnings_estimates(self, *args, **kwargs):
        return self._estimates.get_earnings_estimates(*args, **kwargs)

    def get_quarterly_eps_consensus(self, *args, **kwargs):
        return self._estimates.get_quarterly_eps_consensus(*args, **kwargs)
