"""
StockTwits Trending Discovery Provider

Fetches trending/active symbols from StockTwits API endpoints:
  - top_watched:       symbols with most watchlist additions recently
  - most_active:       symbols with highest message volume
  - symbols_enhanced:  StockTwits' own trending score ranking

All endpoints return price data via `payloads=qprices&enable_price_v2=true`.
No authentication required — endpoints are publicly accessible via browser
TLS impersonation (curl_cffi). An OAuth token can optionally be provided
for authenticated access, but is not needed.
"""

from typing import Any, Dict, List, Optional
from ba2_trade_platform.logger import logger


TRENDING_BASE = "https://api.stocktwits.com/api/2/trending"
ENDPOINTS = ["top_watched", "most_active", "symbols_enhanced"]

_BROWSER_HEADERS = {
    "accept": "application/json",
    "accept-language": "en-US,en;q=0.9",
    "cache-control": "no-cache",
    "origin": "https://stocktwits.com",
    "pragma": "no-cache",
    "referer": "https://stocktwits.com/",
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-site",
}


class StockTwitsTrending:
    """
    Discovery provider that fetches trending symbols from StockTwits.

    Queries three trending endpoints and deduplicates results, optionally
    filtering by maximum price. Uses curl_cffi for TLS browser impersonation.
    No OAuth token required for public access.
    """

    def __init__(
        self,
        oauth_token: str = "",
        price_max: float = 6.0,
        limit_per_endpoint: int = 100,
    ):
        """
        Args:
            oauth_token: Optional StockTwits OAuth token. When provided, adds
                an Authorization header (authenticated rate limits are higher).
                Leave empty for unauthenticated public access.
            price_max: Only include symbols priced at or below this value.
            limit_per_endpoint: Max symbols to request per endpoint (API max: 100).
        """
        try:
            from curl_cffi import requests as cf_requests
        except ImportError as exc:
            raise ImportError(
                "curl_cffi is required for StockTwitsTrending. "
                "Install it with: pip install curl_cffi"
            ) from exc

        self._token = oauth_token.strip()
        self._price_max = price_max
        self._limit = min(max(1, limit_per_endpoint), 100)
        self._session = cf_requests.Session(impersonate="chrome124")
        logger.debug(
            f"Initialized StockTwitsTrending (price_max={price_max}, limit={self._limit}, "
            f"auth={'yes' if self._token else 'no'})"
        )

    def _fetch_endpoint(self, endpoint: str) -> List[Dict[str, Any]]:
        """Fetch one trending endpoint and return raw symbol list."""
        url = f"{TRENDING_BASE}/{endpoint}.json"
        headers = dict(_BROWSER_HEADERS)
        if self._token:
            headers["authorization"] = f"OAuth {self._token}"

        params = {
            "class": "all",
            "limit": self._limit,
            "page_num": 1,
            "payloads": "qprices",
            "enable_price_v2": "true",
        }

        logger.debug(f"StockTwitsTrending: fetching {endpoint}")
        resp = self._session.get(url, headers=headers, params=params, timeout=15)
        resp.raise_for_status()

        data = resp.json()
        # Response shape: {"symbols": [...], "cursor": {...}}
        return data.get("symbols", [])

    def _parse_price(self, symbol_obj: Dict[str, Any]) -> Optional[float]:
        """Extract last price from symbol payload (price_v2 or prices)."""
        for key in ("price_v2", "prices"):
            p = symbol_obj.get(key) or {}
            try:
                val = float(p.get("last") or 0)
                if val > 0:
                    return val
            except (TypeError, ValueError):
                pass
        return None

    def get_trending_symbols(self) -> List[Dict[str, Any]]:
        """
        Fetch all three trending endpoints and return a deduplicated list of symbols.

        Returns:
            List of dicts with keys: symbol, price, change_pct, watchlist_count,
            trending_score, sources (list of endpoints where symbol appeared).
            Only symbols with price > 0 and price <= price_max are included.
        """
        seen: Dict[str, Dict[str, Any]] = {}  # symbol -> merged record

        for endpoint in ENDPOINTS:
            try:
                raw_symbols = self._fetch_endpoint(endpoint)
                logger.debug(
                    f"StockTwitsTrending [{endpoint}]: got {len(raw_symbols)} symbols"
                )
            except Exception as e:
                logger.warning(f"StockTwitsTrending: failed to fetch {endpoint}: {e}")
                continue

            for sym_obj in raw_symbols:
                symbol = (sym_obj.get("symbol") or "").strip().upper()
                if not symbol:
                    continue

                price = self._parse_price(sym_obj)
                if not price or price <= 0:
                    continue
                if price > self._price_max:
                    continue

                if symbol in seen:
                    # Already seen from another endpoint — add source tag
                    seen[symbol]["sources"].append(endpoint)
                else:
                    price_v2 = sym_obj.get("price_v2") or sym_obj.get("prices") or {}
                    try:
                        change_pct = float(price_v2.get("change_percent") or 0)
                    except (TypeError, ValueError):
                        change_pct = 0.0

                    seen[symbol] = {
                        "symbol": symbol,
                        "company_name": sym_obj.get("title") or sym_obj.get("name"),
                        "price": price,
                        "change_pct": change_pct,
                        "watchlist_count": sym_obj.get("watchlist_count"),
                        "trending_score": sym_obj.get("trending_score"),
                        "sources": [endpoint],
                        "_source": "stocktwits_trending",
                    }

        results = list(seen.values())
        # Sort: symbols appearing in multiple endpoints first, then by watchlist count
        results.sort(
            key=lambda x: (-len(x["sources"]), -(x.get("watchlist_count") or 0))
        )
        logger.debug(
            f"StockTwitsTrending: {len(results)} unique symbols after price filter "
            f"(price_max={self._price_max})"
        )
        return results
