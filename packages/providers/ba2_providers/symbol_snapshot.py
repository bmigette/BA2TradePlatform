"""Direct FMP fetch helpers for symbol-level research (SYMBOL360).

No expert instance involved — these are pure data lookups, reusing the same
``fmpsdk.quote``/``fmpsdk.company_profile`` calls PennyScreenerTab/FMPRating
already make and the same RVOL formula
``StockScreener._enrich_with_rvol`` uses.

Three SYMBOL360 cards have no backing expert at all: a plain quote/profile
header, the Weinstein stage classification, and RVOL. This module backs all
three.
"""
from typing import Any, Dict, List, Optional

import fmpsdk

from ba2_common.core.weinstein import classify_weinstein_stage


def fetch_quote(api_key: str, symbol: str) -> Optional[Dict[str, Any]]:
    """Fetch FMP's full real-time quote for a single symbol.

    Mirrors ``PennyScreenerTab._fetch_quotes_chunked``'s call shape (which
    passes a list of symbols per chunk); here we pass a single symbol string
    directly — ``fmpsdk.quote`` accepts either ``str`` or ``List[str]`` and
    joins a list with commas internally, so a bare string is equally valid
    and returns the same one-element list shape.
    """
    data = fmpsdk.quote(apikey=api_key, symbol=symbol)
    return data[0] if isinstance(data, list) and data else None


def fetch_profile(api_key: str, symbol: str) -> Optional[Dict[str, Any]]:
    """Fetch FMP's company profile for a single symbol."""
    data = fmpsdk.company_profile(apikey=api_key, symbol=symbol)
    return data[0] if isinstance(data, list) and data else None


def rvol_from_quote(quote: Dict[str, Any]) -> Optional[float]:
    """Relative volume (today's volume / average volume) from a quote dict.

    Reuses ``StockScreener._enrich_with_rvol``'s ``round(volume / avg_vol, 2)``
    formula. Note one deliberate divergence: the screener's production filter
    falls back to ``0.0`` when ``avgVolume`` is unavailable (so a candidate
    with no history sorts to the bottom rather than raising); this simpler
    research-card helper returns ``None`` instead, since "RVOL unknown" and
    "RVOL is literally zero" are different facts a UI card should be able to
    tell apart. The screener also sources volume/avgVolume from daily bars
    (the last COMPLETE session) rather than the live quote, specifically to
    avoid a near-open cold-start where the day's volume-so-far is ~0 — this
    helper is a lighter-weight snapshot for a research card (not a
    live-scan filter) and accepts that same-day skew as a known trade-off.
    """
    volume = quote.get("volume") or 0
    avg_vol = quote.get("avgVolume") or 0
    if avg_vol <= 0:
        return None
    return round(volume / avg_vol, 2)


def weinstein_from_closes(closes: List[float]) -> Dict[str, Any]:
    """Wraps classify_weinstein_stage with the buy/sell signal SYMBOL360 shows."""
    result = classify_weinstein_stage(closes)
    stage = result.get("stage")
    result["signal"] = "buy" if stage == 2 else ("sell" if stage == 4 else "neutral")
    return result
