"""Feature-source seam for the ML dataset builder (Phase 5, Task 6).

This is the secondary re-source seam, parallel to the primary OHLCV seam at
``app/api/datasets.py::get_ohlcv_provider`` (Task 5). The dataset builder enriches
each OHLCV matrix with sentiment / fundamentals / macro feature blocks via three
services:

  * ``SentimentService.fetch_news_for_ticker(...)``   -> ``news_*`` columns
  * ``FundamentalsService.create_statement_features_v2(...)``
        -> ``bs_*/is_*/cf_*/earn_*`` columns
  * ``MacroService.integrate_macro_with_ohlc(...)``    -> ``macro_*`` columns

Like OHLCV, the secondary feature fetches can be routed THROUGH ``ba2_providers``
so experts (point-in-time slices, Phases 1-4) and ML training (a materialized
feature/target matrix) share one cache. The selection is gated behind the
``FEATURES_SOURCE`` env flag.

DEFAULT IS ``legacy`` (today's per-service ``dataproviders`` clients) so nothing
changes for existing training until the flag is explicitly flipped. Unlike OHLCV
(Task 7), the per-block byte-equality / equivalence for these multi-field feature
blocks is materially harder to verify, so the ``ba2_providers`` route here is
WIRED but VERIFICATION IS DEFERRED (plan Task 8). Do NOT flip the default to
``ba2_providers`` for features until that per-block equivalence is documented.

Each service mirrors the OHLCV pattern: when ``FEATURES_SOURCE=ba2_providers`` it
attempts to construct the ``ba2_providers``-backed provider via
``ba2_providers.get_provider(<category>, <name>)`` and FALLS BACK to the legacy
client on ANY failure (missing package, provider constructor raising on a missing
API key, etc.). The service method signatures and the output column prefixes
(``news_/bs_/is_/cf_/earn_/fundamental_/macro_``) are UNCHANGED in either mode.

``ba2_providers`` categories used by each service (verified against the merged
Phase-1/2 registry, ``ba2_providers/__init__.py``):

  * news               -> ``get_provider("news", name)``
        names: alpaca, alphavantage, finnhub, fmp, google
  * fundamentals       -> ``get_provider("fundamentals_details", name)``
        (statements: balance_sheet / income / cash_flow / earnings)
        names: alphavantage, fmp, yfinance
        (company overview lives in ``fundamentals_overview``: alphavantage, fmp)
  * macro              -> ``get_provider("macro", name)``
        names: fred
"""
from __future__ import annotations

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

# Env flag mirroring OHLCV_SOURCE (Task 5). Default "legacy" -> nothing changes.
FEATURES_SOURCE_ENV = "FEATURES_SOURCE"
_LEGACY = "legacy"
_BA2 = "ba2_providers"


def features_source() -> str:
    """Return the configured feature source, normalized.

    ``legacy`` (default) keeps each service's existing ``dataproviders`` client.
    ``ba2_providers`` routes the feature fetch through the shared ``ba2_providers``
    cache (verification deferred to plan Task 8 — do not flip the default).
    """
    return os.getenv(FEATURES_SOURCE_ENV, _LEGACY).strip().lower()


def use_ba2_providers() -> bool:
    """True when ``FEATURES_SOURCE=ba2_providers`` is explicitly selected."""
    return features_source() == _BA2


def get_ba2_provider(category: str, provider_name: str):
    """Construct a ``ba2_providers`` provider for ``category``/``provider_name``.

    Returns ``None`` (logging at WARNING) if ``ba2_providers`` is unavailable or
    the provider cannot be constructed, so every caller can fall back to its
    legacy client without the dataset build failing. This is the single point
    that touches ``ba2_providers`` for the secondary feature seam.
    """
    name = (provider_name or "").strip().lower()
    try:
        from ba2_providers import get_provider  # lazy: legacy-only installs still load

        return get_provider(category, name)
    except Exception as e:  # pragma: no cover - exercised via service fallbacks
        logger.warning(
            "FEATURES_SOURCE=ba2_providers: could not build provider "
            "(%s, %s) -> %s; falling back to legacy",
            category,
            name,
            e,
        )
        return None
